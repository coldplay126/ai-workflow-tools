"""apply_workflow_result tests for impl/test phase support (§1.2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf.core.workflow_results import (
    apply_workflow_result,
    render_impl_report,
    render_test_report,
)


def _make_workflow_root(tmp_path: Path) -> Path:
    """Minimal .workflow scaffold so apply_workflow_result can land artifacts."""
    wf = tmp_path / ".workflow"
    (wf / "artifacts").mkdir(parents=True)
    state = {
        "id": "test-cycle",
        "currentPhase": "impl",
        "changeClass": "standard",
        "phases": {"impl": {"status": "in_progress", "retries": 0}},
        "gates": {},
        "history": [],
        "totalExecutions": 0,
    }
    (wf / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return tmp_path


def test_render_impl_report_smoke() -> None:
    data = {
        "tasks_completed": ["T001", "T002"],
        "tasks_pending": [],
        "commits": ["abc123"],
        "lint_clean": True,
        "build_passed": True,
        "conclusion": "PASS",
    }
    markdown, passed = render_impl_report(data, True, [{"condition": "stub", "passed": True, "detail": "ok"}])
    assert passed
    assert "# Implementation Report" in markdown
    assert "Tasks completed: 2" in markdown
    assert "abc123" in markdown
    assert "G4: PASS" in markdown


def test_render_test_report_smoke() -> None:
    data = {
        "suites": [{"name": "unit", "passed": 100, "failed": 0, "duration_sec": 1.5}],
        "regressions": [],
        "acceptance": {"passed": 10, "total": 10},
        "coverage": {"percentage": 85},
        "conclusion": "PASS",
    }
    markdown, passed = render_test_report(data, True, [{"condition": "stub", "passed": True, "detail": "ok"}])
    assert passed
    assert "# Test Report" in markdown
    assert "passed=100" in markdown
    assert "Coverage %: 85" in markdown
    assert "G6: PASS" in markdown


def test_apply_workflow_result_impl_writes_artifact(tmp_path: Path) -> None:
    repo = _make_workflow_root(tmp_path)
    result_payload = {
        "status": "completed",
        "result": {
            "tasks_completed": ["T001"],
            "commits": ["abc123"],
            "lint_clean": True,
            "build_passed": True,
            "conclusion": "PASS",
        },
    }
    result_file = tmp_path / "impl-result.json"
    result_file.write_text(json.dumps(result_payload), encoding="utf-8")

    output_path, passed = apply_workflow_result(str(repo), "impl", str(result_file))
    assert output_path.exists()
    assert output_path.name == "implementation-report.md"
    assert passed is True
    content = output_path.read_text(encoding="utf-8")
    assert "# Implementation Report" in content
    assert "abc123" in content

    state = json.loads((repo / ".workflow" / "state.json").read_text(encoding="utf-8"))
    assert state["gates"]["G4"]["passed"] is True


def test_apply_workflow_result_test_writes_artifact(tmp_path: Path) -> None:
    repo = _make_workflow_root(tmp_path)
    (repo / ".workflow" / "state.json").write_text(
        json.dumps({
            "id": "test-cycle",
            "currentPhase": "test",
            "changeClass": "standard",
            "phases": {"test": {"status": "in_progress", "retries": 0}},
            "gates": {},
            "history": [],
            "totalExecutions": 0,
        }),
        encoding="utf-8",
    )
    result_payload = {
        "status": "completed",
        "result": {
            "suites": [{"name": "unit", "passed": 100, "failed": 0}],
            "acceptance": {"passed": 5, "total": 5},
            "coverage": {"percentage": 90},
            "conclusion": "PASS",
        },
    }
    result_file = tmp_path / "test-result.json"
    result_file.write_text(json.dumps(result_payload), encoding="utf-8")

    output_path, passed = apply_workflow_result(str(repo), "test", str(result_file))
    assert output_path.name == "test-report.md"
    assert passed is True
    assert "# Test Report" in output_path.read_text(encoding="utf-8")

    state = json.loads((repo / ".workflow" / "state.json").read_text(encoding="utf-8"))
    # G6 is the test phase gate
    assert state["gates"].get("G6", {}).get("passed") is True


def test_apply_workflow_result_rejects_unknown_phase(tmp_path: Path) -> None:
    repo = _make_workflow_root(tmp_path)
    result_file = tmp_path / "r.json"
    result_file.write_text(json.dumps({"status": "completed", "result": {}}), encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        apply_workflow_result(str(repo), "approve", str(result_file))
    assert "apply-result supports" in str(exc.value)
