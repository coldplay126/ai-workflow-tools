from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from awf.cli import build_parser, main
from worktree_fixtures import make_repository


def capture_main(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        result = main(argv)
    return result, stdout.getvalue(), stderr.getvalue()


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
    db = tmp_path / "state.sqlite3"
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


def test_wt_doctor_reports_unregistered_worktree_without_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repository(tmp_path)
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(tmp_path / "state.sqlite3"))

    rc, stdout, _ = capture_main(
        ["wt", "doctor", "--repo-root", str(repo), "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 0
    assert payload["actions"][0]["kind"] == "unregistered_worktree"
    assert payload["actions"][0]["path"] == str(repo.resolve())
