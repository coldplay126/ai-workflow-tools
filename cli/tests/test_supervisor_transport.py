"""Behavioral contracts for Supervisor command and lease transports."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

import awf.supervisor.transport as transport_module
from awf.supervisor.client import HttpResponse, SupervisorConflict, SupervisorAuthRequired
from awf.supervisor.config import SupervisorConfig
from awf.supervisor.contracts import JobState, SupervisorCommand, SupervisorEvent, SupervisorJob
from awf.supervisor.transport import (
    AwsSqsCommandSource,
    BrokerBearerTransport,
    HttpLeaseApi,
    LocalHttpCommandSource,
)


NOW = "2026-07-30T12:00:00Z"
COMMAND_ID = "5a2c1e31-cf45-4fe4-ae47-f40d3eb90837"
IDEMPOTENCY_KEY = "2b0f54d4-a2cc-4ca8-b229-e84606ed80a6"
SHA256 = "a" * 64

ARTIFACT_SHA256 = hashlib.sha256(b"{}").hexdigest()
PROMPT_SHA256 = hashlib.sha256(b"Do work").hexdigest()

@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    payload: Optional[Mapping[str, Any]]
    headers: Mapping[str, str]


class RecordingHttpTransport:
    def __init__(self, responses: Sequence[HttpResponse]) -> None:
        self._responses = list(responses)
        self.calls: List[RecordedRequest] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> HttpResponse:
        self.calls.append(
            RecordedRequest(method, path, payload, dict(headers or {}))
        )
        if not self._responses:
            raise AssertionError("unexpected HTTP request")
        return self._responses.pop(0)


class RotatingTokenBroker:
    def __init__(self, tokens: Sequence[str]) -> None:
        self._tokens = list(tokens)
        self.current_calls: List[str] = []
        self.invalidations = 0

    def current(
        self, agent_id: str, now: Optional[datetime] = None
    ) -> Any:
        self.current_calls.append(agent_id)
        if not self._tokens:
            raise AssertionError("unexpected broker token request")
        return type("AccessToken", (), {"value": self._tokens.pop(0)})()

    def invalidate(self) -> None:
        self.invalidations += 1


class RecordingSqsClient:
    def __init__(self, messages: Sequence[Mapping[str, Any]]) -> None:
        self._messages = list(messages)
        self.receive_calls: List[Mapping[str, Any]] = []
        self.deleted: List[Mapping[str, str]] = []
        self.visibility_changes: List[Mapping[str, Any]] = []

    def receive_message(self, **kwargs: Any) -> Mapping[str, Any]:
        self.receive_calls.append(kwargs)
        if not self._messages:
            return {}
        return {"Messages": [self._messages.pop(0)]}

    def delete_message(self, **kwargs: str) -> None:
        self.deleted.append(kwargs)

    def change_message_visibility(self, **kwargs: Any) -> None:
        self.visibility_changes.append(kwargs)


def response(status: int, payload: Any) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers={"x-request-id": "request-123"},
        body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    )


def command_fixture(**updates: Any) -> Dict[str, Any]:
    payload = {
        "schema_version": 1,
        "command_id": COMMAND_ID,
        "job_id": "job-1",
        "generation": 2,
        "type": "EXECUTE",
    }
    payload.update(updates)
    return payload


def job_fixture(**updates: Any) -> Dict[str, Any]:
    payload = {
        "schema_version": 1,
        "job_id": "job-1",
        "workflow_id": "2026-07-30-login-contract",
        "state": "CLAIMED",
        "desired_state": "RUNNING",
        "approval_required": True,
        "requested_target": "auto",
        "owner_agent_id": "local-mac-01",
        "lease_expires_at": "2099-07-30T12:05:00Z",
        "generation": 2,
        "attempt": 1,
        "repo_refs": [{"repo": "api", "base": "main"}],
        "required_capabilities": ["git", "omp"],
        "checkpoint": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    payload.update(updates)
    return payload


def event_fixture(**updates: Any) -> Dict[str, Any]:
    payload = {
        "schema_version": 1,
        "job_id": "job-1",
        "generation": 2,
        "sequence": 1,
        "type": "TASK_STARTED",
        "timestamp": NOW,
        "source": "local-mac-01",
        "data": {"summary": "task_started"},
    }
    payload.update(updates)
    return payload


def checkpoint_fixture(**updates: Any) -> Dict[str, Any]:
    payload = {
        "schema_version": 1,
        "kind": "awf-supervisor-recovery-checkpoint",
        "job_id": "job-1",
        "generation": 1,
        "origin_agent_id": "local-mac-01",
        "origin_environment": "local",
        "native": {
            "batch_fingerprint": "b" * 64,
            "state": "interrupted",
            "coordinator_session_id": "session-1",
        },
        "worker_descriptors": [{"name": "SupervisorJob", "sha256": "c" * 64}],
        "handles": {
            "task_id": "task-1",
            "agent_uri": "agent://agent-1",
            "history_uri": "history://history-1",
        },
        "workspace_manifest_sha256": "d" * 64,
        "repos": [
            {
                "repo": "api",
                "base": "main",
                "head": "e" * 40,
                "remote_ref": "refs/heads/main",
                "clean": True,
                "pushed": True,
            }
        ],
        "cross_node_eligible": True,
    }
    payload.update(updates)
    return payload


def encoded_checkpoint(payload: Mapping[str, Any]) -> Dict[str, str]:
    artifact = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return {
        "artifact_base64": base64.b64encode(artifact).decode("ascii"),
        "sha256": hashlib.sha256(artifact).hexdigest(),
    }


def test_local_broker_transport_attaches_a_fresh_bearer_token_to_every_request() -> None:
    http = RecordingHttpTransport([response(200, {}) for _ in range(3)])
    broker = RotatingTokenBroker(["access-1", "access-2", "access-3"])
    transport = BrokerBearerTransport(
        http=http, token_broker=broker, agent_id="local-mac-01"
    )

    transport.request("GET", "/v1/local-agent/commands")
    transport.request("POST", "/v1/local-agent/heartbeat", payload={})
    transport.request("GET", "/v1/local-agent/jobs/job-1")

    assert broker.current_calls == ["local-mac-01"] * 3
    assert [call.headers["Authorization"] for call in http.calls] == [
        "Bearer access-1",
        "Bearer access-2",
        "Bearer access-3",
    ]


def test_local_broker_transport_forces_exactly_one_refresh_after_401_and_preserves_key() -> None:
    http = RecordingHttpTransport([response(401, {"code": "UNAUTHORIZED", "message": "expired"}), response(200, {})])
    broker = RotatingTokenBroker(["old-access", "fresh-access"])
    transport = BrokerBearerTransport(
        http=http, token_broker=broker, agent_id="local-mac-01"
    )

    result = transport.request(
        "POST",
        "/v1/local-agent/jobs/job-1/events",
        payload=event_fixture(),
        headers={"Idempotency-Key": IDEMPOTENCY_KEY},
    )

    assert result.status == 200
    assert broker.invalidations == 1
    assert broker.current_calls == ["local-mac-01", "local-mac-01"]
    assert [call.headers["Authorization"] for call in http.calls] == [
        "Bearer old-access",
        "Bearer fresh-access",
    ]
    assert [call.headers["Idempotency-Key"] for call in http.calls] == [
        IDEMPOTENCY_KEY,
        IDEMPOTENCY_KEY,
    ]


def test_local_broker_transport_never_permits_caller_authorization_override() -> None:
    http = RecordingHttpTransport([response(200, {})])
    transport = BrokerBearerTransport(
        http=http,
        token_broker=RotatingTokenBroker(["broker-access"]),
        agent_id="local-mac-01",
    )

    transport.request(
        "GET",
        "/v1/local-agent/commands",
        headers={"authorization": "Bearer caller-controlled", "X-Trace": "trace-1"},
    )

    assert http.calls[0].headers == {
        "Authorization": "Bearer broker-access",
        "X-Trace": "trace-1",
    }


def test_local_command_source_uses_bounded_poll_and_returns_no_delivery_for_empty_response() -> None:
    http = RecordingHttpTransport([response(204, {})])
    source = LocalHttpCommandSource(transport=http)

    delivery = source.next_command(wait_seconds=20)

    assert delivery is None
    assert http.calls == [
        RecordedRequest(
            "GET",
            "/v1/local-agent/commands",
            None,
            {"X-Awf-Poll-Seconds": "20"},
        )
    ]


@pytest.mark.parametrize("wait_seconds", [0, -1, 21, True, "20"])
def test_local_command_source_rejects_unbounded_or_invalid_poll_time(
    wait_seconds: Any,
) -> None:
    source = LocalHttpCommandSource(transport=RecordingHttpTransport([]))

    with pytest.raises(ValueError, match="wait_seconds"):
        source.next_command(wait_seconds=wait_seconds)


def test_local_command_source_rejects_malformed_or_wrong_schema_delivery() -> None:
    malformed = {"command": command_fixture()}
    wrong_schema = {"schema_version": 2, "command_id": COMMAND_ID, "job_id": "job-1", "generation": 2, "type": "EXECUTE"}
    http = RecordingHttpTransport([response(200, malformed), response(200, wrong_schema)])
    source = LocalHttpCommandSource(transport=http)

    with pytest.raises(ValueError, match="command"):
        source.next_command(wait_seconds=1)
    with pytest.raises(ValueError, match="schema_version"):
        source.next_command(wait_seconds=1)


def test_local_command_delivery_ack_and_release_are_safe_noops() -> None:
    http = RecordingHttpTransport([response(200, command_fixture())])
    source = LocalHttpCommandSource(transport=http)

    delivery = source.next_command(wait_seconds=1)
    assert delivery is not None
    assert delivery.command == SupervisorCommand.from_dict(command_fixture())
    delivery.ack()
    delivery.ack()
    delivery.release()

    assert [(call.method, call.path) for call in http.calls] == [
        ("GET", "/v1/local-agent/commands"),
    ]


def test_aws_sqs_source_handles_empty_poll_and_uses_configured_visibility_timeout() -> None:
    sqs = RecordingSqsClient([])
    source = AwsSqsCommandSource(
        sqs_client=sqs,
        queue_url="https://sqs.ap-northeast-2.amazonaws.com/123/commands",
        visibility_timeout_seconds=120,
    )

    delivery = source.next_command(wait_seconds=20)

    assert delivery is None
    assert sqs.receive_calls == [
        {
            "QueueUrl": "https://sqs.ap-northeast-2.amazonaws.com/123/commands",
            "MaxNumberOfMessages": 1,
            "WaitTimeSeconds": 20,
            "VisibilityTimeout": 120,
        }
    ]


@pytest.mark.parametrize(
    "body",
    [
        "not-json",
        json.dumps({"command": command_fixture()}),
        json.dumps(command_fixture(schema_version=2)),
        json.dumps(command_fixture(command_id="not-a-uuid")),
    ],
)
def test_aws_sqs_source_rejects_malformed_or_wrong_schema_message_without_ack(
    body: str,
) -> None:
    sqs = RecordingSqsClient([{"Body": body, "ReceiptHandle": "receipt-1", "MessageId": "message-1"}])
    source = AwsSqsCommandSource(
        sqs_client=sqs,
        queue_url="queue-url",
        visibility_timeout_seconds=120,
    )

    with pytest.raises(ValueError):
        source.next_command(wait_seconds=1)

    assert sqs.deleted == []
    assert sqs.visibility_changes == [
        {
            "QueueUrl": "queue-url",
            "ReceiptHandle": "receipt-1",
            "VisibilityTimeout": 0,
        }
    ]


def test_aws_sqs_delivery_ack_and_release_are_each_idempotent_and_mutually_exclusive() -> None:
    sqs = RecordingSqsClient(
        [
            {
                "Body": json.dumps(command_fixture()),
                "ReceiptHandle": "receipt-1",
                "MessageId": "message-1",
            },
            {
                "Body": json.dumps(command_fixture()),
                "ReceiptHandle": "receipt-2",
                "MessageId": "message-2",
            },
        ]
    )
    source = AwsSqsCommandSource(
        sqs_client=sqs,
        queue_url="queue-url",
        visibility_timeout_seconds=120,
    )

    acknowledged = source.next_command(wait_seconds=1)
    released = source.next_command(wait_seconds=1)
    assert acknowledged is not None
    assert released is not None
    assert acknowledged.command.command_id == released.command.command_id
    acknowledged.ack()
    acknowledged.ack()
    acknowledged.release()
    released.release()
    released.release()
    released.ack()

    assert sqs.deleted == [{"QueueUrl": "queue-url", "ReceiptHandle": "receipt-1"}]
    assert sqs.visibility_changes == [
        {"QueueUrl": "queue-url", "ReceiptHandle": "receipt-2", "VisibilityTimeout": 0}
    ]


def test_http_lease_api_uses_exact_owner_routes_and_validates_contracts() -> None:
    checkpoint = encoded_checkpoint(checkpoint_fixture())
    checkpoint_job = job_fixture(
        checkpoint={
            "kind": "awf-omp-native",
            "artifact_uri": "s3://bucket/artifacts/checkpoints/job-1/1/"
            + checkpoint["sha256"]
            + ".json",
            "sha256": checkpoint["sha256"],
        }
    )
    http = RecordingHttpTransport(
        [
            response(200, {}),
            response(200, job_fixture()),
            response(200, job_fixture(lease_expires_at="2099-07-30T12:10:00Z")),
            response(200, checkpoint_job),
            response(200, {"prompt": "Do work", "sha256": PROMPT_SHA256}),
            response(200, checkpoint),
            response(201, {"artifact_uri": "s3://bucket/artifacts/provenance/job-1/2/" + ARTIFACT_SHA256 + ".json", "sha256": ARTIFACT_SHA256}),
            response(202, {}),
            response(200, job_fixture(state="PREPARING")),
            response(200, job_fixture(state="SUCCEEDED")),
            response(200, job_fixture(desired_state="RUNNING")),
            response(200, {"schema_version": 1, "generation": 2, "decision": "APPROVE", "requested_action": "CONTINUE"}),
        ]
    )
    api = HttpLeaseApi(transport=http)
    command = SupervisorCommand.from_dict(command_fixture())
    event = SupervisorEvent.from_dict(event_fixture())
    terminal_event = SupervisorEvent.from_dict(
        event_fixture(
            type="TASK_COMPLETED",
            data={
                "summary": "task_completed",
                "terminal_status": "SUCCEEDED",
                "return_code": 0,
                "artifact_uri": "s3://bucket/artifacts/redacted-results/job-1/2/"
                + SHA256
                + ".json",
                "artifact_sha256": SHA256,
                "provenance_uri": "s3://bucket/artifacts/provenance/job-1/2/"
                + SHA256
                + ".json",
                "provenance_sha256": SHA256,
            },
        )
    )

    api.heartbeat(agent_id="local-mac-01")
    accepted = api.accept_claim(command_fixture(), agent_id="local-mac-01")
    renewed = api.renew(job_id="job-1", generation=2, agent_id="local-mac-01")
    job = api.read_job(job_id="job-1", generation=2, agent_id="local-mac-01")
    prompt = api.fetch_prompt(job_id="job-1", generation=2, agent_id="local-mac-01")
    job = SupervisorJob.from_dict(checkpoint_job)
    recovery = api.fetch_checkpoint(job=job, agent_id="local-mac-01")
    artifact = api.upload_artifact(
        {
            "job_id": "job-1",
            "generation": 2,
            "kind": "provenance",
            "body": b"{}",
        },
        agent_id="local-mac-01",
    )
    api.append_event(event_fixture(), agent_id="local-mac-01")
    preparing = api.advance_state(
        job_id="job-1", generation=2, agent_id="local-mac-01", from_state=JobState.CLAIMED, to_state=JobState.PREPARING
    )
    terminal = api.terminal_transition(terminal_event, agent_id="local-mac-01")
    desired = api.read_desired_state(job_id="job-1", generation=2, agent_id="local-mac-01")
    decision = api.read_decision(job_id="job-1", generation=2, agent_id="local-mac-01")

    assert accepted.job_id == "job-1"
    assert renewed.lease_expires_at == "2099-07-30T12:10:00Z"
    assert job.job_id == "job-1"
    assert prompt == ("Do work", PROMPT_SHA256)
    assert recovery["job_id"] == "job-1"
    assert artifact == {
        "artifact_uri": "s3://bucket/artifacts/provenance/job-1/2/" + ARTIFACT_SHA256 + ".json",
        "sha256": ARTIFACT_SHA256,
    }
    assert preparing.state is JobState.PREPARING
    assert terminal.state is JobState.SUCCEEDED
    assert desired == "RUNNING"
    assert decision == {"decision": "APPROVE", "requested_action": "CONTINUE"}
    assert [(call.method, call.path) for call in http.calls] == [
        ("POST", "/v1/local-agent/heartbeat"),
        ("POST", "/v1/local-agent/jobs/job-1/claim"),
        ("POST", "/v1/local-agent/jobs/job-1/renew"),
        ("GET", "/v1/local-agent/jobs/job-1"),
        ("GET", "/v1/local-agent/jobs/job-1/prompt"),
        ("GET", "/v1/local-agent/jobs/job-1/checkpoint"),
        ("POST", "/v1/local-agent/jobs/job-1/artifacts"),
        ("POST", "/v1/local-agent/jobs/job-1/events"),
        ("POST", "/v1/local-agent/jobs/job-1/transition"),
        ("POST", "/v1/local-agent/jobs/job-1/events"),
        ("GET", "/v1/local-agent/jobs/job-1"),
        ("GET", "/v1/local-agent/jobs/job-1/decision"),
    ]


def test_http_lease_api_inspect_claim_signs_exact_preclaim_headers(
) -> None:
    http = RecordingHttpTransport(
        [
            response(
                200,
                job_fixture(
                    state=JobState.QUEUED.value,
                    owner_agent_id=None,
                    lease_expires_at=None,
                ),
            )
        ]
    )
    api = HttpLeaseApi(transport=http)
    command = SupervisorCommand.from_dict(command_fixture())

    inspected = api.inspect_claim(command, agent_id="local-mac-01")

    assert inspected.state is JobState.QUEUED
    assert inspected.owner_agent_id is None
    assert http.calls == [
        RecordedRequest(
            "GET",
            "/v1/local-agent/jobs/job-1",
            None,
            {
                "X-AWF-Command-Id": COMMAND_ID,
                "X-AWF-Generation": "2",
            },
        )
    ]


def test_http_lease_api_rejects_prompt_checksum_mismatch() -> None:
    http = RecordingHttpTransport(
        [response(200, {"prompt": "Do work", "sha256": SHA256})]
    )
    api = HttpLeaseApi(transport=http)

    with pytest.raises(ValueError, match="prompt sha256"):
        api.fetch_prompt(
            job_id="job-1",
            generation=2,
            agent_id="local-mac-01",
        )


def test_http_lease_api_rejects_invalid_owner_reply_before_any_runtime_side_effect() -> None:
    http = RecordingHttpTransport([response(200, job_fixture(owner_agent_id="other-agent"))])
    api = HttpLeaseApi(transport=http)

    with pytest.raises(ValueError, match="owner"):
        api.read_job(job_id="job-1", generation=2, agent_id="local-mac-01")


def test_http_lease_api_surfaces_server_fenced_expiry_without_client_clock_inference() -> None:
    http = RecordingHttpTransport(
        [response(409, {"code": "LEASE_CONFLICT", "message": "lease expired"})]
    )
    api = HttpLeaseApi(transport=http)

    with pytest.raises(SupervisorConflict, match="lease expired"):
        api.read_job(job_id="job-1", generation=2, agent_id="local-mac-01")


def test_http_lease_api_allows_owner_fenced_approval_resume() -> None:
    http = RecordingHttpTransport(
        [response(200, job_fixture(state=JobState.RUNNING.value))]
    )
    api = HttpLeaseApi(transport=http)

    resumed = api.advance_state(
        job_id="job-1",
        generation=2,
        agent_id="local-mac-01",
        from_state=JobState.WAITING_APPROVAL,
        to_state=JobState.RUNNING,
    )

    assert resumed.state is JobState.RUNNING
    assert http.calls[0].payload == {
        "generation": 2,
        "from_state": "WAITING_APPROVAL",
        "to_state": "RUNNING",
    }


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        (JobState.QUEUED, JobState.CLAIMED),
        (JobState.CLAIMED, JobState.RUNNING),
        (JobState.PREPARING, JobState.WAITING_APPROVAL),
        (JobState.RUNNING, JobState.SUCCEEDED),
    ],
)
def test_http_lease_api_rejects_every_non_fixed_pre_run_transition_pair(
    from_state: JobState, to_state: JobState
) -> None:
    http = RecordingHttpTransport([])
    api = HttpLeaseApi(transport=http)

    with pytest.raises(ValueError, match="transition"):
        api.advance_state(
            job_id="job-1", generation=2, agent_id="local-mac-01", from_state=from_state, to_state=to_state
        )

    assert http.calls == []


def test_http_lease_api_rejects_checkpoint_hash_extra_field_and_identity_mismatch() -> None:
    good = encoded_checkpoint(checkpoint_fixture())
    bad_hash = dict(good)
    bad_hash["sha256"] = "0" * 64
    extra = encoded_checkpoint(checkpoint_fixture(extra="not-permitted"))
    wrong_identity = encoded_checkpoint(checkpoint_fixture(job_id="other-job"))
    http = RecordingHttpTransport(
        [
            response(200, bad_hash),
            response(200, extra),
            response(200, wrong_identity),
        ]
    )
    api = HttpLeaseApi(transport=http)

    def checkpoint_job(encoded: Mapping[str, str]) -> SupervisorJob:
        return SupervisorJob.from_dict(
            job_fixture(
                checkpoint={
                    "kind": "awf-omp-native",
                    "artifact_uri": "s3://bucket/artifacts/checkpoints/job-1/1/"
                    + encoded["sha256"]
                    + ".json",
                    "sha256": encoded["sha256"],
                }
            )
        )

    with pytest.raises(ValueError, match="sha256"):
        api.fetch_checkpoint(job=checkpoint_job(good), agent_id="local-mac-01")
    with pytest.raises(ValueError, match="checkpoint"):
        api.fetch_checkpoint(job=checkpoint_job(extra), agent_id="local-mac-01")
    with pytest.raises(ValueError, match="identity"):
        api.fetch_checkpoint(job=checkpoint_job(wrong_identity), agent_id="local-mac-01")

@pytest.mark.parametrize(
    ("canonical", "legacy"),
    (
        ("origin_agent_id", "agent_id"),
        ("origin_environment", "environment"),
        ("native", "native_checkpoint"),
        ("repos", "repositories"),
    ),
)
def test_http_lease_api_rejects_each_legacy_recovery_checkpoint_alias(
    canonical: str, legacy: str
) -> None:
    checkpoint = checkpoint_fixture()
    checkpoint[legacy] = checkpoint.pop(canonical)
    encoded = encoded_checkpoint(checkpoint)
    job = SupervisorJob.from_dict(
        job_fixture(
            checkpoint={
                "kind": "awf-omp-native",
                "artifact_uri": "s3://bucket/artifacts/checkpoints/job-1/1/"
                + encoded["sha256"]
                + ".json",
                "sha256": encoded["sha256"],
            }
        )
    )
    api = HttpLeaseApi(transport=RecordingHttpTransport([response(200, encoded)]))

    with pytest.raises(ValueError, match="recovery checkpoint"):
        api.fetch_checkpoint(job=job, agent_id="local-mac-01")


def test_http_lease_api_rejects_nonresumable_native_recovery_checkpoint() -> None:
    encoded = encoded_checkpoint(
        checkpoint_fixture(
            native={
                "batch_fingerprint": "b" * 64,
                "state": "completed",
                "coordinator_session_id": "session-1",
            }
        )
    )
    job = SupervisorJob.from_dict(
        job_fixture(
            checkpoint={
                "kind": "awf-omp-native",
                "artifact_uri": "s3://bucket/artifacts/checkpoints/job-1/1/"
                + encoded["sha256"]
                + ".json",
                "sha256": encoded["sha256"],
            }
        )
    )
    api = HttpLeaseApi(transport=RecordingHttpTransport([response(200, encoded)]))

    with pytest.raises(ValueError, match="resumable"):
        api.fetch_checkpoint(job=job, agent_id="local-mac-01")


def supervisor_config() -> SupervisorConfig:
    return SupervisorConfig(
        api_url="https://abc123.execute-api.ap-northeast-2.amazonaws.com",
        region="ap-northeast-2",
        profile="team-profile",
        poll_interval_seconds=2,
        request_timeout_seconds=30,
    )


def test_aws_lease_api_selects_sigv4_without_reading_the_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: List[SupervisorConfig] = []

    class RecordingSigV4Transport:
        def __init__(self, config: SupervisorConfig) -> None:
            created.append(config)

        def request(self, *args: Any, **kwargs: Any) -> HttpResponse:
            raise AssertionError("request is not part of transport construction")

    broker = RotatingTokenBroker(["broker-access"])
    monkeypatch.setattr(transport_module, "SigV4Transport", RecordingSigV4Transport)

    api = HttpLeaseApi.for_environment(
        environment="aws",
        config=supervisor_config(),
        token_broker=broker,
    )

    assert isinstance(api.transport, RecordingSigV4Transport)
    assert created == [supervisor_config()]
    assert broker.current_calls == []


def test_aws_lease_api_surfaces_missing_aws_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingCredentialsSigV4Transport:
        def __init__(self, config: SupervisorConfig) -> None:
            raise SupervisorAuthRequired("AWS SSO credentials are unavailable")

    monkeypatch.setattr(
        transport_module, "SigV4Transport", MissingCredentialsSigV4Transport
    )

    with pytest.raises(SupervisorAuthRequired):
        HttpLeaseApi.for_environment(environment="aws", config=supervisor_config())


def test_environment_transport_split_requires_local_broker_configuration() -> None:
    local_http = RecordingHttpTransport([])
    broker = RotatingTokenBroker(["access-token"])
    local = HttpLeaseApi.for_environment(
        environment="local",
        http=local_http,
        token_broker=broker,
        agent_id="local-mac-01",
    )

    assert isinstance(local.transport, BrokerBearerTransport)
    with pytest.raises(ValueError, match="local environment"):
        HttpLeaseApi.for_environment(
            environment="local",
            http=local_http,
            agent_id="local-mac-01",
        )
