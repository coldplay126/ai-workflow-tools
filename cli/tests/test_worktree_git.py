from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf.worktrees.config import ConfigError, WorktreeConfig, load_worktree_config
from awf.worktrees.git import GitClient, GitError
from awf.worktrees.locking import repository_lock


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def repository(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    git(tmp_path, "init", "--bare", "-q", str(bare))
    git(tmp_path, "init", "-q", "-b", "staging", str(repo))
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "AWF Test")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "README.txt")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "-u", "origin", "staging")
    git(bare, "symbolic-ref", "HEAD", "refs/heads/staging")
    git(repo, "fetch", "-q", "origin")
    git(repo, "remote", "set-head", "origin", "-a")
    return repo


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
    repo = repository(tmp_path)
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
    repo = repository(tmp_path)
    client = GitClient(repo)
    spaced_path = repo / "file with spaces.txt"
    spaced_path.write_text("untracked\n", encoding="utf-8")

    assert client.remote_url() == str(tmp_path / "origin.git")
    assert client.default_remote_branch() == "staging"
    assert client.fetch_ref("staging") == git(repo, "rev-parse", "HEAD")
    assert client.resolve_ref("origin/staging") == git(repo, "rev-parse", "HEAD")
    assert client.status_porcelain() == ("?? file with spaces.txt",)


def test_git_client_adds_and_removes_worktrees(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    client = GitClient(repo)
    worktree = tmp_path / "worktree with spaces"

    client.add_worktree(worktree, "awf/test", client.head_sha())

    registered = {item.path: item for item in client.list_worktrees()}
    assert registered[worktree.resolve()].branch == "awf/test"
    client.remove_worktree(worktree)
    assert not worktree.exists()


def test_git_client_deletes_local_and_remote_branches(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    client = GitClient(repo)
    git(repo, "branch", "awf/local-delete")
    git(repo, "branch", "awf/remote-delete")
    git(repo, "push", "-q", "origin", "awf/remote-delete")

    client.delete_local_branch("awf/local-delete")
    client.delete_remote_branch("awf/remote-delete")

    assert "awf/local-delete" not in git(repo, "branch", "--format=%(refname:short)").splitlines()
    assert git(repo, "ls-remote", "--heads", "origin", "awf/remote-delete") == ""


def test_git_client_applies_binary_diff_and_commits_from_a_worktree(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    client = GitClient(repo)
    source = tmp_path / "source"
    target = tmp_path / "target"
    base = client.head_sha()
    client.add_worktree(source, "awf/source", base)
    client.add_worktree(target, "awf/target", base)
    (source / "feature with spaces.txt").write_text("feature\n", encoding="utf-8")
    git(source, "add", "feature with spaces.txt")
    source_head = git(source, "commit", "-q", "-m", "source feature") or git(
        source, "rev-parse", "HEAD"
    )

    patch = client.binary_diff(base, source_head)
    client.apply_indexed_patch(target, patch)
    target_head = client.commit(target, "Apply source feature")

    assert client.merge_base(base, source_head) == base
    assert client.changed_paths(target, base) == ("feature with spaces.txt",)
    assert target_head == git(target, "rev-parse", "HEAD")


def test_git_client_pushes_branch_from_the_given_worktree(tmp_path: Path) -> None:
    repo = repository(tmp_path)
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
