from __future__ import annotations

import io
import json
import subprocess
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

from awf.cli import build_parser, main
from awf.worktrees.git import GitClient
from awf.worktrees.models import Lease, Purpose
from awf.worktrees.registry import WorktreeRegistry
from worktree_fixtures import make_repository


def capture_main(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        result = main(argv)
    return result, stdout.getvalue(), stderr.getvalue()


def register_lease(db: Path, repo: Path, initiative: str) -> None:
    git = GitClient(repo)
    WorktreeRegistry(db).create_lease(
        Lease.new(
            repository_id=git.repository_id(),
            repository_name=git.repository_name(),
            repository_root=git.repository_root(),
            worktree_path=repo / f"worktree-{initiative}",
            initiative=initiative,
            purpose=Purpose.FEATURE,
            branch=f"awf/{initiative}/feature",
            base_ref="origin/staging",
            head_sha=git.head_sha(),
            managed=True,
            owner_kind="awf",
        )
    )


def test_wt_status_parser_surface() -> None:
    args = build_parser().parse_args(
        ["wt", "status", "--repo-root", "/repo", "--initiative", "reward", "--json"]
    )

    assert args.command == "wt"
    assert args.wt_command == "status"
    assert args.repo_root == "/repo"
    assert args.initiative == "reward"
    assert args.json is True


def test_wt_status_emits_one_json_document(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "state" / "worktrees.sqlite3"
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))
    repo = make_repository(tmp_path)

    rc, stdout, stderr = capture_main(
        ["wt", "status", "--repo-root", str(repo), "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 0
    assert stderr == ""
    assert payload["schema_version"] == 1
    assert payload["command"] == "wt.status"
    assert payload["decision"] == "no_op"
    assert not db.parent.exists()


def test_wt_status_lists_registered_leases_without_an_initiative_filter(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "state.sqlite3"
    repo = make_repository(tmp_path)
    register_lease(db, repo, "reward")
    register_lease(db, repo, "metrics")
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))

    rc, stdout, stderr = capture_main(
        ["wt", "status", "--repo-root", str(repo), "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 0
    assert stderr == ""
    assert payload["decision"] == "ready"
    assert {lease["initiative"] for lease in payload["leases"]} == {
        "reward",
        "metrics",
    }


def test_wt_status_filters_registered_leases_by_initiative(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "state.sqlite3"
    repo = make_repository(tmp_path)
    register_lease(db, repo, "reward")
    register_lease(db, repo, "metrics")
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))

    rc, stdout, stderr = capture_main(
        [
            "wt",
            "status",
            "--repo-root",
            str(repo),
            "--initiative",
            "reward",
            "--json",
        ]
    )

    payload = json.loads(stdout)
    assert rc == 0
    assert stderr == ""
    assert payload["decision"] == "ready"
    assert [lease["initiative"] for lease in payload["leases"]] == ["reward"]


def test_wt_doctor_reports_unregistered_worktree_without_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repository(tmp_path)
    db = tmp_path / "state" / "worktrees.sqlite3"
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))

    rc, stdout, _ = capture_main(
        ["wt", "doctor", "--repo-root", str(repo), "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 0
    assert payload["actions"][0]["kind"] == "unregistered_worktree"
    assert payload["actions"][0]["path"] == str(repo.resolve())
    assert not db.parent.exists()


def test_wt_doctor_human_output_lists_each_mismatch(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repository(tmp_path)
    monkeypatch.setenv(
        "AWF_WORKTREE_STATE_DB", str(tmp_path / "state" / "worktrees.sqlite3")
    )

    rc, stdout, stderr = capture_main(
        ["wt", "doctor", "--repo-root", str(repo)]
    )

    assert rc == 0
    assert stderr == ""
    assert stdout == (
        "wt.doctor: preview\n"
        f"unregistered_worktree: {repo.resolve()}\n"
    )


def test_wt_acquire_parser_surface() -> None:
    args = build_parser().parse_args(
        [
            "wt",
            "acquire",
            "--initiative",
            "reward-widget",
            "--purpose",
            "scratch",
            "--repo-root",
            "/repo",
            "--base",
            "staging",
            "--branch",
            "topic/reward-widget",
            "--owner-id",
            "session-1",
            "--apply",
            "--json",
        ]
    )

    assert args.command == "wt"
    assert args.wt_command == "acquire"
    assert args.initiative == "reward-widget"
    assert args.purpose == "scratch"
    assert args.repo_root == "/repo"
    assert args.base == "staging"
    assert args.branch == "topic/reward-widget"
    assert args.owner_id == "session-1"
    assert args.apply is True
    assert args.json is True


def test_wt_acquire_preview_emits_one_json_document(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repository(tmp_path)
    db = tmp_path / "state" / "worktrees.sqlite3"
    cache_dir = tmp_path / "cache"
    (repo / ".awf").mkdir()
    (repo / ".awf" / "worktree.toml").write_text(
        "[worktree]\ndefault_base = \"staging\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))
    monkeypatch.setenv("AWF_WORKTREE_CACHE_DIR", str(cache_dir))

    rc, stdout, stderr = capture_main(
        [
            "wt",
            "acquire",
            "--repo-root",
            str(repo),
            "--initiative",
            "reward-widget",
            "--json",
        ]
    )

    payload = json.loads(stdout)
    assert rc == 0
    assert stderr == ""
    assert payload["command"] == "wt.acquire"
    assert payload["decision"] == "preview"
    assert payload["actions"][0]["kind"] == "create_worktree"
    assert len(GitClient(repo).list_worktrees()) == 1
    assert WorktreeRegistry(db).list_leases() == []
    assert not cache_dir.exists()


def test_wt_acquire_orphaned_lease_emits_blocked_json(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repository(tmp_path)
    db = tmp_path / "state" / "worktrees.sqlite3"
    cache_dir = tmp_path / "cache"
    (repo / ".awf").mkdir()
    (repo / ".awf" / "worktree.toml").write_text(
        "[worktree]\ndefault_base = \"staging\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))
    monkeypatch.setenv("AWF_WORKTREE_CACHE_DIR", str(cache_dir))
    args = [
        "wt",
        "acquire",
        "--repo-root",
        str(repo),
        "--initiative",
        "reward-widget",
        "--apply",
    ]

    first_rc, _, first_stderr = capture_main(args)
    assert first_rc == 0
    assert first_stderr == ""
    lease = WorktreeRegistry(db).find_active(
        GitClient(repo).repository_id(), "reward-widget", Purpose.FEATURE
    )
    assert lease is not None
    subprocess.run(
        ["git", "worktree", "remove", str(lease.worktree_path)],
        cwd=repo,
        check=True,
    )

    rc, stdout, stderr = capture_main([*args[:-1], "--json"])

    payload = json.loads(stdout)
    assert rc == 3
    assert stderr == ""
    assert payload["status"] == "blocked"
    assert payload["blockers"][0]["code"] == "orphaned_lease"



def test_wt_acquire_rejects_unsafe_base_with_json_error(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repository(tmp_path)
    monkeypatch.setenv(
        "AWF_WORKTREE_STATE_DB", str(tmp_path / "state" / "worktrees.sqlite3")
    )
    monkeypatch.setenv("AWF_WORKTREE_CACHE_DIR", str(tmp_path / "cache"))

    rc, stdout, stderr = capture_main(
        [
            "wt",
            "acquire",
            "--repo-root",
            str(repo),
            "--initiative",
            "reward-widget",
            "--base",
            "main:refs/heads/unrelated",
            "--json",
        ]
    )

    payload = json.loads(stdout)
    assert rc == 2
    assert stderr == ""
    assert payload["status"] == "error"
    assert payload["blockers"][0]["code"] == "config_error"
    assert len(GitClient(repo).list_worktrees()) == 1


def test_wt_acquire_lock_filesystem_error_emits_one_json_document(
    tmp_path: Path, monkeypatch
) -> None:
    @contextmanager
    def denied_lock(*args, **kwargs):
        raise PermissionError("lock directory is read-only")
        yield

    repo = make_repository(tmp_path)
    monkeypatch.setenv(
        "AWF_WORKTREE_STATE_DB", str(tmp_path / "state" / "worktrees.sqlite3")
    )
    monkeypatch.setenv("AWF_WORKTREE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("awf.worktrees.service.repository_lock", denied_lock)

    rc, stdout, stderr = capture_main(
        [
            "wt",
            "acquire",
            "--repo-root",
            str(repo),
            "--initiative",
            "reward-widget",
            "--json",
        ]
    )

    payload = json.loads(stdout)
    assert rc == 5
    assert stderr == ""
    assert payload["status"] == "error"
    assert payload["blockers"][0]["code"] == "filesystem_error"