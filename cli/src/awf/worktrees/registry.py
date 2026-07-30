from __future__ import annotations

import sqlite3
from enum import Enum
from pathlib import Path
from typing import Any

from .models import (
    DeploymentState,
    Lease,
    LeaseState,
    Purpose,
    WorktreeEvent,
    now_iso,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS worktree_leases (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    repository_name TEXT NOT NULL,
    repository_root TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    initiative TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('feature','promote','scratch')),
    branch TEXT NOT NULL,
    base_ref TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    managed INTEGER NOT NULL CHECK (managed IN (0,1)),
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('awf','imported','user')),
    owner_id TEXT,
    state TEXT NOT NULL CHECK (state IN (
        'ACTIVE','PR_OPEN','MERGED','DEPLOYING','DEPLOYED','CLEANABLE',
        'REMOVED','DIRTY','CLOSED_UNMERGED','ORPHANED','BLOCKED'
    )),
    source_pr INTEGER,
    target_pr INTEGER,
    deployment_state TEXT NOT NULL CHECK (deployment_state IN (
        'unknown','pending','healthy','failed','not_required'
    )),
    retain INTEGER NOT NULL CHECK (retain IN (0,1)),
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    removed_at TEXT,
    version INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_worktree_active_identity
ON worktree_leases(repository_id, initiative, purpose)
WHERE state <> 'REMOVED';
CREATE UNIQUE INDEX IF NOT EXISTS uq_worktree_active_path
ON worktree_leases(worktree_path)
WHERE state <> 'REMOVED';
CREATE TABLE IF NOT EXISTS worktree_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lease_id TEXT NOT NULL REFERENCES worktree_leases(id),
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    observed_head_sha TEXT,
    pr_number INTEGER,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_worktree_events_lease_id_id
ON worktree_events(lease_id, id);
"""

_LEASE_FILTER_COLUMNS = {
    "id": "id",
    "repository_id": "repository_id",
    "repository_name": "repository_name",
    "repository_root": "repository_root",
    "worktree_path": "worktree_path",
    "initiative": "initiative",
    "purpose": "purpose",
    "branch": "branch",
    "base_ref": "base_ref",
    "head_sha": "head_sha",
    "managed": "managed",
    "owner_kind": "owner_kind",
    "owner_id": "owner_id",
    "state": "state",
    "source_pr": "source_pr",
    "target_pr": "target_pr",
    "deployment_state": "deployment_state",
    "retain": "retain",
}


class WorktreeRegistry:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def ensure(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def create_lease(self, lease: Lease) -> Lease:
        self.ensure()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO worktree_leases (
                        id, repository_id, repository_name, repository_root,
                        worktree_path, initiative, purpose, branch, base_ref,
                        head_sha, managed, owner_kind, owner_id, state, source_pr,
                        target_pr, deployment_state, retain, created_at, last_used_at,
                        updated_at, removed_at, version
                    ) VALUES (
                        :id, :repository_id, :repository_name, :repository_root,
                        :worktree_path, :initiative, :purpose, :branch, :base_ref,
                        :head_sha, :managed, :owner_kind, :owner_id, :state, :source_pr,
                        :target_pr, :deployment_state, :retain, :created_at, :last_used_at,
                        :updated_at, :removed_at, :version
                    )
                    """,
                    self._lease_parameters(lease),
                )
        except sqlite3.IntegrityError as error:
            if self._is_active_identity_conflict(error) or self.find_active(
                lease.repository_id, lease.initiative, lease.purpose
            ):
                raise ValueError("active lease already exists") from error
            raise
        return lease

    def get_lease(self, lease_id: str) -> Lease | None:
        self.ensure()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM worktree_leases WHERE id = ?", (lease_id,)
            ).fetchone()
        return self._lease_from_row(row) if row is not None else None

    def find_active(
        self, repository_id: str, initiative: str, purpose: Purpose
    ) -> Lease | None:
        self.ensure()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM worktree_leases
                WHERE repository_id = ? AND initiative = ? AND purpose = ?
                AND state <> ?
                """,
                (repository_id, initiative, purpose.value, LeaseState.REMOVED.value),
            ).fetchone()
        return self._lease_from_row(row) if row is not None else None

    def list_leases(
        self, *, include_removed: bool = True, **filters: Any
    ) -> list[Lease]:
        self.ensure()
        predicates: list[str] = []
        values: list[Any] = []
        for name, value in filters.items():
            column = _LEASE_FILTER_COLUMNS.get(name)
            if column is None:
                raise ValueError(f"unsupported lease filter: {name}")
            if value is None:
                predicates.append(f"{column} IS NULL")
            else:
                predicates.append(f"{column} = ?")
                values.append(self._database_value(value))
        if not include_removed:
            predicates.append("state <> ?")
            values.append(LeaseState.REMOVED.value)

        statement = "SELECT * FROM worktree_leases"
        if predicates:
            statement += " WHERE " + " AND ".join(predicates)
        statement += " ORDER BY created_at, id"

        with self._connect() as connection:
            rows = connection.execute(statement, values).fetchall()
        return [self._lease_from_row(row) for row in rows]

    def transition(
        self,
        lease_id: str,
        state: LeaseState,
        *,
        expected_version: int,
        event_type: str = "transition",
        summary: str = "",
        observed_head_sha: str | None = None,
        pr_number: int | None = None,
        head_sha: str | None = None,
        deployment_state: DeploymentState | None = None,
        retain: bool | None = None,
        managed: bool | None = None,
    ) -> Lease:
        self.ensure()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(
                "SELECT * FROM worktree_leases WHERE id = ?", (lease_id,)
            ).fetchone()
            if current_row is None or current_row["version"] != expected_version:
                raise RuntimeError("lease changed concurrently")
            current = self._lease_from_row(current_row)
            timestamp = now_iso()
            update_values: dict[str, Any] = {
                "state": state.value,
                "updated_at": timestamp,
                "removed_at": timestamp if state is LeaseState.REMOVED else None,
                "version": expected_version + 1,
            }
            if pr_number is not None:
                update_values["target_pr"] = pr_number
            if head_sha is not None:
                update_values["head_sha"] = head_sha
            if deployment_state is not None:
                update_values["deployment_state"] = deployment_state.value
            if retain is not None:
                update_values["retain"] = int(retain)
            if managed is not None:
                update_values["managed"] = int(managed)

            assignments = ", ".join(f"{column} = ?" for column in update_values)
            cursor = connection.execute(
                f"UPDATE worktree_leases SET {assignments} "
                "WHERE id = ? AND version = ?",
                (*update_values.values(), lease_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("lease changed concurrently")
            connection.execute(
                """
                INSERT INTO worktree_events (
                    lease_id, event_type, from_state, to_state, observed_head_sha,
                    pr_number, summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    event_type,
                    current.state.value,
                    state.value,
                    observed_head_sha,
                    pr_number,
                    self._bound_summary(summary),
                    timestamp,
                ),
            )
            updated_row = connection.execute(
                "SELECT * FROM worktree_leases WHERE id = ?", (lease_id,)
            ).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if updated_row is None:
            raise RuntimeError("lease changed concurrently")
        return self._lease_from_row(updated_row)

    def touch(self, lease_id: str, *, expected_version: int, head_sha: str) -> Lease:
        self.ensure()
        connection = self._connect()
        try:
            timestamp = now_iso()
            cursor = connection.execute(
                """
                UPDATE worktree_leases
                SET last_used_at = ?, updated_at = ?, head_sha = ?, version = ?
                WHERE id = ? AND version = ?
                """,
                (
                    timestamp,
                    timestamp,
                    head_sha,
                    expected_version + 1,
                    lease_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("lease changed concurrently")
            updated_row = connection.execute(
                "SELECT * FROM worktree_leases WHERE id = ?", (lease_id,)
            ).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if updated_row is None:
            raise RuntimeError("lease changed concurrently")
        return self._lease_from_row(updated_row)

    def list_events(self, lease_id: str) -> list[WorktreeEvent]:
        self.ensure()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM worktree_events WHERE lease_id = ? ORDER BY id",
                (lease_id,),
            ).fetchall()
        return [
            WorktreeEvent(
                id=row["id"],
                lease_id=row["lease_id"],
                event_type=row["event_type"],
                from_state=(
                    LeaseState(row["from_state"])
                    if row["from_state"] is not None
                    else None
                ),
                to_state=(
                    LeaseState(row["to_state"]) if row["to_state"] is not None else None
                ),
                observed_head_sha=row["observed_head_sha"],
                pr_number=row["pr_number"],
                summary=row["summary"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _lease_parameters(lease: Lease) -> dict[str, Any]:
        return {
            "id": lease.id,
            "repository_id": lease.repository_id,
            "repository_name": lease.repository_name,
            "repository_root": str(lease.repository_root),
            "worktree_path": str(lease.worktree_path),
            "initiative": lease.initiative,
            "purpose": lease.purpose.value,
            "branch": lease.branch,
            "base_ref": lease.base_ref,
            "head_sha": lease.head_sha,
            "managed": int(lease.managed),
            "owner_kind": lease.owner_kind,
            "owner_id": lease.owner_id,
            "state": lease.state.value,
            "source_pr": lease.source_pr,
            "target_pr": lease.target_pr,
            "deployment_state": lease.deployment_state.value,
            "retain": int(lease.retain),
            "created_at": lease.created_at,
            "last_used_at": lease.last_used_at,
            "updated_at": lease.updated_at,
            "removed_at": lease.removed_at,
            "version": lease.version,
        }

    @staticmethod
    def _lease_from_row(row: sqlite3.Row) -> Lease:
        return Lease(
            id=row["id"],
            repository_id=row["repository_id"],
            repository_name=row["repository_name"],
            repository_root=Path(row["repository_root"]),
            worktree_path=Path(row["worktree_path"]),
            initiative=row["initiative"],
            purpose=Purpose(row["purpose"]),
            branch=row["branch"],
            base_ref=row["base_ref"],
            head_sha=row["head_sha"],
            managed=bool(row["managed"]),
            owner_kind=row["owner_kind"],
            owner_id=row["owner_id"],
            state=LeaseState(row["state"]),
            source_pr=row["source_pr"],
            target_pr=row["target_pr"],
            deployment_state=DeploymentState(row["deployment_state"]),
            retain=bool(row["retain"]),
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            updated_at=row["updated_at"],
            removed_at=row["removed_at"],
            version=row["version"],
        )

    @staticmethod
    def _database_value(value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, bool):
            return int(value)
        return value

    @staticmethod
    def _is_active_identity_conflict(error: sqlite3.IntegrityError) -> bool:
        message = str(error)
        return (
            "UNIQUE constraint failed" in message
            and "worktree_leases.repository_id" in message
            and "worktree_leases.initiative" in message
            and "worktree_leases.purpose" in message
        )

    @staticmethod
    def _bound_summary(summary: str) -> str:
        return summary.encode("utf-8")[:512].decode("utf-8", errors="ignore")
