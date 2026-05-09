from __future__ import annotations

import argparse
import json

from awf.commands.ready_gate import enforce_ready_gate


def _decision(name: str = "analysis", decision: str = "block", exit_code: int = 20) -> dict:
    return {
        "name": name,
        "decision": decision,
        "exit_code": exit_code,
        "reason": "fixture reason",
        "required_capabilities": ["analysis_run"],
        "recommended_next": [{
            "command": "awf ready --repo-root .",
            "why": "fixture next step",
        }],
    }


def test_enforce_ready_gate_bypasses_when_requested(monkeypatch) -> None:
    called = False

    def fail_collect(_repo_root):
        nonlocal called
        called = True
        raise AssertionError("should not collect")

    monkeypatch.setattr("awf.commands.ready_gate.collect_ready_report", fail_collect)

    rc = enforce_ready_gate(argparse.Namespace(repo_root=".", no_ready_gate=True), "analysis")

    assert rc == 0
    assert called is False


def test_enforce_ready_gate_prints_human_block(monkeypatch, capsys) -> None:
    monkeypatch.setattr("awf.commands.ready_gate.collect_ready_report", lambda _repo_root: {})
    monkeypatch.setattr("awf.commands.ready_gate.evaluate_ready_gate", lambda _report, gate: _decision(gate))

    rc = enforce_ready_gate(argparse.Namespace(repo_root=".", no_ready_gate=False), "analysis")

    assert rc == 20
    err = capsys.readouterr().err
    assert "ready gate `analysis` returned block" in err
    assert "next_step: awf ready --repo-root ." in err
    assert "--no-ready-gate" in err


def test_enforce_ready_gate_prints_json_block(monkeypatch, capsys) -> None:
    monkeypatch.setattr("awf.commands.ready_gate.collect_ready_report", lambda _repo_root: {})
    monkeypatch.setattr(
        "awf.commands.ready_gate.evaluate_ready_gate",
        lambda _report, gate: _decision(gate, decision="dry_run_only", exit_code=10),
    )

    rc = enforce_ready_gate(argparse.Namespace(repo_root=".", no_ready_gate=False), "analysis", json_output=True)

    assert rc == 10
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "ready_gate_blocked"
    assert payload["gate"]["decision"] == "dry_run_only"
