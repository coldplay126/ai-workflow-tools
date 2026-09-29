from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

import awf.worktrees.remote_branch_discard as remote_discard_module
from awf.worktrees.config import WorktreeConfig
from awf.worktrees.git import GitClient, GitRemoteError
from awf.worktrees.github import ExternalServiceError, PullRequest
from awf.worktrees.models import Lease, LeaseState, Purpose, ReleaseBridge
from awf.worktrees.registry import WorktreeRegistry
from awf.worktrees.service import WorktreeService
from worktree_fixtures import git, make_repository


_REASON = "The approved remote branch is no longer needed."


@dataclass
class RemoteDiscardGitHub:
    pull_requests: tuple[PullRequest, ...] = ()
    failure: ExternalServiceError | None = None

    def find_open_prs(
        self, *, head: str, repository: str
    ) -> tuple[PullRequest, ...]:
        if self.failure is not None:
            raise self.failure
        return tuple(
            pull_request
            for pull_request in self.pull_requests
            if pull_request.state == "OPEN" and pull_request.head_ref == head
        )


@dataclass
class RemoteDiscardHarness:
    repo: Path
    git: GitClient
    registry: WorktreeRegistry
    cache_dir: Path
    lock_dir: Path
    github: RemoteDiscardGitHub
    service: WorktreeService

    @classmethod
    def create(cls, tmp_path: Path) -> RemoteDiscardHarness:
        repo = make_repository(tmp_path)
        client = GitClient(repo)
        registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
        cache_dir = tmp_path / "cache"
        lock_dir = tmp_path / "locks"
        github = RemoteDiscardGitHub()
        return cls(
            repo=repo,
            git=client,
            registry=registry,
            cache_dir=cache_dir,
            lock_dir=lock_dir,
            github=github,
            service=WorktreeService(
                registry,
                client,
                config=WorktreeConfig(
                    default_base="staging",
                    production_branch="main",
                ),
                github=github,
                cache_dir=cache_dir,
                state_dir=tmp_path / "state",
                lock_dir=lock_dir,
                home_dir=tmp_path / "home",
            ),
        )


def _backup_root(tmp_path: Path, name: str = "backups") -> Path:
    root = tmp_path / name
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def _remote_head(repo: Path, branch: str) -> str | None:
    output = git(repo, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    return output.split()[0] if output else None


def _create_remote_branch(harness: RemoteDiscardHarness, branch: str) -> str:
    git(harness.repo, "branch", branch, "staging")
    expected_sha = git(harness.repo, "rev-parse", branch)
    git(
        harness.repo,
        "push",
        "-q",
        "origin",
        f"{branch}:refs/heads/{branch}",
    )
    return expected_sha

def _create_matching_push_remote(
    tmp_path: Path,
    harness: RemoteDiscardHarness,
    branch: str,
    expected_sha: str,
) -> Path:
    push_remote = tmp_path / "push-target.git"
    git(tmp_path, "init", "--bare", "-q", str(push_remote))
    git(
        harness.repo,
        "push",
        "-q",
        str(push_remote),
        f"{branch}:refs/heads/{branch}",
    )
    assert git(push_remote, "rev-parse", f"refs/heads/{branch}") == expected_sha
    return push_remote


def _preview(
    harness: RemoteDiscardHarness,
    branch: str,
    expected_sha: str,
    backup_root: Path,
    *,
    reason: str = _REASON,
):
    result = harness.service.discard_remote_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=backup_root,
        reason=reason,
    )
    assert result.status == "ok"
    assert result.decision == "preview"
    action = next(
        action for action in result.actions if action["kind"] == "create_commit_bundle"
    )
    token = action["preview_token"]
    destination = Path(action["backup_directory"])
    assert isinstance(token, str)
    assert len(token) == 64
    assert destination.is_relative_to(backup_root)
    assert not destination.exists()
    return token, destination


def _apply(
    harness: RemoteDiscardHarness,
    branch: str,
    expected_sha: str,
    backup_root: Path,
    token: str,
    *,
    reason: str = _REASON,
):
    return harness.service.discard_remote_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=backup_root,
        reason=reason,
        preview_token=token,
        apply=True,
    )


def _blocker_code(result: object) -> str:
    blockers = getattr(result, "blockers")
    assert blockers
    return blockers[0]["code"]


def _primary_state(repo: Path) -> dict[str, object]:
    git_dir = repo / ".git"
    return {
        "heads": git(
            repo,
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/heads",
        ),
        "head": (git_dir / "HEAD").read_bytes(),
        "index": (git_dir / "index").read_bytes(),
        "config": (git_dir / "config").read_bytes(),
        "hooks": {
            path.name: path.read_bytes()
            for path in (git_dir / "hooks").iterdir()
            if path.is_file()
        },
        "worktrees": git(repo, "worktree", "list", "--porcelain"),
        "status": git(repo, "status", "--porcelain"),
        "readme": (repo / "README.txt").read_bytes(),
    }


def _restore_commit_bundle(
    bundle: Path, destination: Path, branch: str
) -> str:
    git(destination.parent, "init", "--bare", "-q", str(destination))
    git(
        destination,
        "fetch",
        "--no-tags",
        str(bundle),
        f"HEAD:refs/heads/{branch}",
    )
    git(destination, "fsck", "--full", "--strict")
    return git(destination, "rev-parse", f"refs/heads/{branch}")


def _assert_private(path: Path, mode: int) -> None:
    assert stat.S_IMODE(path.stat().st_mode) == mode


def _register_lease(
    harness: RemoteDiscardHarness,
    *,
    initiative: str,
    branch: str,
    base_ref: str = "staging",
    repository_root: Path | None = None,
) -> Lease:
    return harness.registry.create_lease(
        Lease.new(
            repository_id=harness.git.repository_id(),
            repository_name=harness.git.repository_name(),
            repository_root=repository_root or harness.git.repository_root(),
            worktree_path=harness.repo.parent / f"registry-{initiative}",
            initiative=initiative,
            purpose=(
                Purpose.PROMOTE
                if initiative.startswith("release-")
                else Purpose.FEATURE
            ),
            branch=branch,
            base_ref=base_ref,
            head_sha=harness.git.head_sha(),
            managed=True,
            owner_kind="awf",
            owner_id="remote-discard-test",
        )
    )


def _open_pull_request(branch: str, head_sha: str) -> PullRequest:
    return PullRequest(
        number=913,
        state="OPEN",
        base_ref="staging",
        base_sha=head_sha,
        head_ref=branch,
        head_sha=head_sha,
        merge_commit_sha=None,
        review_decision="",
        checks_passed=True,
        changed_paths=(),
        url="https://github.example/acme/repo/pull/913",
    )


def _advance_remote_from_second_clone(
    tmp_path: Path,
    harness: RemoteDiscardHarness,
    branch: str,
    *,
    name: str,
) -> tuple[Path, str]:
    clone = tmp_path / name
    origin = harness.repo.parent / "origin.git"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    git(clone, "config", "user.email", "outside@example.com")
    git(clone, "config", "user.name", "Outside Writer")
    git(clone, "checkout", "-q", "-b", branch, f"origin/{branch}")
    (clone / f"{name}.txt").write_text("outside commit\n", encoding="utf-8")
    git(clone, "add", f"{name}.txt")
    git(clone, "commit", "-q", "-m", "advance remote outside primary clone")
    advanced_sha = git(clone, "rev-parse", "HEAD")
    git(clone, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    return clone, advanced_sha


def test_remote_discard_preview_is_read_only_and_apply_restores_a_private_bundle(
    tmp_path: Path,
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/consumer-regression"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    before_preview = _primary_state(harness.repo)
    before_refs = git(harness.repo, "for-each-ref", "--format=%(refname) %(objectname)")

    token, destination = _preview(harness, branch, expected_sha, backup_root)

    assert _primary_state(harness.repo) == before_preview
    assert (
        git(harness.repo, "for-each-ref", "--format=%(refname) %(objectname)")
        == before_refs
    )
    assert list(backup_root.iterdir()) == []
    assert not harness.lock_dir.exists()
    assert not harness.registry.db_path.exists()

    marker = harness.repo / ".git" / "hooks" / "pre-push-ran"
    hook = harness.repo / ".git" / "hooks" / "pre-push"
    hook.write_text(
        f"#!/bin/sh\nprintf hook-ran > {str(marker)!r}\nexit 1\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    before_apply = _primary_state(harness.repo)
    applied = _apply(harness, branch, expected_sha, backup_root, token)

    assert applied.status == "ok"
    assert applied.decision == "discarded"
    assert _remote_head(harness.repo, branch) is None
    assert _primary_state(harness.repo) == before_apply
    assert not marker.exists()
    assert destination.is_dir()
    _assert_private(destination, 0o700)
    for name in ("history.bundle", "manifest.json", "attempt.json", "receipt.json"):
        artifact = destination / name
        assert artifact.is_file()
        _assert_private(artifact, 0o600)
    assert _restore_commit_bundle(
        destination / "history.bundle", tmp_path / "restored.git", branch
    ) == expected_sha
    payload = json.dumps(applied.to_dict(), sort_keys=True)
    assert _REASON not in payload
    assert "bundle_sha256" not in payload
    assert "manifest" not in payload


def test_remote_discard_receipt_is_immutable_and_preserves_a_recreated_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/completed-retry"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, branch, expected_sha, backup_root)
    first = _apply(harness, branch, expected_sha, backup_root, token)

    assert first.status == "ok"
    receipt = destination / "receipt.json"
    attempt = destination / "attempt.json"
    receipt_before = receipt.read_bytes()
    attempt_before = attempt.read_bytes()

    def delete_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a completed receipt must prevent a second push")

    monkeypatch.setattr(harness.git, "delete_remote_branch_if_at", delete_must_not_run)
    repeated = _apply(harness, branch, expected_sha, backup_root, token)

    assert repeated.status == "ok"
    assert any(action.get("idempotent") is True for action in repeated.actions)
    assert receipt.read_bytes() == receipt_before
    assert attempt.read_bytes() == attempt_before

    git(
        harness.repo,
        "push",
        "-q",
        "origin",
        f"{expected_sha}:refs/heads/{branch}",
    )
    recreated = _apply(harness, branch, expected_sha, backup_root, token)

    assert recreated.status == "ok"
    assert _remote_head(harness.repo, branch) == expected_sha
    assert any(
        warning["code"] == "branch_recreated_untouched"
        for warning in recreated.warnings
    )
    assert receipt.read_bytes() == receipt_before
    assert attempt.read_bytes() == attempt_before


def test_remote_discard_recovers_only_an_absent_remote_after_post_push_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/post-push-interruption"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, branch, expected_sha, backup_root)

    def crash_before_receipt(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulate a process crash after the remote push")

    monkeypatch.setattr(
        remote_discard_module.RemoteBranchDiscarder,
        "_write_receipt",
        crash_before_receipt,
    )
    interrupted = _apply(harness, branch, expected_sha, backup_root, token)

    assert interrupted.status == "blocked"
    assert _remote_head(harness.repo, branch) is None
    assert (destination / "attempt.json").is_file()
    assert not (destination / "receipt.json").exists()
    attempt_before = (destination / "attempt.json").read_bytes()

    monkeypatch.undo()
    recovered = _apply(harness, branch, expected_sha, backup_root, token)

    assert recovered.status == "ok"
    assert recovered.decision == "discarded"
    assert (destination / "receipt.json").is_file()
    assert (destination / "attempt.json").read_bytes() == attempt_before
    receipt = json.loads((destination / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["completion"] == "remote_absent_on_retry"


def test_remote_discard_never_retries_a_receiptless_attempt_when_remote_reappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/unknown-delete-outcome"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, branch, expected_sha, backup_root)

    def ambiguous_push(*_args: object, **_kwargs: object) -> None:
        raise GitRemoteError("the remote may have accepted the deletion")

    monkeypatch.setattr(harness.git, "delete_remote_branch_if_at", ambiguous_push)
    interrupted = _apply(harness, branch, expected_sha, backup_root, token)

    assert interrupted.status == "error"
    assert _blocker_code(interrupted) == "remote_delete_failed"
    assert (destination / "attempt.json").is_file()
    assert not (destination / "receipt.json").exists()
    assert _remote_head(harness.repo, branch) == expected_sha

    def delete_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an ambiguous deletion attempt must never be retried")

    monkeypatch.setattr(harness.git, "delete_remote_branch_if_at", delete_must_not_run)
    rejected = _apply(harness, branch, expected_sha, backup_root, token)

    assert rejected.status == "blocked"
    assert _blocker_code(rejected) == "remote_delete_outcome_unknown"
    assert _remote_head(harness.repo, branch) == expected_sha

    git(harness.repo, "push", "-q", "origin", f":refs/heads/{branch}")
    recovered = _apply(harness, branch, expected_sha, backup_root, token)

    assert recovered.status == "ok"
    assert recovered.decision == "discarded"
    receipt = json.loads((destination / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["completion"] == "remote_absent_on_retry"


def test_remote_discard_retries_confirmed_preconnect_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/preconnect-failure"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, branch, expected_sha, backup_root)
    original_delete = harness.git.delete_remote_branch_if_at

    def no_connection(*_args: object, **_kwargs: object) -> None:
        raise GitRemoteError(
            "git push failed (128): fatal: Could not resolve host: github.com",
            returncode=128,
        )

    monkeypatch.setattr(harness.git, "delete_remote_branch_if_at", no_connection)
    failed = _apply(harness, branch, expected_sha, backup_root, token)
    assert failed.status == "error"
    assert _blocker_code(failed) == "remote_transport_failed"
    assert not (destination / "attempt.json").exists()
    assert _remote_head(harness.repo, branch) == expected_sha

    monkeypatch.setattr(harness.git, "delete_remote_branch_if_at", original_delete)
    retried = _apply(harness, branch, expected_sha, backup_root, token)
    assert retried.status == "ok"
    assert retried.decision == "discarded"
    assert _remote_head(harness.repo, branch) is None


def test_remote_discard_does_not_retry_transport_failure_with_remote_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/remote-status"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, branch, expected_sha, backup_root)

    def rejected_after_status(*_args: object, **_kwargs: object) -> None:
        raise GitRemoteError(
            "git push failed (128): fatal: Authentication failed for remote\n"
            "To https://example.test/repo.git\n"
            " ! [remote rejected] retired/remote-status",
            returncode=128,
        )

    monkeypatch.setattr(harness.git, "delete_remote_branch_if_at", rejected_after_status)
    failed = _apply(harness, branch, expected_sha, backup_root, token)
    assert failed.status == "error"
    assert _blocker_code(failed) == "remote_delete_failed"
    assert (destination / "attempt.json").is_file()
    retry = _apply(harness, branch, expected_sha, backup_root, token)
    assert retry.status == "blocked"
    assert _blocker_code(retry) == "remote_delete_outcome_unknown"


def test_remote_discard_preserves_attempt_after_remote_hook_rejects_post_delete(
    tmp_path: Path,
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/remote-hook-delete"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, branch, expected_sha, backup_root)
    hook = tmp_path / "origin.git" / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "read old new ref\n"
        "git update-ref -d \"$ref\" \"$old\"\n"
        "printf '%s\\n' 'Authentication failed for remote; stale info' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    hook.chmod(0o700)

    failed = _apply(harness, branch, expected_sha, backup_root, token)
    assert failed.status == "error"
    assert _blocker_code(failed) == "remote_delete_failed"
    assert (destination / "attempt.json").is_file()
    assert _remote_head(harness.repo, branch) is None
    hook.unlink()
    git(harness.repo, "push", "-q", "origin", f"{branch}:refs/heads/{branch}")
    retry = _apply(harness, branch, expected_sha, backup_root, token)
    assert retry.status == "blocked"
    assert _blocker_code(retry) == "remote_delete_outcome_unknown"
    assert _remote_head(harness.repo, branch) == expected_sha


def test_remote_discard_keeps_same_identity_lease_with_missing_root(tmp_path: Path) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/missing-lease-root"
    expected_sha = _create_remote_branch(harness, branch)
    _register_lease(
        harness,
        initiative="missing-root",
        branch=branch,
        repository_root=tmp_path / "missing-root",
    )
    result = harness.service.discard_remote_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=_backup_root(tmp_path),
        reason=_REASON,
    )
    assert result.status == "blocked"
    assert _blocker_code(result) == "lease_not_removed"
    assert _remote_head(harness.repo, branch) == expected_sha


def test_remote_discard_from_linked_worktree_respects_active_main_lease(tmp_path: Path) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/linked-active"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    linked = tmp_path / "linked"
    git(harness.repo, "worktree", "add", "-q", "-b", "linked-helper", str(linked), "staging")
    harness.service.git = GitClient(linked)
    token, _ = _preview(harness, branch, expected_sha, backup_root)
    _register_lease(harness, initiative="linked-active", branch=branch)

    result = harness.service.discard_remote_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=backup_root,
        reason=_REASON,
    )

    assert result.status == "blocked"
    assert _blocker_code(result) == "lease_not_removed"
    applied = _apply(harness, branch, expected_sha, backup_root, token)
    assert applied.status == "blocked"
    assert _blocker_code(applied) == "lease_not_removed"
    assert _remote_head(harness.repo, branch) == expected_sha


def test_bare_common_dir_linked_worktree_status_and_branch_previews(
    tmp_path: Path,
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    remote_branch = "retired/bare-remote"
    remote_sha = _create_remote_branch(harness, remote_branch)
    bare = tmp_path / "bare-clone.git"
    linked = tmp_path / "bare-linked"
    git(tmp_path, "clone", "--bare", "-q", str(tmp_path / "origin.git"), str(bare))
    git(bare, "worktree", "add", "-q", "-b", "bare-helper", str(linked), "staging")
    git(bare, "fetch", "-q", "origin", "staging:refs/remotes/origin/staging")
    git(bare, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/staging")
    linked_git = GitClient(linked)
    harness.service.git = linked_git
    assert linked_git.common_git_directory() == bare.resolve()
    assert harness.service.status().decision == "no_op"

    backup_root = _backup_root(tmp_path)
    remote_preview = harness.service.discard_remote_branch(
        remote_branch,
        expected_sha=remote_sha,
        backup_root=backup_root,
        reason=_REASON,
    )
    assert remote_preview.status == "ok"
    assert remote_preview.decision == "preview"
    local_branch = "retired/bare-local"
    git(linked, "branch", local_branch, "staging")
    local_sha = linked_git.local_branch_sha(local_branch)
    assert local_sha is not None
    local_preview = harness.service.discard_local_branch(
        local_branch,
        expected_sha=local_sha,
        backup_root=backup_root,
        reason=_REASON,
    )
    assert local_preview.status == "ok"
    assert local_preview.decision == "preview"
    acquired = harness.service.acquire(
        initiative="bare-clone-feature",
        purpose=Purpose.FEATURE,
        base="staging",
        branch=None,
        owner_id="bare-test",
        apply=True,
    )
    assert acquired.status == "ok"
    assert acquired.decision == "ready"
    assert acquired.lease is not None
    assert acquired.lease.repository_root == linked.resolve()


def test_remote_discard_rejects_foreign_identity_sharing_origin(tmp_path: Path) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/shared-origin-lease"
    expected_sha = _create_remote_branch(harness, branch)
    foreign = tmp_path / "other-repository"
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(foreign))
    primary_git = harness.git
    harness.git = GitClient(foreign)
    _register_lease(harness, initiative="shared-origin", branch=branch)
    harness.git = primary_git

    result = harness.service.discard_remote_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=_backup_root(tmp_path),
        reason=_REASON,
    )

    assert result.status == "blocked"
    assert _blocker_code(result) == "repository_mismatch"
    assert _remote_head(harness.repo, branch) == expected_sha


@pytest.mark.parametrize("foreign_case", ("missing_unrelated_root", "removed_shared_origin"))
def test_remote_discard_ignores_unretained_removed_foreign_lease(
    tmp_path: Path, foreign_case: str
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/removed-foreign-lease"
    expected_sha = _create_remote_branch(harness, branch)
    if foreign_case == "missing_unrelated_root":
        other_parent = tmp_path / "unrelated"
        other_parent.mkdir()
        foreign = make_repository(other_parent)
    else:
        foreign = tmp_path / "shared-origin-clone"
        git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(foreign))
    primary_git = harness.git
    harness.git = GitClient(foreign)
    lease = _register_lease(harness, initiative=foreign_case, branch=branch)
    harness.git = primary_git
    harness.registry.transition(
        lease.id, LeaseState.REMOVED, expected_version=lease.version
    )
    if foreign_case == "missing_unrelated_root":
        foreign.rename(tmp_path / "moved-unrelated")

    preview = harness.service.discard_remote_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=_backup_root(tmp_path),
        reason=_REASON,
    )

    assert preview.status == "ok"
    assert preview.decision == "preview"
    assert _remote_head(harness.repo, branch) == expected_sha


def test_remote_discard_blocks_retained_lease_in_separate_clone(tmp_path: Path) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/retained-foreign"
    expected_sha = _create_remote_branch(harness, branch)
    foreign = tmp_path / "shared-origin-clone"
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(foreign))
    primary_git = harness.git
    harness.git = GitClient(foreign)
    lease = _register_lease(harness, initiative="retained-foreign", branch=branch)
    harness.git = primary_git
    harness.registry.transition(
        lease.id, LeaseState.REMOVED, expected_version=lease.version, retain=True
    )
    result = harness.service.discard_remote_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=_backup_root(tmp_path),
        reason=_REASON,
    )
    assert result.status == "blocked"
    assert _blocker_code(result) == "repository_mismatch"
    assert _remote_head(harness.repo, branch) == expected_sha


@pytest.mark.parametrize(
    "scenario",
    (
        "live_worktree",
        "related_live",
        "lease_not_removed",
        "retained_lease",
        "cleanup_reserved",
        "active_release",
        "open_pull_request",
        "provider_failure",
    ),
)
def test_remote_discard_blocks_preflight_hazards_without_creating_a_backup(
    tmp_path: Path, scenario: str
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/preflight-hazard"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path)

    if scenario == "live_worktree":
        git(
            harness.repo,
            "worktree",
            "add",
            "-q",
            str(tmp_path / "unregistered-live"),
            branch,
        )
    elif scenario == "related_live":
        _register_lease(
            harness,
            initiative="related-live-release",
            branch="related/live-branch",
            base_ref=branch,
        )
    elif scenario == "lease_not_removed":
        _register_lease(harness, initiative="registered-target", branch=branch)
    elif scenario == "retained_lease":
        lease = _register_lease(harness, initiative="retained-target", branch=branch)
        harness.registry.transition(
            lease.id,
            LeaseState.REMOVED,
            expected_version=lease.version,
            retain=True,
        )
    elif scenario == "cleanup_reserved":
        lease = _register_lease(harness, initiative="reserved-target", branch=branch)
        harness.registry.reserve_cleanup(
            lease.id,
            expected_version=lease.version,
            branch_sha=expected_sha,
        )
    elif scenario == "active_release":
        release_lease = _register_lease(
            harness,
            initiative="release-fence",
            branch=branch,
        )
        harness.registry.create_release(
            ReleaseBridge.new(
                repository_id=harness.git.repository_id(),
                repository_name=harness.git.repository_name(),
                repository_root=harness.git.repository_root(),
                release_id="fence",
                target_branch=branch,
                lease_id=release_lease.id,
            )
        )
        harness.registry.transition(
            release_lease.id,
            LeaseState.REMOVED,
            expected_version=release_lease.version,
        )
    elif scenario == "open_pull_request":
        harness.github.pull_requests = (_open_pull_request(branch, expected_sha),)
    else:
        assert scenario == "provider_failure"
        harness.github.failure = ExternalServiceError("provider is unavailable")

    result = harness.service.discard_remote_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=backup_root,
        reason=_REASON,
    )

    expected_code = {
        "live_worktree": "live_worktree",
        "related_live": "protected_branch",
        "lease_not_removed": "lease_not_removed",
        "retained_lease": "retained_lease",
        "cleanup_reserved": "cleanup_reserved",
        "active_release": "active_release",
        "open_pull_request": "open_pull_request",
        "provider_failure": "github_refresh_failed",
    }[scenario]
    assert _blocker_code(result) == expected_code
    if scenario == "provider_failure":
        assert result.exit_code == 4
    else:
        assert result.status == "blocked"
    assert _remote_head(harness.repo, branch) == expected_sha
    assert list(backup_root.iterdir()) == []




def test_remote_discard_blocks_protected_and_archive_named_branches(
    tmp_path: Path,
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    backup_root = _backup_root(tmp_path)

    for branch in (
        "staging",
        "main",
        "release/2026-09",
        "release-archive-2026-09",
        "staging-archive-2026-09",
    ):
        expected_sha = (
            harness.git.head_sha()
            if branch == "staging"
            else _create_remote_branch(harness, branch)
        )
        result = harness.service.discard_remote_branch(
            branch,
            expected_sha=expected_sha,
            backup_root=backup_root,
            reason=_REASON,
        )

        assert result.status == "blocked"
        assert _blocker_code(result) == "protected_branch"
        assert _remote_head(harness.repo, branch) == expected_sha
    assert list(backup_root.iterdir()) == []


def test_remote_discard_revalidates_origin_and_remote_head_before_writing(
    tmp_path: Path,
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/origin-and-head-drift"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, branch, expected_sha, backup_root)

    replacement_origin = tmp_path / "replacement-origin.git"
    git(tmp_path, "init", "--bare", "-q", str(replacement_origin))
    git(
        harness.repo,
        "push",
        "-q",
        str(replacement_origin),
        f"{branch}:refs/heads/{branch}",
    )
    git(harness.repo, "remote", "set-url", "origin", str(replacement_origin))
    origin_changed = _apply(harness, branch, expected_sha, backup_root, token)

    assert origin_changed.status == "blocked"
    assert _blocker_code(origin_changed) == "preview_token_mismatch"
    assert not destination.exists()
    assert (
        git(
            harness.repo.parent / "origin.git",
            "rev-parse",
            f"refs/heads/{branch}",
        )
        == expected_sha
    )

    (tmp_path / "head-drift").mkdir()
    harness = RemoteDiscardHarness.create(tmp_path / "head-drift")
    branch = "retired/head-drift"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path / "head-drift")
    token, destination = _preview(harness, branch, expected_sha, backup_root)
    _, advanced_sha = _advance_remote_from_second_clone(
        tmp_path / "head-drift", harness, branch, name="upstream-head-drift"
    )
    before_tracking_refs = git(
        harness.repo,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        "refs/remotes/origin",
    )
    remote_changed = _apply(harness, branch, expected_sha, backup_root, token)

    assert remote_changed.status == "blocked"
    assert _blocker_code(remote_changed) == "remote_head_changed"
    assert _remote_head(harness.repo, branch) == advanced_sha
    assert (
        git(
            harness.repo,
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/remotes/origin",
        )
        == before_tracking_refs
    )
    assert not destination.exists()


@pytest.mark.parametrize("push_target", ("different", "multiple"))
def test_remote_discard_blocks_different_or_multiple_push_targets(
    tmp_path: Path, push_target: str
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/push-target"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    alternate_remote = _create_matching_push_remote(
        tmp_path, harness, branch, expected_sha
    )
    git(
        harness.repo,
        "config",
        "--add",
        "remote.origin.pushurl",
        str(alternate_remote),
    )
    if push_target == "multiple":
        git(
            harness.repo,
            "config",
            "--add",
            "remote.origin.pushurl",
            str(harness.repo.parent / "origin.git"),
        )

    blocked = harness.service.discard_remote_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=backup_root,
        reason=_REASON,
    )

    assert blocked.status == "blocked"
    assert _blocker_code(blocked) == "push_remote_mismatch"
    assert (
        git(
            harness.repo.parent / "origin.git",
            "rev-parse",
            f"refs/heads/{branch}",
        )
        == expected_sha
    )
    assert (
        git(alternate_remote, "rev-parse", f"refs/heads/{branch}") == expected_sha
    )
    assert list(backup_root.iterdir()) == []


def test_remote_discard_blocks_push_target_changed_after_preview(
    tmp_path: Path,
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/push-target-preview-drift"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, branch, expected_sha, backup_root)
    alternate_remote = _create_matching_push_remote(
        tmp_path, harness, branch, expected_sha
    )
    git(
        harness.repo,
        "config",
        "--add",
        "remote.origin.pushurl",
        str(alternate_remote),
    )

    blocked = _apply(harness, branch, expected_sha, backup_root, token)

    assert blocked.status == "blocked"
    assert _blocker_code(blocked) == "push_remote_mismatch"
    assert not destination.exists()
    assert (
        git(
            harness.repo.parent / "origin.git",
            "rev-parse",
            f"refs/heads/{branch}",
        )
        == expected_sha
    )
    assert (
        git(alternate_remote, "rev-parse", f"refs/heads/{branch}") == expected_sha
    )


def test_remote_discard_rechecks_push_target_before_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/push-target-late-drift"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    alternate_remote = _create_matching_push_remote(
        tmp_path, harness, branch, expected_sha
    )
    token, destination = _preview(harness, branch, expected_sha, backup_root)
    original_create = harness.git.create_detached_commit_bundle

    def create_then_change_push_target(
        destination_path: Path, *, commit_sha: str
    ) -> None:
        original_create(destination_path, commit_sha=commit_sha)
        git(
            harness.repo,
            "config",
            "--add",
            "remote.origin.pushurl",
            str(alternate_remote),
        )

    monkeypatch.setattr(
        harness.git,
        "create_detached_commit_bundle",
        create_then_change_push_target,
    )
    blocked = _apply(harness, branch, expected_sha, backup_root, token)

    assert blocked.status == "blocked"
    assert _blocker_code(blocked) == "push_remote_mismatch"
    assert (destination / "history.bundle").is_file()
    assert (destination / "manifest.json").is_file()
    assert not (destination / "attempt.json").exists()
    assert not (destination / "receipt.json").exists()
    assert (
        git(
            harness.repo.parent / "origin.git",
            "rev-parse",
            f"refs/heads/{branch}",
        )
        == expected_sha
    )
    assert (
        git(alternate_remote, "rev-parse", f"refs/heads/{branch}") == expected_sha
    )


@pytest.mark.parametrize(
    ("injection", "expected_code"),
    (("live_worktree", "live_worktree"), ("open_pull_request", "open_pull_request")),
)
def test_remote_discard_rechecks_late_safety_evidence_before_writing_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    injection: str,
    expected_code: str,
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/late-evidence"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, branch, expected_sha, backup_root)
    original_create = harness.git.create_detached_commit_bundle

    def create_then_inject(destination_path: Path, *, commit_sha: str) -> None:
        original_create(destination_path, commit_sha=commit_sha)
        if injection == "live_worktree":
            git(
                harness.repo,
                "worktree",
                "add",
                "-q",
                str(tmp_path / "late-unregistered-live"),
                branch,
            )
        else:
            harness.github.pull_requests = (_open_pull_request(branch, expected_sha),)

    monkeypatch.setattr(
        harness.git,
        "create_detached_commit_bundle",
        create_then_inject,
    )
    blocked = _apply(harness, branch, expected_sha, backup_root, token)

    assert blocked.status == "blocked"
    assert _blocker_code(blocked) == expected_code
    assert (destination / "history.bundle").is_file()
    assert (destination / "manifest.json").is_file()
    assert not (destination / "attempt.json").exists()
    assert not (destination / "receipt.json").exists()
    assert _remote_head(harness.repo, branch) == expected_sha


def test_remote_discard_cas_preserves_a_real_remote_race_after_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/cas-race"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, branch, expected_sha, backup_root)
    upstream = tmp_path / "upstream-racer"
    origin = harness.repo.parent / "origin.git"
    git(tmp_path, "clone", "-q", str(origin), str(upstream))
    git(upstream, "config", "user.email", "racer@example.com")
    git(upstream, "config", "user.name", "Racing Writer")
    git(upstream, "checkout", "-q", "-b", branch, f"origin/{branch}")
    original_delete = harness.git.delete_remote_branch_if_at

    def race_before_delete(
        requested_branch: str, requested_sha: str, *, skip_hooks: bool = False
    ) -> None:
        assert requested_branch == branch
        assert requested_sha == expected_sha
        assert skip_hooks is True
        (upstream / "race.txt").write_text("preserve this commit\n", encoding="utf-8")
        git(upstream, "add", "race.txt")
        git(upstream, "commit", "-q", "-m", "race the remote deletion")
        git(upstream, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        original_delete(requested_branch, requested_sha, skip_hooks=skip_hooks)

    monkeypatch.setattr(harness.git, "delete_remote_branch_if_at", race_before_delete)
    raced = _apply(harness, branch, expected_sha, backup_root, token)

    advanced_sha = git(upstream, "rev-parse", "HEAD")
    assert raced.status == "blocked"
    assert _blocker_code(raced) == "remote_head_changed"
    assert _remote_head(harness.repo, branch) == advanced_sha
    assert (destination / "history.bundle").is_file()
    assert not (destination / "attempt.json").exists()
    assert not (destination / "receipt.json").exists()
    git(
        harness.repo, "push", "-q",
        f"--force-with-lease=refs/heads/{branch}:{advanced_sha}",
        "origin", f"{expected_sha}:refs/heads/{branch}",
    )
    monkeypatch.setattr(harness.git, "delete_remote_branch_if_at", original_delete)
    retried = _apply(harness, branch, expected_sha, backup_root, token)
    assert retried.status == "ok"
    assert retried.decision == "discarded"
    assert _remote_head(harness.repo, branch) is None


def test_remote_discard_refuses_unknown_remote_commit_without_fetching(
    tmp_path: Path,
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/outside-only"
    origin = harness.repo.parent / "origin.git"
    upstream = tmp_path / "outside-only"
    git(tmp_path, "clone", "-q", str(origin), str(upstream))
    git(upstream, "config", "user.email", "outside@example.com")
    git(upstream, "config", "user.name", "Outside Writer")
    git(upstream, "checkout", "-q", "-b", branch, "origin/staging")
    (upstream / "outside-only.txt").write_text("not fetched\n", encoding="utf-8")
    git(upstream, "add", "outside-only.txt")
    git(upstream, "commit", "-q", "-m", "publish outside-only target")
    expected_sha = git(upstream, "rev-parse", "HEAD")
    git(upstream, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    backup_root = _backup_root(tmp_path)
    before_refs = git(harness.repo, "for-each-ref", "--format=%(refname) %(objectname)")

    result = harness.service.discard_remote_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=backup_root,
        reason=_REASON,
    )

    assert result.status == "blocked"
    assert _blocker_code(result) == "remote_commit_missing_locally"
    assert _remote_head(harness.repo, branch) == expected_sha
    assert (
        git(harness.repo, "for-each-ref", "--format=%(refname) %(objectname)")
        == before_refs
    )
    assert list(backup_root.iterdir()) == []


def test_remote_discard_rejects_symlinked_namespaces_and_tampered_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/symlinked-namespace"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, branch, expected_sha, backup_root)
    escape = tmp_path / "escape"
    escape.mkdir(mode=0o700)
    namespace = backup_root / harness.git.repository_id()
    namespace.symlink_to(escape, target_is_directory=True)

    unsafe_namespace = _apply(harness, branch, expected_sha, backup_root, token)

    assert unsafe_namespace.status == "blocked"
    assert _blocker_code(unsafe_namespace) == "archive_unsafe"
    assert _remote_head(harness.repo, branch) == expected_sha
    assert not destination.exists()

    (tmp_path / "tampered-bundle").mkdir()

    harness = RemoteDiscardHarness.create(tmp_path / "tampered-bundle")
    branch = "retired/tampered-bundle"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path / "tampered-bundle")
    token, destination = _preview(harness, branch, expected_sha, backup_root)

    def fail_delete(*_args: object, **_kwargs: object) -> None:
        raise GitRemoteError("simulated post-attempt transport failure")

    monkeypatch.setattr(harness.git, "delete_remote_branch_if_at", fail_delete)
    first = _apply(harness, branch, expected_sha, backup_root, token)
    assert first.status == "error"
    assert (destination / "attempt.json").is_file()
    monkeypatch.undo()
    (destination / "history.bundle").write_bytes(b"tampered bundle")

    corrupt = _apply(harness, branch, expected_sha, backup_root, token)

    assert corrupt.status == "blocked"
    assert _blocker_code(corrupt) == "archive_corrupt"
    assert _remote_head(harness.repo, branch) == expected_sha
    assert not (destination / "receipt.json").exists()


def test_remote_discard_rejects_a_tampered_receipt_without_touching_a_recreated_ref(
    tmp_path: Path,
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/tampered-receipt"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, branch, expected_sha, backup_root)
    applied = _apply(harness, branch, expected_sha, backup_root, token)
    assert applied.status == "ok"

    git(
        harness.repo,
        "push",
        "-q",
        "origin",
        f"{expected_sha}:refs/heads/{branch}",
    )
    receipt = destination / "receipt.json"
    receipt.write_text("{}", encoding="utf-8")
    rejected = _apply(harness, branch, expected_sha, backup_root, token)

    assert rejected.status == "blocked"
    assert _blocker_code(rejected) == "archive_corrupt"
    assert _remote_head(harness.repo, branch) == expected_sha


@pytest.mark.parametrize(
    "name,value",
    (
        ("GIT_DIR", "/unsafe/git-dir"),
        ("GIT_WORK_TREE", "/unsafe/worktree"),
        ("GIT_COMMON_DIR", "/unsafe/common-dir"),
        ("GIT_NAMESPACE", "unsafe-namespace"),
        ("GIT_INDEX_FILE", "/unsafe/index"),
        ("GIT_OBJECT_DIRECTORY", "/unsafe/objects"),
        ("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/unsafe/alternates"),
        ("GIT_CONFIG_PARAMETERS", "unsafe.value=one"),
    ),
)
def test_remote_discard_blocks_git_binding_environment_before_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/environment-binding"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    before = _primary_state(harness.repo)
    monkeypatch.setenv(name, value)

    result = harness.service.discard_remote_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=backup_root,
        reason=_REASON,
    )

    assert result.status == "blocked"
    assert _blocker_code(result) == "unsafe_git_environment"
    assert list(backup_root.iterdir()) == []
    assert not harness.lock_dir.exists()
    monkeypatch.undo()
    assert _primary_state(harness.repo) == before
    assert _remote_head(harness.repo, branch) == expected_sha


def test_remote_discard_keeps_auth_environment_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    branch = "retired/auth-environment"
    expected_sha = _create_remote_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    monkeypatch.setenv("GIT_ASKPASS", "/operator/credential-helper")

    token, _ = _preview(harness, branch, expected_sha, backup_root)

    assert token


def test_remote_discard_uses_git_ref_validation_not_only_a_local_pattern(
    tmp_path: Path,
) -> None:
    harness = RemoteDiscardHarness.create(tmp_path)
    backup_root = _backup_root(tmp_path)
    expected_sha = harness.git.head_sha()

    result = harness.service.discard_remote_branch(
        "retired//invalid-ref",
        expected_sha=expected_sha,
        backup_root=backup_root,
        reason=_REASON,
    )

    assert result.status == "blocked"
    assert _blocker_code(result) == "invalid_branch"
    assert list(backup_root.iterdir()) == []
