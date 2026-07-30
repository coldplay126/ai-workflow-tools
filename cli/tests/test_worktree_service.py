from __future__ import annotations

import subprocess
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from awf.worktrees.config import WorktreeConfig
from awf.worktrees.git import GitClient, GitError, GitWorktree
from awf.worktrees.models import Lease, LeaseState, Purpose
from awf.worktrees.registry import WorktreeRegistry
from awf.worktrees.service import WorktreeService
from worktree_fixtures import git as git_command
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

    def enable_prepare_command(self, log: Path, message: str = "prepared") -> None:
        script = (
            "from pathlib import Path; "
            f"Path({str(log)!r}).open('a', encoding='utf-8').write({message + chr(10)!r})"
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
    def make_external_worktree(self, branch: str) -> Path:
        external = self.repo.parent / branch
        git_command(
            self.repo,
            "worktree",
            "add",
            "-q",
            "-b",
            branch,
            str(external),
            "staging",
        )
        return external

    def import_external(self, branch: str) -> Lease:
        external = self.make_external_worktree(branch)
        result = self.service.import_root(self.repo.parent, apply=True)
        return next(lease for lease in result.leases if lease.worktree_path == external)

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


def test_acquire_retry_succeeds_after_clean_registry_recovery(
    tmp_path: Path,
) -> None:
    class FlakyRegistry(WorktreeRegistry):
        failed = False

        def create_lease(self, lease: Lease) -> Lease:
            if not self.failed:
                self.failed = True
                raise sqlite3.IntegrityError("registry is unavailable")
            return super().create_lease(lease)

    repo = make_repository(tmp_path)
    git = GitClient(repo)
    service = WorktreeService(
        FlakyRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        git,
        config=WorktreeConfig(default_base="staging"),
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        lock_dir=tmp_path / "locks",
    )
    request = {
        "initiative": "reward-widget",
        "purpose": Purpose.FEATURE,
        "base": None,
        "branch": None,
        "owner_id": "session-1",
        "apply": True,
    }

    failed = service.acquire(**request)
    recovered = service.acquire(**request)

    assert failed.status == "blocked"
    assert recovered.decision == "ready"
    assert len(git.list_worktrees()) == 2


@pytest.mark.parametrize(
    "base",
    [
        "main:refs/heads/unrelated",
        "+main",
        "main*",
        "main^{commit}",
    ],
)
def test_acquire_rejects_unsafe_base_before_fetch(
    tmp_path: Path, base: str
) -> None:
    class NoFetchGit(GitClient):
        def fetch_ref(self, ref: str) -> str:
            raise AssertionError(f"fetch_ref unexpectedly called with {ref!r}")

    repo = make_repository(tmp_path)
    git = NoFetchGit(repo)
    service = WorktreeService(
        WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        git,
        config=WorktreeConfig(),
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        lock_dir=tmp_path / "locks",
    )

    with pytest.raises(ValueError, match="invalid base ref"):
        service.acquire(
            initiative="reward-widget",
            purpose=Purpose.FEATURE,
            base=base,
            branch=None,
            owner_id="session-1",
            apply=False,
        )


def test_acquire_reports_branch_cleanup_failure_after_registry_insert_failure(
    tmp_path: Path,
) -> None:
    class FailingRegistry(WorktreeRegistry):
        def create_lease(self, lease: Lease) -> Lease:
            raise sqlite3.IntegrityError("registry is unavailable")

    class BranchDeleteFailingGit(GitClient):
        def delete_branch_if_at(self, branch: str, expected_sha: str) -> None:
            raise GitError(f"could not delete {branch}")

    repo = make_repository(tmp_path)
    git = BranchDeleteFailingGit(repo)
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
    assert result.blockers[0]["code"] == "registry_recovery_failed"
    assert "branch cleanup failed" in result.blockers[0]["message"]
    assert len(git.list_worktrees()) == 1


def test_acquire_reprepares_when_the_prepare_key_changes(
    harness: Harness, tmp_path: Path
) -> None:
    log = tmp_path / "prepare.log"
    harness.enable_prepare_command(log, "first")

    first = harness.acquire("reward-widget")
    harness.enable_prepare_command(log, "second")
    second = harness.acquire("reward-widget")

    assert first.decision == "ready"
    assert second.decision == "reuse"
    assert log.read_text(encoding="utf-8").splitlines() == ["first", "second"]


def test_acquire_blocks_prepare_failure_without_writing_a_marker(
    harness: Harness,
) -> None:
    harness.service = WorktreeService(
        harness.registry,
        harness.git,
        config=WorktreeConfig(
            default_base="staging",
            prepare_command=(sys.executable, "-c", "import sys; sys.exit(7)"),
        ),
        cache_dir=harness.cache_dir,
        state_dir=harness.state_dir,
        lock_dir=harness.lock_dir,
    )

    result = harness.acquire("reward-widget")

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "prepare_failed"
    assert result.lease is not None
    assert result.lease.state is LeaseState.BLOCKED
    assert not (harness.state_dir / "prepare" / f"{result.lease.id}.json").exists()
    assert len(harness.git.list_worktrees()) == 2


def test_acquire_retry_succeeds_after_divergent_base_registry_recovery(
    tmp_path: Path,
) -> None:
    class FlakyRegistry(WorktreeRegistry):
        failed = False

        def create_lease(self, lease: Lease) -> Lease:
            if not self.failed:
                self.failed = True
                raise sqlite3.IntegrityError("registry is unavailable")
            return super().create_lease(lease)

    repo = make_repository(tmp_path)
    git_command(repo, "checkout", "-q", "-b", "release")
    (repo / "release.txt").write_text("release\n", encoding="utf-8")
    git_command(repo, "add", "release.txt")
    git_command(repo, "commit", "-q", "-m", "release")
    git_command(repo, "push", "-q", "-u", "origin", "release")
    git_command(repo, "checkout", "-q", "staging")
    git = GitClient(repo)
    service = WorktreeService(
        FlakyRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        git,
        config=WorktreeConfig(default_base="staging"),
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        lock_dir=tmp_path / "locks",
    )
    request = {
        "initiative": "reward-widget",
        "purpose": Purpose.FEATURE,
        "base": "release",
        "branch": None,
        "owner_id": "session-1",
        "apply": True,
    }

    failed = service.acquire(**request)
    recovered = service.acquire(**request)

    assert failed.status == "blocked"
    assert recovered.decision == "ready"
    assert len(git.list_worktrees()) == 2


def test_acquire_preserves_a_clean_worktree_with_a_new_commit_after_registry_failure(
    tmp_path: Path,
) -> None:
    class CommittingRegistry(WorktreeRegistry):
        def create_lease(self, lease: Lease) -> Lease:
            (lease.worktree_path / "recovery.txt").write_text(
                "retain this commit\n",
                encoding="utf-8",
            )
            git_command(lease.worktree_path, "add", "recovery.txt")
            git_command(lease.worktree_path, "commit", "-q", "-m", "recovery")
            raise sqlite3.IntegrityError("registry is unavailable")

    repo = make_repository(tmp_path)
    git = GitClient(repo)
    service = WorktreeService(
        CommittingRegistry(tmp_path / "state" / "worktrees.sqlite3"),
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
    assert result.blockers[0]["code"] == "registry_recovery_failed"
    assert "head changed" in result.blockers[0]["message"]
    assert result.lease is not None
    assert result.lease.worktree_path.is_dir()
    assert git_command(repo, "rev-parse", result.lease.branch) == git_command(
        result.lease.worktree_path, "rev-parse", "HEAD"
    )


def test_acquire_preserves_branch_when_rollback_cas_detects_a_race(
    tmp_path: Path,
) -> None:
    class FailingRegistry(WorktreeRegistry):
        def create_lease(self, lease: Lease) -> Lease:
            raise sqlite3.IntegrityError("registry is unavailable")

    class RacingGit(GitClient):
        def delete_branch_if_at(self, branch: str, expected_sha: str) -> None:
            git_command(
                self.cwd,
                "update-ref",
                f"refs/heads/{branch}",
                self.head_sha(),
            )
            super().delete_branch_if_at(branch, expected_sha)

    repo = make_repository(tmp_path)
    git_command(repo, "checkout", "-q", "-b", "release")
    (repo / "release.txt").write_text("release\n", encoding="utf-8")
    git_command(repo, "add", "release.txt")
    git_command(repo, "commit", "-q", "-m", "release")
    git_command(repo, "push", "-q", "-u", "origin", "release")
    git_command(repo, "checkout", "-q", "staging")
    git = RacingGit(repo)
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
        base="release",
        branch=None,
        owner_id="session-1",
        apply=True,
    )

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "registry_recovery_failed"
    assert "branch cleanup failed" in result.blockers[0]["message"]
    assert len(git.list_worktrees()) == 1
    assert git_command(repo, "rev-parse", "awf/reward-widget/feature") == git_command(
        repo, "rev-parse", "staging"
    )


def test_import_registers_existing_worktree_as_unmanaged(harness: Harness) -> None:
    external = harness.make_external_worktree("legacy-release")

    result = harness.service.import_root(harness.repo.parent, apply=True)

    imported = next(item for item in result.leases if item.worktree_path == external)
    assert imported.managed is False
    assert imported.owner_kind == "imported"
    assert imported.purpose is Purpose.SCRATCH


def test_import_dry_run_writes_nothing(harness: Harness) -> None:
    harness.make_external_worktree("legacy-release")

    result = harness.service.import_root(harness.repo.parent, apply=False)

    assert result.decision == "preview"
    assert harness.registry.list_leases() == []


def test_adopt_refuses_dirty_imported_worktree(harness: Harness) -> None:
    imported = harness.import_external("legacy-release")
    (imported.worktree_path / "dirty.txt").write_text("dirty", encoding="utf-8")

    result = harness.service.adopt(imported.id, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "dirty_worktree"
    assert harness.registry.get_lease(imported.id).managed is False


def test_adopt_marks_clean_imported_worktree_managed(harness: Harness) -> None:
    imported = harness.import_external("legacy-release")

    result = harness.service.adopt(imported.id, apply=True)

    assert result.decision == "ready"
    assert result.lease.managed is True
    assert result.lease.owner_kind == "imported"


def test_doctor_reports_registered_head_dirty_cache_and_skill_link_mismatches(
    harness: Harness, tmp_path: Path
) -> None:
    imported = harness.import_external("legacy-release")
    stale = harness.registry.transition(
        imported.id,
        imported.state,
        expected_version=imported.version,
        head_sha="0" * 40,
    )
    (imported.worktree_path / "dirty.txt").write_text("dirty", encoding="utf-8")
    abandoned_cache_dir = harness.cache_dir / harness.repo.name / "abandoned"
    abandoned_cache_dir.mkdir(parents=True)
    harness.registry.create_lease(
        Lease.new(
            repository_id=harness.git.repository_id(),
            repository_name=harness.git.repository_name(),
            repository_root=harness.git.repository_root(),
            worktree_path=abandoned_cache_dir,
            initiative="cache-abandoned",
            purpose=Purpose.FEATURE,
            branch="awf/cache-abandoned/feature",
            base_ref="origin/staging",
            head_sha=harness.git.head_sha(),
            managed=True,
            owner_kind="awf",
        )
    )
    skill_source = tmp_path / "release-worktree-lifecycle"
    skill_source.mkdir()
    home_dir = tmp_path / "home"
    claude_link = home_dir / ".claude" / "skills" / "release-worktree-lifecycle"
    claude_link.parent.mkdir(parents=True)
    claude_link.symlink_to(skill_source, target_is_directory=True)
    wrong_target = tmp_path / "wrong-skill"
    wrong_target.mkdir()
    agents_link = home_dir / ".agents" / "skills" / "release-worktree-lifecycle"
    agents_link.parent.mkdir(parents=True)
    agents_link.symlink_to(wrong_target, target_is_directory=True)
    service = WorktreeService(
        harness.registry,
        harness.git,
        config=WorktreeConfig(default_base="staging"),
        cache_dir=harness.cache_dir,
        state_dir=harness.state_dir,
        lock_dir=harness.lock_dir,
        skill_source_dir=skill_source,
        home_dir=home_dir,
    )

    result = service.doctor()

    actions_by_kind = {
        action["kind"]: action for action in result.actions
    }
    assert actions_by_kind["head_mismatch"]["lease_id"] == stale.id
    assert actions_by_kind["dirty_worktree"]["path"] == str(
        imported.worktree_path
    )
    assert actions_by_kind["unregistered_cache_directory"]["path"] == str(
        abandoned_cache_dir
    )
    assert actions_by_kind["wrong_skill_link"]["path"] == str(agents_link)
    assert harness.registry.get_lease(imported.id).version == stale.version


def test_adopt_preview_does_not_create_lock_or_registry_state(
    harness: Harness
) -> None:
    external = harness.make_external_worktree("legacy-release")
    lease = harness.registry.create_lease(
        Lease.new(
            repository_id=harness.git.repository_id(),
            repository_name=harness.git.repository_name(),
            repository_root=harness.git.repository_root(),
            worktree_path=external,
            initiative="import-legacy-release-12345678",
            purpose=Purpose.SCRATCH,
            branch="legacy-release",
            base_ref="legacy-release",
            head_sha=harness.git.head_sha(external),
            managed=False,
            owner_kind="imported",
        )
    )
    assert not harness.lock_dir.exists()

    result = harness.service.adopt(lease.id, apply=False)

    assert result.decision == "preview"
    assert result.actions[0]["kind"] == "adopt"
    assert not harness.lock_dir.exists()
    assert harness.registry.get_lease(lease.id).version == lease.version


def test_import_keeps_exact_initiative_and_skips_identity_collisions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repos"
    candidate = root / "repo"
    (candidate / ".git").mkdir(parents=True)
    first_path = candidate / "first"
    second_path = candidate / "second"
    first_path.mkdir()
    second_path.mkdir()
    head_sha = "0123456789abcdef0123456789abcdef01234567"

    class FakeGit:
        def repository_id(self) -> str:
            return "repository-id"

        def repository_name(self) -> str:
            return "repo"

        def repository_root(self) -> Path:
            return candidate

        def list_worktrees(self) -> tuple[GitWorktree, ...]:
            return (
                GitWorktree(first_path, head_sha, "topic/release"),
                GitWorktree(second_path, head_sha, "topic/release"),
            )

        def status_porcelain(self, cwd: Path) -> tuple[str, ...]:
            return ()

    registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
    service = WorktreeService(
        registry,
        None,
        config=WorktreeConfig(),
        git_factory=lambda _: FakeGit(),
        lock_dir=tmp_path / "locks",
    )

    preview = service.import_root(root, apply=False)
    result = service.import_root(root, apply=True)

    base = "import-topic-release-01234567"
    assert [lease.initiative for lease in preview.leases] == [base]
    assert [lease.worktree_path for lease in preview.leases] == [first_path]
    assert [lease.worktree_path for lease in result.leases] == [first_path]
    assert any(
        action == {
            "kind": "skipped_identity_collision",
            "path": str(second_path),
            "repository_id": "repository-id",
            "initiative": base,
            "purpose": "scratch",
        }
        for action in preview.actions
    )
    assert [lease.worktree_path for lease in registry.list_leases()] == [first_path]


def test_doctor_reports_dirty_detached_registered_worktree(
    harness: Harness,
) -> None:
    imported = harness.import_external("legacy-release")
    git_command(imported.worktree_path, "checkout", "-q", "--detach")
    (imported.worktree_path / "dirty.txt").write_text("dirty", encoding="utf-8")

    result = harness.service.doctor()

    assert any(
        action["kind"] == "dirty_worktree"
        and action["path"] == str(imported.worktree_path)
        for action in result.actions
    )


def test_adopt_blocks_branch_checked_out_at_another_registered_path(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "registered"
    duplicate_path = tmp_path / "duplicate"
    worktree_path.mkdir()
    duplicate_path.mkdir()
    head_sha = "1234567890abcdef1234567890abcdef12345678"

    class FakeGit:
        def repository_id(self) -> str:
            return "repository-id"

        def list_worktrees(self) -> tuple[GitWorktree, ...]:
            return (
                GitWorktree(worktree_path, head_sha, "topic/release"),
                GitWorktree(duplicate_path, head_sha, "topic/release"),
            )

        def head_sha(self, cwd: Path) -> str:
            return head_sha

        def status_porcelain(self, cwd: Path) -> tuple[str, ...]:
            return ()

    registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
    lease = registry.create_lease(
        Lease.new(
            repository_id="repository-id",
            repository_name="repo",
            repository_root=tmp_path,
            worktree_path=worktree_path,
            initiative="import-topic-release-12345678",
            purpose=Purpose.SCRATCH,
            branch="topic/release",
            base_ref="topic/release",
            head_sha=head_sha,
            managed=False,
            owner_kind="imported",
        )
    )
    service = WorktreeService(
        registry,
        FakeGit(),
        config=WorktreeConfig(),
        lock_dir=tmp_path / "locks",
    )

    result = service.adopt(lease.id, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "branch_conflict"
    assert registry.get_lease(lease.id).managed is False


def test_import_uses_non_bare_worktree_metadata_when_primary_is_bare(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repos"
    candidate = root / "repo"
    (candidate / ".git").mkdir(parents=True)
    bare_path = tmp_path / "origin.git"
    bare_path.mkdir()
    worktree_path = candidate / "linked"
    worktree_path.mkdir()
    head_sha = "abcdef0123456789abcdef0123456789abcdef01"

    class CandidateGit:
        def repository_id(self) -> str:
            return "repository-id"

        def repository_name(self) -> str:
            return "repo"

        def repository_root(self) -> Path:
            return candidate

        def list_worktrees(self) -> tuple[GitWorktree, ...]:
            return (
                GitWorktree(bare_path, None, None, bare=True),
                GitWorktree(worktree_path, head_sha, "topic/release"),
            )

        def status_porcelain(self, cwd: Path) -> tuple[str, ...]:
            return ()

    class BareGit:
        def repository_id(self) -> str:
            raise AssertionError("bare worktree metadata must not be used")

    candidate_git = CandidateGit()
    bare_git = BareGit()
    service = WorktreeService(
        WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        None,
        config=WorktreeConfig(),
        git_factory=lambda path: (
            bare_git if path.resolve() == bare_path.resolve() else candidate_git
        ),
        lock_dir=tmp_path / "locks",
    )

    result = service.import_root(root, apply=True)

    assert [lease.worktree_path for lease in result.leases] == [worktree_path]
    assert result.leases[0].repository_root == candidate


def test_import_skips_prunable_and_missing_worktree_registrations(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repos"
    candidate = root / "repo"
    (candidate / ".git").mkdir(parents=True)
    valid_path = candidate / "valid"
    valid_path.mkdir()
    prunable_path = candidate / "prunable"
    missing_path = candidate / "missing"

    class FakeGit:
        def repository_id(self) -> str:
            return "repository-id"

        def repository_name(self) -> str:
            return "repo"

        def repository_root(self) -> Path:
            return candidate

        def list_worktrees(self) -> tuple[GitWorktree, ...]:
            return (
                GitWorktree(valid_path, "1111111111111111111111111111111111111111", "valid"),
                GitWorktree(
                    prunable_path,
                    "2222222222222222222222222222222222222222",
                    "prunable",
                    prunable="gitdir file is missing",
                ),
                GitWorktree(
                    missing_path,
                    "3333333333333333333333333333333333333333",
                    "missing",
                ),
            )

        def status_porcelain(self, cwd: Path) -> tuple[str, ...]:
            return ()

    service = WorktreeService(
        WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        None,
        config=WorktreeConfig(),
        git_factory=lambda _: FakeGit(),
        lock_dir=tmp_path / "locks",
    )

    result = service.import_root(root, apply=True)

    assert [lease.worktree_path for lease in result.leases] == [valid_path]


def test_doctor_reports_duplicate_branch_registrations(tmp_path: Path) -> None:
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    head_sha = "1234567890abcdef1234567890abcdef12345678"

    class FakeGit:
        def repository_id(self) -> str:
            return "repository-id"

        def repository_name(self) -> str:
            return "repo"

        def list_worktrees(self) -> tuple[GitWorktree, ...]:
            return (
                GitWorktree(first_path, head_sha, "topic/release"),
                GitWorktree(second_path, head_sha, "topic/release"),
            )

        def status_porcelain(self, cwd: Path) -> tuple[str, ...]:
            return ()

    registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
    for path, initiative in (
        (first_path, "import-first-12345678"),
        (second_path, "import-second-12345678"),
    ):
        registry.create_lease(
            Lease.new(
                repository_id="repository-id",
                repository_name="repo",
                repository_root=tmp_path,
                worktree_path=path,
                initiative=initiative,
                purpose=Purpose.SCRATCH,
                branch="topic/release",
                base_ref="topic/release",
                head_sha=head_sha,
                managed=False,
                owner_kind="imported",
            )
        )
    service = WorktreeService(
        registry,
        FakeGit(),
        config=WorktreeConfig(),
        lock_dir=tmp_path / "locks",
    )

    result = service.doctor()

    assert {
        "kind": "duplicate_branch",
        "branch": "topic/release",
        "paths": [str(first_path), str(second_path)],
    } in result.actions


def test_import_apply_revalidates_changed_worktree_under_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repos"
    worktree_path = root / "repo"
    (worktree_path / ".git").mkdir(parents=True)
    snapshot_head = "1111111111111111111111111111111111111111"
    current_head = "2222222222222222222222222222222222222222"

    class ChangingGit:
        list_calls = 0

        def repository_id(self) -> str:
            return "repository-id"

        def repository_name(self) -> str:
            return "repo"

        def repository_root(self) -> Path:
            return worktree_path

        def list_worktrees(self) -> tuple[GitWorktree, ...]:
            self.list_calls += 1
            head_sha = snapshot_head if self.list_calls == 1 else current_head
            return (GitWorktree(worktree_path, head_sha, "topic/release"),)

        def status_porcelain(self, cwd: Path) -> tuple[str, ...]:
            return () if self.list_calls == 1 else (" M changed.txt",)

    git = ChangingGit()
    service = WorktreeService(
        WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        None,
        config=WorktreeConfig(),
        git_factory=lambda _: git,
        lock_dir=tmp_path / "locks",
    )

    result = service.import_root(root, apply=True)

    assert git.list_calls >= 2
    assert result.leases[0].head_sha == current_head
    assert result.leases[0].state is LeaseState.DIRTY
    assert [
        action for action in result.actions if action["kind"] == "import_worktree"
    ] == [
        {
            "kind": "import_worktree",
            "path": str(worktree_path),
            "lease_id": result.leases[0].id,
        }
    ]


def test_import_apply_skips_worktree_that_disappears_before_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repos"
    worktree_path = root / "repo"
    (worktree_path / ".git").mkdir(parents=True)
    head_sha = "1111111111111111111111111111111111111111"

    class VanishingGit:
        list_calls = 0

        def repository_id(self) -> str:
            return "repository-id"

        def repository_name(self) -> str:
            return "repo"

        def repository_root(self) -> Path:
            return worktree_path

        def list_worktrees(self) -> tuple[GitWorktree, ...]:
            self.list_calls += 1
            return (
                (GitWorktree(worktree_path, head_sha, "topic/release"),)
                if self.list_calls == 1
                else ()
            )

        def status_porcelain(self, cwd: Path) -> tuple[str, ...]:
            return ()
    git = VanishingGit()

    service = WorktreeService(
        WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        None,
        config=WorktreeConfig(),
        git_factory=lambda _: git,
        lock_dir=tmp_path / "locks",
    )

    result = service.import_root(root, apply=True)

    assert result.leases == ()
    assert any(
        action["kind"] == "skipped_unavailable_worktree"
        and action["path"] == str(worktree_path)
        for action in result.actions
    )
    assert not any(
        action["kind"] == "import_worktree" for action in result.actions
    )


def test_import_apply_converts_active_identity_race_to_skipped_action(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repos"
    worktree_path = root / "repo"
    (worktree_path / ".git").mkdir(parents=True)
    head_sha = "1111111111111111111111111111111111111111"

    class FakeGit:
        def repository_id(self) -> str:
            return "repository-id"

        def repository_name(self) -> str:
            return "repo"

        def repository_root(self) -> Path:
            return worktree_path

        def list_worktrees(self) -> tuple[GitWorktree, ...]:
            return (GitWorktree(worktree_path, head_sha, "topic/release"),)

        def status_porcelain(self, cwd: Path) -> tuple[str, ...]:
            return ()

    competing_path = tmp_path / "competing"
    competing_path.mkdir()
    competing = Lease.new(
        repository_id="repository-id",
        repository_name="repo",
        repository_root=worktree_path,
        worktree_path=competing_path,
        initiative="import-topic-release-11111111",
        purpose=Purpose.SCRATCH,
        branch="topic/release",
        base_ref="topic/release",
        head_sha=head_sha,
        managed=False,
        owner_kind="imported",
    )

    class RacingRegistry(WorktreeRegistry):
        def find_active(
            self, repository_id: str, initiative: str, purpose: Purpose
        ) -> Lease | None:
            return competing

    registry = RacingRegistry(tmp_path / "state" / "worktrees.sqlite3")
    service = WorktreeService(
        registry,
        None,
        config=WorktreeConfig(),
        git_factory=lambda _: FakeGit(),
        lock_dir=tmp_path / "locks",
    )

    result = service.import_root(root, apply=True)

    assert result.leases == ()
    assert any(
        action["kind"] == "skipped_identity_collision"
        and action["path"] == str(worktree_path)
        for action in result.actions
    )
    assert not any(
        action["kind"] == "import_worktree" for action in result.actions
    )


def test_doctor_ignores_same_name_cache_child_from_other_repository(
    harness: Harness,
) -> None:
    foreign_cache_child = harness.cache_dir / harness.repo.name / "foreign"
    foreign_cache_child.mkdir(parents=True)
    harness.registry.create_lease(
        Lease.new(
            repository_id=harness.git.repository_id(),
            repository_name=harness.git.repository_name(),
            repository_root=harness.git.repository_root(),
            worktree_path=foreign_cache_child,
            initiative="foreign-cache",
            purpose=Purpose.FEATURE,
            branch="awf/foreign-cache/feature",
            base_ref="origin/staging",
            head_sha=harness.git.head_sha(),
            managed=True,
            owner_kind="awf",
        )
    )

    class ForeignGit:
        def repository_id(self) -> str:
            return "foreign-repository-id"

    service = WorktreeService(
        harness.registry,
        harness.git,
        config=WorktreeConfig(default_base="staging"),
        cache_dir=harness.cache_dir,
        state_dir=harness.state_dir,
        lock_dir=harness.lock_dir,
        git_factory=lambda path: (
            ForeignGit()
            if path.resolve() == foreign_cache_child.resolve()
            else GitClient(path)
        ),
    )

    result = service.doctor()

    assert not any(
        action["kind"] == "unregistered_cache_directory"
        and action["path"] == str(foreign_cache_child)
        for action in result.actions
    )


def test_import_apply_converts_insertion_identity_race_to_skipped_action(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repos"
    worktree_path = root / "repo"
    (worktree_path / ".git").mkdir(parents=True)
    head_sha = "1111111111111111111111111111111111111111"

    class FakeGit:
        def repository_id(self) -> str:
            return "repository-id"

        def repository_name(self) -> str:
            return "repo"

        def repository_root(self) -> Path:
            return worktree_path

        def list_worktrees(self) -> tuple[GitWorktree, ...]:
            return (GitWorktree(worktree_path, head_sha, "topic/release"),)

        def status_porcelain(self, cwd: Path) -> tuple[str, ...]:
            return ()

    class InsertionRacingRegistry(WorktreeRegistry):
        def create_lease(self, lease: Lease) -> Lease:
            raise sqlite3.IntegrityError("active identity inserted concurrently")

    service = WorktreeService(
        InsertionRacingRegistry(tmp_path / "state" / "worktrees.sqlite3"),
        None,
        config=WorktreeConfig(),
        git_factory=lambda _: FakeGit(),
        lock_dir=tmp_path / "locks",
    )

    result = service.import_root(root, apply=True)

    assert result.leases == ()
    assert any(
        action["kind"] == "skipped_identity_collision"
        and action["path"] == str(worktree_path)
        for action in result.actions
    )
    assert not any(
        action["kind"] == "import_worktree" for action in result.actions
    )