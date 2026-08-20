from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from awf.worktrees.config import ConfigError, WorktreeConfig, load_worktree_config
from awf.worktrees.git import (
    GitCompleted,
    GitClient,
    GitError,
    GitPatchConflict,
    GitRemoteError,
    _bounded_stderr,
    _nul_records,
    _parse_worktrees,
)
from awf.worktrees import locking as locking_module
from awf.worktrees.locking import repository_lock
from worktree_fixtures import git, make_repository




def test_load_config_accepts_only_argv_arrays(tmp_path: Path) -> None:
    config_dir = tmp_path / ".awf"
    config_dir.mkdir()
    (config_dir / "worktree.toml").write_text(
        '[worktree]\ndefault_base="staging"\nproduction_branch="main"\n'
        "[prepare]\ninputs=[\"package-lock.json\"]\n"
        'command=["npm","ci"]\n'
        "[verify.production]\ncommands=[[\"npm\",\"test\"],[\"npm\",\"run\",\"build\"]]\n"
        "[deployment]\nstatus_command=[\"argocd\",\"app\",\"wait\",\"demo\"]\n",
        encoding="utf-8",
    )

    config = load_worktree_config(tmp_path)

    assert config.default_base == "staging"
    assert config.verify_production == (("npm", "test"), ("npm", "run", "build"))



def test_load_config_defaults_to_approved_source_reviews(tmp_path: Path) -> None:
    assert load_worktree_config(tmp_path).source_review_policy == "approved"


def test_load_config_accepts_self_merged_source_review_policy(tmp_path: Path) -> None:
    config_dir = tmp_path / ".awf"
    config_dir.mkdir()
    (config_dir / "worktree.toml").write_text(
        '[promotion]\nsource_review_policy="approved_or_self_merged"\n',
        encoding="utf-8",
    )

    config = load_worktree_config(tmp_path)

    assert config.source_review_policy == "approved_or_self_merged"


def test_load_config_rejects_unknown_source_review_policy(tmp_path: Path) -> None:
    config_dir = tmp_path / ".awf"
    config_dir.mkdir()
    (config_dir / "worktree.toml").write_text(
        '[promotion]\nsource_review_policy="anything"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="source_review_policy"):
        load_worktree_config(tmp_path)

def test_load_config_rejects_shell_strings(tmp_path: Path) -> None:
    config_dir = tmp_path / ".awf"
    config_dir.mkdir()
    (config_dir / "worktree.toml").write_text(
        '[verify.production]\ncommands=["npm test"]\n', encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="argv array"):
        load_worktree_config(tmp_path)


def test_load_config_rejects_unknown_top_level_table(tmp_path: Path) -> None:
    config_dir = tmp_path / ".awf"
    config_dir.mkdir()
    (config_dir / "worktree.toml").write_text(
        "[verfiy.production]\ncommands=[[\"npm\", \"test\"]]\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="unknown top-level table"):
        load_worktree_config(tmp_path)


def test_load_config_defaults_when_file_is_absent(tmp_path: Path) -> None:
    assert load_worktree_config(tmp_path) == WorktreeConfig()


def test_git_client_reports_repository_identity_and_worktrees(tmp_path: Path) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)

    assert client.repository_root() == repo.resolve()
    assert client.repository_name() == "repo"
    assert len(client.repository_id()) == 64
    assert client.head_sha() == git(repo, "rev-parse", "HEAD")
    assert client.list_worktrees()[0].path == repo.resolve()


def test_git_client_rejects_non_repository(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="not a Git repository"):
        GitClient(tmp_path).repository_root()


def test_git_client_reads_refs_and_nul_delimited_status(tmp_path: Path) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    spaced_path = repo / "file with spaces.txt"
    spaced_path.write_text("untracked\n", encoding="utf-8")

    assert client.remote_url() == str(tmp_path / "origin.git")
    assert client.default_remote_branch() == "staging"
    assert client.fetch_ref("staging") == git(repo, "rev-parse", "HEAD")
    assert client.resolve_ref("origin/staging") == git(repo, "rev-parse", "HEAD")
    assert client.status_porcelain() == ("?? file with spaces.txt",)


def test_git_client_reads_remote_branch_sha_and_missing_branch(tmp_path: Path) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    git(repo, "checkout", "-q", "-b", "retry-target")
    (repo / "retry.txt").write_text("retry\n", encoding="utf-8")
    git(repo, "add", "retry.txt")
    git(repo, "commit", "-q", "-m", "retry target")
    retry_sha = client.head_sha()
    git(repo, "push", "-q", "-u", "origin", "retry-target")

    assert client.remote_branch_sha("retry-target") == retry_sha
    assert client.remote_branch_sha("missing") is None


def test_git_client_reads_sha256_remote_branch_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = GitClient(make_repository(tmp_path))
    sha = "a" * 64

    def ls_remote(*args: str, **_kwargs: object) -> GitCompleted:
        assert args == (
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/retry-target",
        )
        return GitCompleted(
            0, f"{sha}\trefs/heads/retry-target\n".encode("ascii"), b""
        )

    monkeypatch.setattr(client, "_run", ls_remote)

    assert client.remote_branch_sha("retry-target") == sha


@pytest.mark.parametrize(
    ("record", "message"),
    (
        (
            f"{'a' * 40}\trefs/heads/retry-target\n"
            f"{'b' * 40}\trefs/heads/retry-target\n",
            "multiple branch records",
        ),
        (f"{'a' * 40}\trefs/heads/other\n", "invalid branch record"),
        (f"{'A' * 40}\trefs/heads/retry-target\n", "invalid branch record"),
        (f"{'g' * 40}\trefs/heads/retry-target\n", "invalid branch record"),
    ),
)
def test_git_client_rejects_malformed_remote_branch_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record: str,
    message: str,
) -> None:
    client = GitClient(make_repository(tmp_path))

    def ls_remote(*_args: str, **_kwargs: object) -> GitCompleted:
        return GitCompleted(0, record.encode("ascii"), b"")

    monkeypatch.setattr(client, "_run", ls_remote)

    with pytest.raises(GitRemoteError, match=message):
        client.remote_branch_sha("retry-target")


def test_git_client_normalizes_remote_branch_lookup_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = GitClient(make_repository(tmp_path))

    def fail_ls_remote(*_args: str, **_kwargs: object) -> GitCompleted:
        raise GitError("git ls-remote failed (128): transport failure")

    monkeypatch.setattr(client, "_run", fail_ls_remote)

    with pytest.raises(GitRemoteError, match="transport failure") as error:
        client.remote_branch_sha("retry-target")

    assert isinstance(error.value.__cause__, GitError)


def test_git_client_adds_and_removes_worktrees(tmp_path: Path) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    worktree = tmp_path / "worktree with spaces"

    client.add_worktree(worktree, "awf/test", client.head_sha())

    registered = {item.path: item for item in client.list_worktrees()}
    assert registered[worktree.resolve()].branch == "awf/test"
    client.remove_worktree(worktree)
    assert not worktree.exists()


def test_git_client_hard_resets_worktree_to_ref_and_clears_tracked_changes(
    tmp_path: Path,
) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    worktree = tmp_path / "reset-worktree"
    base_sha = client.head_sha()
    client.add_worktree(worktree, "awf/reset", base_sha)
    (worktree / "later.txt").write_text("later\n", encoding="utf-8")
    git(worktree, "add", "later.txt")
    git(worktree, "commit", "-q", "-m", "later")
    (worktree / "README.txt").write_text("staged\n", encoding="utf-8")
    git(worktree, "add", "README.txt")
    (worktree / "later.txt").write_text("unstaged\n", encoding="utf-8")

    client.reset_hard(worktree, base_sha)

    assert client.head_sha(worktree) == base_sha
    assert client.status_porcelain(worktree) == ()


def test_git_client_rejects_leading_dash_reset_ref_without_discarding_changes(
    tmp_path: Path,
) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    readme = repo / "README.txt"
    readme.write_text("staged\n", encoding="utf-8")
    git(repo, "add", "README.txt")
    readme.write_text("unstaged\n", encoding="utf-8")
    dirty_status = client.status_porcelain()

    with pytest.raises(GitError):
        client.reset_hard(repo, "--recurse-submodules")

    assert client.status_porcelain() == dirty_status

def test_git_client_interprets_relative_worktree_paths_from_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    monkeypatch.chdir(tmp_path)
    relative_path = Path("relative worktree")
    expected_path = repo / relative_path

    client.add_worktree(relative_path, "awf/relative", client.head_sha())

    assert expected_path.is_dir()
    client.remove_worktree(relative_path)
    assert not expected_path.exists()


def test_git_client_cas_deletes_local_and_remote_branches(tmp_path: Path) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    git(repo, "branch", "awf/local-delete")
    git(repo, "branch", "awf/remote-delete")
    git(repo, "push", "-q", "origin", "awf/remote-delete")
    local_head = git(repo, "rev-parse", "awf/local-delete")
    remote_head = git(repo, "rev-parse", "awf/remote-delete")

    client.delete_branch_if_at("awf/local-delete", local_head)
    client.delete_remote_branch_if_at("awf/remote-delete", remote_head)

    assert "awf/local-delete" not in git(
        repo, "branch", "--format=%(refname:short)"
    ).splitlines()
    assert git(repo, "ls-remote", "--heads", "origin", "awf/remote-delete") == ""




def test_git_client_cas_deletes_a_branch_at_the_expected_head(
    tmp_path: Path,
) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    git(repo, "branch", "awf/cas-delete")
    expected_head = git(repo, "rev-parse", "awf/cas-delete")

    client.delete_branch_if_at("awf/cas-delete", expected_head)

    assert "awf/cas-delete" not in git(
        repo, "branch", "--format=%(refname:short)"
    ).splitlines()


def test_git_client_cas_delete_preserves_a_branch_after_a_race(
    tmp_path: Path,
) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    git(repo, "branch", "awf/cas-race")
    expected_head = git(repo, "rev-parse", "awf/cas-race")
    git(repo, "checkout", "-q", "-b", "upstream-change")
    (repo / "changed.txt").write_text("changed\n", encoding="utf-8")
    git(repo, "add", "changed.txt")
    git(repo, "commit", "-q", "-m", "changed")
    changed_head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "staging")
    git(repo, "branch", "-f", "awf/cas-race", changed_head)

    with pytest.raises(GitError):
        client.delete_branch_if_at("awf/cas-race", expected_head)

    assert git(repo, "rev-parse", "awf/cas-race") == changed_head


def test_git_client_cas_deletes_remote_branch_at_expected_head(
    tmp_path: Path,
) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    git(repo, "branch", "awf/remote-cas-delete")
    git(repo, "push", "-q", "origin", "awf/remote-cas-delete")
    expected_head = git(repo, "rev-parse", "awf/remote-cas-delete")

    client.delete_remote_branch_if_at("awf/remote-cas-delete", expected_head)

    assert git(repo, "ls-remote", "--heads", "origin", "awf/remote-cas-delete") == ""


def test_git_client_remote_cas_delete_preserves_recreated_branch(
    tmp_path: Path,
) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    git(repo, "branch", "awf/remote-cas-race")
    git(repo, "push", "-q", "origin", "awf/remote-cas-race")
    expected_head = git(repo, "rev-parse", "awf/remote-cas-race")
    git(repo, "checkout", "-q", "-b", "remote-upstream-change")
    (repo / "remote-changed.txt").write_text("changed\n", encoding="utf-8")
    git(repo, "add", "remote-changed.txt")
    git(repo, "commit", "-q", "-m", "remote changed")
    changed_head = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "-q", "origin", f"{changed_head}:awf/remote-cas-race")
    git(repo, "checkout", "-q", "staging")

    with pytest.raises(GitError):
        client.delete_remote_branch_if_at("awf/remote-cas-race", expected_head)

    assert git(repo, "ls-remote", "--heads", "origin", "awf/remote-cas-race").split()[0] == changed_head


@pytest.mark.parametrize(
    "detail",
    (
        "Permission denied (publickey).\nCould not read from remote repository.",
        "Could not read from remote repository.",
    ),
)
def test_git_client_classifies_unrecognized_remote_delete_failures_as_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detail: str
) -> None:
    client = GitClient(make_repository(tmp_path))

    def fail_push(*_args: str, **_kwargs: object) -> object:
        raise GitError(f"git push failed (128): {detail}")

    monkeypatch.setattr(client, "_run", fail_push)

    with pytest.raises(GitRemoteError):
        client.delete_remote_branch_if_at("awf/remote-delete", "a" * 40)


def test_git_client_classifies_push_transport_failure_as_external(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)

    def fail_push(*_args: str, **_kwargs: object) -> object:
        raise GitError(
            "git push failed (128): Permission denied (publickey). "
            "Could not read from remote repository."
        )

    monkeypatch.setattr(client, "_run", fail_push)

    with pytest.raises(GitRemoteError):
        client.push_branch(repo, "awf/push")

def _divergent_patch_worktree(
    tmp_path: Path,
    *,
    path: str,
    base_contents: bytes,
    source_contents: bytes | None,
    target_contents: bytes | None,
) -> tuple[GitClient, Path, bytes]:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    (repo / path).write_bytes(base_contents)
    (repo / "staged-sentinel.txt").write_text("base\n", encoding="utf-8")
    (repo / "unstaged-sentinel.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "--", path, "staged-sentinel.txt", "unstaged-sentinel.txt")
    git(repo, "commit", "-q", "-m", "add conflict inputs")
    base = client.head_sha()
    source = tmp_path / "conflict-source"
    target = tmp_path / "conflict-target"
    client.add_worktree(source, "awf/conflict-source", base)
    client.add_worktree(target, "awf/conflict-target", base)
    if source_contents is None:
        git(source, "rm", "--", path)
    else:
        (source / path).write_bytes(source_contents)
        git(source, "add", "--", path)
    git(source, "commit", "-q", "-m", "source change")
    source_head = client.head_sha(source)
    if target_contents is None:
        git(target, "rm", "--", path)
    else:
        (target / path).write_bytes(target_contents)
        git(target, "add", "--", path)
    git(target, "commit", "-q", "-m", "target change")

    return client, target, client.binary_diff(base, source_head)


def _conflicting_patch_worktree(
    tmp_path: Path,
) -> tuple[GitClient, Path, bytes]:
    return _divergent_patch_worktree(
        tmp_path,
        path="shared.txt",
        base_contents=b"base\n",
        source_contents=b"source\n",
        target_contents=b"target\n",
    )


def _add_unrelated_staged_and_unstaged_changes(worktree: Path) -> None:
    (worktree / "staged-sentinel.txt").write_text("staged\n", encoding="utf-8")
    git(worktree, "add", "staged-sentinel.txt")
    (worktree / "unstaged-sentinel.txt").write_text(
        "unstaged\n", encoding="utf-8"
    )


def _assert_unrelated_changes_are_preserved(
    client: GitClient, worktree: Path
) -> None:
    status = client.status_porcelain(worktree)
    assert "M  staged-sentinel.txt" in status
    assert " M unstaged-sentinel.txt" in status


def test_git_client_reports_real_patch_conflict_and_preserves_unmerged_worktree(
    tmp_path: Path,
) -> None:
    client, worktree, patch = _conflicting_patch_worktree(tmp_path)

    with pytest.raises(GitPatchConflict) as caught:
        client.apply_indexed_patch(worktree, patch)

    assert caught.value.paths == ("shared.txt",)
    assert client.unmerged_paths(worktree) == ("shared.txt",)
    assert any(
        entry.endswith("shared.txt") for entry in client.status_porcelain(worktree)
    )


def test_git_client_stage_paths_resolves_conflict_and_preserves_staged_change(
    tmp_path: Path,
) -> None:
    client, worktree, patch = _conflicting_patch_worktree(tmp_path)

    with pytest.raises(GitError):
        client.apply_indexed_patch(worktree, patch)
    (worktree / "shared.txt").write_text("resolved\n", encoding="utf-8")

    client.stage_paths(worktree, ("shared.txt",))

    assert client.unmerged_paths(worktree) == ()
    assert client.status_porcelain(worktree) == ("M  shared.txt",)

def test_git_client_detects_added_conflict_markers_in_staged_and_committed_diffs(
    tmp_path: Path,
) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    base = client.head_sha()
    (repo / "marker.txt").write_text(
        "<<<<<<< ours\nmanual\n=======\ntheirs\n>>>>>>> theirs\n",
        encoding="utf-8",
    )
    client.stage_paths(repo, ("marker.txt",))

    assert client.staged_diff_has_conflict_markers(repo)

    head = client.commit(repo, "marker")
    assert client.committed_diff_has_conflict_markers(repo, base, head)


def test_git_client_allows_trailing_whitespace_without_conflict_markers(
    tmp_path: Path,
) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    base = client.head_sha()
    (repo / "whitespace.txt").write_text("manual  \n", encoding="utf-8")
    client.stage_paths(repo, ("whitespace.txt",))

    assert not client.staged_diff_has_conflict_markers(repo)

    head = client.commit(repo, "trailing whitespace")
    assert not client.committed_diff_has_conflict_markers(repo, base, head)


def test_git_client_snapshots_stage_zero_blobs_with_literal_paths(
    tmp_path: Path,
) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    client.stage_paths(repo, ("tracked.txt",))
    client.commit(repo, "tracked")
    (repo / "tracked.txt").write_text("updated\n", encoding="utf-8")
    client.stage_paths(repo, ("tracked.txt",))

    snapshot = client.index_blob_snapshot(
        repo,
        ("missing.txt", "tracked.txt"),
    )

    assert snapshot == (
        ("missing.txt", None),
        ("tracked.txt", git(repo, "rev-parse", ":tracked.txt")),
    )

def test_git_client_stage_paths_rejects_empty_paths(tmp_path: Path) -> None:
    client = GitClient(make_repository(tmp_path))

    with pytest.raises(GitError, match="at least one path is required for staging"):
        client.stage_paths(client.cwd, ())


def test_git_client_keeps_generic_error_for_non_conflicting_patch_failure(
    tmp_path: Path,
) -> None:
    client = GitClient(make_repository(tmp_path))

    with pytest.raises(GitError) as caught:
        client.apply_indexed_patch(client.cwd, b"not a patch")

    assert type(caught.value) is GitError
    assert caught.value.returncode is not None


def test_git_client_stage_paths_treats_pathspecs_as_literal_paths(
    tmp_path: Path,
) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    paths = (":(glob)**", "-leading.txt", "file with spaces.txt")
    for path in paths:
        (repo / path).write_text("base\n", encoding="utf-8")
    (repo / "staged-sentinel.txt").write_text("base\n", encoding="utf-8")
    (repo / "unstaged-sentinel.txt").write_text("base\n", encoding="utf-8")
    git(
        repo,
        "--literal-pathspecs",
        "add",
        "--",
        *paths,
        "staged-sentinel.txt",
        "unstaged-sentinel.txt",
    )
    git(repo, "commit", "-q", "-m", "add literal pathspec inputs")
    for path in paths:
        (repo / path).write_text("changed\n", encoding="utf-8")
    (repo / "staged-sentinel.txt").write_text("staged\n", encoding="utf-8")
    git(repo, "add", "staged-sentinel.txt")
    (repo / "unstaged-sentinel.txt").write_text("unstaged\n", encoding="utf-8")

    client.stage_paths(repo, paths)

    status = client.status_porcelain(repo)
    assert "M  :(glob)**" in status
    assert "M  -leading.txt" in status
    assert "M  file with spaces.txt" in status
    assert "M  staged-sentinel.txt" in status
    assert " M unstaged-sentinel.txt" in status


def test_git_client_preserves_modify_delete_apply_failure_without_mutating_head(
    tmp_path: Path,
) -> None:
    client, worktree, patch = _divergent_patch_worktree(
        tmp_path,
        path="removed.txt",
        base_contents=b"base\n",
        source_contents=None,
        target_contents=b"target\n",
    )
    _add_unrelated_staged_and_unstaged_changes(worktree)
    head_before = client.head_sha(worktree)

    with pytest.raises(GitError) as caught:
        client.apply_indexed_patch(worktree, patch)

    assert type(caught.value) is GitError
    assert client.unmerged_paths(worktree) == ()
    assert client.head_sha(worktree) == head_before
    _assert_unrelated_changes_are_preserved(client, worktree)


def test_git_client_reports_nul_binary_patch_conflict_without_mutating_head(
    tmp_path: Path,
) -> None:
    client, worktree, patch = _divergent_patch_worktree(
        tmp_path,
        path="binary.dat",
        base_contents=b"\0base\n",
        source_contents=b"\0source\n",
        target_contents=b"\0target\n",
    )
    _add_unrelated_staged_and_unstaged_changes(worktree)
    head_before = client.head_sha(worktree)

    with pytest.raises(GitPatchConflict) as caught:
        client.apply_indexed_patch(worktree, patch)

    assert caught.value.paths == ("binary.dat",)
    assert client.unmerged_paths(worktree) == ("binary.dat",)
    assert client.head_sha(worktree) == head_before
    _assert_unrelated_changes_are_preserved(client, worktree)


def test_git_client_rejects_apply_when_index_already_has_unmerged_paths(
    tmp_path: Path,
) -> None:
    client, worktree, patch = _conflicting_patch_worktree(tmp_path)

    with pytest.raises(GitPatchConflict):
        client.apply_indexed_patch(worktree, patch)

    with pytest.raises(GitError, match="requires a clean index") as caught:
        client.apply_indexed_patch(worktree, b"not a patch")

    assert type(caught.value) is GitError
    assert client.unmerged_paths(worktree) == ("shared.txt",)


def test_git_client_preserves_apply_error_when_unmerged_lookup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = GitClient(make_repository(tmp_path))
    apply_error = GitError("git apply failed (1)", returncode=1)
    lookup_error = GitError("git diff failed (1)")
    unmerged_lookups = 0

    def run(*args: str, **_kwargs: object) -> GitCompleted:
        nonlocal unmerged_lookups
        if args[0] == "diff":
            unmerged_lookups += 1
            if unmerged_lookups == 1:
                return GitCompleted(0, b"", b"")
            raise lookup_error
        assert args[0] == "apply"
        raise apply_error

    monkeypatch.setattr(client, "_run", run)

    with pytest.raises(GitError) as caught:
        client.apply_indexed_patch(client.cwd, b"patch")

    assert caught.value is apply_error


def test_git_client_does_not_inspect_conflicts_after_non_command_apply_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = GitClient(make_repository(tmp_path))
    launch_error = GitError("git apply failed to launch")
    commands: list[str] = []

    def run(*args: str, **_kwargs: object) -> GitCompleted:
        commands.append(args[0])
        if args[0] == "diff":
            return GitCompleted(0, b"", b"")
        assert args[0] == "apply"
        raise launch_error

    monkeypatch.setattr(client, "_run", run)

    with pytest.raises(GitError) as caught:
        client.apply_indexed_patch(client.cwd, b"patch")

    assert caught.value is launch_error
    assert commands == ["diff", "apply"]


def test_git_client_applies_binary_patch_and_commits_from_a_worktree(tmp_path: Path) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    source = tmp_path / "source"
    target = tmp_path / "target"
    base = client.head_sha()
    client.add_worktree(source, "awf/source", base)
    client.add_worktree(target, "awf/target", base)
    (source / "feature with spaces.txt").write_text("feature\n", encoding="utf-8")
    (source / "excluded.txt").write_text("excluded\n", encoding="utf-8")
    git(source, "add", "feature with spaces.txt", "excluded.txt")
    source_head = git(source, "commit", "-q", "-m", "source feature") or git(
        source, "rev-parse", "HEAD"
    )

    patch = client.binary_diff(
        base, source_head, paths=("feature with spaces.txt",)
    )
    client.apply_indexed_patch(target, patch)
    target_head = client.commit(target, "Apply source feature")

    assert client.merge_base(base, source_head) == base
    assert client.changed_paths(target, base) == ("feature with spaces.txt",)
    assert not (target / "excluded.txt").exists()
    assert target_head == git(target, "rev-parse", "HEAD")


def test_git_client_holds_branch_and_worktree_head_transactions(
    tmp_path: Path,
) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    worktree = tmp_path / "lease"
    base = client.head_sha()
    client.add_worktree(worktree, "awf/ref-lock", base)
    expected_head = git(worktree, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "ref-lock-update", "awf/ref-lock")
    (repo / "locked.txt").write_text("locked\n", encoding="utf-8")
    git(repo, "add", "locked.txt")
    git(repo, "commit", "-q", "-m", "locked branch update")
    advanced_head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "staging")
    git(repo, "branch", "alternate-lock", expected_head)

    with client.hold_worktree_branch_if_at(
        worktree, "awf/ref-lock", expected_head
    ):
        ref_race = subprocess.run(
            [
                "git",
                "update-ref",
                "refs/heads/awf/ref-lock",
                advanced_head,
                expected_head,
            ],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
        detach_race = subprocess.run(
            ["git", "checkout", "--detach"],
            cwd=worktree,
            check=False,
            capture_output=True,
            text=True,
        )
        alternate_race = subprocess.run(
            ["git", "checkout", "-q", "alternate-lock"],
            cwd=worktree,
            check=False,
            capture_output=True,
            text=True,
        )

    assert ref_race.returncode != 0
    assert detach_race.returncode != 0
    assert alternate_race.returncode != 0
    assert client.resolve_ref("awf/ref-lock") == expected_head
    assert git(worktree, "symbolic-ref", "--short", "HEAD") == "awf/ref-lock"





def test_git_client_reads_commit_parents(tmp_path: Path) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    base = client.head_sha()
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-q", "-m", "feature")

    assert client.commit_parents(client.head_sha()) == (base,)


def test_git_client_reads_full_commit_message(tmp_path: Path) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    message = "Promote PR #372 to main\n\nAWF-Source-PR: 372"
    client.commit(repo, message, allow_empty=True)

    assert client.commit_message(repo) == message

def test_git_client_pushes_branch_from_the_given_worktree(tmp_path: Path) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    worktree = tmp_path / "push-worktree"
    client.add_worktree(worktree, "awf/push", client.head_sha())

    client.push_branch(worktree, "awf/push")

    assert git(repo, "ls-remote", "--heads", "origin", "awf/push").split()[1] == "refs/heads/awf/push"


def test_repository_lock_blocks_a_second_nonblocking_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / "locks" / "repo.lock"
    with repository_lock(lock_path):
        with pytest.raises(BlockingIOError):
            with repository_lock(lock_path, blocking=False):
                raise AssertionError("unreachable")


def test_git_client_preserves_repository_root_trailing_whitespace(
    tmp_path: Path,
) -> None:
    repo = make_repository(tmp_path, name="repo ")

    assert GitClient(repo).repository_root() == repo.resolve()


def test_repository_identity_ignores_remote_url_userinfo(tmp_path: Path) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    git(
        repo,
        "remote",
        "set-url",
        "origin",
        "https://alice:secret-one@example.com/owner/repository.git",
    )
    first = client.repository_id()
    git(
        repo,
        "remote",
        "set-url",
        "origin",
        "https://bob:secret-two@example.com/owner/repository.git",
    )

    assert client.repository_id() == first

def test_repository_identity_preserves_scp_ssh_user(tmp_path: Path) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    git(repo, "remote", "set-url", "origin", "git@host:owner/repository.git")

    expected = hashlib.sha256(
        b"ssh://git@host/owner/repository\0" + os.fsencode(repo.resolve())
    ).hexdigest()

    assert client.repository_id() == expected


def test_repository_identity_hashes_invalid_repository_root_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repository(tmp_path)
    root_bytes = os.fsencode(tmp_path) + b"/repository-\xff"
    root_output = root_bytes + b"\n"
    _install_fake_git(
        monkeypatch,
        tmp_path,
        "import sys\n"
        "if sys.argv[1:] == ['remote', 'get-url', 'origin']:\n"
        "    sys.stdout.write('https://example.com/owner/repository.git\\n')\n"
        "elif sys.argv[1:] == ['rev-parse', '--show-toplevel']:\n"
        f"    sys.stdout.buffer.write({root_output!r})\n"
        "else:\n"
        "    raise SystemExit(2)\n",
    )

    expected_root = Path(os.fsdecode(root_bytes)).resolve()
    expected = hashlib.sha256(
        b"https://example.com/owner/repository\0" + os.fsencode(expected_root)
    ).hexdigest()

    assert GitClient(repo).repository_id() == expected


@pytest.mark.parametrize(
    "contents",
    [
        "[worktree]\ndefault_base = \"staging\\u0000next\"\n",
        "[worktree]\nproduction_branch = \"main\\u0000next\"\n",
        "[prepare]\ninputs = [\"package\\u0000lock.json\"]\n",
        "[prepare]\ncommand = [\"npm\\u0000ci\"]\n",
        "[verify.production]\ncommands = [[\"npm\", \"test\\u0000all\"]]\n",
        "[deployment]\nstatus_command = [\"argocd\\u0000app\"]\n",
    ],
)
def test_config_rejects_embedded_nul_in_all_string_fields(
    tmp_path: Path, contents: str
) -> None:
    config_dir = tmp_path / ".awf"
    config_dir.mkdir()
    (config_dir / "worktree.toml").write_text(contents, encoding="utf-8")

    with pytest.raises(ConfigError, match="embedded NUL"):
        load_worktree_config(tmp_path)


def test_config_rejects_raw_toml_nul(tmp_path: Path) -> None:
    config_dir = tmp_path / ".awf"
    config_dir.mkdir()
    (config_dir / "worktree.toml").write_bytes(
        b"[worktree]\ndefault_base = \"staging\x00next\"\n"
    )

    with pytest.raises(ConfigError, match="embedded NUL"):
        load_worktree_config(tmp_path)


def test_git_client_maps_embedded_nul_subprocess_argument_to_git_error(
    tmp_path: Path,
) -> None:
    client = GitClient(make_repository(tmp_path))

    with pytest.raises(GitError, match="failed to launch") as caught:
        client.resolve_ref("HEAD\x00invalid")

    assert caught.value.returncode is None


@pytest.mark.parametrize("message", ["한" * 200, "😀" * 200])
def test_git_error_stderr_is_bounded_on_a_utf8_boundary(message: str) -> None:
    bounded = _bounded_stderr(message.encode("utf-8"))

    assert len(bounded.encode("utf-8")) <= 512
    assert "\ufffd" not in bounded


def test_nul_porcelain_round_trips_invalid_path_bytes() -> None:
    name = b"invalid-\xff.txt"

    assert os.fsencode(_nul_records(b"?? " + name + b"\0")[0]) == b"?? " + name
    assert os.fsencode(_nul_records(name + b"\0")[0]) == name


def test_worktree_porcelain_distinguishes_reasonless_lock_fields(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    reasonless = tmp_path / "reasonless"
    reasoned = tmp_path / "reasoned"
    output = (
        f"worktree {plain}\0HEAD {'a' * 40}\0branch refs/heads/main\0\0"
        f"worktree {reasonless}\0HEAD {'b' * 40}\0locked\0prunable\0\0"
        f"worktree {reasoned}\0HEAD {'c' * 40}\0locked maintenance\0"
        "prunable stale metadata\0\0"
    ).encode("utf-8")

    worktrees = _parse_worktrees(output)

    assert worktrees[0].locked is None
    assert worktrees[0].prunable is None
    assert worktrees[1].locked == ""
    assert worktrees[1].prunable == ""
    assert worktrees[2].locked == "maintenance"
    assert worktrees[2].prunable == "stale metadata"


def _install_fake_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "git"
    script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    script.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def test_git_error_redacts_url_userinfo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_git(
        monkeypatch,
        tmp_path,
        "import sys\n"
        "sys.stderr.write('fatal: https://alice:secret@example.com/owner/repo')\n"
        "raise SystemExit(1)\n",
    )

    with pytest.raises(GitError) as error:
        GitClient(tmp_path).resolve_ref("HEAD")

    assert "secret" not in str(error.value)
    assert "alice" not in str(error.value)
    assert "example.com" in str(error.value)


def test_git_client_terminates_timeout_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_pid_file = tmp_path / "parent.pid"
    child_pid_file = tmp_path / "child.pid"
    child_term_file = tmp_path / "child.term"
    monkeypatch.setenv("AWF_PARENT_PID_FILE", str(parent_pid_file))
    monkeypatch.setenv("AWF_CHILD_PID_FILE", str(child_pid_file))
    monkeypatch.setenv("AWF_CHILD_TERM_FILE", str(child_term_file))
    _install_fake_git(
        monkeypatch,
        tmp_path,
        "import os\n"
        "import pathlib\n"
        "import signal\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import os, pathlib, signal, time; "
        "pathlib.Path(os.environ[\\\"AWF_CHILD_PID_FILE\\\"]).write_text(str(os.getpid())); "
        "signal.signal(signal.SIGTERM, lambda *_: pathlib.Path("
        "os.environ[\\\"AWF_CHILD_TERM_FILE\\\"]).write_text(\\\"terminated\\\")); "
        "time.sleep(60)'])\n"
        "pathlib.Path(os.environ['AWF_PARENT_PID_FILE']).write_text(str(os.getpid()))\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "while True:\n"
        "    time.sleep(1)\n",
    )

    with pytest.raises(GitError, match="timed out") as caught:
        GitClient(tmp_path, timeout=0.5).resolve_ref("HEAD")

    assert caught.value.returncode is None

    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    try:
        assert child_term_file.read_text(encoding="utf-8") == "terminated"
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            pytest.fail("timed-out Git descendant remained alive")
    finally:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_repository_lock_closes_descriptor_when_unlock_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "locks" / "repo.lock"
    actual_flock = locking_module.fcntl.flock
    actual_close = locking_module.os.close
    closed: list[int] = []
    unlock_descriptors: list[int] = []

    def failing_unlock(descriptor: int, operation: int) -> None:
        if operation == locking_module.fcntl.LOCK_UN:
            unlock_descriptors.append(descriptor)
            raise OSError("unlock failed")
        actual_flock(descriptor, operation)

    def tracking_close(descriptor: int) -> None:
        closed.append(descriptor)
        actual_close(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(locking_module.fcntl, "flock", failing_unlock)
        patch.setattr(locking_module.os, "close", tracking_close)
        with pytest.raises(OSError, match="unlock failed"):
            with repository_lock(lock_path):
                pass

    try:
        assert closed == unlock_descriptors
    finally:
        if unlock_descriptors and not closed:
            actual_close(unlock_descriptors[0])

    with repository_lock(lock_path, blocking=False):
        pass


@pytest.mark.parametrize("rename_config", ("true", "false"))
def test_changed_path_endpoints_preserve_both_rename_sides(
    tmp_path: Path, rename_config: str
) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    base = client.head_sha()
    git(repo, "config", "diff.renames", rename_config)

    git(repo, "mv", "README.txt", "renamed.txt")
    git(repo, "commit", "-q", "-m", "rename README")
    head = client.head_sha()

    assert client.changed_path_endpoints(repo, base, head) == (
        "README.txt",
        "renamed.txt",
    )


def test_binary_patch_preserves_rename_mode_and_gitlink_delta(tmp_path: Path) -> None:
    repo = make_repository(tmp_path)
    client = GitClient(repo)
    source = tmp_path / "source-delta"
    target = tmp_path / "target-delta"
    base = client.head_sha()
    client.add_worktree(source, "awf/source-delta", base)
    client.add_worktree(target, "awf/target-delta", base)
    git(source, "mv", "README.txt", "renamed.txt")
    (source / "script.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (source / "script.sh").chmod(0o755)
    git(source, "add", "script.sh")
    git(source, "update-index", "--add", "--cacheinfo", f"160000,{base},submodule")
    git(source, "commit", "-q", "-m", "rename mode gitlink")
    source_head = client.head_sha(source)

    client.apply_indexed_patch(target, client.binary_diff(base, source_head))
    target_head = client.commit(target, "apply delta")

    assert client.changed_paths(source, base, source_head, find_renames=True) == (
        "renamed.txt",
        "script.sh",
        "submodule",
    )
    assert client.changed_paths(target, base, target_head, find_renames=True) == (
        "renamed.txt",
        "script.sh",
        "submodule",
    )
    assert (target / "script.sh").stat().st_mode & 0o111
    assert not (target / "README.txt").exists()
    assert (target / "renamed.txt").read_bytes() == (
        source / "renamed.txt"
    ).read_bytes()
    assert client.path_blob(source_head, "submodule") == client.path_blob(
        target_head, "submodule"
    )
