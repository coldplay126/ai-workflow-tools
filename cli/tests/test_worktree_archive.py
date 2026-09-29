from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import subprocess
import tarfile
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

import awf.worktrees.archive as archive_module
from awf.worktrees.archive import (
    ArchiveError,
    snapshot_worktree,
    verify_archive,
)
from awf.worktrees.config import WorktreeConfig
from awf.worktrees.git import GitClient, GitError
from awf.worktrees.github import GhClient, PullRequest
from awf.worktrees.models import Lease, LeaseState, Purpose, ResolutionState
from awf.worktrees.registry import WorktreeRegistry
from awf.worktrees.service import WorktreeService
from worktree_fixtures import git, make_repository


_REASON = "feature work was intentionally abandoned"


@dataclass
class ArchiveGitHub:
    pull_requests: dict[int, PullRequest] = field(default_factory=dict)

    def view_pr(
        self, number: int, *, repository: str | None = None
    ) -> PullRequest:
        return self.pull_requests[number]

    def find_open_pr(self, *, head: str, base: str) -> PullRequest | None:
        return next(
            (
                pull_request
                for pull_request in self.pull_requests.values()
                if pull_request.state == "OPEN"
                and pull_request.head_ref == head
                and pull_request.base_ref == base
            ),
            None,
        )

    def find_open_prs(
        self, *, head: str, repository: str
    ) -> tuple[PullRequest, ...]:
        return tuple(
            pull_request
            for pull_request in self.pull_requests.values()
            if pull_request.state == "OPEN" and pull_request.head_ref == head
        )

    def find_pr(self, *, head: str, base: str) -> PullRequest | None:
        return next(
            (
                pull_request
                for pull_request in self.pull_requests.values()
                if pull_request.head_ref == head and pull_request.base_ref == base
            ),
            None,
        )


@dataclass
class ArchiveHarness:
    repo: Path
    git: GitClient
    registry: WorktreeRegistry
    cache_dir: Path
    github: ArchiveGitHub
    service: WorktreeService

    @classmethod
    def create(cls, tmp_path: Path) -> ArchiveHarness:
        repo = make_repository(tmp_path)
        git_client = GitClient(repo)
        registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
        cache_dir = tmp_path / "cache"
        state_dir = tmp_path / "state"
        github = ArchiveGitHub()
        return cls(
            repo=repo,
            git=git_client,
            registry=registry,
            cache_dir=cache_dir,
            github=github,
            service=WorktreeService(
                registry,
                git_client,
                config=WorktreeConfig(default_base="staging"),
                github=github,
                cache_dir=cache_dir,
                state_dir=state_dir,
                lock_dir=tmp_path / "locks",
                home_dir=tmp_path / "home",
            ),
        )

    def acquire_feature(self, initiative: str = "archive-fixture") -> Lease:
        result = self.service.acquire(
            initiative=initiative,
            purpose=Purpose.FEATURE,
            base=None,
            branch=None,
            owner_id="archive-test",
            apply=True,
        )
        assert result.status == "ok"
        assert result.lease is not None
        return result.lease


def _backup_root(tmp_path: Path, name: str = "backups") -> Path:
    root = tmp_path / name
    root.mkdir()
    root.chmod(0o700)
    return root


def _create_private_archive_namespace(backup_root: Path, destination: Path) -> None:
    parent = backup_root
    for component in destination.parent.relative_to(backup_root).parts:
        parent /= component
        parent.mkdir(mode=0o700, exist_ok=True)
        parent.chmod(0o700)


def _preview(
    harness: ArchiveHarness,
    lease: Lease,
    backup_root: Path,
    *,
    reason: str = _REASON,
    expect_new_archive: bool = True,
    include_uncommitted: bool = False,
) -> tuple[str, Path]:
    result = harness.service.archive_discard(
        lease.id,
        backup_root=backup_root,
        reason=reason,
        include_uncommitted=include_uncommitted,
    )

    assert result.status == "ok"
    assert result.decision == "preview"
    action = next(action for action in result.actions if action["kind"] == "create_archive")
    token = action["preview_token"]
    assert isinstance(token, str)
    assert len(token) == 64
    assert all(character in "0123456789abcdef" for character in token)
    destination = Path(action["backup_directory"])
    assert destination.is_relative_to(backup_root)
    if expect_new_archive:
        assert not destination.exists()
    return token, destination


def _apply(
    harness: ArchiveHarness,
    lease: Lease,
    backup_root: Path,
    token: str,
    *,
    reason: str = _REASON,
    include_uncommitted: bool = False,
):
    return harness.service.archive_discard(
        lease.id,
        backup_root=backup_root,
        reason=reason,
        preview_token=token,
        apply=True,
        include_uncommitted=include_uncommitted,
    )


def _metadata(
    harness: ArchiveHarness, lease: Lease, token: str, *, reason: str = _REASON
) -> dict[str, object]:
    return {
        "head_sha": harness.git.head_sha(lease.worktree_path),
        "lease": lease.to_dict(),
        "repository_id": lease.repository_id,
        "reason": reason,
        "preview_token": token,
    }


def _commit(path: Path, filename: str, content: str, message: str) -> str:
    (path / filename).write_text(content, encoding="utf-8")
    git(path, "add", filename)
    git(path, "commit", "-q", "-m", message)
    return git(path, "rev-parse", "HEAD")


def _open_pull_request(lease: Lease, head_sha: str) -> PullRequest:
    return PullRequest(
        number=731,
        state="OPEN",
        base_ref=lease.base_ref,
        base_sha="a" * 40,
        head_ref=lease.branch,
        head_sha=head_sha,
        merge_commit_sha=None,
        review_decision="",
        checks_passed=True,
        changed_paths=(),
        url="https://github.example/acme/repo/pull/731",
    )


def _closed_promotion(
    harness: ArchiveHarness,
    *,
    initiative: str,
    resolution_state: ResolutionState,
    conflicted_paths: tuple[str, ...] = (),
    protected_index_entries: tuple[tuple[str, tuple[str, str] | None], ...] = (),
) -> Lease:
    acquired = harness.service.acquire(
        initiative=initiative,
        purpose=Purpose.PROMOTE,
        base=None,
        branch=None,
        owner_id="archive-test",
        apply=True,
    )
    assert acquired.status == "ok"
    assert acquired.lease is not None
    target_pr = 732
    lease = harness.registry.transition(
        acquired.lease.id,
        LeaseState.CLOSED_UNMERGED,
        expected_version=acquired.lease.version,
        pr_number=target_pr,
        resolution_state=resolution_state,
        conflicted_paths=conflicted_paths,
        protected_index_entries=protected_index_entries,
    )
    harness.github.pull_requests[target_pr] = PullRequest(
        number=target_pr,
        state="CLOSED",
        base_ref=lease.base_ref,
        base_sha=lease.head_sha,
        head_ref=lease.branch,
        head_sha=harness.git.head_sha(lease.worktree_path),
        merge_commit_sha=None,
        review_decision="",
        checks_passed=True,
        changed_paths=(),
        url=f"https://github.example/acme/repo/pull/{target_pr}",
    )
    return lease


def _unpublished_blocked_manual_promotion(
    harness: ArchiveHarness, *, initiative: str
) -> Lease:
    acquired = harness.service.acquire(
        initiative=initiative,
        purpose=Purpose.PROMOTE,
        base=None,
        branch=None,
        owner_id="archive-test",
        apply=True,
    )
    assert acquired.status == "ok"
    assert acquired.lease is not None
    return harness.registry.transition(
        acquired.lease.id,
        LeaseState.BLOCKED,
        expected_version=acquired.lease.version,
        resolution_state=ResolutionState.MANUAL_REVIEWED,
    )


def _restore_bundle(bundle: Path, destination: Path) -> Path:
    git(destination.parent, "init", "-q", str(destination))
    git(destination, "fetch", "-q", str(bundle), "HEAD:refs/heads/recovered")
    git(destination, "checkout", "-q", "recovered")
    return destination


def test_archive_discard_restores_independent_bundle_and_full_worktree_tar(
    tmp_path: Path,
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature()
    worktree = lease.worktree_path
    (worktree / ".gitignore").write_text("*.private\n", encoding="utf-8")
    (worktree / "run.sh").write_text("#!/bin/sh\necho restored\n", encoding="utf-8")
    (worktree / "run.sh").chmod(0o755)
    (worktree / "run-link").symlink_to("run.sh")
    git(worktree, "add", ".gitignore", "run.sh", "run-link")
    git(worktree, "commit", "-q", "-m", "add executable and link")
    expected_head = git(worktree, "rev-parse", "HEAD")
    (worktree / "operator.private").write_text("not in git\n", encoding="utf-8")

    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, lease, backup_root)
    applied = _apply(harness, lease, backup_root, token)

    assert applied.status == "ok"
    assert not worktree.exists()
    assert (destination / "history.bundle").is_file()
    assert (destination / "worktree.tar").is_file()
    assert (destination / "manifest.json").is_file()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / "history.bundle").stat().st_mode) == 0o600
    assert stat.S_IMODE((destination / "worktree.tar").stat().st_mode) == 0o600
    assert stat.S_IMODE((destination / "manifest.json").stat().st_mode) == 0o600

    restored_git = _restore_bundle(destination / "history.bundle", tmp_path / "restored-git")
    assert git(restored_git, "rev-parse", "HEAD") == expected_head
    assert (restored_git / "run.sh").read_text(encoding="utf-8") == "#!/bin/sh\necho restored\n"

    restored_tree = tmp_path / "restored-tree"
    restored_tree.mkdir()
    with tarfile.open(destination / "worktree.tar") as archive:
        archive.extractall(restored_tree)

    assert (restored_tree / "operator.private").read_text(encoding="utf-8") == "not in git\n"
    assert stat.S_IMODE((restored_tree / "run.sh").stat().st_mode) == 0o755
    assert (restored_tree / "run-link").is_symlink()
    assert (restored_tree / "run-link").readlink() == Path("run.sh")
    assert not (restored_tree / ".git").exists()


def test_archive_restore_recipe_preserves_absent_skip_worktree_file(
    tmp_path: Path,
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature("restore-absent")
    worktree = lease.worktree_path
    (worktree / "absent.txt").write_text("snapshot must omit this\n", encoding="utf-8")
    (worktree / "present.txt").write_text("snapshot keeps this\n", encoding="utf-8")
    git(worktree, "add", "absent.txt", "present.txt")
    git(worktree, "commit", "-q", "-m", "add restore boundary fixtures")
    expected_head = harness.git.head_sha(worktree)
    git(worktree, "update-index", "--skip-worktree", "absent.txt")
    (worktree / "absent.txt").unlink()
    assert not harness.git.status_porcelain(worktree)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, lease, backup_root)
    applied = _apply(harness, lease, backup_root, token)

    assert applied.status == "ok"
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    restore = manifest["restore"]
    for command in restore["commands"]:
        subprocess.run(
            shlex.split(command),
            cwd=destination,
            capture_output=True,
            check=True,
            text=True,
        )

    restored = destination / "restored-worktree"
    assert git(restored, "rev-parse", "HEAD") == expected_head
    assert not (restored / "absent.txt").exists()
    assert (restored / "present.txt").read_text(encoding="utf-8") == (
        "snapshot keeps this\n"
    )


@pytest.mark.parametrize("mutation", ("commit", "ignored"))
def test_archive_discard_rejects_stale_preview_token_without_removing_worktree(
    tmp_path: Path, mutation: str
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature(f"stale-{mutation}")
    worktree = lease.worktree_path
    if mutation == "ignored":
        _commit(worktree, ".gitignore", "*.ignored\n", "ignore local artifacts")

    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, lease, backup_root)
    if mutation == "commit":
        _commit(worktree, "after-preview.txt", "committed drift\n", "drift after preview")
    else:
        (worktree / "after-preview.ignored").write_text("ignored drift\n", encoding="utf-8")

    result = _apply(harness, lease, backup_root, token)

    assert result.status == "blocked"
    assert worktree.exists()
    assert harness.git.resolve_ref(lease.branch) == harness.git.head_sha(worktree)
    assert not destination.exists()


@pytest.mark.parametrize(
    "violation",
    ("dirty", "root", "retain", "open-pr", "symlink", "special-file"),
)
def test_archive_discard_blocks_unsafe_or_protected_worktrees(
    tmp_path: Path, violation: str
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature(f"blocked-{violation}")
    worktree = lease.worktree_path
    expected_path = worktree
    relocated: Path | None = None

    if violation == "dirty":
        (worktree / "uncommitted.txt").write_text("preserve me\n", encoding="utf-8")
    elif violation == "root":
        root_lease = Lease.new(
            repository_id=harness.git.repository_id(),
            repository_name=harness.git.repository_name(),
            repository_root=harness.repo,
            worktree_path=harness.repo,
            initiative="root-archive",
            purpose=Purpose.FEATURE,
            branch="staging",
            base_ref="staging",
            head_sha=harness.git.head_sha(harness.repo),
            managed=True,
            owner_kind="awf",
            owner_id="archive-test",
        )
        lease = harness.registry.create_lease(root_lease)
        expected_path = harness.repo
    elif violation == "retain":
        lease = harness.registry.transition(
            lease.id,
            lease.state,
            expected_version=lease.version,
            retain=True,
        )
    elif violation == "open-pr":
        lease = harness.registry.transition(
            lease.id,
            LeaseState.PR_OPEN,
            expected_version=lease.version,
            pr_number=731,
        )
        harness.github.pull_requests[731] = _open_pull_request(
            lease, harness.git.head_sha(worktree)
        )
    elif violation == "symlink":
        relocated = tmp_path / "relocated-worktree"
        worktree.rename(relocated)
        worktree.symlink_to(relocated, target_is_directory=True)
    else:
        _commit(worktree, ".gitignore", "archive.pipe\n", "ignore fifo")
        os.mkfifo(worktree / "archive.pipe", 0o600)

    expected_branch_head = harness.git.resolve_ref(lease.branch)

    backup_root = _backup_root(tmp_path)
    result = harness.service.archive_discard(
        lease.id,
        backup_root=backup_root,
        reason=_REASON,
    )

    assert result.status == "blocked"
    assert expected_path.exists()
    assert harness.git.resolve_ref(lease.branch) == expected_branch_head
    assert not any(backup_root.iterdir())
    if relocated is not None:
        assert worktree.is_symlink()
        assert relocated.is_dir()


def test_archive_discard_refuses_incomplete_or_corrupt_existing_backup(
    tmp_path: Path,
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature("corrupt-backup")
    backup_root = _backup_root(tmp_path)

    token, incomplete = _preview(harness, lease, backup_root)
    _create_private_archive_namespace(backup_root, incomplete)
    incomplete.mkdir(mode=0o700)
    (incomplete / "manifest.json").write_text("{}", encoding="utf-8")
    blocked_incomplete = _apply(harness, lease, backup_root, token)

    assert blocked_incomplete.status == "blocked"
    assert lease.worktree_path.exists()

    token, corrupt = _preview(harness, lease, backup_root, reason="second attempt")
    snapshot = snapshot_worktree(lease.worktree_path)
    metadata = _metadata(harness, lease, token, reason="second attempt")
    _create_private_archive_namespace(backup_root, corrupt)
    archive_module.create_archive(
        destination=corrupt,
        worktree_path=lease.worktree_path,
        snapshot=snapshot,
        metadata=metadata,
        git=harness.git,
    )
    (corrupt / "history.bundle").write_bytes(b"not a git bundle")

    with pytest.raises(ArchiveError):
        verify_archive(
            corrupt,
            expected_metadata=metadata,
            expected_snapshot=snapshot,
            git=harness.git,
        )
    blocked_corrupt = _apply(
        harness, lease, backup_root, token, reason="second attempt"
    )

    assert blocked_corrupt.status == "blocked"
    assert lease.worktree_path.exists()
    assert (corrupt / "history.bundle").read_bytes() == b"not a git bundle"


def test_archive_discard_keeps_verified_backup_when_removal_fails_then_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature("remove-retry")
    expected_head = _commit(
        lease.worktree_path, "feature.txt", "backup first\n", "feature work"
    )
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, lease, backup_root)
    original_remove = harness.git.remove_worktree

    def fail_remove(_path: Path, *, force: bool = False) -> None:
        raise GitError("simulated worktree removal failure")

    monkeypatch.setattr(harness.git, "remove_worktree", fail_remove)
    failed = _apply(harness, lease, backup_root, token)

    assert failed.status == "blocked"
    assert lease.worktree_path.exists()
    assert (destination / "manifest.json").is_file()
    restored_git = _restore_bundle(
        destination / "history.bundle", tmp_path / "restored-failed-remove"
    )
    assert git(restored_git, "rev-parse", "HEAD") == expected_head
    assert (restored_git / "feature.txt").read_text(encoding="utf-8") == (
        "backup first\n"
    )
    assert harness.registry.get_cleanup_reservation(lease.id) is None

    monkeypatch.setattr(harness.git, "remove_worktree", original_remove)
    retry_token, retry_destination = _preview(
        harness, lease, backup_root, expect_new_archive=False
    )
    retried = _apply(harness, lease, backup_root, retry_token)

    assert retried.status == "ok"
    assert not lease.worktree_path.exists()
    assert destination.exists()
    assert retry_destination.exists()


def test_archive_discard_detects_ignored_drift_after_backup_before_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature("post-backup-drift")
    worktree = lease.worktree_path
    _commit(worktree, ".gitignore", "*.runtime\n", "ignore runtime state")
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, lease, backup_root)
    original_create = archive_module.create_archive

    def create_then_change(*args: object, **kwargs: object) -> dict[str, object]:
        manifest = original_create(*args, **kwargs)
        (worktree / "after-backup.runtime").write_text(
            "must not be discarded\n", encoding="utf-8"
        )
        return manifest

    monkeypatch.setattr(archive_module, "create_archive", create_then_change)
    result = _apply(harness, lease, backup_root, token)

    assert result.status == "blocked"
    assert worktree.exists()
    assert (worktree / "after-backup.runtime").read_text(encoding="utf-8") == (
        "must not be discarded\n"
    )
    assert (destination / "manifest.json").is_file()
    assert harness.registry.get_cleanup_reservation(lease.id) is None


def test_archive_discard_preserves_existing_remote_branch(
    tmp_path: Path,
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature("remote-preserved")
    remote_head = _commit(
        lease.worktree_path, "feature.txt", "remote work\n", "publish feature"
    )
    git(lease.worktree_path, "push", "-q", "-u", "origin", lease.branch)
    backup_root = _backup_root(tmp_path)
    token, _ = _preview(harness, lease, backup_root)

    result = _apply(harness, lease, backup_root, token)

    assert result.status == "ok"
    assert not lease.worktree_path.exists()
    assert (
        git(
            harness.repo.parent / "origin.git",
            "rev-parse",
            f"refs/heads/{lease.branch}",
        )
        == remote_head
    )

def test_archive_discard_removes_custom_managed_feature_and_preserves_branches(
    tmp_path: Path,
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    branch = f"operator/{harness.git.head_sha(harness.repo)[:12]}"
    acquired = harness.service.acquire(
        initiative="custom-branch",
        purpose=Purpose.FEATURE,
        base=None,
        branch=branch,
        owner_id="archive-test",
        apply=True,
    )
    assert acquired.status == "ok"
    assert acquired.lease is not None
    lease = acquired.lease
    expected_head = _commit(
        lease.worktree_path, "feature.txt", "preserve branch\n", "record feature work"
    )
    git(lease.worktree_path, "push", "-q", "-u", "origin", branch)
    backup_root = _backup_root(tmp_path)
    token, _ = _preview(harness, lease, backup_root)

    removed = _apply(harness, lease, backup_root, token)

    assert removed.status == "ok"
    assert not lease.worktree_path.exists()
    assert harness.git.resolve_ref(branch) == expected_head
    assert (
        git(
            harness.repo.parent / "origin.git",
            "rev-parse",
            f"refs/heads/{branch}",
        )
        == expected_head
    )


def test_archive_discard_allows_noncanonical_sync_named_feature(
    tmp_path: Path,
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature("sync-operator-notes")
    backup_root = _backup_root(tmp_path)
    token, _ = _preview(harness, lease, backup_root)

    removed = _apply(harness, lease, backup_root, token)

    assert removed.status == "ok"
    assert not lease.worktree_path.exists()


def test_archive_discard_blocks_canonical_sync_feature(
    tmp_path: Path,
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    head = harness.git.head_sha(harness.repo)
    branch = f"awf/sync-{head[:16]}-{head[-12:]}/feature"
    acquired = harness.service.acquire(
        initiative="archive-sync",
        purpose=Purpose.FEATURE,
        base=None,
        branch=branch,
        owner_id="archive-test",
        apply=True,
    )
    assert acquired.status == "ok"
    assert acquired.lease is not None
    lease = acquired.lease
    backup_root = _backup_root(tmp_path)

    blocked = harness.service.archive_discard(
        lease.id,
        backup_root=backup_root,
        reason=_REASON,
    )

    assert blocked.status == "blocked"
    assert lease.worktree_path.exists()
    assert harness.git.resolve_ref(branch) == harness.git.head_sha(lease.worktree_path)
    assert not any(backup_root.iterdir())


def test_archive_discard_removes_clean_imported_scratch_but_preserves_branches_and_backup(
    tmp_path: Path,
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    branch = "scratch-archive"
    imported_path = tmp_path / "imported-scratch"
    git(
        harness.repo,
        "worktree",
        "add",
        "-q",
        "-b",
        branch,
        str(imported_path),
        "staging",
    )
    _commit(imported_path, ".gitignore", "*.scratch\n", "ignore scratch notes")
    expected_head = _commit(
        imported_path, "scratch.txt", "imported work\n", "record scratch work"
    )
    (imported_path / "operator.scratch").write_text(
        "ignored scratch note\n", encoding="utf-8"
    )
    git(imported_path, "push", "-q", "-u", "origin", branch)

    imported = harness.service.import_root(tmp_path, apply=True)
    lease = next(
        lease for lease in imported.leases if lease.worktree_path == imported_path
    )
    assert lease.purpose is Purpose.SCRATCH
    assert lease.owner_kind == "imported"
    assert lease.managed is False

    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, lease, backup_root)
    result = _apply(harness, lease, backup_root, token)

    assert result.status == "ok"
    assert not imported_path.exists()
    assert harness.git.resolve_ref(branch) == expected_head
    assert (
        git(
            harness.repo.parent / "origin.git",
            "rev-parse",
            f"refs/heads/{branch}",
        )
        == expected_head
    )
    restored_git = _restore_bundle(
        destination / "history.bundle", tmp_path / "restored-imported-git"
    )
    assert git(restored_git, "rev-parse", "HEAD") == expected_head
    restored_tree = tmp_path / "restored-imported-tree"
    restored_tree.mkdir()
    with tarfile.open(destination / "worktree.tar") as archive:
        archive.extractall(restored_tree)
    assert (restored_tree / "operator.scratch").read_text(encoding="utf-8") == (
        "ignored scratch note\n"
    )


def test_archive_discard_rejects_preview_token_issued_for_another_lease(
    tmp_path: Path,
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    source = harness.acquire_feature("token-source")
    target = harness.acquire_feature("token-target")
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, source, backup_root)

    result = _apply(harness, target, backup_root, token)

    assert result.status == "blocked"
    assert source.worktree_path.exists()
    assert target.worktree_path.exists()
    assert not destination.exists()


def test_verify_bundle_ignores_poisoned_git_environment_and_source_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "GIT_DIR",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        monkeypatch.delenv(name, raising=False)
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature("bundle-isolation")
    bundle = tmp_path / "stale.bundle"
    base_head = harness.git.head_sha(lease.worktree_path)
    harness.git.create_bundle(bundle, cwd=lease.worktree_path)
    harness.git.verify_bundle(bundle, expected_head=base_head)
    expected_head = _commit(
        lease.worktree_path,
        "after-bundle.txt",
        "source-only object\n",
        "advance source after bundle",
    )
    source_git_dir = harness.repo / ".git"
    source_objects = source_git_dir / "objects"
    before_head = harness.git.head_sha(harness.repo)
    before_refs = git(harness.repo, "for-each-ref", "--format=%(refname) %(objectname)")
    before_objects = {
        path.relative_to(source_objects): path.read_bytes()
        for path in source_objects.rglob("*")
        if path.is_file()
    }

    with monkeypatch.context() as poisoned_environment:
        poisoned_environment.setenv("GIT_DIR", str(source_git_dir))
        poisoned_environment.setenv("GIT_COMMON_DIR", str(source_git_dir))
        poisoned_environment.setenv("GIT_OBJECT_DIRECTORY", str(source_objects))
        poisoned_environment.setenv(
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", str(source_objects)
        )
        harness.git.verify_bundle(bundle, expected_head=base_head)
        with pytest.raises(GitError):
            harness.git.verify_bundle(bundle, expected_head=expected_head)

    after_objects = {
        path.relative_to(source_objects): path.read_bytes()
        for path in source_objects.rglob("*")
        if path.is_file()
    }
    assert harness.git.head_sha(harness.repo) == before_head
    assert (
        git(harness.repo, "for-each-ref", "--format=%(refname) %(objectname)")
        == before_refs
    )
    assert after_objects == before_objects
    assert harness.git.resolve_ref(lease.branch) == expected_head


def test_archive_discard_recovers_absent_worktree_after_completion_cas_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature("completion-recovery")
    expected_head = _commit(
        lease.worktree_path, "feature.txt", "recover cleanup\n", "feature work"
    )
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, lease, backup_root)
    original_complete = harness.registry.complete_cleanup
    failed_once = True

    def fail_complete_once(*args: object, **kwargs: object):
        nonlocal failed_once
        if failed_once:
            failed_once = False
            raise RuntimeError("simulated completion CAS failure")
        return original_complete(*args, **kwargs)

    monkeypatch.setattr(harness.registry, "complete_cleanup", fail_complete_once)
    interrupted = _apply(harness, lease, backup_root, token)

    assert interrupted.status == "blocked"
    assert interrupted.blockers[0]["code"] == "cleanup_reserved"
    assert not lease.worktree_path.exists()
    assert (destination / "manifest.json").is_file()
    reservation = harness.registry.get_cleanup_reservation(lease.id)
    assert reservation is not None
    assert harness.git.resolve_ref(lease.branch) == expected_head

    monkeypatch.setattr(harness.registry, "complete_cleanup", original_complete)
    recovery_preview = harness.service.archive_discard(
        lease.id,
        backup_root=backup_root,
        reason=_REASON,
    )

    assert recovery_preview.status == "blocked"
    assert recovery_preview.blockers[0]["code"] == "cleanup_reserved"
    assert not lease.worktree_path.exists()
    assert (destination / "manifest.json").is_file()
    assert harness.registry.get_cleanup_reservation(lease.id) == reservation
    recovered = _apply(harness, lease, backup_root, token)
    assert recovered.status == "ok"
    current = harness.registry.get_lease(lease.id)
    assert current is not None
    assert current.state is LeaseState.REMOVED
    assert harness.registry.get_cleanup_reservation(lease.id) is None
    with pytest.raises(GitError):
        harness.git.resolve_ref(lease.branch)


def test_archive_discard_retry_does_not_delete_branch_moved_after_compare_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = harness.acquire_feature("branch-cas-retry")
    expected_head = _commit(
        lease.worktree_path, "feature.txt", "expected branch\n", "feature work"
    )
    moved_head = _commit(
        harness.repo,
        "main-only.txt",
        "must retain this moved ref\n",
        "advance staging separately",
    )
    backup_root = _backup_root(tmp_path)
    token, _ = _preview(harness, lease, backup_root)
    original_delete = harness.git.delete_branch_if_at

    def move_branch_before_delete(branch: str, expected_sha: str) -> None:
        git(
            harness.repo,
            "update-ref",
            f"refs/heads/{branch}",
            moved_head,
            expected_sha,
        )
        original_delete(branch, expected_sha)

    monkeypatch.setattr(
        harness.git, "delete_branch_if_at", move_branch_before_delete
    )
    removed = _apply(harness, lease, backup_root, token)

    assert removed.status == "ok"
    assert not lease.worktree_path.exists()
    assert any(
        warning["code"] == "local_branch_cleanup_failed"
        for warning in removed.warnings
    )
    assert harness.git.resolve_ref(lease.branch) == moved_head
    current = harness.registry.get_lease(lease.id)
    assert current is not None
    assert current.state is LeaseState.REMOVED

    monkeypatch.setattr(harness.git, "delete_branch_if_at", original_delete)
    retried = _apply(harness, lease, backup_root, token)

    assert retried.status == "ok"
    assert any(
        warning["code"] == "local_branch_cleanup_failed"
        for warning in retried.warnings
    )
    assert harness.git.resolve_ref(lease.branch) == moved_head


@pytest.mark.parametrize(
    "resolution_state",
    (ResolutionState.AUTOMATIC, ResolutionState.MANUAL_REVIEWED),
)
def test_archive_discard_archives_completed_closed_promotion_with_pr_provenance(
    tmp_path: Path, resolution_state: ResolutionState
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = _closed_promotion(
        harness,
        initiative=f"closed-{resolution_state.value.replace('_', '-')}",
        resolution_state=resolution_state,
        conflicted_paths=("src/recovery.py",),
        protected_index_entries=(("src/recovery.py", None),),
    )
    assert lease.target_pr is not None
    expected_head = harness.git.head_sha(lease.worktree_path)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, lease, backup_root)

    removed = _apply(harness, lease, backup_root, token)

    assert removed.status == "ok"
    assert not lease.worktree_path.exists()
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    metadata = manifest["metadata"]
    assert metadata["lease"]["resolution_state"] == resolution_state.value
    assert metadata["lease"]["conflicted_paths"] == ["src/recovery.py"]
    assert metadata["lease"]["protected_index_entries"] == [
        {"path": "src/recovery.py", "mode": None, "blob_oid": None}
    ]
    evidence = metadata["archive_discard_evidence"]
    assert evidence["schema"] == "awf.archive-discard/v2"
    assert evidence["closed_promotion_pr"] == {
        "number": lease.target_pr,
        "state": "CLOSED",
        "head_ref": lease.branch,
        "head_sha": expected_head,
        "merge_commit_sha": None,
        "repository_sha256": hashlib.sha256(
            harness.git.remote_url().encode("utf-8")
        ).hexdigest(),
    }


def test_archive_discard_requires_opt_in_for_unpublished_manual_promotion(
    tmp_path: Path,
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = _unpublished_blocked_manual_promotion(
        harness, initiative="unpublished-manual"
    )
    expected_head = harness.git.head_sha(lease.worktree_path)
    backup_root = _backup_root(tmp_path)

    blocked = harness.service.archive_discard(
        lease.id,
        backup_root=backup_root,
        reason=_REASON,
    )

    assert blocked.status == "blocked"
    assert blocked.blockers[0]["code"] == "manual_resolution_present"
    assert lease.worktree_path.exists()
    assert harness.git.resolve_ref(lease.branch) == expected_head
    assert not any(backup_root.iterdir())

    token, destination = _preview(
        harness, lease, backup_root, include_uncommitted=True
    )
    removed = _apply(
        harness, lease, backup_root, token, include_uncommitted=True
    )

    assert removed.status == "ok"
    assert not lease.worktree_path.exists()
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    evidence = manifest["metadata"]["archive_discard_evidence"]
    assert evidence["closed_promotion_pr"] is None


def test_archive_discard_blocks_open_pr_for_unpublished_manual_promotion(
    tmp_path: Path,
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = _unpublished_blocked_manual_promotion(
        harness, initiative="unpublished-open-pr"
    )
    expected_head = harness.git.head_sha(lease.worktree_path)
    harness.github.pull_requests[731] = _open_pull_request(lease, expected_head)
    backup_root = _backup_root(tmp_path)

    blocked = harness.service.archive_discard(
        lease.id,
        backup_root=backup_root,
        reason=_REASON,
        include_uncommitted=True,
    )

    assert blocked.status == "blocked"
    assert blocked.blockers[0]["code"] == "open_pull_request"
    assert lease.worktree_path.exists()
    assert harness.git.resolve_ref(lease.branch) == expected_head
    assert not any(backup_root.iterdir())


@pytest.mark.parametrize(
    "violation", ("pending", "reopened", "head_mismatch", "dirty")
)
def test_archive_discard_preserves_unproven_closed_promotion(
    tmp_path: Path, violation: str
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = _closed_promotion(
        harness,
        initiative=f"closed-{violation.replace('_', '-')}",
        resolution_state=(
            ResolutionState.PENDING
            if violation == "pending"
            else ResolutionState.AUTOMATIC
        ),
    )
    assert lease.target_pr is not None
    expected_head = harness.git.head_sha(lease.worktree_path)
    if violation == "reopened":
        harness.github.pull_requests[lease.target_pr] = replace(
            harness.github.pull_requests[lease.target_pr], state="OPEN"
        )
    elif violation == "head_mismatch":
        mismatch_head = _commit(
            harness.repo,
            "unrelated.txt",
            "not the promotion head\n",
            "advance staging outside promotion",
        )
        harness.github.pull_requests[lease.target_pr] = replace(
            harness.github.pull_requests[lease.target_pr], head_sha=mismatch_head
        )
    elif violation == "dirty":
        (lease.worktree_path / "untracked.txt").write_text(
            "retain this source\n", encoding="utf-8"
        )
    backup_root = _backup_root(tmp_path)

    blocked = harness.service.archive_discard(
        lease.id,
        backup_root=backup_root,
        reason=_REASON,
    )

    assert blocked.status == "blocked"
    assert lease.worktree_path.exists()
    assert harness.git.resolve_ref(lease.branch) == expected_head
    assert not any(backup_root.iterdir())


@pytest.mark.parametrize("mutation", ("reopened", "head_mismatch"))
def test_archive_discard_revalidates_closed_promotion_before_apply(
    tmp_path: Path, mutation: str
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = _closed_promotion(
        harness,
        initiative=f"apply-{mutation.replace('_', '-')}",
        resolution_state=ResolutionState.AUTOMATIC,
    )
    assert lease.target_pr is not None
    expected_head = harness.git.head_sha(lease.worktree_path)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, lease, backup_root)
    if mutation == "reopened":
        harness.github.pull_requests[lease.target_pr] = replace(
            harness.github.pull_requests[lease.target_pr], state="OPEN"
        )
    else:
        mismatch_head = _commit(
            harness.repo,
            "apply-unrelated.txt",
            "not the promotion head\n",
            "advance staging outside promotion",
        )
        harness.github.pull_requests[lease.target_pr] = replace(
            harness.github.pull_requests[lease.target_pr], head_sha=mismatch_head
        )

    blocked = _apply(harness, lease, backup_root, token)

    assert blocked.status == "blocked"
    assert lease.worktree_path.exists()
    assert harness.git.resolve_ref(lease.branch) == expected_head
    assert not destination.exists()


def test_archive_discard_revalidates_closed_promotion_during_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = _closed_promotion(
        harness,
        initiative="recovery-closed",
        resolution_state=ResolutionState.MANUAL_REVIEWED,
    )
    assert lease.target_pr is not None
    expected_head = harness.git.head_sha(lease.worktree_path)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, lease, backup_root)
    original_complete = harness.registry.complete_cleanup

    def fail_complete(*args: object, **kwargs: object):
        raise RuntimeError("simulate interrupted archive completion")

    monkeypatch.setattr(harness.registry, "complete_cleanup", fail_complete)
    interrupted = _apply(harness, lease, backup_root, token)
    assert interrupted.status == "blocked"
    assert not lease.worktree_path.exists()
    assert (destination / "manifest.json").is_file()
    harness.github.pull_requests[lease.target_pr] = replace(
        harness.github.pull_requests[lease.target_pr], state="OPEN"
    )
    monkeypatch.setattr(harness.registry, "complete_cleanup", original_complete)

    blocked = _apply(harness, lease, backup_root, token)

    assert blocked.status == "blocked"
    assert not lease.worktree_path.exists()
    assert harness.git.resolve_ref(lease.branch) == expected_head
    assert (destination / "manifest.json").is_file()


def test_archive_discard_uses_origin_for_closed_promotion_pr_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = _closed_promotion(
        harness,
        initiative="origin-pr-evidence",
        resolution_state=ResolutionState.AUTOMATIC,
    )
    assert lease.target_pr is not None
    expected_head = harness.git.head_sha(lease.worktree_path)
    origin_repository = harness.git.remote_url()
    fork_repository = "fork-owner/repository"
    monkeypatch.setenv("GH_REPO", fork_repository)

    def runner(
        command: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["pr", "list"]:
            return subprocess.CompletedProcess(command, 0, "[]", "")
        if command[1:3] != ["pr", "view"]:
            raise AssertionError(f"unexpected GitHub command: {command}")
        repository = (
            command[command.index("--repo") + 1]
            if "--repo" in command
            else os.environ.get("GH_REPO")
        )
        state = "MERGED" if repository == origin_repository else "CLOSED"
        merge_commit = {"oid": expected_head} if state == "MERGED" else None
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "number": lease.target_pr,
                    "state": state,
                    "baseRefName": lease.base_ref,
                    "baseRefOid": lease.head_sha,
                    "headRefName": lease.branch,
                    "headRefOid": expected_head,
                    "mergeCommit": merge_commit,
                    "reviewDecision": "",
                    "statusCheckRollup": [],
                    "files": [],
                    "url": f"https://github.example/acme/repo/pull/{lease.target_pr}",
                }
            ),
            "",
        )

    harness.service.github = GhClient(harness.repo, command_runner=runner)
    backup_root = _backup_root(tmp_path)

    blocked = harness.service.archive_discard(
        lease.id,
        backup_root=backup_root,
        reason=_REASON,
    )

    assert blocked.status == "blocked"
    assert lease.worktree_path.exists()
    assert harness.git.resolve_ref(lease.branch) == expected_head
    assert not any(backup_root.iterdir())


def test_archive_discard_recovery_rejects_replaced_origin_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ArchiveHarness.create(tmp_path)
    lease = _closed_promotion(
        harness,
        initiative="origin-recovery",
        resolution_state=ResolutionState.AUTOMATIC,
    )
    assert lease.target_pr is not None
    expected_head = harness.git.head_sha(lease.worktree_path)
    git(lease.worktree_path, "push", "-q", "-u", "origin", lease.branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, lease, backup_root)
    original_complete = harness.registry.complete_cleanup

    def fail_complete(*args: object, **kwargs: object):
        raise RuntimeError("simulate interrupted archive completion")

    monkeypatch.setattr(harness.registry, "complete_cleanup", fail_complete)
    interrupted = _apply(harness, lease, backup_root, token)
    assert interrupted.status == "blocked"
    assert not lease.worktree_path.exists()
    replacement_origin = tmp_path / "replacement-origin.git"
    git(tmp_path, "init", "--bare", "-q", str(replacement_origin))
    git(
        harness.repo,
        "push",
        "-q",
        str(replacement_origin),
        f"{lease.branch}:refs/heads/{lease.branch}",
    )
    git(harness.repo, "remote", "set-url", "origin", str(replacement_origin))
    monkeypatch.setattr(harness.registry, "complete_cleanup", original_complete)

    blocked = _apply(harness, lease, backup_root, token)

    assert blocked.status == "blocked"
    assert not lease.worktree_path.exists()
    assert harness.git.resolve_ref(lease.branch) == expected_head
    assert (destination / "manifest.json").is_file()
