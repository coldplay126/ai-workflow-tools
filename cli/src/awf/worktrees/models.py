from __future__ import annotations

import hashlib
import json
import re
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


class PromotionMode(str, Enum):
    EXACT = "exact"
    OUT_OF_ORDER = "out_of_order"


class ResolutionState(str, Enum):
    NONE = "none"
    PENDING = "pending"
    AUTOMATIC = "automatic"
    MANUAL_REVIEWED = "manual_reviewed"


class ReleaseState(str, Enum):
    OPEN = "OPEN"
    SEALED = "SEALED"
    PUBLISHED = "PUBLISHED"
    MERGED = "MERGED"
    CLOSED_UNMERGED = "CLOSED_UNMERGED"
    CLEANED = "CLEANED"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_GIT_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_SHA256 = re.compile(r"[0-9a-f]{64}")


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
    promotion_mode: PromotionMode = PromotionMode.EXACT
    resolution_state: ResolutionState = ResolutionState.NONE
    source_base_sha: str | None = None
    source_head_sha: str | None = None
    target_base_sha: str | None = None
    reviewed_paths: tuple[str, ...] = ()
    conflicted_paths: tuple[str, ...] = ()
    conflict_source_ordinal: int | None = None
    legacy_source_trailers: bool = False
    protected_index_entries: tuple[tuple[str, tuple[str, str] | None], ...] = ()

    def __post_init__(self) -> None:
        self._validate_path_metadata("reviewed_paths", self.reviewed_paths)
        self._validate_path_metadata("conflicted_paths", self.conflicted_paths)
        if self.conflict_source_ordinal is not None and (
            not isinstance(self.conflict_source_ordinal, int)
            or isinstance(self.conflict_source_ordinal, bool)
            or self.conflict_source_ordinal < 0
        ):
            raise ValueError("conflict_source_ordinal must be a non-negative integer")
        if not isinstance(self.legacy_source_trailers, bool):
            raise ValueError("legacy_source_trailers must be a boolean")
        self._validate_protected_index_entries(self.protected_index_entries)

    @staticmethod
    def _validate_path_metadata(name: str, paths: tuple[str, ...]) -> None:
        if not isinstance(paths, tuple) or any(
            not isinstance(path, str) for path in paths
        ):
            raise ValueError(f"{name} must be a tuple of strings")
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError(f"{name} must be sorted and unique")


    @classmethod
    def _validate_protected_index_entries(
        cls,
        protected_index_entries: tuple[tuple[str, tuple[str, str] | None], ...],
    ) -> None:
        if not isinstance(protected_index_entries, tuple) or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or (
                item[1] is not None
                and (
                    not isinstance(item[1], tuple)
                    or len(item[1]) != 2
                    or item[1][0] not in ("100644", "100755", "120000", "160000")
                    or not isinstance(item[1][1], str)
                    or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", item[1][1])
                    is None
                )
            )
            for item in protected_index_entries
        ):
            raise ValueError(
                "protected_index_entries must map paths to stage-zero entries or null"
            )
        cls._validate_path_metadata(
            "protected_index_entries",
            tuple(path for path, _entry in protected_index_entries),
        )
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
        promotion_mode: PromotionMode = PromotionMode.EXACT,
        resolution_state: ResolutionState = ResolutionState.NONE,
        source_base_sha: str | None = None,
        source_head_sha: str | None = None,
        target_base_sha: str | None = None,
        reviewed_paths: tuple[str, ...] = (),
        conflicted_paths: tuple[str, ...] = (),
        conflict_source_ordinal: int | None = None,
        legacy_source_trailers: bool = False,
        protected_index_entries: tuple[
            tuple[str, tuple[str, str] | None], ...
        ] = (),
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
            promotion_mode=promotion_mode,
            resolution_state=resolution_state,
            source_base_sha=source_base_sha,
            source_head_sha=source_head_sha,
            target_base_sha=target_base_sha,
            reviewed_paths=reviewed_paths,
            conflicted_paths=conflicted_paths,
            conflict_source_ordinal=conflict_source_ordinal,
            legacy_source_trailers=legacy_source_trailers,
            protected_index_entries=protected_index_entries,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["repository_root"] = str(self.repository_root)
        payload["worktree_path"] = str(self.worktree_path)
        payload["purpose"] = self.purpose.value
        payload["state"] = self.state.value
        payload["deployment_state"] = self.deployment_state.value
        payload["promotion_mode"] = self.promotion_mode.value
        payload["resolution_state"] = self.resolution_state.value
        payload["reviewed_paths"] = list(self.reviewed_paths)
        payload["conflicted_paths"] = list(self.conflicted_paths)
        payload["protected_index_entries"] = [
            {
                "path": path,
                "mode": entry[0] if entry is not None else None,
                "blob_oid": entry[1] if entry is not None else None,
            }
            for path, entry in self.protected_index_entries
        ]
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
class PromotionSource:
    """An ordered immutable source pin owned by an out-of-order promotion lease."""

    lease_id: str
    ordinal: int
    source_pr: int
    base_ref: str
    base_sha: str
    head_sha: str
    merge_sha: str
    changed_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.lease_id:
            raise ValueError("promotion source lease id must be non-empty")
        if self.ordinal < 0:
            raise ValueError("promotion source ordinal must be non-negative")
        if self.source_pr <= 0:
            raise ValueError("promotion source pull request must be positive")
        if not self.base_ref:
            raise ValueError("promotion source base ref must be non-empty")
        for name, value in (
            ("base_sha", self.base_sha),
            ("head_sha", self.head_sha),
            ("merge_sha", self.merge_sha),
        ):
            if _GIT_OBJECT_ID.fullmatch(value) is None:
                raise ValueError(f"promotion source {name} must be a Git object id")
        Lease._validate_path_metadata("promotion source changed_paths", self.changed_paths)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "source_pr": self.source_pr,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "merge_sha": self.merge_sha,
            "changed_paths": list(self.changed_paths),
        }


@dataclass(frozen=True)
class ReleaseSource:
    bridge_id: str
    ordinal: int
    source_pr: int
    base_ref: str
    base_sha: str
    head_sha: str
    merge_sha: str
    changed_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise ValueError("release source ordinal must be non-negative")
        if self.source_pr <= 0:
            raise ValueError("release source pull request must be positive")
        if not self.base_ref:
            raise ValueError("release source base ref must be non-empty")
        for name, value in (
            ("base_sha", self.base_sha),
            ("head_sha", self.head_sha),
            ("merge_sha", self.merge_sha),
        ):
            if _GIT_OBJECT_ID.fullmatch(value) is None:
                raise ValueError(f"release source {name} must be a Git object id")
        Lease._validate_path_metadata("release source changed_paths", self.changed_paths)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "source_pr": self.source_pr,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "merge_sha": self.merge_sha,
            "changed_paths": list(self.changed_paths),
        }


def release_source_digest(sources: tuple[ReleaseSource, ...]) -> str:
    """Return a stable fingerprint of the ordered immutable source pins."""
    payload = [
        {
            "ordinal": source.ordinal,
            "source_pr": source.source_pr,
            "base_ref": source.base_ref,
            "base_sha": source.base_sha,
            "head_sha": source.head_sha,
            "merge_sha": source.merge_sha,
            "changed_paths": list(source.changed_paths),
        }
        for source in sources
    ]
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class ReleaseBridge:
    id: str
    repository_id: str
    repository_name: str
    repository_root: Path
    release_id: str
    target_branch: str
    lease_id: str
    state: ReleaseState
    source_digest: str
    last_verified_target_sha: str | None
    published_head_sha: str | None
    target_pr: int | None
    created_at: str
    updated_at: str
    version: int
    sources: tuple[ReleaseSource, ...] = ()

    def __post_init__(self) -> None:
        if not self.release_id:
            raise ValueError("release id must be non-empty")
        if not self.target_branch:
            raise ValueError("release target branch must be non-empty")
        if not self.lease_id:
            raise ValueError("release lease id must be non-empty")
        if _SHA256.fullmatch(self.source_digest) is None:
            raise ValueError("release source digest must be a SHA-256 digest")
        if self.sources != tuple(sorted(self.sources, key=lambda source: source.ordinal)):
            raise ValueError("release sources must be ordered by ordinal")
        if tuple(source.ordinal for source in self.sources) != tuple(
            range(len(self.sources))
        ):
            raise ValueError("release source ordinals must be contiguous")
        if len({source.source_pr for source in self.sources}) != len(self.sources):
            raise ValueError("release source pull requests must be unique")
        if any(source.bridge_id != self.id for source in self.sources):
            raise ValueError("release source bridge id must match its bridge")
        if self.source_digest != release_source_digest(self.sources):
            raise ValueError("release source digest does not match immutable source pins")

    @classmethod
    def new(
        cls,
        *,
        repository_id: str,
        repository_name: str,
        repository_root: Path,
        release_id: str,
        target_branch: str,
        lease_id: str,
    ) -> ReleaseBridge:
        timestamp = now_iso()
        return cls(
            id=str(uuid.uuid4()),
            repository_id=repository_id,
            repository_name=repository_name,
            repository_root=repository_root.resolve(),
            release_id=release_id,
            target_branch=target_branch,
            lease_id=lease_id,
            state=ReleaseState.OPEN,
            source_digest=release_source_digest(()),
            last_verified_target_sha=None,
            published_head_sha=None,
            target_pr=None,
            created_at=timestamp,
            updated_at=timestamp,
            version=0,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repository_id": self.repository_id,
            "repository_name": self.repository_name,
            "repository_root": str(self.repository_root),
            "release_id": self.release_id,
            "target_branch": self.target_branch,
            "lease_id": self.lease_id,
            "state": self.state.value,
            "source_digest": self.source_digest,
            "last_verified_target_sha": self.last_verified_target_sha,
            "published_head_sha": self.published_head_sha,
            "target_pr": self.target_pr,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
            "sources": [source.to_dict() for source in self.sources],
        }


@dataclass(frozen=True)
class CommandResult:
    command: str
    status: str
    decision: str
    lease: Lease | None = None
    release: ReleaseBridge | None = None
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
        cls,
        command: str,
        *,
        code: str,
        message: str,
        exit_code: int,
        **values: Any,
    ) -> CommandResult:
        return cls(
            command=command,
            status="error",
            decision="blocked",
            blockers=({"code": code, "message": message},),
            exit_code=exit_code,
            **values,
        )

    @classmethod
    def external_error(
        cls, command: str, *, code: str, message: str, **values: Any
    ) -> CommandResult:
        return cls.error(
            command,
            code=code,
            message=message,
            exit_code=4,
            **values,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "command": self.command,
            "status": self.status,
            "decision": self.decision,
            "lease": self.lease.to_dict() if self.lease else None,
            "release": self.release.to_dict() if self.release else None,
            "leases": [item.to_dict() for item in self.leases],
            "actions": list(self.actions),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "exit_code": self.exit_code,
            "observed_at": self.observed_at,
        }
