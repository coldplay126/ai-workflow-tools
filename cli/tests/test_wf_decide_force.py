"""§1.6 `awf wf decide --force-from` tests."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from awf.commands.wf import run_wf_decide


def _make_workflow(tmp_path: Path, *, phase: str = "impl", status: str = "in_progress") -> Path:
    wf = tmp_path / ".workflow"
    (wf / "artifacts").mkdir(parents=True)
    state = {
        "id": "decide-test",
        "repo": tmp_path.name,
        "branch": "feat/x",
        "currentPhase": phase,
        "phases": {
            phase: {"status": status, "retries": 0},
        },
        "gates": {},
        "history": [],
        "totalExecutions": 0,
        "loop": {"replanCount": 0, "maxReplans": 3},
    }
    (wf / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return tmp_path


def _state(repo: Path) -> dict:
    return json.loads((repo / ".workflow" / "state.json").read_text(encoding="utf-8"))


def test_decide_without_force_rejects_non_deciding(tmp_path: Path, capsys) -> None:
    repo = _make_workflow(tmp_path, phase="impl", status="in_progress")
    rc = run_wf_decide(Namespace(
        decision="continue",
        repo_root=str(repo),
        phase=None,
        target=None,
        force_from=None,
    ))
    assert rc == 2
    err = capsys.readouterr().err
    assert "not in deciding state" in err
    assert "--force-from" in err


def test_force_from_status_match_continues(tmp_path: Path, capsys) -> None:
    repo = _make_workflow(tmp_path, phase="impl", status="in_progress")
    rc = run_wf_decide(Namespace(
        decision="continue",
        repo_root=str(repo),
        phase=None,
        target=None,
        force_from="in_progress",
    ))
    assert rc == 0
    err = capsys.readouterr().err
    assert "force_decide" in err

    state = _state(repo)
    history = state.get("history", [])
    actions = [h.get("action") for h in history]
    assert "force_decide" in actions
    force_entry = next(h for h in history if h.get("action") == "force_decide")
    assert "force_from=in_progress" in force_entry["details"]
    assert "prior_status=in_progress" in force_entry["details"]


def test_force_from_any_bypasses_status_check(tmp_path: Path) -> None:
    repo = _make_workflow(tmp_path, phase="verify", status="failed")
    rc = run_wf_decide(Namespace(
        decision="abort",
        repo_root=str(repo),
        phase=None,
        target=None,
        force_from="any",
    ))
    assert rc == 0
    state = _state(repo)
    history = state.get("history", [])
    actions = [h.get("action") for h in history]
    assert "force_decide" in actions
    # phase state should reflect the abort outcome from abort_workflow
    assert state.get("currentPhase") in {"aborted", "verify"}


def test_force_from_status_mismatch_still_rejects(tmp_path: Path, capsys) -> None:
    repo = _make_workflow(tmp_path, phase="impl", status="in_progress")
    rc = run_wf_decide(Namespace(
        decision="continue",
        repo_root=str(repo),
        phase=None,
        target=None,
        force_from="failed",  # mismatches current status
    ))
    assert rc == 2
    err = capsys.readouterr().err
    assert "not in deciding state" in err
    # When the user already passed --force-from, we don't re-suggest it
    assert "Pass `--force-from" not in err


def test_decide_in_deciding_state_works_without_force(tmp_path: Path) -> None:
    repo = _make_workflow(tmp_path, phase="impl", status="deciding")
    rc = run_wf_decide(Namespace(
        decision="continue",
        repo_root=str(repo),
        phase=None,
        target=None,
        force_from=None,
    ))
    assert rc == 0
    state = _state(repo)
    # No force_decide history entry expected in this path
    actions = [h.get("action") for h in state.get("history", [])]
    assert "force_decide" not in actions
