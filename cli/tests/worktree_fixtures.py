from __future__ import annotations

import subprocess
from pathlib import Path


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def make_repository(tmp_path: Path, *, name: str = "repo") -> Path:
    bare = tmp_path / "origin.git"
    repo = tmp_path / name
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
