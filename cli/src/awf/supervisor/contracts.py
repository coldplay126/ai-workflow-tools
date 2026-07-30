"""Version 1 public wire contracts for the AWF Supervisor."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from importlib import resources
from types import MappingProxyType
from typing import Any, Dict, Optional, Tuple

from jsonschema import Draft202012Validator, FormatChecker

from awf.core.events import EventType, ExecutionEvent


class JobState(str, Enum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    STALE = "STALE"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class RequestedTarget(str, Enum):
    AUTO = "auto"
    LOCAL = "local"
    AWS = "aws"


class AgentEnvironment(str, Enum):
    LOCAL = "local"
    AWS = "aws"


class AgentStatus(str, Enum):
    ONLINE = "ONLINE"
    DRAINING = "DRAINING"
    OFFLINE = "OFFLINE"


class SupervisorErrorCode(str, Enum):
    TRANSIENT = "TRANSIENT"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    POLICY_DENIED = "POLICY_DENIED"
    CONFLICT = "CONFLICT"
    CORRUPT_ARTIFACT = "CORRUPT_ARTIFACT"
    UNSAFE_RECOVERY = "UNSAFE_RECOVERY"
    TERMINAL_EXECUTION = "TERMINAL_EXECUTION"


class SupervisorEventType(str, Enum):
    TASK_STARTED = "TASK_STARTED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    ESCAPE_TRIGGERED = "ESCAPE_TRIGGERED"
    ORCHESTRATOR_DECIDED = "ORCHESTRATOR_DECIDED"
    STAGE_STARTED = "STAGE_STARTED"
    STAGE_COMPLETED = "STAGE_COMPLETED"
    PHASE_STARTED = "PHASE_STARTED"
    PHASE_COMPLETED = "PHASE_COMPLETED"
    WORKER_SPAWNED = "WORKER_SPAWNED"
    WORKER_PROGRESS = "WORKER_PROGRESS"
    WORKER_COMPLETED = "WORKER_COMPLETED"
    ARTIFACT_CREATED = "ARTIFACT_CREATED"
    ARTIFACT_UPDATED = "ARTIFACT_UPDATED"
    PROVIDER_OUTPUT = "PROVIDER_OUTPUT"
    PROVIDER_TOOL_CALL = "PROVIDER_TOOL_CALL"
    GATE_EVALUATED = "GATE_EVALUATED"
    HEARTBEAT = "HEARTBEAT"
    PROGRESS_UPDATE = "PROGRESS_UPDATE"
    MULTI_AGENT_STARTED = "MULTI_AGENT_STARTED"
    AGENT_COMPLETED = "AGENT_COMPLETED"
    JUDGE_VERDICT = "JUDGE_VERDICT"
    TEAM_TURN_STARTED = "TEAM_TURN_STARTED"
    TEAM_TURN_COMPLETED = "TEAM_TURN_COMPLETED"


class CommandType(str, Enum):
    EXECUTE = "EXECUTE"


EXECUTION_EVENT_TYPE_MAP: Mapping[EventType, SupervisorEventType] = {
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

_EVENT_SUMMARIES: Mapping[SupervisorEventType, str] = MappingProxyType(
    {
        SupervisorEventType.TASK_STARTED: "task_started",
        SupervisorEventType.TASK_COMPLETED: "task_completed",
        SupervisorEventType.TASK_FAILED: "task_failed",
        SupervisorEventType.ESCAPE_TRIGGERED: "escape_triggered",
        SupervisorEventType.ORCHESTRATOR_DECIDED: "orchestrator_decided",
        SupervisorEventType.STAGE_STARTED: "stage_started",
        SupervisorEventType.STAGE_COMPLETED: "stage_completed",
        SupervisorEventType.PHASE_STARTED: "phase_started",
        SupervisorEventType.PHASE_COMPLETED: "phase_completed",
        SupervisorEventType.WORKER_SPAWNED: "worker_spawned",
        SupervisorEventType.WORKER_PROGRESS: "worker_progress",
        SupervisorEventType.WORKER_COMPLETED: "worker_completed",
        SupervisorEventType.ARTIFACT_CREATED: "artifact_created",
        SupervisorEventType.ARTIFACT_UPDATED: "artifact_updated",
        SupervisorEventType.PROVIDER_OUTPUT: "provider_output_suppressed",
        SupervisorEventType.PROVIDER_TOOL_CALL: "provider_tool_call_suppressed",
        SupervisorEventType.GATE_EVALUATED: "gate_evaluated",
        SupervisorEventType.HEARTBEAT: "heartbeat",
        SupervisorEventType.PROGRESS_UPDATE: "progress_update",
        SupervisorEventType.MULTI_AGENT_STARTED: "multi_agent_started",
        SupervisorEventType.AGENT_COMPLETED: "agent_completed",
        SupervisorEventType.JUDGE_VERDICT: "judge_verdict",
        SupervisorEventType.TEAM_TURN_STARTED: "team_turn_started",
        SupervisorEventType.TEAM_TURN_COMPLETED: "team_turn_completed",
    }
)

_SCHEMA_FILES = {
    "job": "job-v1.json",
    "agent": "agent-v1.json",
    "event": "event-v1.json",
    "command": "command-v1.json",
}

_ARTIFACT_URI_PATTERN = re.compile(
    r"^s3://[A-Za-z0-9.-]+/artifacts/"
    r"(?P<family>redacted-results|provenance|checkpoints)/"
    r"(?P<job_id>[A-Za-z0-9][A-Za-z0-9._:-]{0,127})/"
    r"(?P<generation>[0-9]+)/"
    r"(?P<digest>[0-9a-f]{64})\.json$"
)


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


def _plain_int(value: Any) -> bool:
    return type(value) is int


def _assert_plain_integer(value: Any, field: str, kind: str) -> None:
    if not _plain_int(value):
        raise ValueError("invalid {} contract: {} must be an integer".format(kind, field))


def _artifact_identity_matches(
    uri: str,
    *,
    family: str,
    job_id: str,
    generation: Optional[int],
    digest: str,
) -> bool:
    if (
        type(uri) is not str
        or type(job_id) is not str
        or (generation is not None and not _plain_int(generation))
        or type(digest) is not str
    ):
        return False

    match = _ARTIFACT_URI_PATTERN.fullmatch(uri)
    if match is None or (
        match["family"],
        match["job_id"],
        match["digest"],
    ) != (family, job_id, digest):
        return False
    return generation is None or int(match["generation"]) == generation


def _validate_python_extras(kind: str, payload: Mapping[str, Any]) -> None:
    integer_fields = {
        "job": ("schema_version", "generation", "attempt"),
        "agent": ("schema_version", "max_concurrency", "active_jobs"),
        "event": ("schema_version", "generation", "sequence"),
        "command": ("schema_version", "generation"),
    }
    for field in integer_fields[kind]:
        _assert_plain_integer(payload[field], field, kind)

    if kind == "job":
        repo_names = [repo_ref["repo"] for repo_ref in payload["repo_refs"]]
        if len(repo_names) != len(set(repo_names)):
            raise ValueError("invalid job contract: repo_refs contains duplicate repo names")
        checkpoint = payload.get("checkpoint")
        if checkpoint is not None and not _artifact_identity_matches(
            checkpoint["artifact_uri"],
            family="checkpoints",
            job_id=payload["job_id"],
            generation=None,
            digest=checkpoint["sha256"],
        ):
            raise ValueError(
                "invalid job contract: checkpoint artifact_uri does not match "
                "job_id and sha256"
            )
        return

    if kind == "agent":
        if payload["active_jobs"] > payload["max_concurrency"]:
            raise ValueError("invalid agent contract: active_jobs exceeds max_concurrency")
        return

    if kind == "event":
        data = payload["data"]
        if "return_code" in data:
            _assert_plain_integer(data["return_code"], "data.return_code", kind)
        for uri_key, digest_key, family in (
            ("artifact_uri", "artifact_sha256", "redacted-results"),
            ("provenance_uri", "provenance_sha256", "provenance"),
            ("checkpoint_uri", "checkpoint_sha256", "checkpoints"),
        ):
            if uri_key in data and not _artifact_identity_matches(
                data[uri_key],
                family=family,
                job_id=payload["job_id"],
                generation=payload["generation"],
                digest=data[digest_key],
            ):
                raise ValueError(
                    "invalid event contract: {} does not match job_id, generation, "
                    "and {}".format(uri_key, digest_key)
                )


def validate_contract(kind: str, payload: Mapping[str, Any]) -> None:
    """Validate a version 1 public Supervisor envelope.

    Validation intentionally fails closed: only the four named public contract
    kinds and schema version 1 are accepted.
    """

    if kind not in _SCHEMA_FILES:
        raise ValueError("unknown supervisor contract: {}".format(kind))
    if not isinstance(payload, Mapping):
        raise ValueError("invalid {} contract: payload must be an object".format(kind))
    version = payload.get("schema_version")
    if type(version) is not int or version != 1:
        raise ValueError("unsupported {} schema_version: {!r}".format(kind, version))

    schema = json.loads(
        resources.files("awf.supervisor.schemas")
        .joinpath(_SCHEMA_FILES[kind])
        .read_text(encoding="utf-8")
    )
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload),
        key=lambda item: (tuple(str(part) for part in item.path), item.message),
    )
    if errors:
        raise ValueError("invalid {} contract: {}".format(kind, errors[0].message))
    _validate_python_extras(kind, payload)


@dataclass(frozen=True)
class SupervisorJob:
    schema_version: int
    job_id: str
    workflow_id: str
    state: JobState
    desired_state: str
    approval_required: bool
    requested_target: RequestedTarget
    owner_agent_id: Optional[str]
    lease_expires_at: Optional[str]
    generation: int
    attempt: int
    repo_refs: Tuple[Tuple[str, str], ...]
    required_capabilities: Tuple[str, ...]
    checkpoint: Optional[Mapping[str, str]]
    created_at: str
    updated_at: str

    @classmethod
    def new(
        cls,
        *,
        workflow_id: str,
        requested_target: RequestedTarget,
        repo_refs: Tuple[Tuple[str, str], ...],
        required_capabilities: Tuple[str, ...],
        now: str,
        job_id: str,
    ) -> "SupervisorJob":
        job = cls(
            schema_version=1,
            job_id=job_id,
            workflow_id=workflow_id,
            state=JobState.QUEUED,
            desired_state="RUNNING",
            approval_required=True,
            requested_target=requested_target,
            owner_agent_id=None,
            lease_expires_at=None,
            generation=0,
            attempt=0,
            repo_refs=tuple((repo, base) for repo, base in repo_refs),
            required_capabilities=tuple(required_capabilities),
            checkpoint=None,
            created_at=now,
            updated_at=now,
        )
        validate_contract("job", job.to_dict())
        return job

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "workflow_id": self.workflow_id,
            "state": self.state.value,
            "desired_state": self.desired_state,
            "approval_required": self.approval_required,
            "requested_target": self.requested_target.value,
            "owner_agent_id": self.owner_agent_id,
            "lease_expires_at": self.lease_expires_at,
            "generation": self.generation,
            "attempt": self.attempt,
            "repo_refs": [
                {"repo": repo, "base": base} for repo, base in self.repo_refs
            ],
            "required_capabilities": list(self.required_capabilities),
            "checkpoint": None if self.checkpoint is None else dict(self.checkpoint),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SupervisorJob":
        validate_contract("job", payload)
        checkpoint = payload.get("checkpoint")
        return cls(
            schema_version=payload["schema_version"],
            job_id=payload["job_id"],
            workflow_id=payload["workflow_id"],
            state=JobState(payload["state"]),
            desired_state=payload["desired_state"],
            approval_required=payload["approval_required"],
            requested_target=RequestedTarget(payload["requested_target"]),
            owner_agent_id=payload.get("owner_agent_id"),
            lease_expires_at=payload.get("lease_expires_at"),
            generation=payload["generation"],
            attempt=payload["attempt"],
            repo_refs=tuple((item["repo"], item["base"]) for item in payload["repo_refs"]),
            required_capabilities=tuple(payload["required_capabilities"]),
            checkpoint=None if checkpoint is None else _freeze_mapping(checkpoint),
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
        )


@dataclass(frozen=True)
class SupervisorAgent:
    schema_version: int
    agent_id: str
    environment: AgentEnvironment
    status: AgentStatus
    last_heartbeat_at: str
    max_concurrency: int
    active_jobs: int
    capabilities: Tuple[str, ...]
    repos: Tuple[str, ...]
    version: Mapping[str, str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "agent_id": self.agent_id,
            "environment": self.environment.value,
            "status": self.status.value,
            "last_heartbeat_at": self.last_heartbeat_at,
            "max_concurrency": self.max_concurrency,
            "active_jobs": self.active_jobs,
            "capabilities": list(self.capabilities),
            "repos": list(self.repos),
            "version": dict(self.version),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SupervisorAgent":
        validate_contract("agent", payload)
        return cls(
            schema_version=payload["schema_version"],
            agent_id=payload["agent_id"],
            environment=AgentEnvironment(payload["environment"]),
            status=AgentStatus(payload["status"]),
            last_heartbeat_at=payload["last_heartbeat_at"],
            max_concurrency=payload["max_concurrency"],
            active_jobs=payload["active_jobs"],
            capabilities=tuple(payload["capabilities"]),
            repos=tuple(payload["repos"]),
            version=_freeze_mapping(payload["version"]),
        )


@dataclass(frozen=True)
class SupervisorEvent:
    schema_version: int
    job_id: str
    generation: int
    sequence: int
    type: SupervisorEventType
    timestamp: str
    source: str
    data: Mapping[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "generation": self.generation,
            "sequence": self.sequence,
            "type": self.type.value,
            "timestamp": self.timestamp,
            "source": self.source,
            "data": dict(self.data),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SupervisorEvent":
        validate_contract("event", payload)
        return cls(
            schema_version=payload["schema_version"],
            job_id=payload["job_id"],
            generation=payload["generation"],
            sequence=payload["sequence"],
            type=SupervisorEventType(payload["type"]),
            timestamp=payload["timestamp"],
            source=payload["source"],
            data=_freeze_mapping(payload["data"]),
        )

    @classmethod
    def from_agent_request(
        cls,
        payload: Mapping[str, Any],
        *,
        authenticated_agent_id: str,
    ) -> "SupervisorEvent":
        validate_contract("event", payload)
        if payload["source"] != authenticated_agent_id:
            raise ValueError("event source does not match authenticated agent")
        return cls.from_dict(payload)

    @classmethod
    def from_execution_event(
        cls,
        event: ExecutionEvent,
        *,
        authenticated_agent_id: str,
        job_id: str,
        generation: int,
        sequence: int,
        data: Mapping[str, Any],
    ) -> "SupervisorEvent":
        """Create a redacted Supervisor event from a native execution event."""

        if not isinstance(data, Mapping):
            raise ValueError("invalid event metadata: data must be an object")
        if "summary" in data:
            raise ValueError("invalid event metadata: summary is selected by the converter")
        try:
            supervisor_type = EXECUTION_EVENT_TYPE_MAP[event.type]
        except KeyError as error:
            raise ValueError("unsupported native execution event type") from error

        event_data = dict(data)
        event_data["summary"] = _EVENT_SUMMARIES[supervisor_type]
        payload = {
            "schema_version": 1,
            "job_id": job_id,
            "generation": generation,
            "sequence": sequence,
            "type": supervisor_type.value,
            "timestamp": event.timestamp,
            "source": authenticated_agent_id,
            "data": event_data,
        }
        validate_contract("event", payload)
        return cls.from_dict(payload)


@dataclass(frozen=True)
class SupervisorCommand:
    schema_version: int
    command_id: str
    job_id: str
    generation: int
    type: CommandType

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "command_id": self.command_id,
            "job_id": self.job_id,
            "generation": self.generation,
            "type": self.type.value,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SupervisorCommand":
        validate_contract("command", payload)
        return cls(
            schema_version=payload["schema_version"],
            command_id=payload["command_id"],
            job_id=payload["job_id"],
            generation=payload["generation"],
            type=CommandType(payload["type"]),
        )


__all__ = [
    "AgentEnvironment",
    "AgentStatus",
    "CommandType",
    "EXECUTION_EVENT_TYPE_MAP",
    "JobState",
    "RequestedTarget",
    "SupervisorAgent",
    "SupervisorCommand",
    "SupervisorErrorCode",
    "SupervisorEvent",
    "SupervisorEventType",
    "SupervisorJob",
    "validate_contract",
]
