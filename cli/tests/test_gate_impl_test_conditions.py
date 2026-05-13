"""§1.1 evaluate_gate impl/test conditions.

Drives the four impl conditions and four test conditions added in Group F.
The legacy review/verify path is unchanged; this file only covers the new
machine-evaluable strings.
"""

from __future__ import annotations

import json
from pathlib import Path

from awf.core.gates import evaluate_gate


def _scaffold(tmp_path: Path, phase: str, *, conditions: list[str], gate_id: str) -> Path:
    wf = tmp_path / ".workflow"
    (wf / "agent-cards").mkdir(parents=True)
    state = {
        "id": "gate-test",
        "currentPhase": phase,
        "phases": {phase: {"status": "in_progress", "retries": 0}},
        "gates": {},
        "history": [],
        "totalExecutions": 0,
        "changeClass": "standard",
    }
    (wf / "state.json").write_text(json.dumps(state), encoding="utf-8")
    card = {
        "name": f"phase-{phase}",
        "version": "1.0.0",
        "gate": {"id": gate_id, "pass_conditions": conditions},
    }
    (wf / "agent-cards" / f"{phase}.json").write_text(json.dumps(card), encoding="utf-8")
    return tmp_path


def test_impl_all_conditions_pass(tmp_path: Path) -> None:
    repo = _scaffold(
        tmp_path,
        "impl",
        conditions=[
            "tasks.pending == 0",
            "lint_clean == true",
            "build_passed == true",
            "commits.count > 0",
        ],
        gate_id="G4",
    )
    data = {
        "tasks_completed": ["T001", "T002"],
        "tasks_pending": [],
        "lint_clean": True,
        "build_passed": True,
        "commits": ["abc123"],
    }
    passed, checks = evaluate_gate(str(repo), "impl", data)
    assert passed
    by_cond = {c["condition"]: c for c in checks}
    assert by_cond["tasks.pending == 0"]["passed"]
    assert by_cond["lint_clean == true"]["passed"]
    assert by_cond["build_passed == true"]["passed"]
    assert by_cond["commits.count > 0"]["passed"]


def test_impl_pending_tasks_fail_gate(tmp_path: Path) -> None:
    repo = _scaffold(
        tmp_path,
        "impl",
        conditions=["tasks.pending == 0"],
        gate_id="G4",
    )
    data = {"tasks_pending": ["T005", "T006"]}
    passed, checks = evaluate_gate(str(repo), "impl", data)
    assert not passed
    assert "tasks_pending=2" in checks[0]["detail"]


def test_impl_lint_dirty_fails(tmp_path: Path) -> None:
    repo = _scaffold(tmp_path, "impl", conditions=["lint_clean == true"], gate_id="G4")
    data = {"lint_clean": False}
    passed, _ = evaluate_gate(str(repo), "impl", data)
    assert not passed


def test_impl_no_commits_fails(tmp_path: Path) -> None:
    repo = _scaffold(tmp_path, "impl", conditions=["commits.count > 0"], gate_id="G4")
    data = {"commits": []}
    passed, _ = evaluate_gate(str(repo), "impl", data)
    assert not passed


def test_test_phase_all_conditions_pass(tmp_path: Path) -> None:
    repo = _scaffold(
        tmp_path,
        "test",
        conditions=[
            "suites.failed == 0",
            "regressions.count == 0",
            "acceptance.passed == acceptance.total",
            "coverage.percentage >= 70",
        ],
        gate_id="G6",
    )
    data = {
        "suites": [{"name": "unit", "passed": 100, "failed": 0}],
        "regressions": [],
        "acceptance": {"passed": 5, "total": 5},
        "coverage": {"percentage": 85},
    }
    passed, checks = evaluate_gate(str(repo), "test", data)
    assert passed, checks


def test_test_phase_acceptance_partial_fails(tmp_path: Path) -> None:
    repo = _scaffold(
        tmp_path,
        "test",
        conditions=["acceptance.passed == acceptance.total"],
        gate_id="G6",
    )
    data = {"acceptance": {"passed": 4, "total": 5}}
    passed, checks = evaluate_gate(str(repo), "test", data)
    assert not passed
    assert "acceptance=4/5" in checks[0]["detail"]


def test_test_phase_acceptance_zero_total_fails(tmp_path: Path) -> None:
    """Empty acceptance suite must NOT pass — guards against missing data."""
    repo = _scaffold(
        tmp_path,
        "test",
        conditions=["acceptance.passed == acceptance.total"],
        gate_id="G6",
    )
    data = {"acceptance": {"passed": 0, "total": 0}}
    passed, _ = evaluate_gate(str(repo), "test", data)
    assert not passed


def test_test_phase_coverage_below_threshold_fails(tmp_path: Path) -> None:
    repo = _scaffold(
        tmp_path,
        "test",
        conditions=["coverage.percentage >= 70"],
        gate_id="G6",
    )
    data = {"coverage": {"percentage": 50}}
    passed, checks = evaluate_gate(str(repo), "test", data)
    assert not passed
    assert "coverage_percentage=50" in checks[0]["detail"]


def test_test_phase_suite_failure_fails(tmp_path: Path) -> None:
    repo = _scaffold(
        tmp_path,
        "test",
        conditions=["suites.failed == 0"],
        gate_id="G6",
    )
    data = {"suites": [{"name": "unit", "passed": 90, "failed": 2}, {"name": "e2e", "passed": 5, "failed": 1}]}
    passed, checks = evaluate_gate(str(repo), "test", data)
    assert not passed
    # 2 + 1 = 3 failed
    assert "suites_failed=3" in checks[0]["detail"]
