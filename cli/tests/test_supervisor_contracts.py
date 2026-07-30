"""Wire-contract regression tests for AWF Supervisor version 1.

These tests deliberately exercise the public, serializable envelopes.  They do
not inspect schema implementation details: every rejection is observed through
the public validation or typed round-trip APIs.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Type
from zipfile import ZipFile

import pytest

from awf.core.events import EventType, ExecutionEvent
from awf.supervisor.contracts import (
    EXECUTION_EVENT_TYPE_MAP,
    AgentEnvironment,
    AgentStatus,
    CommandType,
    JobState,
    RequestedTarget,
    SupervisorAgent,
    SupervisorCommand,
    SupervisorErrorCode,
    SupervisorEvent,
    SupervisorEventType,
    SupervisorJob,
    validate_contract,
)


NOW = "2026-07-30T12:00:00Z"
LATER = "2026-07-30T12:01:00Z"
JOB_ID = "job-1"
OTHER_JOB_ID = "job-2"
OTHER_GENERATION = 1
AUTHENTICATED_AGENT_ID = "local-agent-1"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
COMMAND_ID = "123e4567-e89b-42d3-a456-426614174000"

EXPECTED_JOB_STATES = {
    "QUEUED",
    "CLAIMED",
    "PREPARING",
    "RUNNING",
    "WAITING_APPROVAL",
    "PAUSED",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "BLOCKED",
    "STALE",
    "RECOVERY_REQUIRED",
}
EXPECTED_REQUESTED_TARGETS = {"auto", "local", "aws"}
EXPECTED_AGENT_ENVIRONMENTS = {"local", "aws"}
EXPECTED_AGENT_STATUSES = {"ONLINE", "DRAINING", "OFFLINE"}
EXPECTED_ERROR_CODES = {
    "TRANSIENT",
    "AUTH_REQUIRED",
    "POLICY_DENIED",
    "CONFLICT",
    "CORRUPT_ARTIFACT",
    "UNSAFE_RECOVERY",
    "TERMINAL_EXECUTION",
}
EXPECTED_EVENT_TYPES = {
    "TASK_STARTED",
    "TASK_COMPLETED",
    "TASK_FAILED",
    "ESCAPE_TRIGGERED",
    "ORCHESTRATOR_DECIDED",
    "STAGE_STARTED",
    "STAGE_COMPLETED",
    "PHASE_STARTED",
    "PHASE_COMPLETED",
    "WORKER_SPAWNED",
    "WORKER_PROGRESS",
    "WORKER_COMPLETED",
    "ARTIFACT_CREATED",
    "ARTIFACT_UPDATED",
    "PROVIDER_OUTPUT",
    "PROVIDER_TOOL_CALL",
    "GATE_EVALUATED",
    "HEARTBEAT",
    "PROGRESS_UPDATE",
    "MULTI_AGENT_STARTED",
    "AGENT_COMPLETED",
    "JUDGE_VERDICT",
    "TEAM_TURN_STARTED",
    "TEAM_TURN_COMPLETED",
}
EXPECTED_SUMMARIES = {
    "task_started",
    "task_completed",
    "task_failed",
    "escape_triggered",
    "orchestrator_decided",
    "stage_started",
    "stage_completed",
    "phase_started",
    "phase_completed",
    "worker_spawned",
    "worker_progress",
    "worker_completed",
    "artifact_created",
    "artifact_updated",
    "provider_output_suppressed",
    "provider_tool_call_suppressed",
    "gate_evaluated",
    "heartbeat",
    "progress_update",
    "multi_agent_started",
    "agent_completed",
    "judge_verdict",
    "team_turn_started",
    "team_turn_completed",
}

EXPECTED_EXECUTION_EVENT_TYPE_MAP = {
    EventType.TASK_STARTED: SupervisorEventType.TASK_STARTED,
    EventType.TASK_COMPLETED: SupervisorEventType.TASK_COMPLETED,
    EventType.TASK_FAILED: SupervisorEventType.TASK_FAILED,
    EventType.ESCAPE_TRIGGERED: SupervisorEventType.ESCAPE_TRIGGERED,
    EventType.ORCHESTRATOR_DECIDED: SupervisorEventType.ORCHESTRATOR_DECIDED,
    EventType.STAGE_STARTED: SupervisorEventType.STAGE_STARTED,
    EventType.STAGE_COMPLETED: SupervisorEventType.STAGE_COMPLETED,
    EventType.PHASE_STARTED: SupervisorEventType.PHASE_STARTED,
    EventType.PHASE_COMPLETED: SupervisorEventType.PHASE_COMPLETED,
    EventType.WORKER_SPAWNED: SupervisorEventType.WORKER_SPAWNED,
    EventType.WORKER_PROGRESS: SupervisorEventType.WORKER_PROGRESS,
    EventType.WORKER_COMPLETED: SupervisorEventType.WORKER_COMPLETED,
    EventType.ARTIFACT_CREATED: SupervisorEventType.ARTIFACT_CREATED,
    EventType.ARTIFACT_UPDATED: SupervisorEventType.ARTIFACT_UPDATED,
    EventType.PROVIDER_OUTPUT: SupervisorEventType.PROVIDER_OUTPUT,
    EventType.PROVIDER_TOOL_CALL: SupervisorEventType.PROVIDER_TOOL_CALL,
    EventType.GATE_EVALUATED: SupervisorEventType.GATE_EVALUATED,
    EventType.HEARTBEAT: SupervisorEventType.HEARTBEAT,
    EventType.PROGRESS_UPDATE: SupervisorEventType.PROGRESS_UPDATE,
    EventType.MULTI_AGENT_STARTED: SupervisorEventType.MULTI_AGENT_STARTED,
    EventType.AGENT_COMPLETED: SupervisorEventType.AGENT_COMPLETED,
    EventType.JUDGE_VERDICT: SupervisorEventType.JUDGE_VERDICT,
    EventType.TEAM_TURN_STARTED: SupervisorEventType.TEAM_TURN_STARTED,
    EventType.TEAM_TURN_COMPLETED: SupervisorEventType.TEAM_TURN_COMPLETED,
}


def artifact_uri(
    kind: str,
    digest: str = DIGEST_A,
    *,
    job_id: str = JOB_ID,
    generation: int = 0,
) -> str:
    return "s3://awf-supervisor/artifacts/{}/{}/{}/{}.json".format(
        kind, job_id, generation, digest
    )


def checkpoint_fixture() -> Dict[str, Any]:
    return {
        "kind": "awf-omp-native",
        "artifact_uri": artifact_uri("checkpoints"),
        "sha256": DIGEST_A,
    }


def job_fixture(**updates: Any) -> Dict[str, Any]:
    payload = {
        "schema_version": 1,
        "job_id": JOB_ID,
        "workflow_id": "2026-07-30-login-contract",
        "state": "QUEUED",
        "desired_state": "RUNNING",
        "approval_required": True,
        "requested_target": "auto",
        "owner_agent_id": None,
        "lease_expires_at": None,
        "generation": 0,
        "attempt": 0,
        "repo_refs": [{"repo": "blip-server", "base": "main"}],
        "required_capabilities": ["git", "omp"],
        "checkpoint": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    payload.update(updates)
    return payload


def agent_fixture(**updates: Any) -> Dict[str, Any]:
    payload = {
        "schema_version": 1,
        "agent_id": AUTHENTICATED_AGENT_ID,
        "environment": "local",
        "status": "ONLINE",
        "last_heartbeat_at": NOW,
        "max_concurrency": 2,
        "active_jobs": 1,
        "capabilities": ["git", "omp"],
        "repos": ["blip-server"],
        "version": {"awf": "1.0.0", "omp": "0.1.0"},
    }
    payload.update(updates)
    return payload


def event_fixture(**updates: Any) -> Dict[str, Any]:
    payload = {
        "schema_version": 1,
        "job_id": JOB_ID,
        "generation": 0,
        "sequence": 1,
        "type": "TASK_STARTED",
        "timestamp": NOW,
        "source": AUTHENTICATED_AGENT_ID,
        "data": {},
    }
    payload.update(updates)
    return payload


def command_fixture(**updates: Any) -> Dict[str, Any]:
    payload = {
        "schema_version": 1,
        "command_id": COMMAND_ID,
        "job_id": JOB_ID,
        "generation": 0,
        "type": "EXECUTE",
    }
    payload.update(updates)
    return payload


def terminal_success_event_fixture(**updates: Any) -> Dict[str, Any]:
    data = {
        "phase": "verify",
        "status_code": "COMPLETE",
        "return_code": 0,
        "summary": "task_completed",
        "terminal_status": "SUCCEEDED",
        "artifact_uri": artifact_uri("redacted-results"),
        "artifact_sha256": DIGEST_A,
        "provenance_uri": artifact_uri("provenance"),
        "provenance_sha256": DIGEST_A,
        "checkpoint_uri": artifact_uri("checkpoints"),
        "checkpoint_sha256": DIGEST_A,
    }
    payload = event_fixture(data=data)
    payload.update(updates)
    return payload


def terminal_failed_event_fixture(**updates: Any) -> Dict[str, Any]:
    payload = event_fixture(
        data={
            "terminal_status": "FAILED",
            "retryable": False,
            "error_code": "TERMINAL_EXECUTION",
            "stopped_at": NOW,
            "cleanup_completed": True,
        }
    )
    payload.update(updates)
    return payload


def terminal_cancelled_event_fixture(**updates: Any) -> Dict[str, Any]:
    payload = event_fixture(
        data={
            "terminal_status": "CANCELLED",
            "stopped_at": NOW,
            "cleanup_completed": True,
        }
    )
    payload.update(updates)
    return payload


def assert_rejected(kind: str, payload: Mapping[str, Any]) -> None:
    with pytest.raises(ValueError):
        validate_contract(kind, payload)


def test_job_contract_round_trip_preserves_fencing_fields() -> None:
    job = SupervisorJob.new(
        workflow_id="2026-07-30-login-contract",
        requested_target=RequestedTarget.AUTO,
        repo_refs=(("blip-server", "main"),),
        required_capabilities=("git", "omp"),
        now=NOW,
        job_id=JOB_ID,
    )

    payload = job.to_dict()

    validate_contract("job", payload)
    assert payload["schema_version"] == 1
    assert payload["state"] == JobState.QUEUED.value
    assert payload["generation"] == 0
    assert "prompt" not in payload
    assert "prompt_artifact_uri" not in payload
    assert "prompt_sha256" not in payload
    assert SupervisorJob.from_dict(payload).to_dict() == payload


@pytest.mark.parametrize(
    ("kind", "factory", "field"),
    [
        *[("job", job_fixture, field) for field in (
            "schema_version",
            "job_id",
            "workflow_id",
            "state",
            "desired_state",
            "approval_required",
            "requested_target",
            "generation",
            "attempt",
            "repo_refs",
            "required_capabilities",
            "created_at",
            "updated_at",
        )],
        *[("agent", agent_fixture, field) for field in (
            "schema_version",
            "agent_id",
            "environment",
            "status",
            "last_heartbeat_at",
            "max_concurrency",
            "active_jobs",
            "capabilities",
            "repos",
            "version",
        )],
        *[("event", event_fixture, field) for field in (
            "schema_version",
            "job_id",
            "generation",
            "sequence",
            "type",
            "timestamp",
            "source",
            "data",
        )],
        *[("command", command_fixture, field) for field in (
            "schema_version",
            "command_id",
            "job_id",
            "generation",
            "type",
        )],
    ],
)
def test_contract_requires_every_envelope_field(
    kind: str, factory: Callable[[], Dict[str, Any]], field: str
) -> None:
    payload = factory()
    payload.pop(field)

    assert_rejected(kind, payload)


@pytest.mark.parametrize(
    ("model", "payload", "expected_fields"),
    [
        (
            SupervisorJob,
            job_fixture(),
            {
                "schema_version",
                "job_id",
                "workflow_id",
                "state",
                "desired_state",
                "approval_required",
                "requested_target",
                "owner_agent_id",
                "lease_expires_at",
                "generation",
                "attempt",
                "repo_refs",
                "required_capabilities",
                "checkpoint",
                "created_at",
                "updated_at",
            },
        ),
        (
            SupervisorAgent,
            agent_fixture(),
            {
                "schema_version",
                "agent_id",
                "environment",
                "status",
                "last_heartbeat_at",
                "max_concurrency",
                "active_jobs",
                "capabilities",
                "repos",
                "version",
            },
        ),
        (
            SupervisorEvent,
            event_fixture(),
            {
                "schema_version",
                "job_id",
                "generation",
                "sequence",
                "type",
                "timestamp",
                "source",
                "data",
            },
        ),
        (
            SupervisorCommand,
            command_fixture(),
            {"schema_version", "command_id", "job_id", "generation", "type"},
        ),
    ],
)
def test_typed_contracts_are_frozen_and_round_trip_exactly(
    model: Type[Any], payload: Dict[str, Any], expected_fields: set[str]
) -> None:
    validate_contract(
        {
            SupervisorJob: "job",
            SupervisorAgent: "agent",
            SupervisorEvent: "event",
            SupervisorCommand: "command",
        }[model],
        payload,
    )
    assert dataclasses.is_dataclass(model)
    assert {field.name for field in dataclasses.fields(model)} == expected_fields

    restored = model.from_dict(payload)

    assert restored.to_dict() == payload
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        setattr(restored, next(iter(expected_fields)), "mutated")


def test_contract_rejects_unknown_major_schema_and_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unsupported job schema_version"):
        validate_contract("job", {"schema_version": 2, "job_id": JOB_ID})
    for version in (0, True, "1", None):
        assert_rejected("job", job_fixture(schema_version=version))
    with pytest.raises(ValueError, match="unknown supervisor contract"):
        validate_contract("unknown", {"schema_version": 1})


def test_public_envelopes_reject_unknown_top_level_fields() -> None:
    for kind, payload in (
        ("job", job_fixture()),
        ("agent", agent_fixture()),
        ("event", event_fixture()),
        ("command", command_fixture()),
    ):
        payload["unknown_field"] = "not allowed"
        assert_rejected(kind, payload)


@pytest.mark.parametrize(
    "field",
    ("prompt", "prompt_artifact_uri", "prompt_sha256", "dispatch_command_id"),
)
def test_public_job_envelope_rejects_prompt_and_internal_dispatch_fields(field: str) -> None:
    payload = job_fixture()
    payload[field] = "secret"

    assert_rejected("job", payload)


def test_job_contract_requires_a_lease_for_an_owner() -> None:
    payload = job_fixture(owner_agent_id=AUTHENTICATED_AGENT_ID, lease_expires_at=None)

    with pytest.raises(ValueError):
        validate_contract("job", payload)


def test_job_contract_forbids_a_lease_without_an_owner() -> None:
    payload = job_fixture(owner_agent_id=None, lease_expires_at=LATER)

    with pytest.raises(ValueError):
        validate_contract("job", payload)


def test_job_contract_allows_only_valid_owner_lease_shapes() -> None:
    validate_contract(
        "job",
        job_fixture(owner_agent_id=AUTHENTICATED_AGENT_ID, lease_expires_at=LATER),
    )
    validate_contract("job", job_fixture(owner_agent_id=None, lease_expires_at=None))

    ownerless_without_lease = job_fixture()
    ownerless_without_lease.pop("owner_agent_id")
    ownerless_without_lease.pop("lease_expires_at")
    validate_contract("job", ownerless_without_lease)

    lease_without_owner = job_fixture()
    lease_without_owner.pop("owner_agent_id")
    lease_without_owner["lease_expires_at"] = LATER
    assert_rejected("job", lease_without_owner)
    assert_rejected(
        "job",
        job_fixture(
            owner_agent_id=AUTHENTICATED_AGENT_ID,
            lease_expires_at="not-a-date-time",
        ),
    )


@pytest.mark.parametrize("invalid", ("contains whitespace", "caf\u00e9"))
def test_contract_rejects_whitespace_and_non_ascii_identifiers_everywhere(invalid: str) -> None:
    mutations = (
        ("job", job_fixture(), lambda payload: payload.__setitem__("job_id", invalid)),
        ("job", job_fixture(), lambda payload: payload.__setitem__("workflow_id", invalid)),
        (
            "job",
            job_fixture(owner_agent_id=AUTHENTICATED_AGENT_ID, lease_expires_at=LATER),
            lambda payload: payload.__setitem__("owner_agent_id", invalid),
        ),
        (
            "job",
            job_fixture(),
            lambda payload: payload["required_capabilities"].__setitem__(0, invalid),
        ),
        (
            "job",
            job_fixture(),
            lambda payload: payload["repo_refs"][0].__setitem__("repo", invalid),
        ),
        (
            "job",
            job_fixture(),
            lambda payload: payload["repo_refs"][0].__setitem__("base", invalid),
        ),
        ("agent", agent_fixture(), lambda payload: payload.__setitem__("agent_id", invalid)),
        (
            "agent",
            agent_fixture(),
            lambda payload: payload["capabilities"].__setitem__(0, invalid),
        ),
        (
            "agent",
            agent_fixture(),
            lambda payload: payload["repos"].__setitem__(0, invalid),
        ),
        ("event", event_fixture(), lambda payload: payload.__setitem__("job_id", invalid)),
        ("event", event_fixture(), lambda payload: payload.__setitem__("source", invalid)),
        (
            "event",
            event_fixture(data={"phase": "verify"}),
            lambda payload: payload["data"].__setitem__("phase", invalid),
        ),
        ("command", command_fixture(), lambda payload: payload.__setitem__("job_id", invalid)),
    )
    for kind, payload, mutate in mutations:
        mutate(payload)
        assert_rejected(kind, payload)


def test_job_rejects_duplicate_repo_names_and_malformed_checkpoint() -> None:
    duplicate_repos = job_fixture(
        repo_refs=[
            {"repo": "blip-server", "base": "main"},
            {"repo": "blip-server", "base": "release"},
        ]
    )
    assert_rejected("job", duplicate_repos)

    for checkpoint in (
        {"kind": "awf-omp-native", "artifact_uri": artifact_uri("checkpoints"), "sha256": "A" * 64},
        {"kind": "awf-omp-native", "artifact_uri": "s3://awf-supervisor/jobs/job-1/checkpoint.json", "sha256": DIGEST_A},
        {"kind": "wrong-kind", "artifact_uri": artifact_uri("checkpoints"), "sha256": DIGEST_A},
        {"kind": "awf-omp-native", "artifact_uri": artifact_uri("checkpoints"), "sha256": DIGEST_A, "extra": True},
    ):
        assert_rejected("job", job_fixture(checkpoint=checkpoint))


def test_job_rejects_checkpoint_uri_digest_disagreement_after_schema_validation() -> None:
    payload = job_fixture(
        checkpoint={
            "kind": "awf-omp-native",
            "artifact_uri": artifact_uri("checkpoints", DIGEST_B),
            "sha256": DIGEST_A,
        }
    )

    assert_rejected("job", payload)

@pytest.mark.parametrize(
    ("checkpoint_job_id", "checkpoint_generation"),
    ((OTHER_JOB_ID, 0),),
    ids=("path-job-id",),
)
def test_job_rejects_checkpoint_uri_with_path_identity_mismatches(
    checkpoint_job_id: str, checkpoint_generation: int
) -> None:
    payload = job_fixture(checkpoint=checkpoint_fixture())
    validate_contract("job", payload)

    payload["checkpoint"]["artifact_uri"] = artifact_uri(
        "checkpoints",
        DIGEST_B,
        job_id=checkpoint_job_id,
        generation=checkpoint_generation,
    )
    payload["checkpoint"]["sha256"] = DIGEST_B

    assert_rejected("job", payload)

def test_job_accepts_checkpoint_from_prior_generation() -> None:
    payload = job_fixture(
        generation=OTHER_GENERATION,
        checkpoint=checkpoint_fixture(),
    )

    validate_contract("job", payload)


@pytest.mark.parametrize("field", ("created_at", "updated_at"))
def test_job_rejects_malformed_timestamps(field: str) -> None:
    assert_rejected("job", job_fixture(**{field: "not-a-date-time"}))


def test_agent_contract_rejects_capacity_overcommit_and_duplicate_arrays() -> None:
    assert_rejected("agent", agent_fixture(max_concurrency=1, active_jobs=2))
    assert_rejected("agent", agent_fixture(max_concurrency=0))
    assert_rejected("agent", agent_fixture(active_jobs=-1))
    assert_rejected("agent", agent_fixture(capabilities=["git", "git"]))
    assert_rejected("agent", agent_fixture(repos=["blip-server", "blip-server"]))


@pytest.mark.parametrize("field", ("awf", "omp"))
def test_agent_contract_requires_closed_nonempty_ascii_version_fields(field: str) -> None:
    missing = agent_fixture()
    missing["version"].pop(field)
    assert_rejected("agent", missing)

    for value in ("", "caf\u00e9", "x" * 129):
        invalid = agent_fixture()
        invalid["version"][field] = value
        assert_rejected("agent", invalid)

    extra = agent_fixture()
    extra["version"]["python"] = "3.9"
    assert_rejected("agent", extra)


@pytest.mark.parametrize(
    ("kind", "payload", "field"),
    (
        ("job", job_fixture(), "state"),
        ("job", job_fixture(), "desired_state"),
        ("job", job_fixture(), "requested_target"),
        ("agent", agent_fixture(), "environment"),
        ("agent", agent_fixture(), "status"),
        ("event", event_fixture(), "type"),
        ("command", command_fixture(), "type"),
    ),
)
def test_contract_rejects_unknown_enum_values(
    kind: str, payload: Dict[str, Any], field: str
) -> None:
    payload[field] = "UNKNOWN_ENUM_VALUE"
    assert_rejected(kind, payload)


def test_public_enum_members_are_closed_and_complete() -> None:
    assert {member.value for member in JobState} == EXPECTED_JOB_STATES
    assert {member.value for member in RequestedTarget} == EXPECTED_REQUESTED_TARGETS
    assert {member.value for member in AgentEnvironment} == EXPECTED_AGENT_ENVIRONMENTS
    assert {member.value for member in AgentStatus} == EXPECTED_AGENT_STATUSES
    assert {member.value for member in SupervisorErrorCode} == EXPECTED_ERROR_CODES
    assert {member.value for member in SupervisorEventType} == EXPECTED_EVENT_TYPES
    assert {member.value for member in CommandType} == {"EXECUTE"}


@pytest.mark.parametrize("state", tuple(JobState))
def test_job_contract_accepts_every_declared_state(state: JobState) -> None:
    validate_contract("job", job_fixture(state=state.value))


@pytest.mark.parametrize("target", tuple(RequestedTarget))
def test_job_contract_accepts_every_declared_requested_target(target: RequestedTarget) -> None:
    validate_contract("job", job_fixture(requested_target=target.value))


@pytest.mark.parametrize("desired_state", ("RUNNING", "CANCELLED", "PAUSED"))
def test_job_contract_accepts_every_declared_desired_state(desired_state: str) -> None:
    validate_contract("job", job_fixture(desired_state=desired_state))


@pytest.mark.parametrize("environment", tuple(AgentEnvironment))
def test_agent_contract_accepts_every_declared_environment(environment: AgentEnvironment) -> None:
    validate_contract("agent", agent_fixture(environment=environment.value))


@pytest.mark.parametrize("status", tuple(AgentStatus))
def test_agent_contract_accepts_every_declared_status(status: AgentStatus) -> None:
    validate_contract("agent", agent_fixture(status=status.value))


@pytest.mark.parametrize("event_type", tuple(SupervisorEventType))
def test_event_contract_accepts_every_declared_event_type(event_type: SupervisorEventType) -> None:
    validate_contract("event", event_fixture(type=event_type.value))


def test_command_contract_accepts_its_only_declared_type_and_rejects_noncanonical_uuid() -> None:
    validate_contract("command", command_fixture(type=CommandType.EXECUTE.value))
    for invalid_command_id in (
        "123E4567-E89B-42D3-A456-426614174000",
        "123e4567-e89b-12d3-a456-426614174000",
        "123e4567-e89b-42d3-7456-426614174000",
        "not-a-uuid",
    ):
        assert_rejected("command", command_fixture(command_id=invalid_command_id))


@pytest.mark.parametrize("field", ("last_heartbeat_at",))
def test_agent_rejects_malformed_timestamps(field: str) -> None:
    assert_rejected("agent", agent_fixture(**{field: "not-a-date-time"}))


def test_event_rejects_malformed_timestamp_and_invalid_scalar_metadata() -> None:
    assert_rejected("event", event_fixture(timestamp="not-a-date-time"))
    assert_rejected("event", event_fixture(data={"status_code": "not upper"}))
    assert_rejected("event", event_fixture(data={"return_code": "0"}))
    assert_rejected("event", event_fixture(data={"stopped_at": "not-a-date-time"}))
    assert_rejected("event", event_fixture(data={"error_code": "UNKNOWN_ERROR"}))
    assert_rejected("event", event_fixture(data={"terminal_status": "UNKNOWN_TERMINAL"}))


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("unknown_metadata", "value"),
        ("run_id", "native-run-1"),
        ("task_id", "native-task-1"),
        ("data", {"raw": "native event data"}),
        ("text", "caller text"),
        ("result", "free-form result"),
        ("tool", {"name": "shell"}),
        ("path", "/private/tmp/worktree"),
        ("prompt", "do not persist prompts"),
        ("model_output", "assistant response"),
        ("source_code", "print('secret')"),
        ("code", "print('secret')"),
    ),
)
def test_event_metadata_rejects_unknown_and_sensitive_fields(key: str, value: Any) -> None:
    assert_rejected("event", event_fixture(data={key: value}))


@pytest.mark.parametrize("summary", tuple(sorted(EXPECTED_SUMMARIES)))
def test_event_contract_accepts_every_closed_summary(summary: str) -> None:
    validate_contract("event", event_fixture(data={"summary": summary}))


@pytest.mark.parametrize(
    "summary",
    (
        "arbitrary caller summary",
        "prompt: exfiltrate the repository",
        "model_output: here is the internal chain of thought",
    ),
)
def test_event_contract_rejects_free_form_and_sensitive_summary_text(summary: str) -> None:
    assert_rejected("event", event_fixture(data={"summary": summary}))


def test_event_success_requires_complete_artifact_and_provenance_proof() -> None:
    validate_contract("event", terminal_success_event_fixture())

    for required_field in (
        "return_code",
        "artifact_uri",
        "artifact_sha256",
        "provenance_uri",
        "provenance_sha256",
    ):
        payload = terminal_success_event_fixture()
        payload["data"].pop(required_field)
        assert_rejected("event", payload)

    invalid_return_code = terminal_success_event_fixture()
    invalid_return_code["data"]["return_code"] = 1
    assert_rejected("event", invalid_return_code)


def test_event_failed_requires_nonretryable_error_stop_and_flat_cleanup_proof() -> None:
    validate_contract("event", terminal_failed_event_fixture())

    for required_field in ("retryable", "error_code", "stopped_at", "cleanup_completed"):
        payload = terminal_failed_event_fixture()
        payload["data"].pop(required_field)
        assert_rejected("event", payload)

    for field, invalid_value in (("retryable", True), ("cleanup_completed", False)):
        payload = terminal_failed_event_fixture()
        payload["data"][field] = invalid_value
        assert_rejected("event", payload)

    nested_cleanup = terminal_failed_event_fixture()
    nested_cleanup["data"]["cleanup"] = {"completed": True}
    assert_rejected("event", nested_cleanup)


def test_event_cancelled_requires_stop_cleanup_and_forbids_error_code() -> None:
    validate_contract("event", terminal_cancelled_event_fixture())

    for required_field in ("stopped_at", "cleanup_completed"):
        payload = terminal_cancelled_event_fixture()
        payload["data"].pop(required_field)
        assert_rejected("event", payload)

    with_error = terminal_cancelled_event_fixture()
    with_error["data"]["error_code"] = "TERMINAL_EXECUTION"
    assert_rejected("event", with_error)


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("terminal_status", "SUCCEEDED"),
        ("retryable", False),
        ("stopped_at", NOW),
        ("cleanup_completed", True),
    ),
)
def test_non_terminal_event_forbids_terminal_metadata(key: str, value: Any) -> None:
    assert_rejected("event", event_fixture(data={key: value}))


@pytest.mark.parametrize(
    ("uri_key", "sha_key", "kind"),
    (
        ("artifact_uri", "artifact_sha256", "redacted-results"),
        ("provenance_uri", "provenance_sha256", "provenance"),
        ("checkpoint_uri", "checkpoint_sha256", "checkpoints"),
    ),
)
def test_event_rejects_uri_digest_disagreement_for_every_artifact_pair(
    uri_key: str, sha_key: str, kind: str
) -> None:
    payload = terminal_success_event_fixture()
    payload["data"][uri_key] = artifact_uri(kind, DIGEST_B)
    payload["data"][sha_key] = DIGEST_A

    assert_rejected("event", payload)

@pytest.mark.parametrize(
    ("uri_key", "sha_key", "kind"),
    (
        ("artifact_uri", "artifact_sha256", "redacted-results"),
        ("provenance_uri", "provenance_sha256", "provenance"),
        ("checkpoint_uri", "checkpoint_sha256", "checkpoints"),
    ),
)
@pytest.mark.parametrize(
    ("artifact_job_id", "artifact_generation"),
    (
        (OTHER_JOB_ID, 0),
        (JOB_ID, OTHER_GENERATION),
    ),
    ids=("path-job-id", "path-generation"),
)
def test_event_rejects_artifact_uri_with_path_identity_mismatches(
    uri_key: str,
    sha_key: str,
    kind: str,
    artifact_job_id: str,
    artifact_generation: int,
) -> None:
    payload = terminal_success_event_fixture()
    validate_contract("event", payload)

    payload["data"][uri_key] = artifact_uri(
        kind,
        DIGEST_B,
        job_id=artifact_job_id,
        generation=artifact_generation,
    )
    payload["data"][sha_key] = DIGEST_B

    assert_rejected("event", payload)


@pytest.mark.parametrize(
    ("uri_key", "sha_key", "wrong_kind"),
    (
        ("artifact_uri", "artifact_sha256", "checkpoints"),
        ("provenance_uri", "provenance_sha256", "redacted-results"),
        ("checkpoint_uri", "checkpoint_sha256", "provenance"),
    ),
)
def test_event_rejects_wrong_artifact_uri_family(
    uri_key: str, sha_key: str, wrong_kind: str
) -> None:
    payload = terminal_success_event_fixture()
    payload["data"][uri_key] = artifact_uri(wrong_kind)
    payload["data"][sha_key] = DIGEST_A

    assert_rejected("event", payload)




@pytest.mark.parametrize(
    "data",
    (
        {"checkpoint_uri": artifact_uri("checkpoints")},
        {"checkpoint_sha256": DIGEST_A},
    ),
)
def test_event_requires_both_members_of_an_optional_checkpoint_artifact_pair(
    data: Dict[str, Any]
) -> None:
    assert_rejected("event", event_fixture(data=data))

def test_execution_event_mapping_is_exhaustive_and_uses_only_supervisor_context() -> None:
    assert EXECUTION_EVENT_TYPE_MAP == EXPECTED_EXECUTION_EVENT_TYPE_MAP
    assert set(EXECUTION_EVENT_TYPE_MAP) == set(EventType)
    assert len(EXECUTION_EVENT_TYPE_MAP) == 24

    for native_type, supervisor_type in EXPECTED_EXECUTION_EVENT_TYPE_MAP.items():
        native_event = ExecutionEvent(
            type=native_type,
            timestamp=NOW,
            run_id="native-run-id",
            task_id="native-task-id",
            source="untrusted-native-source",
            sequence=999,
            data={
                "prompt": "must not be copied",
                "model_output": "must not be copied",
                "path": "/private/tmp/worktree",
            },
        )

        supervisor_event = SupervisorEvent.from_execution_event(
            native_event,
            job_id=JOB_ID,
            generation=7,
            sequence=23,
            authenticated_agent_id=AUTHENTICATED_AGENT_ID,
            data={"phase": "verification"},
        )
        payload = supervisor_event.to_dict()

        assert payload == {
            "schema_version": 1,
            "job_id": JOB_ID,
            "generation": 7,
            "sequence": 23,
            "type": supervisor_type.value,
            "timestamp": NOW,
            "source": AUTHENTICATED_AGENT_ID,
            "data": {
                "phase": "verification",
                "summary": (
                    "provider_output_suppressed"
                    if native_type is EventType.PROVIDER_OUTPUT
                    else "provider_tool_call_suppressed"
                    if native_type is EventType.PROVIDER_TOOL_CALL
                    else native_type.value
                ),
            },
        }
        validate_contract("event", payload)


def test_execution_event_converter_canonicalizes_authenticated_source_before_returning_shape() -> None:
    native_event = ExecutionEvent(
        type=EventType.TASK_STARTED,
        timestamp=NOW,
        run_id="run-not-for-storage",
        task_id="task-not-for-storage",
        source="different-agent-id",
        sequence=101,
        data={"result": "untrusted native data"},
    )

    converted = SupervisorEvent.from_execution_event(
        native_event,
        job_id=JOB_ID,
        generation=0,
        sequence=1,
        authenticated_agent_id=AUTHENTICATED_AGENT_ID,
        data={},
    )

    payload = converted.to_dict()
    assert payload["source"] == AUTHENTICATED_AGENT_ID
    assert payload["source"] != native_event.source
    assert payload["timestamp"] == native_event.timestamp
    assert "run_id" not in payload
    assert "task_id" not in payload
    assert payload["data"] == {"summary": "task_started"}


def test_execution_event_converter_rejects_invalid_metadata_before_returning_shape() -> None:
    native_event = ExecutionEvent(
        type=EventType.TASK_STARTED,
        timestamp=NOW,
        run_id="native-run-id",
        task_id="native-task-id",
        source="untrusted-native-source",
        sequence=1,
    )

    with pytest.raises(ValueError):
        SupervisorEvent.from_execution_event(
            native_event,
            job_id=JOB_ID,
            generation=0,
            sequence=1,
            authenticated_agent_id=AUTHENTICATED_AGENT_ID,
            data={"prompt": "not allowed"},
        )



def test_agent_event_request_round_trips_only_for_the_authenticated_source() -> None:
    stored_event = event_fixture()

    accepted = SupervisorEvent.from_agent_request(
        stored_event,
        authenticated_agent_id=AUTHENTICATED_AGENT_ID,
    )

    assert accepted.to_dict() == stored_event

    stored_event["source"] = "different-agent-id"

    with pytest.raises(ValueError):
        SupervisorEvent.from_agent_request(
            stored_event,
            authenticated_agent_id=AUTHENTICATED_AGENT_ID,
        )



def test_packaged_contract_manifest_contains_exactly_the_approved_json_resources(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    build_root = tmp_path / "project"
    wheel_dir = tmp_path / "wheel"
    shutil.copytree(
        project_root,
        build_root,
        ignore=shutil.ignore_patterns(
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "build",
            "dist",
            "*.egg-info",
        ),
    )

    subprocess.run(
        ("uv", "build", "--wheel", "--out-dir", str(wheel_dir)),
        cwd=build_root,
        check=True,
        capture_output=True,
        text=True,
    )

    wheels = sorted(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1

    with ZipFile(wheels[0]) as archive:
        packaged_resources = {
            name
            for name in archive.namelist()
            if name.endswith(".json")
            and name.startswith(
                ("awf/supervisor/schemas/", "awf/supervisor/fixtures/")
            )
        }

    assert packaged_resources == {
        "awf/supervisor/schemas/agent-v1.json",
        "awf/supervisor/schemas/command-v1.json",
        "awf/supervisor/schemas/event-v1.json",
        "awf/supervisor/schemas/job-v1.json",
        "awf/supervisor/fixtures/state-machine-v1.json",
    }
