from __future__ import annotations

import sqlite3

from pathlib import Path

import pytest

from awf.worktrees.models import (
    CommandResult,
    DeploymentState,
    Lease,
    LeaseState,
    Purpose,
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
    )


def test_registry_creates_and_round_trips_a_lease(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))

    loaded = registry.get_lease(created.id)

    assert loaded == created
    assert loaded.state is LeaseState.ACTIVE
    assert loaded.deployment_state is DeploymentState.NOT_REQUIRED


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
