"""§1.1 evaluate_gate impl/test conditions.

Drives the four impl conditions and four test conditions added in Group F.
The legacy review/verify path is unchanged; this file only covers the new
machine-evaluable strings.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from awf.core.db_validation import detect_database_signal, run_database_check
from awf.core.gates import evaluate_gate
from awf.core.state import promote_database_change_to_high_risk


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


# ---------------------------------------------------------------------------
# P0 database evidence: mandatory G5/G6 conditions
# ---------------------------------------------------------------------------

def _database_command(payload: dict[str, object]) -> list[str]:
    return [sys.executable, "-c", f"print({json.dumps(payload)!r})"]


def _database_schema() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "production_schema",
        "target_class": "production_metadata",
        "read_only": True,
        "schema_only": True,
        "engine": "mysql",
        "engine_version": "8.0",
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "schema_hash": "a" * 64,
        "object_counts": {"tables": 1, "columns": 8, "indexes": 2, "constraints": 3},
    }


def _database_verify() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "database_verify",
        "production_schema_hash": "a" * 64,
        "selected_option_id": "rewrite-query",
        "engine": "mysql",
        "execution_target": "local_same_engine",
        "production_primary_queries": False,
        "raw_production_rows": False,
        "equivalence": "pass",
        "integrity": "pass",
        "query_plan": "pass",
        "migration": "not_applicable",
        "rollback": "pass",
    }


def _database_decision(*, waiver: bool = False) -> dict[str, object]:
    baseline = {
        "id": "maintain-current",
        "kind": "maintain",
        "applicable": True,
        "summary": "Keep current query",
        "equivalence_plan": "Use the current result set",
        "integrity_plan": "Verify existing constraints",
        "normalization_assessment": "No model change",
        "read_write_cost": "Measure production-shaped workload",
        "operational_risks": [],
        "transition_risks": [],
        "rollback_or_exit": "No change",
        "unavailable_reason": None,
        "denormalization_assessment": None,
        "physical_design_assessment": None,
    }
    rewrite = {
        "id": "rewrite-query",
        "kind": "query_change",
        "applicable": True,
        "summary": "Rewrite aggregation query",
        "equivalence_plan": "Compare baseline results",
        "integrity_plan": "Verify constraints before and after",
        "normalization_assessment": "No model change",
        "read_write_cost": "Measure production-shaped latency",
        "operational_risks": [],
        "transition_risks": [],
        "rollback_or_exit": "Restore current query",
        "unavailable_reason": None,
        "denormalization_assessment": None,
        "physical_design_assessment": None,
    }
    decision: dict[str, object] = {
        "schema_version": 1,
        "status": "selected",
        "change_surfaces": ["query"],
        "baseline_option_id": "maintain-current",
        "recommended_option_id": "rewrite-query",
        "selected_option_id": "rewrite-query",
        "candidates": [baseline, rewrite],
        "recommendation_rationale": "Preserves correctness at the lowest lifecycle cost.",
    }
    if waiver:
        decision["local_data_test_waiver"] = {
            "reason": "Approved local data is unavailable",
            "approver": "database-owner",
            "timestamp": "2026-08-24T00:00:00Z",
        }
    return decision


def _prepare_database_plan_evidence(
    repo: Path,
    *,
    waiver: bool = False,
    verify: bool = False,
) -> None:
    artifacts = repo / ".workflow" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "spec.md").write_text("Database query change", encoding="utf-8")
    (artifacts / "database-decision.json").write_text(
        json.dumps(_database_decision(waiver=waiver)),
        encoding="utf-8",
    )
    (repo / ".workflow" / "manifest.json").write_text(
        json.dumps(
            {
                "database_validation": {
                    "enabled": True,
                    "schema_command": _database_command(_database_schema()),
                    "verify_command": _database_command(_database_verify()) if verify else [],
                    "test_command": [],
                    "command_timeout_seconds": 5,
                    "max_schema_age_hours": 24,
                    "allow_production_replica_sample": False,
                }
            }
        ),
        encoding="utf-8",
    )
    promote_database_change_to_high_risk(
        str(repo),
        detect_database_signal(repo).reasons,
    )
    result = run_database_check(repo, "plan")
    assert result.status == "pass"


def _checks_by_condition(checks: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(check["condition"]): check for check in checks}


def _passing_verify_data() -> dict[str, object]:
    return {
        "conclusion": "PASS",
        "scope": {"violations": 0},
        "compliance": {"fail": 0, "percentage": 100},
        "quality": {"critical": 0},
    }


def _passing_test_data() -> dict[str, object]:
    return {
        "conclusion": "PASS",
        "suites": [{"name": "unit", "passed": 1, "failed": 0}],
        "regressions": [],
        "acceptance": {"passed": 1, "total": 1},
        "coverage": {"percentage": 100},
    }


def test_database_verify_evidence_is_mandatory_for_small_changes(tmp_path: Path) -> None:
    repo = _scaffold(
        tmp_path,
        "verify",
        conditions=["scope.violations == 0"],
        gate_id="G5",
    )
    _prepare_database_plan_evidence(repo)

    passed, checks = evaluate_gate(
        str(repo),
        "verify",
        _passing_verify_data(),
        change_class="small",
    )

    assert not passed
    assert _checks_by_condition(checks)["database.production_schema"]["passed"] is False


def test_database_local_test_is_mandatory_for_standard_changes(tmp_path: Path) -> None:
    repo = _scaffold(
        tmp_path,
        "test",
        conditions=["suites.failed == 0"],
        gate_id="G6",
    )
    _prepare_database_plan_evidence(repo)

    passed, checks = evaluate_gate(
        str(repo),
        "test",
        _passing_test_data(),
        change_class="standard",
    )

    assert not passed
    assert _checks_by_condition(checks)["database.local_test"]["passed"] is False


def test_database_local_test_valid_waiver_passes_g6(tmp_path: Path) -> None:
    repo = _scaffold(
        tmp_path,
        "test",
        conditions=["suites.failed == 0"],
        gate_id="G6",
    )
    _prepare_database_plan_evidence(repo, waiver=True)
    result = run_database_check(repo, "test")
    assert result.status == "pass"

    passed, checks = evaluate_gate(
        str(repo),
        "test",
        _passing_test_data(),
        change_class="standard",
    )

    assert passed, checks
    assert _checks_by_condition(checks)["database.local_test"]["passed"] is True
    summary = _checks_by_condition(checks)["database.local_test"]["database_summary"]
    assert summary["waiver_present"] == "true"
    assert "waiver_reason" not in summary


def _set_change_class(repo: Path, change_class: str) -> None:
    state_path = repo / ".workflow" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["changeClass"] = change_class
    state_path.write_text(json.dumps(state), encoding="utf-8")


def test_database_risk_class_is_mandatory_for_small_verify_state(
    tmp_path: Path,
) -> None:
    repo = _scaffold(
        tmp_path,
        "verify",
        conditions=["scope.violations == 0"],
        gate_id="G5",
    )
    _prepare_database_plan_evidence(repo, verify=True)
    assert run_database_check(repo, "verify").status == "pass"
    _set_change_class(repo, "small")

    passed, checks = evaluate_gate(
        str(repo),
        "verify",
        _passing_verify_data(),
        change_class="small",
    )

    assert not passed
    assert _checks_by_condition(checks)["database.risk_class"]["passed"] is False


def test_database_risk_class_is_mandatory_for_small_test_state(
    tmp_path: Path,
) -> None:
    repo = _scaffold(
        tmp_path,
        "test",
        conditions=["suites.failed == 0"],
        gate_id="G6",
    )
    _prepare_database_plan_evidence(repo, waiver=True)
    assert run_database_check(repo, "test").status == "pass"
    _set_change_class(repo, "small")

    passed, checks = evaluate_gate(
        str(repo),
        "test",
        _passing_test_data(),
        change_class="small",
    )

    assert not passed
    assert _checks_by_condition(checks)["database.risk_class"]["passed"] is False


def test_missing_agent_card_is_a_stable_gate_configuration_failure(tmp_path: Path) -> None:
    repo = _scaffold(
        tmp_path,
        "impl",
        conditions=["tasks.pending == 0"],
        gate_id="G4",
    )
    (repo / ".workflow" / "agent-cards" / "impl.json").unlink()

    passed, checks = evaluate_gate(str(repo), "impl", {})

    assert not passed
    assert checks == [
        {
            "condition": "gate_configuration",
            "passed": False,
            "detail": "agent_card_missing",
        }
    ]


def test_empty_agent_card_conditions_fail_even_for_empty_impl_result(tmp_path: Path) -> None:
    repo = _scaffold(tmp_path, "impl", conditions=[], gate_id="G4")

    passed, checks = evaluate_gate(str(repo), "impl", {})

    assert not passed
    assert checks == [
        {
            "condition": "gate_configuration",
            "passed": False,
            "detail": "pass_conditions_empty",
        }
    ]


def test_unknown_agent_card_condition_is_a_stable_configuration_failure(
    tmp_path: Path,
) -> None:
    repo = _scaffold(
        tmp_path,
        "impl",
        conditions=["worker.can_override_database_gate == true"],
        gate_id="G4",
    )

    passed, checks = evaluate_gate(str(repo), "impl", {})

    assert not passed
    assert checks == [
        {
            "condition": "gate_configuration",
            "passed": False,
            "detail": "pass_conditions_unsupported",
        }
    ]


def test_database_check_rejects_signal_inputs_changed_by_schema_command(
    tmp_path: Path,
) -> None:
    for name, relative_path, replacement in (
        (
            "spec",
            ".workflow/artifacts/spec.md",
            "Database query changed while schema check ran",
        ),
        (
            "allowed_files",
            ".workflow/artifacts/allowed-files.json",
            json.dumps({"planned_files": ["db/schema.sql"]}),
        ),
    ):
        repo = _scaffold(
            tmp_path / name,
            "impl",
            conditions=["tasks.pending == 0"],
            gate_id="G4",
        )
        artifacts = repo / ".workflow" / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "spec.md").write_text("Database query change", encoding="utf-8")
        (artifacts / "database-decision.json").write_text(
            json.dumps(_database_decision()),
            encoding="utf-8",
        )
        target = repo / relative_path
        script = (
            "from pathlib import Path; "
            f"Path({str(target)!r}).write_text({replacement!r}, encoding='utf-8'); "
            f"print({json.dumps(_database_schema())!r})"
        )
        (repo / ".workflow" / "manifest.json").write_text(
            json.dumps(
                {
                    "database_validation": {
                        "enabled": True,
                        "schema_command": [sys.executable, "-c", script],
                        "verify_command": [],
                        "test_command": [],
                        "command_timeout_seconds": 5,
                        "max_schema_age_hours": 24,
                        "allow_production_replica_sample": False,
                    }
                }
            ),
            encoding="utf-8",
        )

        result = run_database_check(repo, "plan")

        assert result.status == "fail"
        assert result.blockers == ("database_signal_changed",)
        assert not (artifacts / "database-validation-evidence.json").exists()


def test_impl_and_test_gate_conditions_reject_malformed_worker_types(
    tmp_path: Path,
) -> None:
    cases = (
        ("impl", "tasks.pending == 0", {"tasks_pending": "none"}, "G4"),
        ("impl", "commits.count > 0", {"commits": ("commit",)}, "G4"),
        ("impl", "lint_clean == true", {"lint_clean": "yes"}, "G4"),
        ("impl", "build_passed == true", {"build_passed": 1}, "G4"),
        ("test", "suites.failed == 0", {"suites": "none"}, "G6"),
        ("test", "suites.failed == 0", {"suites": [{"failed": False}]}, "G6"),
        ("test", "suites.failed == 0", {"suites": [{"failed": "0"}]}, "G6"),
        ("test", "suites.failed == 0", {"suites": ["not-a-suite"]}, "G6"),
        ("test", "regressions.count == 0", {"regressions": "none"}, "G6"),
    )
    for index, (phase, condition, data, gate_id) in enumerate(cases):
        repo = _scaffold(
            tmp_path / str(index),
            phase,
            conditions=[condition],
            gate_id=gate_id,
        )

        passed, checks = evaluate_gate(str(repo), phase, data)

        assert not passed
        assert checks[0]["passed"] is False
