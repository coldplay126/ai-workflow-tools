"""apply_workflow_result tests for impl/test phase support (§1.2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf.core.judge import _finding_signature, synthesize_workflow_multi_provider_results
from awf.core.workflow_results import (
    apply_workflow_result,
    render_impl_report,
    render_test_report,
    render_verify_report,
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


def test_render_verify_report_accepts_string_evidence_entries() -> None:
    data = {
        "conclusion": "PASS",
        "scope": {"changed_files": 3, "planned_files": 5, "violations": 0},
        "compliance": {"pass": 3, "fail": 0, "total_requirements": 3, "percentage": 100},
        "quality": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        "evidence": ["OMP verifier passed"],
        "risks": ["Scope base is current HEAD"],
        "action_items": ["Use parent-to-commit diff"],
    }

    markdown, passed = render_verify_report(data, True, [])

    assert passed
    assert "- evidence: OMP verifier passed" in markdown
    assert "- risk: Scope base is current HEAD" in markdown
    assert "- action: Use parent-to-commit diff" in markdown


def test_database_gate_reports_render_only_sanitized_fields() -> None:
    unsafe_detail = (
        "argv=['db-client', '--password=secret'] stdout=CREATE TABLE users "
        "env=DATABASE_URL sample=[customer-row]"
    )
    verify_data = {
        "conclusion": "PASS",
        "scope": {"changed_files": 0, "planned_files": 0, "violations": 0},
        "compliance": {"pass": 1, "fail": 0, "total_requirements": 1, "percentage": 100},
        "quality": {"critical": 0, "high": 0, "medium": 0, "low": 0},
    }
    test_data = {
        "conclusion": "PASS",
        "suites": [{"name": "unit", "passed": 1, "failed": 0}],
        "regressions": [],
        "acceptance": {"passed": 1, "total": 1},
        "coverage": {"percentage": 100},
    }
    gate_checks = [
        {
            "condition": "database.production_schema",
            "passed": True,
            "detail": unsafe_detail,
            "database_summary": {
                "schema_hash_prefix": "aaaaaaaaaaaa",
                "engine": "mysql",
                "engine_version": "8.0",
                "selected_option": "rewrite-query",
                "stage": "verify",
                "status": "pass",
            },
        },
        {
            "condition": "database.local_test",
            "passed": True,
            "detail": unsafe_detail,
            "database_summary": {
                "selected_option": "rewrite-query",
                "stage": "test",
                "status": "waived",
                "waiver_reason": "DROP TABLE customer",
            },
        },
    ]

    for waiver_reason in (
        "DROP TABLE customer",
        "CREATE INDEX customer_email_idx",
        "DATABASE_URL=postgres://user:password@db.internal/service",
        "<script>document.write('sample')</script>",
        "sample row:\ncustomer@example.com",
    ):
        gate_checks[1]["database_summary"]["waiver_reason"] = waiver_reason
        verify_markdown, _ = render_verify_report(verify_data, True, gate_checks)
        test_markdown, _ = render_test_report(test_data, True, gate_checks)
        rendered = verify_markdown + test_markdown

        assert "schema_hash_prefix=aaaaaaaaaaaa" in rendered
        assert "engine=mysql" in rendered
        assert "engine_version=8.0" in rendered
        assert "selected_option=rewrite-query" in rendered
        assert "waiver_present=true" in rendered
        assert "waiver_reason=" not in rendered
        assert waiver_reason not in rendered
        assert "db-client" not in rendered
        assert "password=secret" not in rendered
        assert "CREATE TABLE" not in rendered
        assert "DATABASE_URL" not in rendered
        assert "customer-row" not in rendered


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


def test_apply_workflow_result_accepts_nested_phase_metrics(tmp_path: Path) -> None:
    repo = _make_workflow_root(tmp_path)
    result_payload = {
        "status": "completed",
        "phase": "review",
        "provider": "omp",
        "result": {
            "conclusion": "PASS",
            "findings": [],
            "phase_metrics": {
                "coverage": {
                    "total_requirements": 3,
                    "mapped_requirements": 3,
                    "percentage": 100,
                    "gaps": [],
                }
            },
        },
    }
    result_file = tmp_path / "review-result.json"
    result_file.write_text(json.dumps(result_payload), encoding="utf-8")

    output_path, passed = apply_workflow_result(
        str(repo),
        "review",
        str(result_file),
    )

    assert passed is True
    assert "Coverage: 100%" in output_path.read_text(encoding="utf-8")


def test_apply_escaped_result_accepts_scalar_evidence(tmp_path: Path) -> None:
    repo = _make_workflow_root(tmp_path)
    result_file = tmp_path / "escaped-result.json"
    result_file.write_text(
        json.dumps(
            {
                "status": "escaped",
                "phase": "review",
                "result": {},
                "provider": "omp",
                "escape": {
                    "reason": "provider_unavailable",
                    "severity": "HIGH",
                    "summary": "Native worker could not start",
                    "recommended_action": "configure_provider",
                    "evidence": ["provider unavailable"],
                },
            }
        ),
        encoding="utf-8",
    )

    output_path, passed = apply_workflow_result(
        str(repo),
        "review",
        str(result_file),
    )

    assert passed is False
    content = output_path.read_text(encoding="utf-8")
    assert "Worker Escaped" in content
    assert "- [note] provider unavailable" in content


def test_synthesis_normalizes_nested_phase_metrics_before_judging(
    tmp_path: Path,
) -> None:
    repo = _make_workflow_root(tmp_path)
    coverage = {
        "total_requirements": 3,
        "mapped_requirements": 3,
        "percentage": 100,
        "gaps": [],
    }
    primary = tmp_path / "primary.json"
    secondary = tmp_path / "secondary.json"
    primary.write_text(
        json.dumps(
            {
                "status": "completed",
                "phase": "review",
                "provider": "omp",
                "result": {
                    "conclusion": "PASS",
                    "findings": [],
                    "phase_metrics": {"coverage": coverage},
                },
            }
        ),
        encoding="utf-8",
    )
    secondary.write_text(
        json.dumps({"conclusion": "PASS", "findings": [], "coverage": coverage}),
        encoding="utf-8",
    )

    synthesis = synthesize_workflow_multi_provider_results(
        str(repo),
        "review",
        str(primary),
        str(secondary),
    )

    assert synthesis["primary_gate_passed"] is True
    assert synthesis["secondary_gate_passed"] is True
    assert synthesis["judge_passed"] is True


def test_apply_workflow_result_rejects_unknown_phase(tmp_path: Path) -> None:
    repo = _make_workflow_root(tmp_path)
    result_file = tmp_path / "r.json"
    result_file.write_text(json.dumps({"status": "completed", "result": {}}), encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        apply_workflow_result(str(repo), "approve", str(result_file))
    assert "apply-result supports" in str(exc.value)

def test_finding_signature_sorts_locations_and_uses_description_summary_fallback() -> None:
    finding = {
        "severity": "HIGH",
        "category": "security",
        "locations": ["src/z.py:9", "src/a.py:2"],
        "description": "authorization bypass",
    }
    expected = (
        "HIGH",
        "security",
        "src/a.py:2|src/z.py:9",
        "authorization bypass",
    )

    assert _finding_signature(finding) == expected
    assert _finding_signature(
        {**finding, "description": "", "summary": "authorization bypass"}
    ) == expected


def test_review_high_findings_with_different_locations_are_a_mismatch(tmp_path: Path) -> None:
    repo = _make_workflow_root(tmp_path)
    coverage = {
        "total_requirements": 1,
        "mapped_requirements": 1,
        "percentage": 100,
        "gaps": [],
    }
    finding = {
        "severity": "HIGH",
        "category": "security",
        "description": "authorization bypass",
    }
    primary = tmp_path / "primary.json"
    secondary = tmp_path / "secondary.json"
    primary.write_text(
        json.dumps(
            {
                "conclusion": "PASS",
                "coverage": coverage,
                "findings": [{**finding, "location": "src/auth.py:12"}],
            }
        ),
        encoding="utf-8",
    )
    secondary.write_text(
        json.dumps(
            {
                "conclusion": "PASS",
                "coverage": coverage,
                "findings": [{**finding, "location": "src/session.py:44"}],
            }
        ),
        encoding="utf-8",
    )

    synthesis = synthesize_workflow_multi_provider_results(
        str(repo),
        "review",
        str(primary),
        str(secondary),
    )

    assert synthesis["judge_passed"] is False
    assert "high_severity_findings_mismatch" in synthesis["judge_reasons"]
