from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class Purpose(str, Enum):
    FEATURE = "feature"
    PROMOTE = "promote"
    SCRATCH = "scratch"


class LeaseState(str, Enum):
    ACTIVE = "ACTIVE"
    PR_OPEN = "PR_OPEN"
    MERGED = "MERGED"
    DEPLOYING = "DEPLOYING"
    DEPLOYED = "DEPLOYED"
    CLEANABLE = "CLEANABLE"
    REMOVED = "REMOVED"
    DIRTY = "DIRTY"
    CLOSED_UNMERGED = "CLOSED_UNMERGED"
    ORPHANED = "ORPHANED"
    BLOCKED = "BLOCKED"


class DeploymentState(str, Enum):
    UNKNOWN = "unknown"
    PENDING = "pending"
    HEALTHY = "healthy"
    FAILED = "failed"
    NOT_REQUIRED = "not_required"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Lease:
    id: str
    repository_id: str
    repository_name: str
    repository_root: Path
    worktree_path: Path
    initiative: str
    purpose: Purpose
    branch: str
    base_ref: str
    head_sha: str
    managed: bool
    owner_kind: str
    owner_id: str | None
    state: LeaseState
    source_pr: int | None
    target_pr: int | None
    deployment_state: DeploymentState
    retain: bool
    created_at: str
    last_used_at: str
    updated_at: str
    removed_at: str | None
    version: int

    @classmethod
    def new(
        cls,
        *,
        repository_id: str,
        repository_name: str,
        repository_root: Path,
        worktree_path: Path,
        initiative: str,
        purpose: Purpose,
        branch: str,
        base_ref: str,
        head_sha: str,
        managed: bool,
        owner_kind: str,
        owner_id: str | None = None,
        source_pr: int | None = None,
    ) -> Lease:
        timestamp = now_iso()
        deployment = (
            DeploymentState.UNKNOWN
            if purpose is Purpose.PROMOTE
            else DeploymentState.NOT_REQUIRED
        )
        return cls(
            id=str(uuid.uuid4()),
            repository_id=repository_id,
            repository_name=repository_name,
            repository_root=repository_root.resolve(),
            worktree_path=worktree_path.resolve(),
            initiative=initiative,
            purpose=purpose,
            branch=branch,
            base_ref=base_ref,
            head_sha=head_sha,
            managed=managed,
            owner_kind=owner_kind,
            owner_id=owner_id,
            state=LeaseState.ACTIVE,
            source_pr=source_pr,
            target_pr=None,
            deployment_state=deployment,
            retain=False,
            created_at=timestamp,
            last_used_at=timestamp,
            updated_at=timestamp,
            removed_at=None,
            version=0,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["repository_root"] = str(self.repository_root)
        payload["worktree_path"] = str(self.worktree_path)
        payload["purpose"] = self.purpose.value
        payload["state"] = self.state.value
        payload["deployment_state"] = self.deployment_state.value
        return payload


@dataclass(frozen=True)
class WorktreeEvent:
    id: int
    lease_id: str
    event_type: str
    from_state: LeaseState | None
    to_state: LeaseState | None
    observed_head_sha: str | None
    pr_number: int | None
    summary: str
    created_at: str


@dataclass(frozen=True)
class CleanupReservation:
    lease_id: str
    reserved_version: int
    branch_sha: str
    created_at: str


@dataclass(frozen=True)
class CommandResult:
    command: str
    status: str
    decision: str
    lease: Lease | None = None
    leases: tuple[Lease, ...] = ()
    actions: tuple[dict[str, Any], ...] = ()
    blockers: tuple[dict[str, str], ...] = ()
    warnings: tuple[dict[str, str], ...] = ()
    exit_code: int = 0
    observed_at: str = field(default_factory=now_iso)

    @classmethod
    def ok(cls, command: str, *, decision: str, **values: Any) -> CommandResult:
        return cls(command=command, status="ok", decision=decision, **values)

    @classmethod
    def blocked(
        cls, command: str, *, blockers: tuple[dict[str, str], ...], **values: Any
    ) -> CommandResult:
        return cls(
            command=command,
            status="blocked",
            decision="blocked",
            blockers=blockers,
            exit_code=3,
            **values,
        )

    @classmethod
    def error(
        cls, command: str, *, code: str, message: str, exit_code: int
    ) -> CommandResult:
        return cls(
            command=command,
            status="error",
            decision="blocked",
            blockers=({"code": code, "message": message},),
            exit_code=exit_code,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "command": self.command,
            "status": self.status,
            "decision": self.decision,
            "lease": self.lease.to_dict() if self.lease else None,
            "leases": [item.to_dict() for item in self.leases],
            "actions": list(self.actions),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "observed_at": self.observed_at,
        }
