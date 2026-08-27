from __future__ import annotations

from contextlib import closing

import json

import sqlite3
from enum import Enum
from pathlib import Path
from typing import Any

from .models import (
    CleanupReservation,
    DeploymentState,
    Lease,
    LeaseState,
    PromotionMode,
    Purpose,
    ReleaseBridge,
    ReleaseSource,
    ReleaseState,
    ResolutionState,
    WorktreeEvent,
    now_iso,
    release_source_digest,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS worktree_leases (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    repository_name TEXT NOT NULL,
    repository_root TEXT NOT NULL,
    worktree_path TEXT NOT NULL UNIQUE,
    initiative TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('feature','promote','scratch')),
    branch TEXT NOT NULL,
    base_ref TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    promotion_mode TEXT NOT NULL DEFAULT 'exact' CHECK (promotion_mode IN (
        'exact','out_of_order'
    )),
    managed INTEGER NOT NULL CHECK (managed IN (0,1)),
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('awf','imported','user')),
    owner_id TEXT,
    state TEXT NOT NULL CHECK (state IN (
        'ACTIVE','PR_OPEN','MERGED','DEPLOYING','DEPLOYED','CLEANABLE',
        'REMOVED','DIRTY','CLOSED_UNMERGED','ORPHANED','BLOCKED'
    )),
    source_pr INTEGER,
    target_pr INTEGER,
    resolution_state TEXT NOT NULL DEFAULT 'none' CHECK (resolution_state IN (
        'none','pending','automatic','manual_reviewed'
    )),
    source_base_sha TEXT,
    source_head_sha TEXT,
    target_base_sha TEXT,
    reviewed_paths TEXT NOT NULL DEFAULT '[]',
    conflicted_paths TEXT NOT NULL DEFAULT '[]',
    protected_index_entries TEXT NOT NULL DEFAULT '[]',
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
CREATE TABLE IF NOT EXISTS worktree_cleanup_reservations (
    lease_id TEXT PRIMARY KEY REFERENCES worktree_leases(id),
    reserved_version INTEGER NOT NULL,
    branch_sha TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS release_bridges (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    repository_name TEXT NOT NULL,
    repository_root TEXT NOT NULL,
    release_id TEXT NOT NULL,
    target_branch TEXT NOT NULL,
    lease_id TEXT NOT NULL UNIQUE REFERENCES worktree_leases(id),
    state TEXT NOT NULL CHECK (state IN (
        'OPEN','SEALED','PUBLISHED','MERGED','CLOSED_UNMERGED','CLEANED'
    )),
    source_digest TEXT NOT NULL,
    last_verified_target_sha TEXT,
    published_head_sha TEXT,
    target_pr INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    UNIQUE(repository_id, release_id)
);
CREATE INDEX IF NOT EXISTS idx_release_bridges_repository_state
ON release_bridges(repository_id, state);
CREATE TABLE IF NOT EXISTS release_bridge_sources (
    bridge_id TEXT NOT NULL REFERENCES release_bridges(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    source_pr INTEGER NOT NULL CHECK (source_pr > 0),
    base_ref TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    merge_sha TEXT NOT NULL,
    changed_paths TEXT NOT NULL,
    PRIMARY KEY (bridge_id, ordinal),
    UNIQUE (bridge_id, source_pr)
);
CREATE TABLE IF NOT EXISTS release_bridge_pending_sources (
    bridge_id TEXT PRIMARY KEY REFERENCES release_bridges(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    source_pr INTEGER NOT NULL CHECK (source_pr > 0),
    base_ref TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    merge_sha TEXT NOT NULL,
    changed_paths TEXT NOT NULL
);
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
        migrations = {
            "promotion_mode": (
                "ALTER TABLE worktree_leases ADD COLUMN promotion_mode "
                "TEXT NOT NULL DEFAULT 'exact'"
            ),
            "resolution_state": (
                "ALTER TABLE worktree_leases ADD COLUMN resolution_state "
                "TEXT NOT NULL DEFAULT 'none'"
            ),
            "source_base_sha": (
                "ALTER TABLE worktree_leases ADD COLUMN source_base_sha TEXT"
            ),
            "source_head_sha": (
                "ALTER TABLE worktree_leases ADD COLUMN source_head_sha TEXT"
            ),
            "target_base_sha": (
                "ALTER TABLE worktree_leases ADD COLUMN target_base_sha TEXT"
            ),
            "reviewed_paths": (
                "ALTER TABLE worktree_leases ADD COLUMN reviewed_paths "
                "TEXT NOT NULL DEFAULT '[]'"
            ),
            "conflicted_paths": (
                "ALTER TABLE worktree_leases ADD COLUMN conflicted_paths "
                "TEXT NOT NULL DEFAULT '[]'"
            ),
            "protected_index_entries": (
                "ALTER TABLE worktree_leases ADD COLUMN protected_index_entries "
                "TEXT NOT NULL DEFAULT '[]'"
            ),
        }
        with closing(self._connect()) as connection:
            connection.executescript(_SCHEMA)
            try:
                connection.execute("BEGIN IMMEDIATE")
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(worktree_leases)")
                }
                for name, statement in migrations.items():
                    if name not in columns:
                        connection.execute(statement)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def create_lease(self, lease: Lease) -> Lease:
        self.ensure()
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """
                    INSERT INTO worktree_leases (
                        id, repository_id, repository_name, repository_root,
                        worktree_path, initiative, purpose, branch, base_ref,
                        head_sha, promotion_mode, managed, owner_kind, owner_id,
                        state, source_pr, target_pr, resolution_state, source_base_sha,
                        source_head_sha, target_base_sha, reviewed_paths,
                        conflicted_paths, protected_index_entries, deployment_state, retain,
                        created_at, last_used_at, updated_at, removed_at, version
                    ) VALUES (
                        :id, :repository_id, :repository_name, :repository_root,
                        :worktree_path, :initiative, :purpose, :branch, :base_ref,
                        :head_sha, :promotion_mode, :managed, :owner_kind, :owner_id,
                        :state, :source_pr, :target_pr, :resolution_state,
                        :source_base_sha, :source_head_sha, :target_base_sha,
                        :reviewed_paths,
                        :conflicted_paths, :protected_index_entries, :deployment_state, :retain,
                        :created_at, :last_used_at, :updated_at, :removed_at, :version
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
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM worktree_leases WHERE id = ?", (lease_id,)
            ).fetchone()
        return self._lease_from_row(row) if row is not None else None

    def get_lease_read_only(self, lease_id: str) -> Lease | None:
        if not self.db_path.is_file():
            return None
        with closing(self._connect_read_only()) as connection:
            row = connection.execute(
                "SELECT * FROM worktree_leases WHERE id = ?", (lease_id,)
            ).fetchone()
        return self._lease_from_row(row) if row is not None else None

    def create_release(self, release: ReleaseBridge) -> ReleaseBridge:
        """Persist an empty release bridge after its managed promotion lease exists."""
        if release.sources:
            raise ValueError("release bridges must be created without sources")
        self.ensure()
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """
                    INSERT INTO release_bridges (
                        id, repository_id, repository_name, repository_root,
                        release_id, target_branch, lease_id, state, source_digest,
                        last_verified_target_sha, published_head_sha, target_pr,
                        created_at, updated_at, version
                    ) VALUES (
                        :id, :repository_id, :repository_name, :repository_root,
                        :release_id, :target_branch, :lease_id, :state, :source_digest,
                        :last_verified_target_sha, :published_head_sha, :target_pr,
                        :created_at, :updated_at, :version
                    )
                    """,
                    self._release_parameters(release),
                )
        except sqlite3.IntegrityError as error:
            if self.find_release(release.repository_id, release.release_id) is not None:
                raise ValueError("release bridge already exists") from error
            raise
        return release

    def create_release_with_lease(
        self, lease: Lease, release: ReleaseBridge
    ) -> tuple[Lease, ReleaseBridge]:
        """Atomically register a new promotion lease and its empty release bridge."""
        if release.sources:
            raise ValueError("release bridges must be created without sources")
        if release.lease_id != lease.id:
            raise ValueError("release bridge lease id does not match")
        self.ensure()
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO worktree_leases (
                        id, repository_id, repository_name, repository_root,
                        worktree_path, initiative, purpose, branch, base_ref,
                        head_sha, promotion_mode, managed, owner_kind, owner_id,
                        state, source_pr, target_pr, resolution_state, source_base_sha,
                        source_head_sha, target_base_sha, reviewed_paths,
                        conflicted_paths, protected_index_entries, deployment_state, retain,
                        created_at, last_used_at, updated_at, removed_at, version
                    ) VALUES (
                        :id, :repository_id, :repository_name, :repository_root,
                        :worktree_path, :initiative, :purpose, :branch, :base_ref,
                        :head_sha, :promotion_mode, :managed, :owner_kind, :owner_id,
                        :state, :source_pr, :target_pr, :resolution_state,
                        :source_base_sha, :source_head_sha, :target_base_sha,
                        :reviewed_paths,
                        :conflicted_paths, :protected_index_entries, :deployment_state, :retain,
                        :created_at, :last_used_at, :updated_at, :removed_at, :version
                    )
                    """,
                    self._lease_parameters(lease),
                )
                connection.execute(
                    """
                    INSERT INTO release_bridges (
                        id, repository_id, repository_name, repository_root,
                        release_id, target_branch, lease_id, state, source_digest,
                        last_verified_target_sha, published_head_sha, target_pr,
                        created_at, updated_at, version
                    ) VALUES (
                        :id, :repository_id, :repository_name, :repository_root,
                        :release_id, :target_branch, :lease_id, :state, :source_digest,
                        :last_verified_target_sha, :published_head_sha, :target_pr,
                        :created_at, :updated_at, :version
                    )
                    """,
                    self._release_parameters(release),
                )
                connection.commit()
        except sqlite3.IntegrityError as error:
            if self.find_release(lease.repository_id, release.release_id) is not None:
                raise ValueError("release bridge already exists") from error
            if self.find_active(lease.repository_id, lease.initiative, lease.purpose):
                raise ValueError("active lease already exists") from error
            raise
        return lease, release

    def get_release(self, bridge_id: str) -> ReleaseBridge | None:
        self.ensure()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM release_bridges WHERE id = ?", (bridge_id,)
            ).fetchone()
            return self._release_from_row(connection, row) if row is not None else None

    def get_release_read_only(self, bridge_id: str) -> ReleaseBridge | None:
        if not self.db_path.is_file():
            return None
        with closing(self._connect_read_only()) as connection:
            row = connection.execute(
                "SELECT * FROM release_bridges WHERE id = ?", (bridge_id,)
            ).fetchone()
            return self._release_from_row(connection, row) if row is not None else None

    def find_release(
        self, repository_id: str, release_id: str
    ) -> ReleaseBridge | None:
        self.ensure()
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM release_bridges
                WHERE repository_id = ? AND release_id = ?
                """,
                (repository_id, release_id),
            ).fetchone()
            return self._release_from_row(connection, row) if row is not None else None

    def find_release_read_only(
        self, repository_id: str, release_id: str
    ) -> ReleaseBridge | None:
        if not self.db_path.is_file():
            return None
        with closing(self._connect_read_only()) as connection:
            row = connection.execute(
                """
                SELECT * FROM release_bridges
                WHERE repository_id = ? AND release_id = ?
                """,
                (repository_id, release_id),
            ).fetchone()
            return self._release_from_row(connection, row) if row is not None else None


    def get_pending_release_source(
        self, bridge_id: str
    ) -> ReleaseSource | None:
        if not self.db_path.is_file():
            return None
        with closing(self._connect_read_only()) as connection:
            row = connection.execute(
                "SELECT * FROM release_bridge_pending_sources WHERE bridge_id = ?",
                (bridge_id,),
            ).fetchone()
        return self._release_source_from_row(row) if row is not None else None

    def stage_release_source(
        self,
        source: ReleaseSource,
        *,
        expected_version: int,
    ) -> ReleaseBridge:
        """Durably record one pending source intent before mutating its worktree."""
        self.ensure()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM release_bridges WHERE id = ?", (source.bridge_id,)
            ).fetchone()
            if row is None or row["version"] != expected_version:
                raise RuntimeError("release bridge changed concurrently")
            if ReleaseState(row["state"]) is not ReleaseState.OPEN:
                raise ValueError("release bridge is not open for sources")
            existing = self._release_sources(connection, source.bridge_id)
            if source.ordinal != len(existing):
                raise ValueError("release source ordinal must append after existing sources")
            if any(item.source_pr == source.source_pr for item in existing):
                raise ValueError("release source pull request is already pinned")
            pending_row = connection.execute(
                "SELECT * FROM release_bridge_pending_sources WHERE bridge_id = ?",
                (source.bridge_id,),
            ).fetchone()
            if pending_row is not None:
                pending = self._release_source_from_row(pending_row)
                if pending != source:
                    raise ValueError("another release source add is pending")
                release = self._release_from_row(connection, row)
                connection.commit()
                return release
            connection.execute(
                """
                INSERT INTO release_bridge_pending_sources (
                    bridge_id, ordinal, source_pr, base_ref, base_sha, head_sha,
                    merge_sha, changed_paths
                ) VALUES (
                    :bridge_id, :ordinal, :source_pr, :base_ref, :base_sha, :head_sha,
                    :merge_sha, :changed_paths
                )
                """,
                self._release_source_parameters(source),
            )
            timestamp = now_iso()
            cursor = connection.execute(
                """
                UPDATE release_bridges
                SET updated_at = ?, version = ?
                WHERE id = ? AND version = ?
                """,
                (
                    timestamp,
                    expected_version + 1,
                    source.bridge_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("release bridge changed concurrently")
            row = connection.execute(
                "SELECT * FROM release_bridges WHERE id = ?", (source.bridge_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("release bridge changed concurrently")
            release = self._release_from_row(connection, row)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return release

    def clear_pending_release_source(
        self,
        source: ReleaseSource,
        *,
        expected_version: int,
    ) -> ReleaseBridge:
        """Clear one exact pending source after a rejected add and keep the bridge open."""
        self.ensure()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM release_bridges WHERE id = ?", (source.bridge_id,)
            ).fetchone()
            pending_row = connection.execute(
                "SELECT * FROM release_bridge_pending_sources WHERE bridge_id = ?",
                (source.bridge_id,),
            ).fetchone()
            if row is None or row["version"] != expected_version:
                raise RuntimeError("release bridge changed concurrently")
            if pending_row is None or self._release_source_from_row(pending_row) != source:
                raise ValueError("pending release source does not match")
            connection.execute(
                "DELETE FROM release_bridge_pending_sources WHERE bridge_id = ?",
                (source.bridge_id,),
            )
            timestamp = now_iso()
            cursor = connection.execute(
                """
                UPDATE release_bridges
                SET updated_at = ?, version = ?
                WHERE id = ? AND version = ?
                """,
                (
                    timestamp,
                    expected_version + 1,
                    source.bridge_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("release bridge changed concurrently")
            row = connection.execute(
                "SELECT * FROM release_bridges WHERE id = ?", (source.bridge_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("release bridge changed concurrently")
            release = self._release_from_row(connection, row)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return release

    def accept_release_source(
        self,
        source: ReleaseSource,
        *,
        expected_release_version: int,
        lease_id: str,
        expected_lease_version: int,
        head_sha: str,
        target_base_sha: str,
    ) -> tuple[Lease, ReleaseBridge]:
        """Atomically accept one rebuilt source pin and its resulting lease head."""
        self.ensure()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            release_row = connection.execute(
                "SELECT * FROM release_bridges WHERE id = ?", (source.bridge_id,)
            ).fetchone()
            lease_row = connection.execute(
                "SELECT * FROM worktree_leases WHERE id = ?", (lease_id,)
            ).fetchone()
            pending_row = connection.execute(
                "SELECT * FROM release_bridge_pending_sources WHERE bridge_id = ?",
                (source.bridge_id,),
            ).fetchone()
            if (
                release_row is None
                or release_row["version"] != expected_release_version
                or lease_row is None
                or lease_row["version"] != expected_lease_version
            ):
                raise RuntimeError("release source acceptance changed concurrently")
            if release_row["lease_id"] != lease_id:
                raise ValueError("release source lease does not match")
            if ReleaseState(release_row["state"]) is not ReleaseState.OPEN:
                raise ValueError("release bridge is not open for sources")
            if (
                pending_row is None
                or self._release_source_from_row(pending_row) != source
            ):
                raise ValueError("pending release source does not match")
            existing = self._release_sources(connection, source.bridge_id)
            if source.ordinal != len(existing):
                raise ValueError("release source ordinal must append after existing sources")
            if any(item.source_pr == source.source_pr for item in existing):
                raise ValueError("release source pull request is already pinned")
            connection.execute(
                """
                INSERT INTO release_bridge_sources (
                    bridge_id, ordinal, source_pr, base_ref, base_sha, head_sha,
                    merge_sha, changed_paths
                ) VALUES (
                    :bridge_id, :ordinal, :source_pr, :base_ref, :base_sha, :head_sha,
                    :merge_sha, :changed_paths
                )
                """,
                self._release_source_parameters(source),
            )
            connection.execute(
                "DELETE FROM release_bridge_pending_sources WHERE bridge_id = ?",
                (source.bridge_id,),
            )
            timestamp = now_iso()
            sources = (*existing, source)
            release_cursor = connection.execute(
                """
                UPDATE release_bridges
                SET source_digest = ?, updated_at = ?, version = ?
                WHERE id = ? AND version = ?
                """,
                (
                    release_source_digest(sources),
                    timestamp,
                    expected_release_version + 1,
                    source.bridge_id,
                    expected_release_version,
                ),
            )
            lease_cursor = connection.execute(
                """
                UPDATE worktree_leases
                SET head_sha = ?, target_base_sha = ?, updated_at = ?, version = ?
                WHERE id = ? AND version = ?
                """,
                (
                    head_sha,
                    target_base_sha,
                    timestamp,
                    expected_lease_version + 1,
                    lease_id,
                    expected_lease_version,
                ),
            )
            if release_cursor.rowcount != 1 or lease_cursor.rowcount != 1:
                raise RuntimeError("release source acceptance changed concurrently")
            connection.execute(
                """
                INSERT INTO worktree_events (
                    lease_id, event_type, from_state, to_state, observed_head_sha,
                    pr_number, summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    "release_source_added",
                    lease_row["state"],
                    lease_row["state"],
                    head_sha,
                    source.source_pr,
                    self._bound_summary(
                        f"release source PR #{source.source_pr} accepted"
                    ),
                    timestamp,
                ),
            )
            lease_row = connection.execute(
                "SELECT * FROM worktree_leases WHERE id = ?", (lease_id,)
            ).fetchone()
            release_row = connection.execute(
                "SELECT * FROM release_bridges WHERE id = ?", (source.bridge_id,)
            ).fetchone()
            if lease_row is None or release_row is None:
                raise RuntimeError("release source acceptance changed concurrently")
            lease = self._lease_from_row(lease_row)
            release = self._release_from_row(connection, release_row)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return lease, release


    def transition_release(
        self,
        bridge_id: str,
        state: ReleaseState,
        *,
        expected_version: int,
        last_verified_target_sha: str | None = None,
        published_head_sha: str | None = None,
        target_pr: int | None = None,
    ) -> ReleaseBridge:
        """CAS-update release state and observed verified/publication provenance."""
        self.ensure()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM release_bridges WHERE id = ?", (bridge_id,)
            ).fetchone()
            if row is None or row["version"] != expected_version:
                raise RuntimeError("release bridge changed concurrently")
            current = ReleaseState(row["state"])
            if not self._valid_release_transition(current, state):
                raise ValueError(
                    f"invalid release state transition {current.value} -> {state.value}"
                )
            if state is ReleaseState.SEALED:
                pending = connection.execute(
                    """
                    SELECT 1 FROM release_bridge_pending_sources
                    WHERE bridge_id = ?
                    """,
                    (bridge_id,),
                ).fetchone()
                if pending is not None:
                    raise ValueError("release bridge has a pending source add")
            timestamp = now_iso()
            values: dict[str, Any] = {
                "state": state.value,
                "updated_at": timestamp,
                "version": expected_version + 1,
            }
            if last_verified_target_sha is not None:
                values["last_verified_target_sha"] = last_verified_target_sha
            if published_head_sha is not None:
                values["published_head_sha"] = published_head_sha
            if target_pr is not None:
                values["target_pr"] = target_pr
            assignments = ", ".join(f"{column} = ?" for column in values)
            cursor = connection.execute(
                f"UPDATE release_bridges SET {assignments} "
                "WHERE id = ? AND version = ?",
                (*values.values(), bridge_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("release bridge changed concurrently")
            row = connection.execute(
                "SELECT * FROM release_bridges WHERE id = ?", (bridge_id,)
            ).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if row is None:
            raise RuntimeError("release bridge changed concurrently")
        with closing(self._connect()) as connection:
            return self._release_from_row(connection, row)

    def publish_release(
        self,
        bridge_id: str,
        lease_id: str,
        *,
        expected_release_version: int,
        expected_lease_version: int,
        target_pr: int,
        head_sha: str,
        target_base_sha: str,
    ) -> tuple[Lease, ReleaseBridge]:
        """Atomically bind one published PR to its release and promotion lease."""
        self.ensure()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            release_row = connection.execute(
                "SELECT * FROM release_bridges WHERE id = ?", (bridge_id,)
            ).fetchone()
            lease_row = connection.execute(
                "SELECT * FROM worktree_leases WHERE id = ?", (lease_id,)
            ).fetchone()
            if (
                release_row is None
                or release_row["version"] != expected_release_version
                or lease_row is None
                or lease_row["version"] != expected_lease_version
            ):
                raise RuntimeError("release publication changed concurrently")
            if release_row["lease_id"] != lease_id:
                raise ValueError("release publication lease does not match")
            if ReleaseState(release_row["state"]) is not ReleaseState.SEALED:
                raise ValueError("release bridge is not sealed")
            current_lease = self._lease_from_row(lease_row)
            if current_lease.state not in {LeaseState.ACTIVE, LeaseState.PR_OPEN}:
                raise ValueError("release promotion lease is not publishable")
            if (
                current_lease.state is LeaseState.PR_OPEN
                and current_lease.target_pr not in {None, target_pr}
            ):
                raise ValueError("release promotion lease already targets another PR")
            timestamp = now_iso()
            lease_cursor = connection.execute(
                """
                UPDATE worktree_leases
                SET state = ?, target_pr = ?, head_sha = ?, target_base_sha = ?,
                    updated_at = ?, version = ?
                WHERE id = ? AND version = ?
                """,
                (
                    LeaseState.PR_OPEN.value,
                    target_pr,
                    head_sha,
                    target_base_sha,
                    timestamp,
                    expected_lease_version + 1,
                    lease_id,
                    expected_lease_version,
                ),
            )
            release_cursor = connection.execute(
                """
                UPDATE release_bridges
                SET state = ?, last_verified_target_sha = ?,
                    published_head_sha = ?, target_pr = ?, updated_at = ?, version = ?
                WHERE id = ? AND version = ?
                """,
                (
                    ReleaseState.PUBLISHED.value,
                    target_base_sha,
                    head_sha,
                    target_pr,
                    timestamp,
                    expected_release_version + 1,
                    bridge_id,
                    expected_release_version,
                ),
            )
            if lease_cursor.rowcount != 1 or release_cursor.rowcount != 1:
                raise RuntimeError("release publication changed concurrently")
            connection.execute(
                """
                INSERT INTO worktree_events (
                    lease_id, event_type, from_state, to_state, observed_head_sha,
                    pr_number, summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    "release_pr_open",
                    current_lease.state.value,
                    LeaseState.PR_OPEN.value,
                    head_sha,
                    target_pr,
                    self._bound_summary(
                        f"release {release_row['release_id']} PR #{target_pr} opened"
                    ),
                    timestamp,
                ),
            )
            lease_row = connection.execute(
                "SELECT * FROM worktree_leases WHERE id = ?", (lease_id,)
            ).fetchone()
            release_row = connection.execute(
                "SELECT * FROM release_bridges WHERE id = ?", (bridge_id,)
            ).fetchone()
            if lease_row is None or release_row is None:
                raise RuntimeError("release publication changed concurrently")
            lease = self._lease_from_row(lease_row)
            release = self._release_from_row(connection, release_row)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return lease, release

    def sync_release_state_for_lease(
        self, lease_id: str, state: ReleaseState
    ) -> ReleaseBridge | None:
        """Best-effort CAS state synchronization from an observed PR lease."""
        self.ensure()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM release_bridges WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            current = ReleaseState(row["state"])
            if current is state:
                connection.commit()
                return self._release_from_row(connection, row)
            if not self._valid_release_transition(current, state):
                raise ValueError(
                    f"invalid release state transition {current.value} -> {state.value}"
                )
            timestamp = now_iso()
            cursor = connection.execute(
                """
                UPDATE release_bridges
                SET state = ?, updated_at = ?, version = version + 1
                WHERE id = ? AND version = ?
                """,
                (state.value, timestamp, row["id"], row["version"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("release bridge changed concurrently")
            row = connection.execute(
                "SELECT * FROM release_bridges WHERE id = ?", (row["id"],)
            ).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if row is None:
            raise RuntimeError("release bridge changed concurrently")
        with closing(self._connect()) as connection:
            return self._release_from_row(connection, row)

    def find_active(
        self, repository_id: str, initiative: str, purpose: Purpose
    ) -> Lease | None:
        self.ensure()
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM worktree_leases
                WHERE repository_id = ? AND initiative = ? AND purpose = ?
                AND state <> ?
                """,
                (repository_id, initiative, purpose.value, LeaseState.REMOVED.value),
            ).fetchone()
        return self._lease_from_row(row) if row is not None else None

    def find_active_read_only(
        self, repository_id: str, initiative: str, purpose: Purpose
    ) -> Lease | None:
        if not self.db_path.is_file():
            return None
        with closing(self._connect_read_only()) as connection:
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
        with closing(self._connect()) as connection:
            return self._list_leases(
                connection,
                include_removed=include_removed,
                filters=filters,
            )

    def list_leases_read_only(
        self, *, include_removed: bool = True, **filters: Any
    ) -> list[Lease]:
        if not self.db_path.is_file():
            return []
        with closing(self._connect_read_only()) as connection:
            return self._list_leases(
                connection,
                include_removed=include_removed,
                filters=filters,
            )

    def _list_leases(
        self,
        connection: sqlite3.Connection,
        *,
        include_removed: bool,
        filters: dict[str, Any],
    ) -> list[Lease]:
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
        target_base_sha: str | None = None,
        retain: bool | None = None,
        managed: bool | None = None,
        resolution_state: ResolutionState | None = None,
        conflicted_paths: tuple[str, ...] | None = None,
        protected_index_entries: tuple[
            tuple[str, tuple[str, str] | None], ...
        ]
        | None = None,
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
            if self._has_cleanup_reservation(connection, lease_id):
                raise RuntimeError("lease cleanup is reserved")
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
            if target_base_sha is not None:
                update_values["target_base_sha"] = target_base_sha
            if deployment_state is not None:
                update_values["deployment_state"] = deployment_state.value
            if retain is not None:
                update_values["retain"] = int(retain)
            if managed is not None:
                update_values["managed"] = int(managed)
            if resolution_state is not None:
                update_values["resolution_state"] = resolution_state.value
            if conflicted_paths is not None:
                update_values["conflicted_paths"] = self._path_metadata_to_json(
                    conflicted_paths, name="conflicted_paths"
                )
            if protected_index_entries is not None:
                update_values[
                    "protected_index_entries"
                ] = self._protected_index_entries_to_json(protected_index_entries)

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
            connection.execute("BEGIN IMMEDIATE")
            if self._has_cleanup_reservation(connection, lease_id):
                raise RuntimeError("lease cleanup is reserved")
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

    def reserve_cleanup(
        self, lease_id: str, *, expected_version: int, branch_sha: str
    ) -> CleanupReservation:
        self.ensure()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(
                "SELECT * FROM worktree_leases WHERE id = ?", (lease_id,)
            ).fetchone()
            if (
                current_row is None
                or current_row["version"] != expected_version
                or current_row["state"] == LeaseState.REMOVED.value
            ):
                raise RuntimeError("lease changed concurrently")
            if self._has_cleanup_reservation(connection, lease_id):
                raise RuntimeError("lease cleanup is reserved")
            timestamp = now_iso()
            reserved_version = expected_version + 1
            cursor = connection.execute(
                """
                UPDATE worktree_leases
                SET updated_at = ?, version = ?
                WHERE id = ? AND version = ?
                """,
                (timestamp, reserved_version, lease_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("lease changed concurrently")
            connection.execute(
                """
                INSERT INTO worktree_cleanup_reservations (
                    lease_id, reserved_version, branch_sha, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (lease_id, reserved_version, branch_sha, timestamp),
            )
            connection.execute(
                """
                INSERT INTO worktree_events (
                    lease_id, event_type, from_state, to_state, observed_head_sha,
                    pr_number, summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    "cleanup_reserved",
                    current_row["state"],
                    current_row["state"],
                    branch_sha,
                    current_row["target_pr"],
                    "Cleanup reserved",
                    timestamp,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return CleanupReservation(
            lease_id=lease_id,
            reserved_version=reserved_version,
            branch_sha=branch_sha,
            created_at=timestamp,
        )

    def get_cleanup_reservation(self, lease_id: str) -> CleanupReservation | None:
        if not self.db_path.is_file():
            return None
        with closing(self._connect_read_only()) as connection:
            row = connection.execute(
                """
                SELECT lease_id, reserved_version, branch_sha, created_at
                FROM worktree_cleanup_reservations
                WHERE lease_id = ?
                """,
                (lease_id,),
            ).fetchone()
        return self._cleanup_reservation_from_row(row) if row is not None else None

    def complete_cleanup(self, lease_id: str, *, expected_version: int) -> Lease:
        self.ensure()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            reservation = connection.execute(
                """
                SELECT lease_id, reserved_version, branch_sha, created_at
                FROM worktree_cleanup_reservations
                WHERE lease_id = ?
                """,
                (lease_id,),
            ).fetchone()
            current_row = connection.execute(
                "SELECT * FROM worktree_leases WHERE id = ?", (lease_id,)
            ).fetchone()
            if (
                reservation is None
                or reservation["reserved_version"] != expected_version
                or current_row is None
                or current_row["version"] != expected_version
            ):
                raise RuntimeError("lease changed concurrently")
            timestamp = now_iso()
            cursor = connection.execute(
                """
                UPDATE worktree_leases
                SET state = ?, updated_at = ?, removed_at = ?, version = ?
                WHERE id = ? AND version = ?
                """,
                (
                    LeaseState.REMOVED.value,
                    timestamp,
                    timestamp,
                    expected_version + 1,
                    lease_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("lease changed concurrently")
            release_row = connection.execute(
                "SELECT * FROM release_bridges WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
            if release_row is not None:
                release_state = ReleaseState(release_row["state"])
                if release_state not in {
                    ReleaseState.PUBLISHED,
                    ReleaseState.MERGED,
                }:
                    raise ValueError(
                        "release bridge is not eligible for completed cleanup"
                    )
                release_cursor = connection.execute(
                    """
                    UPDATE release_bridges
                    SET state = ?, updated_at = ?, version = version + 1
                    WHERE id = ? AND version = ?
                    """,
                    (
                        ReleaseState.CLEANED.value,
                        timestamp,
                        release_row["id"],
                        release_row["version"],
                    ),
                )
                if release_cursor.rowcount != 1:
                    raise RuntimeError("release bridge changed concurrently")
            connection.execute(
                """
                INSERT INTO worktree_events (
                    lease_id, event_type, from_state, to_state, observed_head_sha,
                    pr_number, summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    "worktree_removed",
                    current_row["state"],
                    LeaseState.REMOVED.value,
                    reservation["branch_sha"],
                    current_row["target_pr"],
                    "Removed proven-safe AWF worktree lease",
                    timestamp,
                ),
            )
            connection.execute(
                "DELETE FROM worktree_cleanup_reservations WHERE lease_id = ?",
                (lease_id,),
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

    def release_cleanup_reservation(
        self, lease_id: str, *, expected_version: int
    ) -> Lease:
        self.ensure()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            reservation = connection.execute(
                """
                SELECT reserved_version FROM worktree_cleanup_reservations
                WHERE lease_id = ?
                """,
                (lease_id,),
            ).fetchone()
            current_row = connection.execute(
                "SELECT * FROM worktree_leases WHERE id = ?", (lease_id,)
            ).fetchone()
            if (
                reservation is None
                or reservation["reserved_version"] != expected_version
                or current_row is None
                or current_row["version"] != expected_version
            ):
                raise RuntimeError("lease changed concurrently")
            timestamp = now_iso()
            cursor = connection.execute(
                """
                UPDATE worktree_leases
                SET updated_at = ?, version = ?
                WHERE id = ? AND version = ?
                """,
                (timestamp, expected_version + 1, lease_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("lease changed concurrently")
            connection.execute(
                "DELETE FROM worktree_cleanup_reservations WHERE lease_id = ?",
                (lease_id,),
            )
            connection.execute(
                """
                INSERT INTO worktree_events (
                    lease_id, event_type, from_state, to_state, observed_head_sha,
                    pr_number, summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    "cleanup_released",
                    current_row["state"],
                    current_row["state"],
                    None,
                    current_row["target_pr"],
                    "Cleanup reservation released",
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

    def record_cleanup_event(
        self, lease_id: str, *, event_type: str, summary: str
    ) -> None:
        self.ensure()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(
                "SELECT * FROM worktree_leases WHERE id = ?", (lease_id,)
            ).fetchone()
            if current_row is None:
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
                    current_row["state"],
                    current_row["state"],
                    None,
                    current_row["target_pr"],
                    self._bound_summary(summary),
                    now_iso(),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_events(self, lease_id: str) -> list[WorktreeEvent]:
        self.ensure()
        with closing(self._connect()) as connection:
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

    @staticmethod
    def _valid_release_transition(
        current: ReleaseState, target: ReleaseState
    ) -> bool:
        if current is target:
            return True
        return target in {
            ReleaseState.OPEN: {ReleaseState.SEALED},
            ReleaseState.SEALED: {ReleaseState.PUBLISHED},
            ReleaseState.PUBLISHED: {
                ReleaseState.MERGED,
                ReleaseState.CLOSED_UNMERGED,
            },
            ReleaseState.MERGED: {ReleaseState.CLEANED},
            ReleaseState.CLOSED_UNMERGED: set(),
            ReleaseState.CLEANED: set(),
        }[current]

    @staticmethod
    def _release_parameters(release: ReleaseBridge) -> dict[str, Any]:
        return {
            "id": release.id,
            "repository_id": release.repository_id,
            "repository_name": release.repository_name,
            "repository_root": str(release.repository_root),
            "release_id": release.release_id,
            "target_branch": release.target_branch,
            "lease_id": release.lease_id,
            "state": release.state.value,
            "source_digest": release.source_digest,
            "last_verified_target_sha": release.last_verified_target_sha,
            "published_head_sha": release.published_head_sha,
            "target_pr": release.target_pr,
            "created_at": release.created_at,
            "updated_at": release.updated_at,
            "version": release.version,
        }

    @staticmethod
    def _release_source_parameters(source: ReleaseSource) -> dict[str, Any]:
        return {
            "bridge_id": source.bridge_id,
            "ordinal": source.ordinal,
            "source_pr": source.source_pr,
            "base_ref": source.base_ref,
            "base_sha": source.base_sha,
            "head_sha": source.head_sha,
            "merge_sha": source.merge_sha,
            "changed_paths": WorktreeRegistry._path_metadata_to_json(
                source.changed_paths,
                name="release source changed_paths",
            ),
        }

    @staticmethod
    def _release_source_from_row(row: sqlite3.Row) -> ReleaseSource:
        return ReleaseSource(
            bridge_id=row["bridge_id"],
            ordinal=row["ordinal"],
            source_pr=row["source_pr"],
            base_ref=row["base_ref"],
            base_sha=row["base_sha"],
            head_sha=row["head_sha"],
            merge_sha=row["merge_sha"],
            changed_paths=WorktreeRegistry._path_metadata_from_json(
                row["changed_paths"],
                name="release source changed_paths",
            ),
        )

    def _release_sources(
        self, connection: sqlite3.Connection, bridge_id: str
    ) -> tuple[ReleaseSource, ...]:
        rows = connection.execute(
            """
            SELECT * FROM release_bridge_sources
            WHERE bridge_id = ?
            ORDER BY ordinal
            """,
            (bridge_id,),
        ).fetchall()
        return tuple(self._release_source_from_row(row) for row in rows)

    def _release_from_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> ReleaseBridge:
        return ReleaseBridge(
            id=row["id"],
            repository_id=row["repository_id"],
            repository_name=row["repository_name"],
            repository_root=Path(row["repository_root"]),
            release_id=row["release_id"],
            target_branch=row["target_branch"],
            lease_id=row["lease_id"],
            state=ReleaseState(row["state"]),
            source_digest=row["source_digest"],
            last_verified_target_sha=row["last_verified_target_sha"],
            published_head_sha=row["published_head_sha"],
            target_pr=row["target_pr"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            version=row["version"],
            sources=self._release_sources(connection, row["id"]),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _connect_read_only(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self.db_path.resolve().as_uri()}?mode=ro",
            uri=True,
        )
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
            "promotion_mode": lease.promotion_mode.value,
            "managed": int(lease.managed),
            "owner_kind": lease.owner_kind,
            "owner_id": lease.owner_id,
            "state": lease.state.value,
            "source_pr": lease.source_pr,
            "target_pr": lease.target_pr,
            "resolution_state": lease.resolution_state.value,
            "source_base_sha": lease.source_base_sha,
            "source_head_sha": lease.source_head_sha,
            "target_base_sha": lease.target_base_sha,
            "reviewed_paths": WorktreeRegistry._path_metadata_to_json(
                lease.reviewed_paths, name="reviewed_paths"
            ),
            "conflicted_paths": WorktreeRegistry._path_metadata_to_json(
                lease.conflicted_paths, name="conflicted_paths"
            ),
            "protected_index_entries": WorktreeRegistry._protected_index_entries_to_json(
                lease.protected_index_entries
            ),
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
            promotion_mode=PromotionMode(row["promotion_mode"]),
            managed=bool(row["managed"]),
            owner_kind=row["owner_kind"],
            owner_id=row["owner_id"],
            state=LeaseState(row["state"]),
            source_pr=row["source_pr"],
            target_pr=row["target_pr"],
            resolution_state=ResolutionState(row["resolution_state"]),
            source_base_sha=row["source_base_sha"],
            source_head_sha=row["source_head_sha"],
            target_base_sha=row["target_base_sha"],
            reviewed_paths=WorktreeRegistry._path_metadata_from_json(
                row["reviewed_paths"], name="reviewed_paths"
            ),
            conflicted_paths=WorktreeRegistry._path_metadata_from_json(
                row["conflicted_paths"], name="conflicted_paths"
            ),
            protected_index_entries=(
                WorktreeRegistry._protected_index_entries_from_json(
                    row["protected_index_entries"]
                )
            ),
            deployment_state=DeploymentState(row["deployment_state"]),
            retain=bool(row["retain"]),
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            updated_at=row["updated_at"],
            removed_at=row["removed_at"],
            version=row["version"],
        )

    @staticmethod
    def _cleanup_reservation_from_row(row: sqlite3.Row) -> CleanupReservation:
        return CleanupReservation(
            lease_id=row["lease_id"],
            reserved_version=row["reserved_version"],
            branch_sha=row["branch_sha"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _has_cleanup_reservation(
        connection: sqlite3.Connection, lease_id: str
    ) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM worktree_cleanup_reservations WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
            is not None
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
    def _path_metadata_to_json(paths: tuple[str, ...], *, name: str) -> str:
        return json.dumps(
            list(WorktreeRegistry._validated_path_metadata(paths, name=name)),
            separators=(",", ":"),
        )

    @staticmethod
    def _path_metadata_from_json(value: str, *, name: str) -> tuple[str, ...]:
        if not isinstance(value, str):
            raise ValueError(f"invalid {name} metadata")
        try:
            paths = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid {name} metadata") from error
        if not isinstance(paths, list):
            raise ValueError(f"invalid {name} metadata")
        return WorktreeRegistry._validated_path_metadata(tuple(paths), name=name)

    @staticmethod
    def _validated_path_metadata(value: Any, *, name: str) -> tuple[str, ...]:
        if not isinstance(value, tuple) or any(
            not isinstance(path, str) for path in value
        ):
            raise ValueError(f"invalid {name} metadata")
        if value != tuple(sorted(value)) or len(value) != len(set(value)):
            raise ValueError(f"invalid {name} metadata")
        return value

    @staticmethod
    def _protected_index_entries_to_json(
        protected_index_entries: tuple[
            tuple[str, tuple[str, str] | None], ...
        ],
    ) -> str:
        Lease._validate_protected_index_entries(protected_index_entries)
        return json.dumps(protected_index_entries, separators=(",", ":"))

    @staticmethod
    def _protected_index_entries_from_json(
        value: str,
    ) -> tuple[tuple[str, tuple[str, str] | None], ...]:
        if not isinstance(value, str):
            raise ValueError("invalid protected_index_entries metadata")
        try:
            raw_entries = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("invalid protected_index_entries metadata") from error
        if not isinstance(raw_entries, list):
            raise ValueError("invalid protected_index_entries metadata")
        protected_index_entries = tuple(
            (
                item[0],
                tuple(item[1]) if item[1] is not None else None,
            )
            if isinstance(item, list) and len(item) == 2
            else item
            for item in raw_entries
        )
        Lease._validate_protected_index_entries(protected_index_entries)
        return protected_index_entries
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
