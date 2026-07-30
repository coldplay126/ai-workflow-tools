from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
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
