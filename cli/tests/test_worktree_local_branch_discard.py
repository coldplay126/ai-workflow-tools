from __future__ import annotations

import json
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

import awf.worktrees.local_branch_discard as local_discard_module
from awf.worktrees.config import WorktreeConfig
from awf.worktrees.git import GitClient, GitError
from awf.worktrees.github import ExternalServiceError, PullRequest
from awf.worktrees.models import Lease, LeaseState, Purpose, ReleaseBridge
from awf.worktrees.registry import WorktreeRegistry
from awf.worktrees.service import WorktreeService
from worktree_fixtures import git, make_repository


_REASON = "The approved local branch is no longer needed."


@dataclass
class LocalDiscardGitHub:
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
class LocalDiscardHarness:
    repo: Path
    git: GitClient
    registry: WorktreeRegistry
    cache_dir: Path
    lock_dir: Path
    github: LocalDiscardGitHub
    service: WorktreeService

    @classmethod
    def create(cls, tmp_path: Path) -> LocalDiscardHarness:
        repo = make_repository(tmp_path)
        client = GitClient(repo)
        registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
        cache_dir = tmp_path / "cache"
        lock_dir = tmp_path / "locks"
        github = LocalDiscardGitHub()
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


def _local_head(repo: Path, branch: str) -> str | None:
    output = git(
        repo,
        "for-each-ref",
        "--format=%(objectname)",
        f"refs/heads/{branch}",
    )
    return output or None


def _remote_head(repo: Path, branch: str) -> str | None:
    output = git(repo, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    return output.split()[0] if output else None


def _create_local_branch(harness: LocalDiscardHarness, branch: str) -> str:
    git(harness.repo, "branch", branch, "staging")
    expected_sha = git(harness.repo, "rev-parse", branch)
    assert _local_head(harness.repo, branch) == expected_sha
    return expected_sha


def _publish_local_branch(harness: LocalDiscardHarness, branch: str) -> str:
    expected_sha = _create_local_branch(harness, branch)
    git(harness.repo, "push", "-q", "origin", f"{branch}:refs/heads/{branch}")
    git(harness.repo, "fetch", "-q", "origin")
    assert _remote_head(harness.repo, branch) == expected_sha
    return expected_sha


def _advance_local_branch(
    harness: LocalDiscardHarness, branch: str, expected_sha: str
) -> str:
    tree = git(harness.repo, "rev-parse", f"{expected_sha}^{{tree}}")
    advanced_sha = git(
        harness.repo,
        "commit-tree",
        tree,
        "-p",
        expected_sha,
        "-m",
        "advance local target outside discard",
    )
    git(
        harness.repo,
        "update-ref",
        f"refs/heads/{branch}",
        advanced_sha,
        expected_sha,
    )
    return advanced_sha


def _preview(
    harness: LocalDiscardHarness,
    branch: str,
    expected_sha: str,
    backup_root: Path,
    *,
    reason: str = _REASON,
):
    result = harness.service.discard_local_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=backup_root,
        reason=reason,
    )
    assert result.status == "ok"
    assert result.decision == "preview"
    bundle_action = next(
        action for action in result.actions if action["kind"] == "create_commit_bundle"
    )
    delete_action = next(
        action for action in result.actions if action["kind"] == "delete_local_branch"
    )
    token = bundle_action["preview_token"]
    destination = Path(bundle_action["backup_directory"])
    assert isinstance(token, str)
    assert len(token) == 64
    assert destination.is_relative_to(backup_root)
    assert bundle_action["local_sha"] == expected_sha
    assert delete_action["local_sha"] == expected_sha
    assert "remote_sha" not in bundle_action
    assert "remote_sha" not in delete_action
    assert not destination.exists()
    return token, destination


def _apply(
    harness: LocalDiscardHarness,
    branch: str,
    expected_sha: str,
    backup_root: Path,
    token: str,
    *,
    reason: str = _REASON,
):
    return harness.service.discard_local_branch(
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
    remote_head = git_dir / "refs" / "remotes" / "origin" / "HEAD"
    return {
        "heads": git(
            repo,
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/heads",
        ),
        "remote_refs": git(
            repo,
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/remotes",
        ),
        "remote_head": remote_head.read_bytes(),
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


def _state_without_branch(state: dict[str, object], branch: str) -> dict[str, object]:
    target = f"refs/heads/{branch} "
    retained = dict(state)
    retained["heads"] = "\n".join(
        line
        for line in str(state["heads"]).splitlines()
        if not line.startswith(target)
    )
    return retained


def _restore_commit_bundle(
    bundle: Path,
    destination: Path,
    branch: str,
    *,
    object_format: str | None = None,
) -> str:
    init_arguments = ["init", "--bare", "-q"]
    if object_format is not None:
        init_arguments.append(f"--object-format={object_format}")
    git(destination.parent, *init_arguments, str(destination))
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
    harness: LocalDiscardHarness,
    *,
    initiative: str,
    branch: str,
    base_ref: str = "staging",
    managed: bool = True,
    owner_kind: str = "awf",
    purpose: Purpose = Purpose.FEATURE,
    repository_root: Path | None = None,
) -> Lease:
    return harness.registry.create_lease(
        Lease.new(
            repository_id=harness.git.repository_id(),
            repository_name=harness.git.repository_name(),
            repository_root=repository_root or harness.git.repository_root(),
            worktree_path=harness.repo.parent / f"registry-{initiative}",
            initiative=initiative,
            purpose=purpose,
            branch=branch,
            base_ref=base_ref,
            head_sha=harness.git.head_sha(),
            managed=managed,
            owner_kind=owner_kind,
            owner_id="local-discard-test",
        )
    )


def _open_pull_request(branch: str, head_sha: str) -> PullRequest:
    return PullRequest(
        number=914,
        state="OPEN",
        base_ref="staging",
        base_sha=head_sha,
        head_ref=branch,
        head_sha=head_sha,
        merge_commit_sha=None,
        review_decision="",
        checks_passed=True,
        changed_paths=(),
        url="https://github.example/acme/repo/pull/914",
    )


def _create_receiptless_attempt(
    harness: LocalDiscardHarness,
    branch: str,
    expected_sha: str,
    backup_root: Path,
    token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    original_delete = harness.git.delete_inactive_branch_if_at

    def write_attempt_then_fail(
        requested_branch: str,
        requested_sha: str,
        *,
        before_commit: Callable[[], None],
    ) -> None:
        assert requested_branch == branch
        assert requested_sha == expected_sha
        assert callable(before_commit)
        before_commit()
        raise GitError("simulate an unconfirmed local compare-and-delete")

    monkeypatch.setattr(
        harness.git, "delete_inactive_branch_if_at", write_attempt_then_fail
    )
    failed = _apply(harness, branch, expected_sha, backup_root, token)
    assert failed.status == "blocked"
    assert _local_head(harness.repo, branch) == expected_sha
    monkeypatch.setattr(
        harness.git, "delete_inactive_branch_if_at", original_delete
    )
    action = next(
        action for action in failed.actions if action["kind"] == "create_commit_bundle"
    )
    return Path(action["backup_directory"])


def test_local_discard_preview_is_read_only_and_apply_preserves_repository_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired-local-consumer"
    expected_sha = _publish_local_branch(harness, branch)
    preserved_branch = "keep/unrelated-local-head"
    preserved_sha = _create_local_branch(harness, preserved_branch)
    alternate_push = tmp_path / "alternate-push.git"
    git(tmp_path, "init", "--bare", "-q", str(alternate_push))
    git(
        harness.repo,
        "push",
        "-q",
        str(alternate_push),
        f"{branch}:refs/heads/{branch}",
    )
    git(harness.repo, "config", "--add", "remote.origin.pushurl", str(alternate_push))
    git(harness.repo, "config", f"branch.{branch}.discard-note", "preserve")
    backup_root = _backup_root(tmp_path)
    before_preview = _primary_state(harness.repo)

    token, destination = _preview(harness, branch, expected_sha, backup_root)

    assert _primary_state(harness.repo) == before_preview
    assert list(backup_root.iterdir()) == []
    assert not harness.lock_dir.exists()
    assert not harness.registry.db_path.exists()

    marker = harness.repo / ".git" / "hooks" / "reference-transaction-ran"
    hook = harness.repo / ".git" / "hooks" / "reference-transaction"
    hook.write_text(
        f"#!/bin/sh\nprintf isolated-hook > {str(marker)!r}\nexit 1\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    before_apply = _state_without_branch(_primary_state(harness.repo), branch)

    def remote_delete_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("local discard must not invoke the remote deletion primitive")

    monkeypatch.setattr(
        harness.git, "delete_remote_branch_if_at", remote_delete_must_not_run
    )
    applied = _apply(harness, branch, expected_sha, backup_root, token)

    assert applied.status == "ok"
    assert applied.decision == "discarded"
    assert _local_head(harness.repo, branch) is None
    assert _remote_head(harness.repo, branch) == expected_sha
    assert git(alternate_push, "rev-parse", f"refs/heads/{branch}") == expected_sha
    assert _local_head(harness.repo, preserved_branch) == preserved_sha
    assert _state_without_branch(_primary_state(harness.repo), branch) == before_apply
    assert not marker.exists()
    assert destination.is_dir()
    _assert_private(destination, 0o700)
    for name in ("history.bundle", "manifest.json", "attempt.json", "receipt.json"):
        artifact = destination / name
        assert artifact.is_file()
        _assert_private(artifact, 0o600)
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["metadata"]["local_sha"] == expected_sha
    assert "remote_sha" not in manifest["metadata"]
    assert _restore_commit_bundle(
        destination / "history.bundle", tmp_path / "restored-local.git", branch
    ) == expected_sha
    payload = json.dumps(applied.to_dict(), sort_keys=True)
    assert _REASON not in payload
    assert "remote_sha" not in payload


def test_local_discard_handles_a_local_only_branch_and_sixteen_removed_records(
    tmp_path: Path,
) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/local-only-with-registry"
    expected_sha = _create_local_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    removed: list[Lease] = []
    for number in range(16):
        imported = number % 2 == 1
        registered = _register_lease(
            harness,
            initiative=f"{'imported' if imported else 'custom'}-removed-{number}",
            branch=branch,
            managed=not imported,
            owner_kind="imported" if imported else "awf",
            purpose=Purpose.SCRATCH if imported else Purpose.FEATURE,
        )
        removed.append(
            harness.registry.transition(
                registered.id,
                LeaseState.REMOVED,
                expected_version=registered.version,
            )
        )

    assert len(removed) == 16
    assert all(lease.state is LeaseState.REMOVED for lease in removed)
    assert all(lease.retain is False for lease in removed)
    assert all(
        harness.registry.get_cleanup_reservation(lease.id) is None for lease in removed
    )
    assert _remote_head(harness.repo, branch) is None
    token, destination = _preview(harness, branch, expected_sha, backup_root)
    applied = _apply(harness, branch, expected_sha, backup_root, token)

    assert applied.status == "ok"
    assert _local_head(harness.repo, branch) is None
    assert _remote_head(harness.repo, branch) is None
    assert _restore_commit_bundle(
        destination / "history.bundle", tmp_path / "restored-local-only.git", branch
    ) == expected_sha

def test_local_discard_keeps_same_identity_lease_with_missing_root(tmp_path: Path) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/missing-lease-root"
    expected_sha = _create_local_branch(harness, branch)
    _register_lease(
        harness,
        initiative="missing-root",
        branch=branch,
        repository_root=tmp_path / "missing-root",
    )
    result = harness.service.discard_local_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=_backup_root(tmp_path),
        reason=_REASON,
    )
    assert result.status == "blocked"
    assert _blocker_code(result) == "lease_not_removed"
    assert _local_head(harness.repo, branch) == expected_sha


def test_local_discard_blocks_active_legacy_lease_after_linked_root_removed(
    tmp_path: Path,
) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/missing-legacy-root"
    expected_sha = _create_local_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, branch, expected_sha, backup_root)
    linked = tmp_path / "legacy-linked"
    git(harness.repo, "worktree", "add", "-q", "-b", "legacy-helper", str(linked), "staging")
    main_git = harness.git
    harness.git = GitClient(linked)
    legacy_lease = _register_lease(harness, initiative="legacy-active", branch=branch)
    assert legacy_lease.repository_id != main_git.repository_id()
    harness.git = main_git
    git(harness.repo, "worktree", "remove", "--force", str(linked))
    assert not linked.exists()

    preview = harness.service.discard_local_branch(
        branch, expected_sha=expected_sha, backup_root=backup_root, reason=_REASON
    )
    assert preview.status == "blocked"
    assert _blocker_code(preview) == "lease_not_removed"
    assert legacy_lease.id in preview.blockers[0]["message"]
    assert str(linked) in preview.blockers[0]["message"]
    applied = _apply(harness, branch, expected_sha, backup_root, token)
    assert applied.status == "blocked"
    assert _blocker_code(applied) == "lease_not_removed"
    assert _local_head(harness.repo, branch) == expected_sha
    assert not destination.exists()


def test_local_discard_keeps_same_store_lease_after_origin_url_format_change(
    tmp_path: Path,
) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/origin-format-change"
    expected_sha = _create_local_branch(harness, branch)
    lease = _register_lease(harness, initiative="origin-format", branch=branch)
    git(
        harness.repo,
        "remote",
        "set-url",
        "origin",
        f"file://{tmp_path / 'origin.git'}",
    )
    assert lease.repository_id != harness.git.repository_id()

    preview = harness.service.discard_local_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=_backup_root(tmp_path),
        reason=_REASON,
    )
    assert preview.status == "blocked"
    assert _blocker_code(preview) == "lease_not_removed"
    assert _local_head(harness.repo, branch) == expected_sha


def test_local_discard_from_linked_worktree_respects_active_main_lease(tmp_path: Path) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/linked-active"
    expected_sha = _create_local_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    linked = tmp_path / "linked"
    git(harness.repo, "worktree", "add", "-q", "-b", "linked-helper", str(linked), "staging")
    harness.service.git = GitClient(linked)
    token, _ = _preview(harness, branch, expected_sha, backup_root)
    _register_lease(harness, initiative="linked-active", branch=branch)

    result = harness.service.discard_local_branch(
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
    assert _local_head(harness.repo, branch) == expected_sha



def test_local_discard_protects_nonstandard_origin_default(tmp_path: Path) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "trunk-default"
    expected_sha = _publish_local_branch(harness, branch)
    git(tmp_path / "origin.git", "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    git(harness.repo, "remote", "set-head", "origin", "-a")

    result = harness.service.discard_local_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=_backup_root(tmp_path),
        reason=_REASON,
    )

    assert result.status == "blocked"
    assert _blocker_code(result) == "protected_branch"
    assert _local_head(harness.repo, branch) == expected_sha


def test_local_discard_blocks_when_origin_default_cannot_be_read(tmp_path: Path) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/unknown-default"
    expected_sha = _create_local_branch(harness, branch)
    git(harness.repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")

    result = harness.service.discard_local_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=_backup_root(tmp_path),
        reason=_REASON,
    )

    assert result.status == "blocked"
    assert _blocker_code(result) == "default_branch_unknown"
    assert _local_head(harness.repo, branch) == expected_sha


def test_local_discard_ignores_active_lease_in_separate_clone(tmp_path: Path) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/shared-origin-lease"
    expected_sha = _create_local_branch(harness, branch)
    foreign = tmp_path / "other-repository"
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(foreign))
    primary_git = harness.git
    harness.git = GitClient(foreign)
    _register_lease(harness, initiative="shared-origin", branch=branch)
    harness.git = primary_git

    backup_root = _backup_root(tmp_path)
    token, _ = _preview(harness, branch, expected_sha, backup_root)
    applied = _apply(harness, branch, expected_sha, backup_root, token)

    assert applied.status == "ok"
    assert applied.decision == "discarded"
    assert _local_head(harness.repo, branch) is None
    assert harness.registry.list_leases_read_only(include_removed=False)[0].state is LeaseState.ACTIVE

@pytest.mark.parametrize("foreign_case", ("missing_unrelated_root", "removed_shared_origin"))
def test_local_discard_ignores_unretained_removed_foreign_lease(
    tmp_path: Path, foreign_case: str
) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/removed-foreign-lease"
    expected_sha = _create_local_branch(harness, branch)
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

    preview = harness.service.discard_local_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=_backup_root(tmp_path),
        reason=_REASON,
    )

    assert preview.status == "ok"
    assert preview.decision == "preview"
    assert _local_head(harness.repo, branch) == expected_sha



@pytest.mark.parametrize(
    "scenario",
    (
        "current_worktree",
        "unregistered_checkout",
        "lease_not_removed",
        "retained_lease",
        "cleanup_reserved",
        "active_release",
        "open_pull_request",
        "provider_failure",
    ),
)
def test_local_discard_blocks_live_and_registry_hazards_without_a_backup(
    tmp_path: Path, scenario: str
) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/local-preflight-hazard"
    expected_sha = _create_local_branch(harness, branch)
    backup_root = _backup_root(tmp_path)

    if scenario == "current_worktree":
        git(harness.repo, "checkout", "-q", branch)
    elif scenario == "unregistered_checkout":
        git(
            harness.repo,
            "worktree",
            "add",
            "-q",
            str(tmp_path / "unregistered-live"),
            branch,
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
            initiative="release-local-fence",
            branch=branch,
        )
        harness.registry.create_release(
            ReleaseBridge.new(
                repository_id=harness.git.repository_id(),
                repository_name=harness.git.repository_name(),
                repository_root=harness.git.repository_root(),
                release_id="local-fence",
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

    result = harness.service.discard_local_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=backup_root,
        reason=_REASON,
    )

    expected_code = {
        "current_worktree": "live_worktree",
        "unregistered_checkout": "live_worktree",
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
    assert _local_head(harness.repo, branch) == expected_sha
    assert list(backup_root.iterdir()) == []


def test_local_discard_blocks_protected_and_symbolic_branches_without_dereferencing(
    tmp_path: Path,
) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
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
            else _create_local_branch(harness, branch)
        )
        result = harness.service.discard_local_branch(
            branch,
            expected_sha=expected_sha,
            backup_root=backup_root,
            reason=_REASON,
        )

        assert result.status == "blocked"
        assert _blocker_code(result) == "protected_branch"
        assert _local_head(harness.repo, branch) == expected_sha

    main_sha = _local_head(harness.repo, "main")
    assert main_sha is not None
    symbolic = "retired/symbolic-main"
    git(harness.repo, "symbolic-ref", f"refs/heads/{symbolic}", "refs/heads/main")
    before_main = _local_head(harness.repo, "main")
    before_symbolic = (harness.repo / ".git" / "refs" / "heads" / symbolic).read_bytes()

    symbolic_result = harness.service.discard_local_branch(
        symbolic,
        expected_sha=main_sha,
        backup_root=backup_root,
        reason=_REASON,
    )

    assert symbolic_result.status == "blocked"
    assert _local_head(harness.repo, "main") == before_main
    assert (harness.repo / ".git" / "refs" / "heads" / symbolic).read_bytes() == before_symbolic
    assert list(backup_root.iterdir()) == []


def test_local_discard_blocks_expected_sha_drift_before_and_after_preview(
    tmp_path: Path,
) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/local-sha-drift"
    expected_sha = _create_local_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    advanced_sha = _advance_local_branch(harness, branch, expected_sha)

    stale_preview = harness.service.discard_local_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=backup_root,
        reason=_REASON,
    )

    assert stale_preview.status == "blocked"
    assert _blocker_code(stale_preview) == "local_head_mismatch"
    assert _local_head(harness.repo, branch) == advanced_sha
    assert list(backup_root.iterdir()) == []

    (tmp_path / "after-preview").mkdir()
    harness = LocalDiscardHarness.create(tmp_path / "after-preview")
    branch = "retired/local-sha-drift-after-preview"
    expected_sha = _create_local_branch(harness, branch)
    backup_root = _backup_root(tmp_path / "after-preview")
    token, destination = _preview(harness, branch, expected_sha, backup_root)
    advanced_sha = _advance_local_branch(harness, branch, expected_sha)

    stale_apply = _apply(harness, branch, expected_sha, backup_root, token)

    assert stale_apply.status == "blocked"
    assert _blocker_code(stale_apply) == "local_head_changed"
    assert _local_head(harness.repo, branch) == advanced_sha
    assert not destination.exists()


def test_local_discard_writes_attempt_inside_guard_callback_and_never_retries_unknown_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/local-guard-callback"
    expected_sha = _create_local_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, branch, expected_sha, backup_root)

    def callback_then_fail(
        requested_branch: str,
        requested_sha: str,
        *,
        before_commit: Callable[[], None],
    ) -> None:
        assert requested_branch == branch
        assert requested_sha == expected_sha
        assert callable(before_commit)
        before_commit()
        raise GitError("simulate callback path failing before ref deletion")

    monkeypatch.setattr(
        harness.git, "delete_inactive_branch_if_at", callback_then_fail
    )
    interrupted = _apply(harness, branch, expected_sha, backup_root, token)

    assert interrupted.status == "blocked"
    assert _blocker_code(interrupted) == "local_delete_failed"
    assert _local_head(harness.repo, branch) == expected_sha
    assert (destination / "attempt.json").is_file()
    assert not (destination / "receipt.json").exists()

    def delete_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a receiptless local deletion attempt must never be retried")

    monkeypatch.setattr(
        harness.git, "delete_inactive_branch_if_at", delete_must_not_run
    )
    rejected = _apply(harness, branch, expected_sha, backup_root, token)

    assert rejected.status == "blocked"
    assert _blocker_code(rejected) == "local_delete_outcome_unknown"
    assert _local_head(harness.repo, branch) == expected_sha


def test_local_discard_retries_aborted_transaction_after_attempt_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/aborted-transaction"
    expected_sha = _create_local_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, branch, expected_sha, backup_root)
    original_revalidate = harness.git._revalidate_inactive_branch
    inspections = 0

    def fail_after_attempt(*args: object, **kwargs: object) -> None:
        nonlocal inspections
        inspections += 1
        if inspections == 2:
            assert (destination / "attempt.json").is_file()
            raise GitError("worktree inventory changed before commit")
        original_revalidate(*args, **kwargs)

    monkeypatch.setattr(harness.git, "_revalidate_inactive_branch", fail_after_attempt)
    aborted = _apply(harness, branch, expected_sha, backup_root, token)
    assert aborted.status == "blocked"
    assert _blocker_code(aborted) == "local_delete_failed"
    assert _local_head(harness.repo, branch) == expected_sha
    assert not (destination / "attempt.json").exists()

    monkeypatch.setattr(harness.git, "_revalidate_inactive_branch", original_revalidate)
    retried = _apply(harness, branch, expected_sha, backup_root, token)
    assert retried.status == "ok"
    assert retried.decision == "discarded"
    assert _local_head(harness.repo, branch) is None


def test_local_discard_recovers_absent_ref_after_receipt_write_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/local-receipt-crash"
    expected_sha = _create_local_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, branch, expected_sha, backup_root)

    def crash_before_receipt(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulate a crash after local ref deletion")

    monkeypatch.setattr(
        local_discard_module.LocalBranchDiscarder,
        "_write_receipt",
        crash_before_receipt,
    )
    interrupted = _apply(harness, branch, expected_sha, backup_root, token)

    assert interrupted.status == "blocked"
    assert _local_head(harness.repo, branch) is None
    assert (destination / "attempt.json").is_file()
    assert not (destination / "receipt.json").exists()

    monkeypatch.undo()
    recovered = _apply(harness, branch, expected_sha, backup_root, token)

    assert recovered.status == "ok"
    assert recovered.decision == "discarded"
    receipt = json.loads((destination / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["completion"] == "local_absent_on_retry"


def test_local_discard_rejects_symlinked_and_tampered_backup_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/local-symlinked-namespace"
    expected_sha = _create_local_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, branch, expected_sha, backup_root)
    escape = tmp_path / "escape"
    escape.mkdir(mode=0o700)
    namespace = backup_root / harness.git.repository_id() / "local-branch-discard"
    namespace.parent.mkdir(mode=0o700)
    namespace.parent.chmod(0o700)
    namespace.symlink_to(escape, target_is_directory=True)

    unsafe_namespace = _apply(harness, branch, expected_sha, backup_root, token)

    assert unsafe_namespace.status == "blocked"
    assert _blocker_code(unsafe_namespace) == "archive_unsafe"
    assert _local_head(harness.repo, branch) == expected_sha
    assert not destination.exists()

    (tmp_path / "tampered-artifact").mkdir()
    harness = LocalDiscardHarness.create(tmp_path / "tampered-artifact")
    branch = "retired/local-tampered-artifact"
    expected_sha = _create_local_branch(harness, branch)
    backup_root = _backup_root(tmp_path / "tampered-artifact")
    token, destination = _preview(harness, branch, expected_sha, backup_root)
    _create_receiptless_attempt(
        harness, branch, expected_sha, backup_root, token, monkeypatch
    )
    (destination / "history.bundle").write_bytes(b"tampered local bundle")

    corrupt = _apply(harness, branch, expected_sha, backup_root, token)

    assert corrupt.status == "blocked"
    assert _blocker_code(corrupt) == "archive_corrupt"
    assert _local_head(harness.repo, branch) == expected_sha
    assert not (destination / "receipt.json").exists()


def test_local_discard_rejects_tampered_attempt_and_receipt_without_touching_a_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/local-tampered-attempt"
    expected_sha = _create_local_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, branch, expected_sha, backup_root)
    _create_receiptless_attempt(
        harness, branch, expected_sha, backup_root, token, monkeypatch
    )
    (destination / "attempt.json").write_text("{}", encoding="utf-8")

    corrupt_attempt = _apply(harness, branch, expected_sha, backup_root, token)

    assert corrupt_attempt.status == "blocked"
    assert _blocker_code(corrupt_attempt) == "archive_corrupt"
    assert _local_head(harness.repo, branch) == expected_sha

    (tmp_path / "tampered-receipt").mkdir()
    harness = LocalDiscardHarness.create(tmp_path / "tampered-receipt")
    branch = "retired/local-tampered-receipt"
    expected_sha = _create_local_branch(harness, branch)
    backup_root = _backup_root(tmp_path / "tampered-receipt")
    token, destination = _preview(harness, branch, expected_sha, backup_root)
    applied = _apply(harness, branch, expected_sha, backup_root, token)
    assert applied.status == "ok"
    git(harness.repo, "update-ref", f"refs/heads/{branch}", expected_sha)
    (destination / "receipt.json").write_text("{}", encoding="utf-8")

    corrupt_receipt = _apply(harness, branch, expected_sha, backup_root, token)

    assert corrupt_receipt.status == "blocked"
    assert _blocker_code(corrupt_receipt) == "archive_corrupt"
    assert _local_head(harness.repo, branch) == expected_sha


def test_local_discard_receipt_preserves_a_recreated_same_sha_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/local-recreated-same-sha"
    expected_sha = _create_local_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    token, destination = _preview(harness, branch, expected_sha, backup_root)
    first = _apply(harness, branch, expected_sha, backup_root, token)

    assert first.status == "ok"
    receipt_before = (destination / "receipt.json").read_bytes()
    attempt_before = (destination / "attempt.json").read_bytes()
    git(harness.repo, "update-ref", f"refs/heads/{branch}", expected_sha)

    def delete_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a completed local receipt must prevent a second deletion")

    monkeypatch.setattr(
        harness.git, "delete_inactive_branch_if_at", delete_must_not_run
    )
    repeated = _apply(harness, branch, expected_sha, backup_root, token)

    assert repeated.status == "ok"
    assert _local_head(harness.repo, branch) == expected_sha
    assert any(
        warning["code"] == "branch_recreated_untouched" for warning in repeated.warnings
    )
    assert (destination / "receipt.json").read_bytes() == receipt_before
    assert (destination / "attempt.json").read_bytes() == attempt_before


@pytest.mark.parametrize(
    "prior_state", ("receiptless_attempt", "completed_receipt")
)
def test_local_discard_blocks_changed_lease_evidence_at_stable_intent_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior_state: str
) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/local-stable-intent-evidence"
    expected_sha = _create_local_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    lease = _register_lease(
        harness, initiative="stable-intent-evidence", branch=branch
    )
    removed = harness.registry.transition(
        lease.id, LeaseState.REMOVED, expected_version=lease.version
    )
    original_token, destination = _preview(
        harness, branch, expected_sha, backup_root
    )

    if prior_state == "receiptless_attempt":
        _create_receiptless_attempt(
            harness,
            branch,
            expected_sha,
            backup_root,
            original_token,
            monkeypatch,
        )
    else:
        assert prior_state == "completed_receipt"
        completed = _apply(
            harness, branch, expected_sha, backup_root, original_token
        )
        assert completed.status == "ok"
        git(harness.repo, "update-ref", f"refs/heads/{branch}", expected_sha)

    artifacts_before = {
        artifact.name: artifact.read_bytes() for artifact in destination.iterdir()
    }
    updated = harness.registry.touch(
        removed.id, expected_version=removed.version, head_sha=removed.head_sha
    )

    assert updated.version == removed.version + 1
    assert updated.state is LeaseState.REMOVED
    assert updated.retain is False
    assert updated.head_sha == removed.head_sha

    stale_apply = _apply(
        harness, branch, expected_sha, backup_root, original_token
    )
    assert stale_apply.status == "blocked"
    assert _blocker_code(stale_apply) == "preview_token_mismatch"

    fresh_preview = harness.service.discard_local_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=backup_root,
        reason=_REASON,
    )

    assert fresh_preview.status == "blocked"
    assert _blocker_code(fresh_preview) == "local_intent_evidence_changed"
    assert _local_head(harness.repo, branch) == expected_sha
    assert {
        artifact.name: artifact.read_bytes() for artifact in destination.iterdir()
    } == artifacts_before


def test_local_discard_treats_a_new_reason_as_a_new_intent_after_an_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/local-new-reason-intent"
    expected_sha = _create_local_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    original_token, original_destination = _preview(
        harness, branch, expected_sha, backup_root
    )
    _create_receiptless_attempt(
        harness,
        branch,
        expected_sha,
        backup_root,
        original_token,
        monkeypatch,
    )
    original_artifacts = {
        artifact.name: artifact.read_bytes()
        for artifact in original_destination.iterdir()
    }
    new_reason = "A separate explicit approval creates a new local discard intent."

    new_token, new_destination = _preview(
        harness, branch, expected_sha, backup_root, reason=new_reason
    )
    applied = _apply(
        harness,
        branch,
        expected_sha,
        backup_root,
        new_token,
        reason=new_reason,
    )

    assert new_token != original_token
    assert new_destination != original_destination
    assert applied.status == "ok"
    assert _local_head(harness.repo, branch) is None
    assert {
        artifact.name: artifact.read_bytes()
        for artifact in original_destination.iterdir()
    } == original_artifacts
    assert (new_destination / "receipt.json").is_file()


def test_local_discard_supports_sha256_after_default_hash_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DEFAULT_HASH", "sha256")
    harness = LocalDiscardHarness.create(tmp_path)
    monkeypatch.delenv("GIT_DEFAULT_HASH")
    assert git(harness.repo, "rev-parse", "--show-object-format") == "sha256"
    branch = "retired/local-sha256-consumer"
    expected_sha = _create_local_branch(harness, branch)
    backup_root = _backup_root(tmp_path)

    token, destination = _preview(harness, branch, expected_sha, backup_root)
    applied = _apply(harness, branch, expected_sha, backup_root, token)

    assert len(expected_sha) == 64
    assert applied.status == "ok"
    assert _local_head(harness.repo, branch) is None
    assert _restore_commit_bundle(
        destination / "history.bundle",
        tmp_path / "restored-local-sha256.git",
        branch,
        object_format="sha256",
    ) == expected_sha


def test_local_and_remote_discard_tokens_are_not_interchangeable(
    tmp_path: Path,
) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/token-namespace-separation"
    expected_sha = _publish_local_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    local_token, local_destination = _preview(
        harness, branch, expected_sha, backup_root
    )
    remote_preview = harness.service.discard_remote_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=backup_root,
        reason=_REASON,
    )
    assert remote_preview.status == "ok"
    remote_action = next(
        action
        for action in remote_preview.actions
        if action["kind"] == "create_commit_bundle"
    )
    remote_token = remote_action["preview_token"]
    remote_destination = Path(remote_action["backup_directory"])
    assert local_destination != remote_destination

    remote_with_local_token = harness.service.discard_remote_branch(
        branch,
        expected_sha=expected_sha,
        backup_root=backup_root,
        reason=_REASON,
        preview_token=local_token,
        apply=True,
    )
    local_with_remote_token = _apply(
        harness, branch, expected_sha, backup_root, remote_token
    )

    assert remote_with_local_token.status == "blocked"
    assert _blocker_code(remote_with_local_token) == "preview_token_mismatch"
    assert local_with_remote_token.status == "blocked"
    assert _blocker_code(local_with_remote_token) == "preview_token_mismatch"
    assert _remote_head(harness.repo, branch) == expected_sha
    assert _local_head(harness.repo, branch) == expected_sha
    assert not local_destination.exists()
    assert not remote_destination.exists()


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
def test_local_discard_blocks_git_binding_environment_before_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    harness = LocalDiscardHarness.create(tmp_path)
    branch = "retired/local-environment-binding"
    expected_sha = _create_local_branch(harness, branch)
    backup_root = _backup_root(tmp_path)
    before = _primary_state(harness.repo)
    monkeypatch.setenv(name, value)

    result = harness.service.discard_local_branch(
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
    assert _local_head(harness.repo, branch) == expected_sha
