"""§3.4 cycle-complete hook tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf.core.state import apply_gate_result


def _make_workflow(tmp_path: Path, *, phase: str, status: str = "in_progress") -> Path:
    wf = tmp_path / ".workflow"
    (wf / "artifacts").mkdir(parents=True)
    state = {
        "id": "hook-test",
        "repo": tmp_path.name,
        "branch": "feat/x",
        "currentPhase": phase,
        "phases": {phase: {"status": status, "retries": 0}},
        "gates": {},
        "history": [],
        "totalExecutions": 0,
        "loop": {"replanCount": 0, "maxReplans": 3},
    }
    (wf / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return tmp_path


def test_hook_prints_when_done_reached(tmp_path: Path, capsys) -> None:
    """Mark the final phase as passing — currentPhase becomes 'completed'."""
    repo = _make_workflow(tmp_path, phase="done", status="in_progress")
    apply_gate_result(str(repo), "done", True)
    err = capsys.readouterr().err
    assert "cycle complete" in err
    assert "awf wf pr" in err
    assert "--dry-run" in err


def test_hook_silent_when_phase_failed(tmp_path: Path, capsys) -> None:
    repo = _make_workflow(tmp_path, phase="done", status="in_progress")
    apply_gate_result(str(repo), "done", False)
    err = capsys.readouterr().err
    assert "cycle complete" not in err


def test_hook_silent_for_intermediate_phase(tmp_path: Path, capsys) -> None:
    """Passing an intermediate phase does NOT trigger the cycle-complete hook."""
    repo = _make_workflow(tmp_path, phase="review", status="in_progress")
    apply_gate_result(str(repo), "review", True)
    err = capsys.readouterr().err
    assert "cycle complete" not in err
