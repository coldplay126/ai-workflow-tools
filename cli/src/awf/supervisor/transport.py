"""Authenticated command delivery and lease transports for Supervisor agents."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple, TYPE_CHECKING

from awf.supervisor.client import (
    HttpResponse,
    SigV4Transport,
    SupervisorAuthRequired,
    Transport,
    _successful_json,
)
from awf.supervisor.config import SupervisorConfig
from awf.supervisor.contracts import (
    AgentEnvironment,
    JobState,
    SupervisorCommand,
    SupervisorEvent,
    SupervisorJob,
)
from awf.supervisor.recovery import (
    RecoveryCheckpointError,
    normalize_recovery_checkpoint,
)

if TYPE_CHECKING:
    from awf.supervisor.credentials import AccessTokenBroker


_MEBIBYTE = 1024 * 1024
_MAX_POLL_SECONDS = 20
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_URI = re.compile(
    r"^s3://(?P<bucket>[A-Za-z0-9.-]+)/artifacts/"
    r"(?P<kind>checkpoints|provenance|redacted-results)/"
    r"(?P<job_id>[A-Za-z0-9][A-Za-z0-9._:-]{0,127})/"
    r"(?P<generation>[0-9]+)/(?P<sha256>[0-9a-f]{64})\.json$"
)
_ARTIFACT_PATH_KINDS = {
    "checkpoint": "checkpoints",
    "provenance": "provenance",
    "redacted-result": "redacted-results",
}
_PRE_RUN_TRANSITIONS = frozenset(
    {
        (JobState.CLAIMED, JobState.PREPARING),
        (JobState.PREPARING, JobState.RUNNING),
        (JobState.WAITING_APPROVAL, JobState.RUNNING),
    }
)


class CommandDelivery(Protocol):
    """A command whose queue-specific receipt is owned by this process."""

    command: SupervisorCommand

    def ack(self) -> None:
        ...

    def release(self) -> None:
        ...


class CommandSource(Protocol):
    """The bounded source of Supervisor work commands."""

    def next_command(self, *, wait_seconds: int) -> Optional[CommandDelivery]:
        ...


class LeaseApi(Protocol):
    """Authenticated, owner-fenced Supervisor lease operations."""

    def heartbeat(
        self,
        *,
        agent_id: str,
        capabilities: Sequence[str] = (),
        repos: Sequence[str] = (),
        max_concurrency: int = 1,
        active_jobs: int = 0,
        version: Optional[Mapping[str, str]] = None,
    ) -> None:
        ...

    def accept_claim(self, command: Any, *, agent_id: str) -> SupervisorJob:
        ...

    def inspect_claim(self, command: Any, *, agent_id: str) -> SupervisorJob:
        ...

    def renew(
        self, *, job_id: str, generation: int, agent_id: str
    ) -> SupervisorJob:
        ...

    def read_job(
        self, *, job_id: str, generation: int, agent_id: str
    ) -> SupervisorJob:
        ...

    def fetch_prompt(
        self, *, job_id: str, generation: int, agent_id: str
    ) -> Tuple[str, str]:
        ...

    def fetch_checkpoint(
        self, *, job: SupervisorJob, agent_id: str
    ) -> Mapping[str, Any]:
        ...

    def upload_artifact(
        self,
        artifact: Optional[Mapping[str, Any]] = None,
        *,
        agent_id: str,
        job_id: Optional[str] = None,
        generation: Optional[int] = None,
        kind: Optional[str] = None,
        body: Optional[bytes] = None,
    ) -> Mapping[str, str]:
        ...

    def append_event(self, event: Any, *, agent_id: str) -> None:
        ...

    def advance_state(
        self,
        *,
        job_id: str,
        generation: int,
        agent_id: str,
        from_state: JobState,
        to_state: JobState,
    ) -> SupervisorJob:
        ...

    def terminal_transition(self, event: Any, *, agent_id: str) -> SupervisorJob:
        ...

    def read_desired_state(
        self, *, job_id: str, generation: int, agent_id: str
    ) -> str:
        ...

    def read_decision(
        self, *, job_id: str, generation: int, agent_id: str
    ) -> Optional[Mapping[str, str]]:
        ...


class BrokerBearerTransport:
    """Local-agent HTTP transport backed by the shared opaque token broker.

    The broker, rather than this transport, owns token lifetime caching.  Calling
    ``current`` immediately before every request is intentional: it makes a
    shared broker the sole authority for access-token reuse and expiry refresh.
    """

    def __init__(
        self,
        *,
        http: Transport,
        token_broker: "AccessTokenBroker",
        agent_id: str,
    ) -> None:
        _validate_identifier(agent_id, "agent_id")
        self._http = http
        self._token_broker = token_broker
        self._agent_id = agent_id

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> HttpResponse:
        response = self._request_with_fresh_token(method, path, payload, headers)
        if response.status != 401:
            return response

        # A token can be revoked server-side before its cached expiry.  There is
        # exactly one retry, and the original request fields (including an
        # idempotency key) are reconstructed unchanged.
        self._token_broker.invalidate()
        return self._request_with_fresh_token(method, path, payload, headers)

    def _request_with_fresh_token(
        self,
        method: str,
        path: str,
        payload: Optional[Mapping[str, Any]],
        headers: Optional[Mapping[str, str]],
    ) -> HttpResponse:
        token = self._token_broker.current(self._agent_id)
        value = getattr(token, "value", None)
        if not isinstance(value, str) or not value:
            raise SupervisorAuthRequired("local access token is unavailable")
        request_headers = _broker_headers(headers, value)
        return self._http.request(
            method,
            path,
            payload=payload,
            headers=request_headers,
        )


@dataclass
class _LocalHttpDelivery:
    command: SupervisorCommand

    def ack(self) -> None:
        # HTTP command delivery is a read of durable claimed-job state.  The
        # command ledger, not a remote receipt, determines duplicate handling.
        return None

    def release(self) -> None:
        # A subsequent bounded poll may return the command again; that is safe
        # because the command ledger is the only deduplication authority.
        return None


class LocalHttpCommandSource:
    """Bounded local-agent command polling over the broker-authenticated HTTP port."""

    def __init__(self, *, transport: Transport) -> None:
        self._transport = transport

    def next_command(self, *, wait_seconds: int) -> Optional[CommandDelivery]:
        _validate_poll_seconds(wait_seconds)
        response = self._transport.request(
            "GET",
            "/v1/local-agent/commands",
            headers={"X-Awf-Poll-Seconds": str(wait_seconds)},
        )
        if response.status == 204:
            return None
        command = SupervisorCommand.from_dict(_successful_json(response))
        return _LocalHttpDelivery(command=command)


class _SqsClient(Protocol):
    def receive_message(self, **kwargs: Any) -> Mapping[str, Any]:
        ...

    def delete_message(self, **kwargs: Any) -> Any:
        ...

    def change_message_visibility(self, **kwargs: Any) -> Any:
        ...


@dataclass
class _SqsDelivery:
    command: SupervisorCommand
    _sqs: _SqsClient
    _queue_url: str
    _receipt_handle: str
    _settled: bool = False

    def ack(self) -> None:
        if self._settled:
            return None
        self._sqs.delete_message(
            QueueUrl=self._queue_url,
            ReceiptHandle=self._receipt_handle,
        )
        self._settled = True

    def release(self) -> None:
        if self._settled:
            return None
        self._sqs.change_message_visibility(
            QueueUrl=self._queue_url,
            ReceiptHandle=self._receipt_handle,
            VisibilityTimeout=0,
        )
        self._settled = True


class AwsSqsCommandSource:
    """SQS command delivery for AWS agents with explicit receipt lifecycle."""

    def __init__(
        self,
        *,
        sqs_client: _SqsClient,
        queue_url: str,
        visibility_timeout_seconds: int,
    ) -> None:
        if not isinstance(queue_url, str) or not queue_url:
            raise ValueError("queue_url must be non-empty")
        if (
            type(visibility_timeout_seconds) is not int
            or visibility_timeout_seconds <= 0
        ):
            raise ValueError("visibility_timeout_seconds must be positive")
        self._sqs = sqs_client
        self._queue_url = queue_url
        self._visibility_timeout_seconds = visibility_timeout_seconds

    def next_command(self, *, wait_seconds: int) -> Optional[CommandDelivery]:
        _validate_poll_seconds(wait_seconds)
        response = self._sqs.receive_message(
            QueueUrl=self._queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=wait_seconds,
            VisibilityTimeout=self._visibility_timeout_seconds,
        )
        messages = response.get("Messages", [])
        if messages is None:
            return None
        if not isinstance(messages, list):
            raise ValueError("SQS Messages must be a list")
        if not messages:
            return None
        message = messages[0]
        if not isinstance(message, Mapping):
            raise ValueError("SQS message must be an object")
        receipt_handle = message.get("ReceiptHandle")
        body = message.get("Body")
        if not isinstance(receipt_handle, str) or not receipt_handle:
            raise ValueError("SQS message is missing ReceiptHandle")
        try:
            if not isinstance(body, str):
                raise ValueError("SQS message body must be a string")
            decoded = json.loads(body)
            if not isinstance(decoded, Mapping):
                raise ValueError("SQS command must be an object")
            command = SupervisorCommand.from_dict(decoded)
        except (json.JSONDecodeError, RecursionError, ValueError):
            self._release_receipt(receipt_handle)
            raise
        return _SqsDelivery(
            command=command,
            _sqs=self._sqs,
            _queue_url=self._queue_url,
            _receipt_handle=receipt_handle,
        )

    def _release_receipt(self, receipt_handle: str) -> None:
        self._sqs.change_message_visibility(
            QueueUrl=self._queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=0,
        )


class HttpLeaseApi:
    """HTTP implementation of the Supervisor's owner-fenced lease API."""

    def __init__(self, *, transport: Transport, environment: Any = "local") -> None:
        self._environment = _environment_name(environment)
        self._transport = transport

    @property
    def transport(self) -> Transport:
        return self._transport

    @classmethod
    def for_environment(
        cls,
        *,
        environment: Any,
        http: Optional[Transport] = None,
        token_broker: Optional["AccessTokenBroker"] = None,
        agent_id: Optional[str] = None,
        config: Optional[SupervisorConfig] = None,
    ) -> "HttpLeaseApi":
        name = _environment_name(environment)
        if name == AgentEnvironment.LOCAL.value:
            if http is None or token_broker is None or agent_id is None:
                raise ValueError("local environment requires HTTP transport, token broker, and agent_id")
            return cls(
                transport=BrokerBearerTransport(
                    http=http,
                    token_broker=token_broker,
                    agent_id=agent_id,
                ),
                environment=name,
            )
        if config is None:
            raise ValueError("aws environment requires SupervisorConfig")
        return cls(transport=SigV4Transport(config), environment=name)

    def heartbeat(
        self,
        *,
        agent_id: str,
        capabilities: Sequence[str] = (),
        repos: Sequence[str] = (),
        max_concurrency: int = 1,
        active_jobs: int = 0,
        version: Optional[Mapping[str, str]] = None,
    ) -> None:
        _validate_identifier(agent_id, "agent_id")
        payload = {
            "capabilities": _validated_string_list(capabilities, "capabilities"),
            "repos": _validated_string_list(repos, "repos"),
            "max_concurrency": _positive_int(max_concurrency, "max_concurrency"),
            "active_jobs": _nonnegative_int(active_jobs, "active_jobs"),
            "version": _validated_version(version),
        }
        if payload["active_jobs"] > payload["max_concurrency"]:
            raise ValueError("active_jobs must not exceed max_concurrency")
        _successful_json(self._request("POST", "/heartbeat", payload=payload))

    def inspect_claim(self, command: Any, *, agent_id: str) -> SupervisorJob:
        """Read the command-bound public job before a remote lease is accepted."""
        inspected = _coerce_command(command)
        _validate_identifier(agent_id, "agent_id")
        job = _job_from_response(
            self._request(
                "GET",
                "/jobs/{}".format(inspected.job_id),
                headers={
                    "X-AWF-Command-Id": inspected.command_id,
                    "X-AWF-Generation": str(inspected.generation),
                },
            )
        )
        if (
            job.job_id != inspected.job_id
            or job.generation != inspected.generation
            or job.state not in {JobState.QUEUED, JobState.PAUSED}
        ):
            raise ValueError("preclaim job does not match command")
        return job

    def accept_claim(self, command: Any, *, agent_id: str) -> SupervisorJob:
        accepted = _coerce_command(command)
        _validate_identifier(agent_id, "agent_id")
        job = _job_from_response(
            self._request(
                "POST",
                "/jobs/{}/claim".format(accepted.job_id),
                payload={
                    "generation": accepted.generation,
                    "command_id": accepted.command_id,
                },
            )
        )
        return _owned_job(
            job,
            job_id=accepted.job_id,
            generation=accepted.generation,
            agent_id=agent_id,
        )

    def renew(
        self, *, job_id: str, generation: int, agent_id: str
    ) -> SupervisorJob:
        _validate_owner_inputs(job_id, generation, agent_id)
        job = _job_from_response(
            self._request(
                "POST",
                "/jobs/{}/renew".format(job_id),
                payload={"generation": generation},
            )
        )
        return _owned_job(job, job_id=job_id, generation=generation, agent_id=agent_id)

    def read_job(
        self, *, job_id: str, generation: int, agent_id: str
    ) -> SupervisorJob:
        _validate_owner_inputs(job_id, generation, agent_id)
        job = _job_from_response(self._request("GET", "/jobs/{}".format(job_id)))
        return _owned_job(job, job_id=job_id, generation=generation, agent_id=agent_id)

    def fetch_prompt(
        self, *, job_id: str, generation: int, agent_id: str
    ) -> Tuple[str, str]:
        _validate_owner_inputs(job_id, generation, agent_id)
        payload = _successful_json(
            self._request("GET", "/jobs/{}/prompt".format(job_id))
        )
        if set(payload) != {"prompt", "sha256"}:
            raise ValueError("invalid prompt response")
        prompt = payload["prompt"]
        digest = payload["sha256"]
        if not isinstance(prompt, str) or not _is_sha256(digest):
            raise ValueError("invalid prompt response")
        if hashlib.sha256(prompt.encode("utf-8")).hexdigest() != digest:
            raise ValueError("prompt sha256 mismatch")
        return prompt, digest

    def fetch_checkpoint(
        self, *, job: SupervisorJob, agent_id: str
    ) -> Mapping[str, Any]:
        if type(job) is not SupervisorJob:
            raise ValueError("job must be SupervisorJob")
        _validate_owner_inputs(job.job_id, job.generation, agent_id)
        _owned_job(job, job_id=job.job_id, generation=job.generation, agent_id=agent_id)
        if job.checkpoint is None:
            raise ValueError("job has no checkpoint")
        if (
            set(job.checkpoint) != {"kind", "artifact_uri", "sha256"}
            or job.checkpoint["kind"] != "awf-omp-native"
        ):
            raise ValueError("invalid job checkpoint")
        stored_digest = job.checkpoint["sha256"]
        if not _is_sha256(stored_digest):
            raise ValueError("invalid job checkpoint sha256")
        payload = _successful_json(
            self._request("GET", "/jobs/{}/checkpoint".format(job.job_id))
        )
        if set(payload) != {"artifact_base64", "sha256"}:
            raise ValueError("invalid checkpoint response")
        encoded = payload["artifact_base64"]
        returned_digest = payload["sha256"]
        if not isinstance(encoded, str) or not _is_sha256(returned_digest):
            raise ValueError("invalid checkpoint response")
        if returned_digest != stored_digest:
            raise ValueError("checkpoint sha256 does not match the stored checkpoint")
        try:
            decoded = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError, binascii.Error) as error:
            raise ValueError("invalid checkpoint artifact_base64") from error
        if len(decoded) > _MEBIBYTE:
            raise ValueError("checkpoint exceeds 1 MiB")
        actual_digest = hashlib.sha256(decoded).hexdigest()
        if actual_digest != stored_digest:
            raise ValueError("checkpoint sha256 mismatch")
        try:
            checkpoint = json.loads(decoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise ValueError("invalid recovery checkpoint JSON") from error
        if not isinstance(checkpoint, Mapping):
            raise ValueError("invalid recovery checkpoint")
        return _validate_recovery_checkpoint(checkpoint, job=job)

    def upload_artifact(
        self,
        artifact: Optional[Mapping[str, Any]] = None,
        *,
        agent_id: str,
        job_id: Optional[str] = None,
        generation: Optional[int] = None,
        kind: Optional[str] = None,
        body: Optional[bytes] = None,
    ) -> Mapping[str, str]:
        job_id, generation, kind, body = _artifact_inputs(
            artifact=artifact,
            job_id=job_id,
            generation=generation,
            kind=kind,
            body=body,
        )
        _validate_owner_inputs(job_id, generation, agent_id)
        expected_path_kind = _ARTIFACT_PATH_KINDS.get(kind)
        if expected_path_kind is None:
            raise ValueError("invalid artifact kind")
        if not isinstance(body, bytes) or not body or len(body) > _MEBIBYTE:
            raise ValueError("artifact body must be non-empty bytes no larger than 1 MiB")
        digest = hashlib.sha256(body).hexdigest()
        payload = _successful_json(
            self._request(
                "POST",
                "/jobs/{}/artifacts".format(job_id),
                payload={
                    "generation": generation,
                    "kind": kind,
                    "artifact_base64": base64.b64encode(body).decode("ascii"),
                    "sha256": digest,
                },
            )
        )
        if set(payload) != {"artifact_uri", "sha256"}:
            raise ValueError("invalid artifact response")
        uri = payload["artifact_uri"]
        returned_digest = payload["sha256"]
        if not isinstance(uri, str) or returned_digest != digest:
            raise ValueError("artifact response digest mismatch")
        match = _ARTIFACT_URI.fullmatch(uri)
        if (
            match is None
            or match["kind"] != expected_path_kind
            or match["job_id"] != job_id
            or int(match["generation"]) != generation
            or match["sha256"] != digest
        ):
            raise ValueError("artifact response identity mismatch")
        return {"artifact_uri": uri, "sha256": digest}

    def append_event(self, event: Any, *, agent_id: str) -> None:
        accepted = _coerce_event(event)
        _validate_event_owner(accepted, agent_id)
        _successful_json(
            self._request(
                "POST",
                "/jobs/{}/events".format(accepted.job_id),
                payload=accepted.to_dict(),
            )
        )

    def advance_state(
        self,
        *,
        job_id: str,
        generation: int,
        agent_id: str,
        from_state: JobState,
        to_state: JobState,
    ) -> SupervisorJob:
        _validate_owner_inputs(job_id, generation, agent_id)
        if (
            not isinstance(from_state, JobState)
            or not isinstance(to_state, JobState)
            or (from_state, to_state) not in _PRE_RUN_TRANSITIONS
        ):
            raise ValueError("invalid pre-run transition")
        job = _job_from_response(
            self._request(
                "POST",
                "/jobs/{}/transition".format(job_id),
                payload={
                    "generation": generation,
                    "from_state": from_state.value,
                    "to_state": to_state.value,
                },
            )
        )
        owned = _owned_job(job, job_id=job_id, generation=generation, agent_id=agent_id)
        if owned.state is not to_state:
            raise ValueError("transition response has wrong state")
        return owned

    def terminal_transition(self, event: Any, *, agent_id: str) -> SupervisorJob:
        accepted = _coerce_event(event)
        _validate_event_owner(accepted, agent_id)
        terminal_status = accepted.data.get("terminal_status")
        if terminal_status not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            raise ValueError("terminal event must carry a fixed terminal_status")
        job = _job_from_response(
            self._request(
                "POST",
                "/jobs/{}/events".format(accepted.job_id),
                payload=accepted.to_dict(),
            )
        )
        owned = _owned_job(
            job,
            job_id=accepted.job_id,
            generation=accepted.generation,
            agent_id=agent_id,
        )
        if owned.state.value != terminal_status:
            raise ValueError("terminal transition response has wrong state")
        return owned

    def read_desired_state(
        self, *, job_id: str, generation: int, agent_id: str
    ) -> str:
        return self.read_job(
            job_id=job_id,
            generation=generation,
            agent_id=agent_id,
        ).desired_state

    def read_decision(
        self, *, job_id: str, generation: int, agent_id: str
    ) -> Optional[Mapping[str, str]]:
        _validate_owner_inputs(job_id, generation, agent_id)
        response = self._request("GET", "/jobs/{}/decision".format(job_id))
        if response.status == 204:
            return None
        payload = _successful_json(response)
        if set(payload) != {
            "schema_version",
            "generation",
            "decision",
            "requested_action",
        }:
            raise ValueError("invalid decision response")
        if (
            payload["schema_version"] != 1
            or type(payload["generation"]) is not int
            or payload["generation"] != generation
            or not isinstance(payload["decision"], str)
            or not isinstance(payload["requested_action"], str)
        ):
            raise ValueError("invalid decision response")
        pair = (payload["decision"], payload["requested_action"])
        if pair not in {("APPROVE", "CONTINUE"), ("REJECT", "CANCEL")}:
            raise ValueError("invalid decision response")
        return {
            "decision": payload["decision"],
            "requested_action": payload["requested_action"],
        }

    def _request(
        self,
        method: str,
        suffix: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> HttpResponse:
        return self._transport.request(
            method,
            "/v1/{}-agent{}".format(self._environment, suffix),
            payload=payload,
            headers=headers,
        )


def _broker_headers(
    headers: Optional[Mapping[str, str]], token: str
) -> Dict[str, str]:
    result: Dict[str, str] = {}
    if headers is not None:
        for name, value in headers.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError("HTTP headers must be string pairs")
            if name.lower() != "authorization":
                result[name] = value
    result["Authorization"] = "Bearer {}".format(token)
    return result


def _environment_name(environment: Any) -> str:
    value = environment.value if isinstance(environment, AgentEnvironment) else environment
    if value not in {AgentEnvironment.LOCAL.value, AgentEnvironment.AWS.value}:
        raise ValueError("environment must be local or aws")
    return value


def _validate_poll_seconds(wait_seconds: int) -> None:
    if (
        type(wait_seconds) is not int
        or wait_seconds < 1
        or wait_seconds > _MAX_POLL_SECONDS
    ):
        raise ValueError("wait_seconds must be an integer from 1 through 20")


def _validate_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("invalid {}".format(field))


def _nonnegative_int(value: int, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("{} must be a non-negative integer".format(field))
    return value


def _positive_int(value: int, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("{} must be a positive integer".format(field))
    return value


def _validated_string_list(values: Sequence[str], field: str) -> list:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError("{} must be a sequence of strings".format(field))
    result = []
    for value in values:
        _validate_identifier(value, field)
        result.append(value)
    if result != sorted(result) or len(result) != len(set(result)):
        raise ValueError("{} must be sorted and unique".format(field))
    return result


def _validated_version(value: Optional[Mapping[str, str]]) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("version must be an object")
    result = {}
    for key, item in value.items():
        _validate_identifier(key, "version key")
        if not isinstance(item, str) or not item:
            raise ValueError("version values must be non-empty strings")
        result[key] = item
    return result


def _coerce_command(value: Any) -> SupervisorCommand:
    if type(value) is SupervisorCommand:
        return SupervisorCommand.from_dict(value.to_dict())
    if isinstance(value, Mapping):
        return SupervisorCommand.from_dict(value)
    raise ValueError("command must be SupervisorCommand or a command mapping")


def _coerce_event(value: Any) -> SupervisorEvent:
    if type(value) is SupervisorEvent:
        return SupervisorEvent.from_dict(value.to_dict())
    if isinstance(value, Mapping):
        return SupervisorEvent.from_dict(value)
    raise ValueError("event must be SupervisorEvent or an event mapping")


def _artifact_inputs(
    *,
    artifact: Optional[Mapping[str, Any]],
    job_id: Optional[str],
    generation: Optional[int],
    kind: Optional[str],
    body: Optional[bytes],
) -> Tuple[str, int, str, bytes]:
    if artifact is not None:
        if any(value is not None for value in (job_id, generation, kind, body)):
            raise ValueError("artifact mapping cannot be mixed with named artifact fields")
        if not isinstance(artifact, Mapping) or set(artifact) != {
            "job_id",
            "generation",
            "kind",
            "body",
        }:
            raise ValueError("artifact must contain exactly job_id, generation, kind, and body")
        job_id = artifact["job_id"]
        generation = artifact["generation"]
        kind = artifact["kind"]
        body = artifact["body"]
    if not isinstance(job_id, str) or type(generation) is not int or not isinstance(
        kind, str
    ) or not isinstance(body, bytes):
        raise ValueError("artifact fields are invalid")
    return job_id, generation, kind, body


def _validate_owner_inputs(job_id: str, generation: int, agent_id: str) -> None:
    _validate_identifier(job_id, "job_id")
    _nonnegative_int(generation, "generation")
    _validate_identifier(agent_id, "agent_id")


def _job_from_response(response: HttpResponse) -> SupervisorJob:
    return SupervisorJob.from_dict(_successful_json(response))


def _owned_job(
    job: SupervisorJob, *, job_id: str, generation: int, agent_id: str
) -> SupervisorJob:
    validated = SupervisorJob.from_dict(job.to_dict())
    if validated.job_id != job_id or validated.generation != generation:
        raise ValueError("job identity does not match owner request")
    if validated.owner_agent_id != agent_id:
        raise ValueError("job owner does not match authenticated agent")
    if validated.lease_expires_at is None:
        raise ValueError("job lease is missing")
    return validated


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _validate_event_owner(event: SupervisorEvent, agent_id: str) -> None:
    if type(event) is not SupervisorEvent:
        raise ValueError("event must be SupervisorEvent")
    _validate_identifier(agent_id, "agent_id")
    SupervisorEvent.from_dict(event.to_dict())
    if event.source != agent_id:
        raise ValueError("event source does not match authenticated agent")


def _validate_recovery_checkpoint(
    checkpoint: Mapping[str, Any], *, job: SupervisorJob
) -> Mapping[str, Any]:
    try:
        return normalize_recovery_checkpoint(
            checkpoint,
            job_id=job.job_id,
            checkpoint_generation=job.generation - 1,
            repo_refs=job.repo_refs,
        )
    except RecoveryCheckpointError as error:
        raise ValueError(str(error)) from error


__all__ = [
    "AwsSqsCommandSource",
    "BrokerBearerTransport",
    "CommandDelivery",
    "CommandSource",
    "HttpLeaseApi",
    "LeaseApi",
    "LocalHttpCommandSource",
]
