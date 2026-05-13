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


def _write_provider_config(repo: Path, payload: dict) -> None:
    (repo / ".workflow" / "provider-config.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_hook_auto_invokes_wf_pr_when_pr_creation_auto_true(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = _make_workflow(tmp_path, phase="done", status="in_progress")
    _write_provider_config(repo, {"pr_creation": {"auto": True, "base": "main", "draft": True}})

    captured: dict = {}

    class _Completed:
        returncode = 0
        stdout = "https://github.com/x/y/pull/42\n"
        stderr = ""

    def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return _Completed()

    import awf.core.state as state_mod
    monkeypatch.setattr(state_mod.subprocess, "run", fake_run)

    apply_gate_result(str(repo), "done", True)

    assert captured["cmd"][:3] == ["awf", "wf", "pr"]
    assert "--base" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--base") + 1] == "main"
    assert "--draft" in captured["cmd"]
    err = capsys.readouterr().err
    assert "pr_creation.auto=true" in err


def test_hook_auto_with_dry_run_passes_flag(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = _make_workflow(tmp_path, phase="done", status="in_progress")
    _write_provider_config(repo, {"pr_creation": {"auto": True, "dry_run": True}})

    captured: dict = {}

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, cwd=None, text=None, capture_output=None, timeout=None):
        captured["cmd"] = cmd
        return _Completed()

    import awf.core.state as state_mod
    monkeypatch.setattr(state_mod.subprocess, "run", fake_run)
    apply_gate_result(str(repo), "done", True)
    assert "--dry-run" in captured["cmd"]


def test_hook_falls_back_when_awf_missing(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = _make_workflow(tmp_path, phase="done", status="in_progress")
    _write_provider_config(repo, {"pr_creation": {"auto": True}})

    import awf.core.state as state_mod

    def fake_run(*a, **k):
        raise FileNotFoundError("awf not found")

    monkeypatch.setattr(state_mod.subprocess, "run", fake_run)
    apply_gate_result(str(repo), "done", True)
    err = capsys.readouterr().err
    assert "awf CLI not found on PATH" in err


def test_hook_keeps_hint_when_auto_false(tmp_path: Path, capsys) -> None:
    repo = _make_workflow(tmp_path, phase="done", status="in_progress")
    _write_provider_config(repo, {"pr_creation": {"auto": False}})
    apply_gate_result(str(repo), "done", True)
    err = capsys.readouterr().err
    assert "cycle complete" in err
    assert "pr_creation.auto = true" in err  # tip mentions opt-in
