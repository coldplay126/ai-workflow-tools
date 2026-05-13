"""§8.7-P1 phase telemetry tests."""

from __future__ import annotations

import json
from pathlib import Path

from awf.core.state import record_phase_telemetry, load_workflow_state
from awf.core.workflow_status import summarize_workflow_state


def _make_workflow(tmp_path: Path) -> Path:
    wf = tmp_path / ".workflow"
    wf.mkdir(parents=True)
    state = {
        "id": "telem-test",
        "currentPhase": "impl",
        "phases": {"impl": {"status": "in_progress", "retries": 0}},
        "gates": {},
        "history": [],
        "totalExecutions": 0,
    }
    (wf / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return tmp_path


def test_first_record_creates_phase_entry(tmp_path: Path) -> None:
    repo = _make_workflow(tmp_path)
    record_phase_telemetry(
        str(repo),
        "impl",
        input_tokens=1000,
        output_tokens=500,
        cost_usd=0.012,
        provider="claude-code",
    )
    state = load_workflow_state(str(repo))
    entry = state["telemetry"]["phases"]["impl"]
    assert entry["input_tokens"] == 1000
    assert entry["output_tokens"] == 500
    assert entry["cost_usd"] == 0.012
    assert entry["runs"] == 1
    assert entry["providers"] == ["claude-code"]


def test_subsequent_records_accumulate(tmp_path: Path) -> None:
    repo = _make_workflow(tmp_path)
    for _ in range(3):
        record_phase_telemetry(
            str(repo),
            "impl",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.001,
            provider="codex",
        )
    state = load_workflow_state(str(repo))
    entry = state["telemetry"]["phases"]["impl"]
    assert entry["input_tokens"] == 300
    assert entry["output_tokens"] == 150
    assert abs(entry["cost_usd"] - 0.003) < 1e-9
    assert entry["runs"] == 3
    assert entry["providers"] == ["codex"]


def test_multiple_providers_tracked(tmp_path: Path) -> None:
    repo = _make_workflow(tmp_path)
    record_phase_telemetry(str(repo), "verify", input_tokens=10, output_tokens=5, cost_usd=0.001, provider="codex")
    record_phase_telemetry(str(repo), "verify", input_tokens=20, output_tokens=10, cost_usd=0.002, provider="claude-code")
    state = load_workflow_state(str(repo))
    entry = state["telemetry"]["phases"]["verify"]
    assert set(entry["providers"]) == {"codex", "claude-code"}
    assert entry["runs"] == 2


def test_negative_inputs_clamp_to_zero(tmp_path: Path) -> None:
    repo = _make_workflow(tmp_path)
    record_phase_telemetry(str(repo), "impl", input_tokens=-50, output_tokens=-20, cost_usd=-0.5)
    state = load_workflow_state(str(repo))
    entry = state["telemetry"]["phases"]["impl"]
    assert entry["input_tokens"] == 0
    assert entry["output_tokens"] == 0
    assert entry["cost_usd"] == 0


def test_missing_state_is_silent(tmp_path: Path) -> None:
    # No state.json — function must not raise
    record_phase_telemetry(str(tmp_path / "nonexistent"), "impl", input_tokens=10, output_tokens=5, cost_usd=0.01)


def test_summary_includes_telemetry_block(tmp_path: Path) -> None:
    repo = _make_workflow(tmp_path)
    record_phase_telemetry(str(repo), "impl", input_tokens=1000, output_tokens=500, cost_usd=0.015, provider="codex")
    record_phase_telemetry(str(repo), "verify", input_tokens=2000, output_tokens=300, cost_usd=0.020, provider="claude-code")
    state = load_workflow_state(str(repo))
    text = summarize_workflow_state(state)
    assert "telemetry:" in text
    assert "- impl:" in text
    assert "in=1,000" in text
    assert "- verify:" in text
    assert "cost=$0.0150" in text or "cost=$0.0200" in text
    assert "total:" in text


def test_summary_omits_telemetry_when_absent(tmp_path: Path) -> None:
    repo = _make_workflow(tmp_path)
    state = load_workflow_state(str(repo))
    text = summarize_workflow_state(state)
    assert "telemetry:" not in text
