from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock

import pytest

from awf.worktrees.models import (
    CommandResult,
    DeploymentState,
    Lease,
    LeaseState,
    PromotionMode,
    Purpose,
    ResolutionState,
)
from awf.worktrees.registry import WorktreeRegistry


def lease(
    path: Path,
    *,
    initiative: str = "reward-widget",
    worktree_name: str | None = None,
) -> Lease:
    return Lease.new(
        repository_id="repo-1",
        repository_name="demo",
        repository_root=path / "repo",
        worktree_path=path / "cache" / (worktree_name or initiative),
        initiative=initiative,
        purpose=Purpose.FEATURE,
        branch=f"awf/{initiative}/feature",
        base_ref="origin/staging",
        head_sha="a" * 40,
        managed=True,
        owner_kind="awf",
        promotion_mode=PromotionMode.EXACT,
        resolution_state=ResolutionState.NONE,
        source_base_sha=None,
        source_head_sha=None,
        target_base_sha=None,
        reviewed_paths=(),
        conflicted_paths=(),
    )


def test_registry_creates_and_round_trips_a_lease(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))

    loaded = registry.get_lease(created.id)

    assert loaded == created
    assert loaded.state is LeaseState.ACTIVE
    assert loaded.deployment_state is DeploymentState.NOT_REQUIRED



def test_registry_round_trips_out_of_order_provenance(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = replace(
        lease(tmp_path),
        promotion_mode=PromotionMode.OUT_OF_ORDER,
        resolution_state=ResolutionState.PENDING,
        source_base_sha="a" * 40,
        source_head_sha="b" * 40,
        target_base_sha="c" * 40,
        reviewed_paths=("src/a.py", "src/b.py"),
        conflicted_paths=("src/b.py",),
        protected_index_entries=(("src/a.py", ("100644", "d" * 40)),),
    )

    registry.create_lease(created)

    assert registry.get_lease(created.id) == created
    payload = created.to_dict()
    assert {
        "promotion_mode": payload["promotion_mode"],
        "resolution_state": payload["resolution_state"],
        "source_base_sha": payload["source_base_sha"],
        "source_head_sha": payload["source_head_sha"],
        "target_base_sha": payload["target_base_sha"],
        "reviewed_paths": payload["reviewed_paths"],
        "conflicted_paths": payload["conflicted_paths"],
        "protected_index_entries": payload["protected_index_entries"],
    } == {
        "promotion_mode": "out_of_order",
        "resolution_state": "pending",
        "source_base_sha": "a" * 40,
        "source_head_sha": "b" * 40,
        "target_base_sha": "c" * 40,
        "reviewed_paths": ["src/a.py", "src/b.py"],
        "conflicted_paths": ["src/b.py"],
        "protected_index_entries": [
            {"path": "src/a.py", "mode": "100644", "blob_oid": "d" * 40}
        ],
    }


@pytest.mark.parametrize("mode", ("120000", "160000"))
def test_registry_round_trips_supported_protected_index_entry_modes(
    tmp_path: Path,
    mode: str,
) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = replace(
        lease(tmp_path),
        promotion_mode=PromotionMode.OUT_OF_ORDER,
        protected_index_entries=(("entry", (mode, "d" * 40)),),
    )

    registry.create_lease(created)

    assert registry.get_lease(created.id) == created

def test_registry_migrates_legacy_lease_with_promotion_defaults(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "worktrees.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE worktree_leases (
                id TEXT PRIMARY KEY,
                repository_id TEXT NOT NULL,
                repository_name TEXT NOT NULL,
                repository_root TEXT NOT NULL,
                worktree_path TEXT NOT NULL UNIQUE,
                initiative TEXT NOT NULL,
                purpose TEXT NOT NULL,
                branch TEXT NOT NULL,
                base_ref TEXT NOT NULL,
                head_sha TEXT NOT NULL,
                managed INTEGER NOT NULL,
                owner_kind TEXT NOT NULL,
                owner_id TEXT,
                state TEXT NOT NULL,
                source_pr INTEGER,
                target_pr INTEGER,
                deployment_state TEXT NOT NULL,
                retain INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                removed_at TEXT,
                version INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        connection.execute(
            """
            INSERT INTO worktree_leases (
                id, repository_id, repository_name, repository_root, worktree_path,
                initiative, purpose, branch, base_ref, head_sha, managed, owner_kind,
                owner_id, state, source_pr, target_pr, deployment_state, retain,
                created_at, last_used_at, updated_at, removed_at, version
            ) VALUES (
                :id, :repository_id, :repository_name, :repository_root, :worktree_path,
                :initiative, :purpose, :branch, :base_ref, :head_sha, :managed,
                :owner_kind, :owner_id, :state, :source_pr, :target_pr,
                :deployment_state, :retain, :created_at, :last_used_at, :updated_at,
                :removed_at, :version
            )
            """,
            {
                "id": "legacy-lease",
                "repository_id": "repo-1",
                "repository_name": "demo",
                "repository_root": str(tmp_path / "repo"),
                "worktree_path": str(tmp_path / "cache" / "legacy"),
                "initiative": "legacy",
                "purpose": "feature",
                "branch": "awf/legacy/feature",
                "base_ref": "origin/staging",
                "head_sha": "a" * 40,
                "managed": 1,
                "owner_kind": "awf",
                "owner_id": None,
                "state": "ACTIVE",
                "source_pr": None,
                "target_pr": None,
                "deployment_state": "not_required",
                "retain": 0,
                "created_at": "2030-01-02T03:04:05+00:00",
                "last_used_at": "2030-01-02T03:04:05+00:00",
                "updated_at": "2030-01-02T03:04:05+00:00",
                "removed_at": None,
                "version": 0,
            },
        )

    registry = WorktreeRegistry(db_path)
    registry.ensure()

    loaded = registry.get_lease("legacy-lease")

    assert loaded is not None
    assert loaded.promotion_mode is PromotionMode.EXACT
    assert loaded.resolution_state is ResolutionState.NONE
    assert loaded.source_base_sha is None
    assert loaded.source_head_sha is None
    assert loaded.target_base_sha is None
    assert loaded.reviewed_paths == ()
    assert loaded.conflicted_paths == ()


def test_transition_updates_resolution_metadata_with_compare_and_swap(
    tmp_path: Path,
) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(
        replace(
            lease(tmp_path),
            promotion_mode=PromotionMode.OUT_OF_ORDER,
            resolution_state=ResolutionState.PENDING,
            conflicted_paths=("src/a.py",),
            protected_index_entries=(("src/a.py", ("100644", "d" * 40)),),
        )
    )

    updated = registry.transition(
        created.id,
        LeaseState.ACTIVE,
        expected_version=created.version,
        resolution_state=ResolutionState.MANUAL_REVIEWED,
        conflicted_paths=(),
        protected_index_entries=(("src/a.py", None),),
    )

    assert updated.resolution_state is ResolutionState.MANUAL_REVIEWED
    assert updated.conflicted_paths == ()
    assert updated.protected_index_entries == (("src/a.py", None),)
    assert registry.get_lease(created.id) == updated


@pytest.mark.parametrize(
    ("field", "metadata"),
    [
        ("reviewed_paths", "not-json"),
        ("reviewed_paths", '["src/b.py","src/a.py"]'),
        ("reviewed_paths", '["src/a.py","src/a.py"]'),
        ("conflicted_paths", '["src/a.py",1]'),
    ],
)
def test_registry_rejects_invalid_path_metadata(
    tmp_path: Path, field: str, metadata: str
) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))
    with sqlite3.connect(registry.db_path) as connection:
        connection.execute(
            f"UPDATE worktree_leases SET {field} = ? WHERE id = ?",
            (metadata, created.id),
        )

    with pytest.raises(ValueError, match=field):
        registry.get_lease(created.id)


def test_registry_rejects_blob_path_metadata(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))
    with sqlite3.connect(registry.db_path) as connection:
        connection.execute(
            "UPDATE worktree_leases SET reviewed_paths = ? WHERE id = ?",
            (sqlite3.Binary(b'["src/a.py"]'), created.id),
        )

    with pytest.raises(ValueError, match="reviewed_paths"):
        registry.get_lease(created.id)


@pytest.mark.parametrize(
    "name",
    [
        "promotion_mode",
        "resolution_state",
        "source_base_sha",
        "source_head_sha",
        "target_base_sha",
        "reviewed_paths",
        "conflicted_paths",
    ],
)
def test_registry_rejects_promotion_metadata_filters(tmp_path: Path, name: str) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")

    with pytest.raises(ValueError, match="unsupported lease filter"):
        registry.list_leases(**{name: None})


def test_ensure_resumes_and_repeats_a_partial_legacy_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "worktrees.sqlite3"
    _create_partial_legacy_lease_table(db_path)
    registry = WorktreeRegistry(db_path)

    registry.ensure()
    registry.ensure()

    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(worktree_leases)")
        }
    assert {
        "promotion_mode",
        "resolution_state",
        "source_base_sha",
        "source_head_sha",
        "target_base_sha",
        "reviewed_paths",
        "conflicted_paths",
        "protected_index_entries",
    } <= columns


def test_ensure_migrates_legacy_schema_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "worktrees.sqlite3"
    _create_partial_legacy_lease_table(db_path)
    registry = WorktreeRegistry(db_path)
    actual_connect = registry._connect
    gate = EnsureMigrationGate()

    def connect() -> PausingMigrationConnection:
        return PausingMigrationConnection(actual_connect(), gate)

    monkeypatch.setattr(registry, "_connect", connect)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(registry.ensure)
        assert gate.first_pragma.wait(timeout=1)
        second = executor.submit(registry.ensure)
        try:
            assert not gate.second_pragma.wait(timeout=1)
        finally:
            gate.release.set()
        first.result()
        second.result()


def test_registry_rejects_two_active_leases_for_same_identity(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    registry.create_lease(lease(tmp_path))

    with pytest.raises(ValueError, match="active lease already exists"):
        registry.create_lease(lease(tmp_path))


def test_removed_lease_does_not_block_a_replacement(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    first = registry.create_lease(lease(tmp_path))
    registry.transition(first.id, LeaseState.REMOVED, expected_version=first.version)

    replacement = lease(tmp_path, worktree_name="reward-widget-replacement")
    second = registry.create_lease(replacement)

    assert second.id != first.id
    assert second.worktree_path == replacement.worktree_path
    with pytest.raises(
        sqlite3.IntegrityError, match="worktree_leases.worktree_path"
    ):
        registry.create_lease(
            lease(
                tmp_path,
                initiative="another-initiative",
                worktree_name="reward-widget",
            )
        )


def test_transition_is_compare_and_swap_and_appends_event(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))

    updated = registry.transition(
        created.id,
        LeaseState.PR_OPEN,
        expected_version=created.version,
        summary="target PR opened",
        pr_number=42,
    )

    assert updated.version == created.version + 1
    assert updated.target_pr == 42
    assert registry.list_events(created.id)[-1].to_state is LeaseState.PR_OPEN
    with pytest.raises(RuntimeError, match="lease changed concurrently"):
        registry.transition(created.id, LeaseState.MERGED, expected_version=created.version)



def test_cleanup_reservation_is_cas_guarded_and_completes_removal(
    tmp_path: Path,
) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))

    reservation = registry.reserve_cleanup(
        created.id,
        expected_version=created.version,
        branch_sha="a" * 40,
    )

    assert reservation.lease_id == created.id
    assert reservation.reserved_version == created.version + 1
    with pytest.raises(RuntimeError, match="cleanup is reserved"):
        registry.transition(
            created.id,
            LeaseState.PR_OPEN,
            expected_version=reservation.reserved_version,
        )
    removed = registry.complete_cleanup(
        created.id, expected_version=reservation.reserved_version
    )

    assert removed.state is LeaseState.REMOVED
    assert registry.get_cleanup_reservation(created.id) is None


def test_releasing_cleanup_reservation_allows_later_refresh_transition(
    tmp_path: Path,
) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))
    reservation = registry.reserve_cleanup(
        created.id,
        expected_version=created.version,
        branch_sha="a" * 40,
    )

    registry.release_cleanup_reservation(
        created.id, expected_version=reservation.reserved_version
    )
    refreshed = registry.transition(
        created.id,
        LeaseState.PR_OPEN,
        expected_version=reservation.reserved_version + 1,
    )

    assert refreshed.state is LeaseState.PR_OPEN


def test_cleanup_warning_event_preserves_the_reservation_version(
    tmp_path: Path,
) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))
    reservation = registry.reserve_cleanup(
        created.id,
        expected_version=created.version,
        branch_sha="a" * 40,
    )

    registry.record_cleanup_event(
        created.id, event_type="remote_branch_cleanup_failed", summary="branch changed"
    )

    assert registry.get_lease(created.id).version == reservation.reserved_version
    assert registry.list_events(created.id)[-1].event_type == "remote_branch_cleanup_failed"

def test_event_summary_is_bounded_to_512_utf8_bytes(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))

    registry.transition(
        created.id,
        LeaseState.PR_OPEN,
        expected_version=created.version,
        summary="한" * 300,
    )

    stored = registry.list_events(created.id)[-1].summary
    assert len(stored.encode("utf-8")) <= 512
    assert stored == "한" * 170


def test_touch_updates_only_usage_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))
    timestamp = "2030-01-02T03:04:05+00:00"
    monkeypatch.setattr("awf.worktrees.registry.now_iso", lambda: timestamp)

    updated = registry.touch(
        created.id,
        expected_version=created.version,
        head_sha="b" * 40,
    )

    expected = created.to_dict()
    expected.update(
        head_sha="b" * 40,
        last_used_at=timestamp,
        updated_at=timestamp,
        version=created.version + 1,
    )
    assert updated.to_dict() == expected


def test_touch_rejects_a_stale_version(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))
    registry.touch(created.id, expected_version=created.version, head_sha="b" * 40)

    with pytest.raises(RuntimeError, match="lease changed concurrently"):
        registry.touch(created.id, expected_version=created.version, head_sha="c" * 40)


def test_registry_closes_each_sqlite_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    actual_connect = registry._connect
    connections: list[TrackingConnection] = []

    def connect() -> TrackingConnection:
        tracked = TrackingConnection(actual_connect())
        connections.append(tracked)
        return tracked

    monkeypatch.setattr(registry, "_connect", connect)
    registry.ensure()
    created = registry.create_lease(lease(tmp_path))

    assert registry.get_lease(created.id) == created
    assert registry.find_active(
        created.repository_id, created.initiative, created.purpose
    ) == created
    assert registry.list_leases() == [created]
    assert registry.list_events(created.id) == []
    assert connections
    assert all(connection.closed for connection in connections)


class TrackingConnection:
    def __init__(self, connection: object) -> None:
        self.connection = connection
        self.closed = False

    def __enter__(self) -> "TrackingConnection":
        self.connection.__enter__()
        return self

    def __exit__(self, *args: object) -> bool | None:
        return self.connection.__exit__(*args)

    def close(self) -> None:
        self.closed = True
        self.connection.close()

    def __getattr__(self, name: str) -> object:
        return getattr(self.connection, name)


def _create_partial_legacy_lease_table(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE worktree_leases (
                id TEXT PRIMARY KEY,
                repository_id TEXT NOT NULL,
                initiative TEXT NOT NULL,
                purpose TEXT NOT NULL,
                state TEXT NOT NULL,
                promotion_mode TEXT NOT NULL DEFAULT 'exact'
            );
            """
        )


class EnsureMigrationGate:
    def __init__(self) -> None:
        self.first_pragma = Event()
        self.second_pragma = Event()
        self.release = Event()
        self._lock = Lock()
        self._pragma_count = 0

    def pause_after_pragma(self) -> None:
        with self._lock:
            self._pragma_count += 1
            pragma_count = self._pragma_count
        if pragma_count == 1:
            self.first_pragma.set()
        elif pragma_count == 2:
            self.second_pragma.set()
        else:
            return
        if not self.release.wait(timeout=5):
            raise RuntimeError("migration gate timed out")


class PausingMigrationConnection:
    def __init__(
        self, connection: sqlite3.Connection, gate: EnsureMigrationGate
    ) -> None:
        self.connection = connection
        self.gate = gate

    def __enter__(self) -> "PausingMigrationConnection":
        self.connection.__enter__()
        return self

    def __exit__(self, *args: object) -> bool | None:
        return self.connection.__exit__(*args)

    def close(self) -> None:
        self.connection.close()

    def execute(self, statement: str, parameters: object = ()) -> object:
        result = self.connection.execute(statement, parameters)
        if statement == "PRAGMA table_info(worktree_leases)":
            self.gate.pause_after_pragma()
        return result

    def __getattr__(self, name: str) -> object:
        return getattr(self.connection, name)


def test_command_result_has_versioned_json_envelope() -> None:
    result = CommandResult.ok("wt.status", decision="no_op")

    payload = result.to_dict()

    assert payload["schema_version"] == 1
    assert payload["command"] == "wt.status"
    assert payload["status"] == "ok"
    assert payload["decision"] == "no_op"
    assert payload["actions"] == []
    assert payload["blockers"] == []
    assert payload["warnings"] == []


def test_command_result_external_error_uses_external_failure_exit_code() -> None:
    result = CommandResult.external_error(
        "wt.promote",
        code="github_unavailable",
        message="gh authentication failed",
    )

    payload = result.to_dict()

    assert payload["status"] == "error"
    assert payload["decision"] == "blocked"
    assert payload["exit_code"] == 4
    assert payload["blockers"] == [
        {"code": "github_unavailable", "message": "gh authentication failed"}
    ]
