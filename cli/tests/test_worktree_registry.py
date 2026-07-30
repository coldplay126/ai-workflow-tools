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
