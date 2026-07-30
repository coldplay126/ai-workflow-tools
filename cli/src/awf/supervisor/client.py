"""Authenticated HTTP client for the AWF Supervisor admin API."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from http import client as http_client
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Type
from urllib import error as urllib_error
from urllib import request as urllib_request

import botocore.session
from botocore.auth import SigV4Auth
from botocore.exceptions import BotoCoreError
from botocore.awsrequest import AWSRequest

from awf.supervisor.config import SupervisorConfig
from awf.supervisor.contracts import RequestedTarget, SupervisorAgent, SupervisorJob


_MEBIBYTE = 1024 * 1024
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REPO_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_BASE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,254}$")
_CAPABILITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True)
class HttpResponse:
    """The bounded bytes returned by a supervisor HTTP transport."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class SupervisorRemoteError(RuntimeError):
    """An API response that the Supervisor rejected or could not serve."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        code: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        self.status = status
        self.code = code
        self.request_id = request_id
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        if self.request_id is None:
            return self.message
        return "{} (request_id={})".format(self.message, self.request_id)


class SupervisorAuthRequired(SupervisorRemoteError):
    """AWS credentials or authorization are unavailable for the request."""


class SupervisorConflict(SupervisorRemoteError):
    """The request used a stale Supervisor resource generation."""


@dataclass(frozen=True)
class RepoRef:
    """A validated immutable repository and branch reference."""

    repo: str
    base: str

    def __post_init__(self) -> None:
        _validate_pattern(self.repo, "repo", _REPO_PATTERN)
        _validate_pattern(self.base, "base", _BASE_PATTERN)

    def to_dict(self) -> Dict[str, str]:
        return {"repo": self.repo, "base": self.base}


class Transport(Protocol):
    """The testable port used by :class:`SupervisorClient`."""

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> HttpResponse:
        ...


class SigV4Transport:
    """A one-shot AWS SigV4 transport for the Supervisor execute-api endpoint."""

    def __init__(self, config: SupervisorConfig) -> None:
        if not config.api_url:
            raise ValueError("Supervisor API URL is required")
        self._config = config
        try:
            session = botocore.session.Session(profile=config.profile or None)
            self._credentials = session.get_credentials()
        except BotoCoreError as error:
            raise SupervisorAuthRequired("AWS SSO credentials are unavailable") from error
        if self._credentials is None:
            raise SupervisorAuthRequired("AWS SSO credentials are unavailable")

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> HttpResponse:
        _validate_transport_path(path)
        body = (
            b""
            if payload is None
            else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        request_headers = {"Content-Type": "application/json"}
        if headers is not None:
            request_headers.update(dict(headers))
        request = AWSRequest(
            method=method,
            url="{}{}".format(self._config.api_url, path),
            data=body,
            headers=request_headers,
        )
        try:
            frozen_credentials = self._credentials.get_frozen_credentials()
        except BotoCoreError as error:
            raise SupervisorAuthRequired("AWS SSO credentials are unavailable") from error
        SigV4Auth(
            frozen_credentials,
            "execute-api",
            self._config.region,
        ).add_auth(request)
        return _send_prepared(
            request.prepare(), timeout=self._config.request_timeout_seconds
        )


class SupervisorClient:
    """One-shot methods for the version 1 Supervisor admin API."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def submit_job(
        self,
        *,
        workflow_id: str,
        requested_target: RequestedTarget,
        repo_refs: Sequence[RepoRef],
        required_capabilities: Sequence[str],
        prompt: str,
        idempotency_key: Optional[str] = None,
    ) -> SupervisorJob:
        _validate_pattern(workflow_id, "workflow_id", _IDENTIFIER_PATTERN)
        if not isinstance(requested_target, RequestedTarget):
            raise ValueError("requested_target must be a RequestedTarget")
        refs = _validate_repo_refs(repo_refs)
        capabilities = _validate_capabilities(required_capabilities)
        if not isinstance(prompt, str):
            raise ValueError("prompt must be a string")
        key = (
            _validate_uuid4(idempotency_key)
            if idempotency_key is not None
            else _new_idempotency_key()
        )
        payload = {
            "schema_version": 1,
            "workflow_id": workflow_id,
            "requested_target": requested_target.value,
            "repo_refs": [repo_ref.to_dict() for repo_ref in refs],
            "required_capabilities": capabilities,
            "prompt": prompt,
        }
        response = self._transport.request(
            "POST",
            "/v1/admin/jobs",
            payload=payload,
            headers={"Idempotency-Key": key},
        )
        return SupervisorJob.from_dict(_successful_json(response))

    def get_job(self, job_id: str) -> SupervisorJob:
        _validate_pattern(job_id, "job_id", _IDENTIFIER_PATTERN)
        response = self._transport.request("GET", "/v1/admin/jobs/{}".format(job_id))
        return SupervisorJob.from_dict(_successful_json(response))

    def cancel_job(self, job_id: str, *, generation: int) -> SupervisorJob:
        _validate_pattern(job_id, "job_id", _IDENTIFIER_PATTERN)
        _validate_generation(generation)
        response = self._transport.request(
            "POST",
            "/v1/admin/jobs/{}/cancel".format(job_id),
            payload={"generation": generation},
            headers={"Idempotency-Key": _new_idempotency_key()},
        )
        return SupervisorJob.from_dict(_successful_json(response))

    def approve_job(self, job_id: str, *, generation: int) -> SupervisorJob:
        return self._decide_job(
            job_id,
            generation=generation,
            decision="APPROVE",
            requested_action="CONTINUE",
        )

    def reject_job(self, job_id: str, *, generation: int) -> SupervisorJob:
        return self._decide_job(
            job_id,
            generation=generation,
            decision="REJECT",
            requested_action="CANCEL",
        )

    def list_agents(self) -> List[SupervisorAgent]:
        response = self._transport.request("GET", "/v1/admin/agents")
        payload = _successful_json(response)
        if set(payload) != {"schema_version", "agents"}:
            raise ValueError("invalid agents response envelope")
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
            raise ValueError("unsupported agents response schema_version")
        agents = payload["agents"]
        if not isinstance(agents, list):
            raise ValueError("invalid agents response envelope")
        return [
            SupervisorAgent.from_dict(agent)
            if isinstance(agent, Mapping)
            else _invalid_agent_response()
            for agent in agents
        ]

    def _decide_job(
        self,
        job_id: str,
        *,
        generation: int,
        decision: str,
        requested_action: str,
    ) -> SupervisorJob:
        _validate_pattern(job_id, "job_id", _IDENTIFIER_PATTERN)
        _validate_generation(generation)
        response = self._transport.request(
            "POST",
            "/v1/admin/jobs/{}/decisions".format(job_id),
            payload={
                "generation": generation,
                "decision": decision,
                "requested_action": requested_action,
            },
            headers={"Idempotency-Key": _new_idempotency_key()},
        )
        return SupervisorJob.from_dict(_successful_json(response))


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    """Preserve redirect responses without replaying signed requests."""

    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _send_prepared(prepared: Any, *, timeout: int) -> HttpResponse:
    """Send one prepared request through urllib without retrying it."""

    request = urllib_request.Request(
        prepared.url,
        data=prepared.body,
        headers=dict(prepared.headers.items()),
        method=prepared.method,
    )
    try:
        try:
            opener = urllib_request.build_opener(_NoRedirectHandler())
            with opener.open(request, timeout=timeout) as result:
                return HttpResponse(
                    status=result.getcode(),
                    headers=_headers_to_dict(result.headers),
                    body=_read_bounded_body(result),
                )
        except urllib_error.HTTPError as error:
            return HttpResponse(
                status=error.code,
                headers=_headers_to_dict(error.headers),
                body=_read_bounded_body(error),
            )
    except (OSError, http_client.HTTPException) as error:
        raise SupervisorRemoteError(
            "Supervisor API request failed", status=0, code="NETWORK_ERROR"
        ) from error


def _successful_json(response: HttpResponse) -> Mapping[str, Any]:
    _validate_response_status(response.status)
    if not 200 <= response.status < 300:
        raise _remote_error(response)
    return _decode_json_object(response.body)


def _remote_error(response: HttpResponse) -> SupervisorRemoteError:
    error_type: Type[SupervisorRemoteError]
    if response.status == 401:
        error_type = SupervisorAuthRequired
    elif response.status == 409:
        error_type = SupervisorConflict
    else:
        error_type = SupervisorRemoteError

    request_id = _request_id(response.headers)
    try:
        payload = _decode_json_object(response.body)
        if set(payload) != {"code", "message"}:
            raise ValueError("invalid Supervisor error response envelope")
        code = payload["code"]
        message = payload["message"]
        if not isinstance(code, str) or not code:
            raise ValueError("invalid Supervisor error response code")
        if not isinstance(message, str) or not message:
            raise ValueError("invalid Supervisor error response message")
    except ValueError:
        return error_type(
            "Supervisor returned an invalid error response",
            status=response.status,
            code="INVALID_ERROR_RESPONSE",
            request_id=request_id,
        )
    return error_type(
        message,
        status=response.status,
        code=code,
        request_id=request_id,
    )


def _decode_json_object(body: bytes) -> Mapping[str, Any]:
    if not isinstance(body, bytes):
        raise ValueError("Supervisor response body must be bytes")
    if len(body) > _MEBIBYTE:
        raise ValueError("Supervisor response body exceeds 1 MiB")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("invalid Supervisor JSON response") from error
    if not isinstance(payload, Mapping):
        raise ValueError("Supervisor response must be a JSON object")
    return payload


def _headers_to_dict(headers: Any) -> Dict[str, str]:
    if headers is None:
        return {}
    return {str(key): str(value) for key, value in headers.items()}


def _read_bounded_body(response: Any) -> bytes:
    body = response.read(_MEBIBYTE + 1)
    if not isinstance(body, bytes):
        raise ValueError("Supervisor response body must be bytes")
    return body


def _request_id(headers: Mapping[str, str]) -> Optional[str]:
    for name, value in headers.items():
        if name.lower() == "x-request-id" and isinstance(value, str):
            return value
    return None


def _validate_response_status(status: int) -> None:
    if type(status) is not int:
        raise ValueError("Supervisor response status must be an integer")


def _validate_transport_path(path: str) -> None:
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or "?" in path
        or "#" in path
    ):
        raise ValueError("Supervisor request path must be an absolute API path")


def _validate_uuid4(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("idempotency_key must be a canonical UUID4")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as error:
        raise ValueError("idempotency_key must be a canonical UUID4") from error
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("idempotency_key must be a canonical UUID4")
    return value


def _new_idempotency_key() -> str:
    return _validate_uuid4(str(uuid.uuid4()))


def _validate_repo_refs(repo_refs: Sequence[RepoRef]) -> Sequence[RepoRef]:
    if isinstance(repo_refs, (str, bytes)) or not isinstance(repo_refs, Sequence):
        raise ValueError("repo_refs must be a sequence of RepoRef values")
    if not repo_refs:
        raise ValueError("repo_refs must not be empty")
    if not all(isinstance(repo_ref, RepoRef) for repo_ref in repo_refs):
        raise ValueError("repo_refs must contain only RepoRef values")
    names = [repo_ref.repo for repo_ref in repo_refs]
    if len(names) != len(set(names)):
        raise ValueError("repo_refs contains duplicate repo names")
    return repo_refs


def _validate_capabilities(required_capabilities: Sequence[str]) -> List[str]:
    if isinstance(required_capabilities, (str, bytes)) or not isinstance(
        required_capabilities, Sequence
    ):
        raise ValueError("required_capabilities must be a sequence of strings")
    if not required_capabilities:
        raise ValueError("required_capabilities must not be empty")
    capabilities = list(required_capabilities)
    for capability in capabilities:
        _validate_pattern(capability, "required_capability", _CAPABILITY_PATTERN)
    if len(capabilities) != len(set(capabilities)):
        raise ValueError("required_capabilities contains duplicates")
    return capabilities


def _validate_generation(generation: int) -> None:
    if type(generation) is not int or generation < 0:
        raise ValueError("generation must be a non-negative integer")


def _validate_pattern(value: str, field: str, pattern: re.Pattern[str]) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError("invalid {}".format(field))


def _invalid_agent_response() -> SupervisorAgent:
    raise ValueError("invalid agent contract: payload must be an object")


__all__ = [
    "HttpResponse",
    "RepoRef",
    "SigV4Transport",
    "SupervisorAuthRequired",
    "SupervisorClient",
    "SupervisorConflict",
    "SupervisorRemoteError",
    "Transport",
]
