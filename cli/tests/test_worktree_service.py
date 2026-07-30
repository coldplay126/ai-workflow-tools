from __future__ import annotations

import subprocess
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from awf.worktrees.config import WorktreeConfig
from awf.worktrees.git import GitClient
from awf.worktrees.models import Lease, Purpose
from awf.worktrees.registry import WorktreeRegistry
from awf.worktrees.service import WorktreeService
from worktree_fixtures import make_repository


@dataclass
class Harness:
    repo: Path
    git: GitClient
    registry: WorktreeRegistry
    cache_dir: Path
    state_dir: Path
    lock_dir: Path
    service: WorktreeService

    @classmethod
    def create(cls, tmp_path: Path) -> Harness:
        repo = make_repository(tmp_path)
        git = GitClient(repo)
        registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
        cache_dir = tmp_path / "cache"
        state_dir = tmp_path / "state"
        lock_dir = tmp_path / "locks"
        return cls(
            repo=repo,
            git=git,
            registry=registry,
            cache_dir=cache_dir,
            state_dir=state_dir,
            lock_dir=lock_dir,
            service=WorktreeService(
                registry,
                git,
                config=WorktreeConfig(default_base="staging"),
                cache_dir=cache_dir,
                state_dir=state_dir,
                lock_dir=lock_dir,
            ),
        )

    def acquire(self, initiative: str):
        return self.service.acquire(
            initiative=initiative,
            purpose=Purpose.FEATURE,
            base=None,
            branch=None,
            owner_id="session-1",
            apply=True,
        )

    def enable_prepare_command(self, log: Path) -> None:
        script = (
            "from pathlib import Path; "
            f"Path({str(log)!r}).open('a', encoding='utf-8').write('prepared\\n')"
        )
        self.service = WorktreeService(
            self.registry,
            self.git,
            config=WorktreeConfig(
                default_base="staging",
                prepare_inputs=("README.txt",),
                prepare_command=(sys.executable, "-c", script),
            ),
            cache_dir=self.cache_dir,
            state_dir=self.state_dir,
            lock_dir=self.lock_dir,
        )

@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness.create(tmp_path)


def test_acquire_previews_without_creating_a_worktree(harness: Harness) -> None:
    result = harness.service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base="staging",
        branch=None,
        owner_id="session-1",
        apply=False,
    )

    assert result.decision == "preview"
    assert result.actions[0]["kind"] == "create_worktree"
    assert len(harness.git.list_worktrees()) == 1
    assert harness.registry.list_leases() == []
    assert not harness.cache_dir.exists()


def test_acquire_creates_one_managed_worktree(harness: Harness) -> None:
    result = harness.service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base="staging",
        branch=None,
        owner_id="session-1",
        apply=True,
    )

    assert result.decision == "ready"
    assert result.lease is not None
    assert result.lease.branch == "awf/reward-widget/feature"
    assert result.lease.worktree_path.exists()
    assert len(harness.git.list_worktrees()) == 2


def test_acquire_reuses_the_exact_active_lease(harness: Harness) -> None:
    first = harness.acquire("reward-widget")
    second = harness.acquire("reward-widget")

    assert second.decision == "reuse"
    assert second.lease is not None
    assert first.lease is not None
    assert second.lease.id == first.lease.id
    assert len(harness.git.list_worktrees()) == 2


def test_acquire_blocks_when_registered_path_is_missing(harness: Harness) -> None:
    first = harness.acquire("reward-widget")
    assert first.lease is not None
    subprocess.run(
        ["git", "worktree", "remove", str(first.lease.worktree_path)],
        cwd=harness.repo,
        check=True,
    )

    result = harness.acquire("reward-widget")

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "orphaned_lease"


def test_acquire_prepares_new_worktree_and_reuses_matching_prepare_key(
    harness: Harness, tmp_path: Path
) -> None:
    log = tmp_path / "prepare.log"
    harness.enable_prepare_command(log)

    first = harness.acquire("reward-widget")
    second = harness.acquire("reward-widget")

    assert first.decision == "ready"
    assert second.decision == "reuse"
    assert log.read_text(encoding="utf-8").splitlines() == ["prepared"]


def test_acquire_blocks_for_a_conflicting_requested_branch(harness: Harness) -> None:
    harness.acquire("reward-widget")

    result = harness.service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base=None,
        branch="awf/reward-widget/alternate",
        owner_id="session-1",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "lease_conflict"



def test_acquire_removes_a_clean_worktree_when_registry_insert_fails(
    tmp_path: Path,
) -> None:
    class FailingRegistry(WorktreeRegistry):
        def create_lease(self, lease: Lease) -> Lease:
            raise sqlite3.IntegrityError("registry is unavailable")

    repo = make_repository(tmp_path)
    git = GitClient(repo)
    service = WorktreeService(
        FailingRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        git,
        config=WorktreeConfig(default_base="staging"),
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        lock_dir=tmp_path / "locks",
    )

    result = service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base=None,
        branch=None,
        owner_id="session-1",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "registry_conflict"
    assert len(git.list_worktrees()) == 1


def test_acquire_removes_a_clean_worktree_when_registry_filesystem_insert_fails(
    tmp_path: Path,
) -> None:
    class FailingRegistry(WorktreeRegistry):
        def create_lease(self, lease: Lease) -> Lease:
            raise PermissionError("state directory is read-only")

    repo = make_repository(tmp_path)
    git = GitClient(repo)
    service = WorktreeService(
        FailingRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        git,
        config=WorktreeConfig(default_base="staging"),
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        lock_dir=tmp_path / "locks",
    )

    result = service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base=None,
        branch=None,
        owner_id="session-1",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "registry_conflict"
    assert len(git.list_worktrees()) == 1


def test_acquire_preserves_a_dirty_worktree_when_registry_filesystem_insert_fails(
    tmp_path: Path,
) -> None:
    class FailingRegistry(WorktreeRegistry):
        def create_lease(self, lease: Lease) -> Lease:
            (lease.worktree_path / "recovery.txt").write_text(
                "keep this worktree\n",
                encoding="utf-8",
            )
            raise PermissionError("state directory is read-only")

    repo = make_repository(tmp_path)
    git = GitClient(repo)
    service = WorktreeService(
        FailingRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        git,
        config=WorktreeConfig(default_base="staging"),
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        lock_dir=tmp_path / "locks",
    )

    result = service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base=None,
        branch=None,
        owner_id="session-1",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "registry_conflict"
    assert len(git.list_worktrees()) == 2