"""Focused CLI contract tests for ``awf wf db-check``."""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import awf.commands.wf as wf_commands
from awf.commands import wf_apply
from awf.cli import build_parser, main
from awf.core.db_validation import DatabaseCheckResult
from fixture_support import (
    VERIFY_RESULT,
    initialize_workflow_fixture,
    mark_workflow_prerequisites_passed,
    prepare_workflow_repo,
)



def capture_main(argv: list[str]) -> tuple[int, str, str]:
    """Run the public CLI entry point while capturing its process streams."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        return_code = main(argv)
    return return_code, stdout.getvalue(), stderr.getvalue()

def test_database_evidence_bridge_runs_only_when_gate_is_not_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        wf_commands,
        "evaluate_database_gate",
        lambda root, stage: [{"passed": False, "status": "fail"}],
        raising=False,
    )
    monkeypatch.setattr(
        wf_commands,
        "run_database_check",
        lambda root, stage: calls.append((str(root), stage)),
    )

    wf_commands._ensure_database_evidence_before_apply(tmp_path, "verify")

    assert calls == [(str(tmp_path), "verify")]


def test_database_evidence_bridge_skips_current_or_not_applicable_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        wf_commands,
        "evaluate_database_gate",
        lambda root, stage: [{"passed": True, "status": "not_applicable"}],
        raising=False,
    )
    monkeypatch.setattr(
        wf_commands,
        "run_database_check",
        lambda root, stage: pytest.fail("current evidence must not rerun commands"),
    )

    wf_commands._ensure_database_evidence_before_apply(tmp_path, "test")

@pytest.mark.parametrize("failure", ["gate", "command"])
def test_database_evidence_bridge_suppresses_operational_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    if failure == "gate":
        monkeypatch.setattr(
            wf_commands,
            "evaluate_database_gate",
            lambda root, stage: (_ for _ in ()).throw(
                RuntimeError("RAW_COMMAND_OUTPUT DATABASE_URL=postgres://secret@example.test")
            ),
        )
        monkeypatch.setattr(
            wf_commands,
            "run_database_check",
            lambda root, stage: DatabaseCheckResult(
                stage=stage,
                status="fail",
                evidence_path=None,
                evidence_hash=None,
                signal_reasons=("text:database",),
                blockers=("profile_disabled",),
            ),
        )
    else:
        monkeypatch.setattr(
            wf_commands,
            "evaluate_database_gate",
            lambda root, stage: [{"passed": False, "status": "fail"}],
        )
        monkeypatch.setattr(
            wf_commands,
            "run_database_check",
            lambda root, stage: (_ for _ in ()).throw(
                RuntimeError("RAW_COMMAND_OUTPUT DATABASE_URL=postgres://secret@example.test")
            ),
        )

    wf_commands._ensure_database_evidence_before_apply(tmp_path, "verify")


@pytest.mark.parametrize("phase", ["verify", "test"])
def test_apply_result_continues_after_database_bridge_error_without_leak(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    _write_state(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        wf_apply,
        "_ensure_database_evidence_before_apply",
        lambda root, stage: (_ for _ in ()).throw(
            RuntimeError("RAW_COMMAND_OUTPUT DATABASE_URL=postgres://secret@example.test")
        ),
    )
    monkeypatch.setattr(
        wf_apply,
        "apply_workflow_result",
        lambda root, stage, result_file: (
            calls.append(f"apply:{stage}") or (tmp_path / f"{stage}-report.md", False)
        ),
    )

    return_code = wf_apply.run_wf_apply_result(
        argparse.Namespace(
            repo_root=str(tmp_path),
            phase=phase,
            result_file=str(tmp_path / "worker-result.json"),
        )
    )

    captured = capsys.readouterr()
    assert return_code == 3
    assert calls == [f"apply:{phase}"]
    assert "RAW_COMMAND_OUTPUT" not in captured.out
    assert "postgres://secret" not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("phase", ["verify", "test"])
def test_apply_result_ensures_database_evidence_before_applying_worker_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    _write_state(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        wf_apply,
        "_ensure_database_evidence_before_apply",
        lambda root, stage: calls.append(f"ensure:{stage}"),
        raising=False,
    )
    monkeypatch.setattr(
        wf_apply,
        "apply_workflow_result",
        lambda root, stage, result_file: (
            calls.append(f"apply:{stage}") or (tmp_path / f"{stage}-report.md", False)
        ),
    )
    args = argparse.Namespace(
        repo_root=str(tmp_path),
        phase=phase,
        result_file=str(tmp_path / "worker-result.json"),
    )

    return_code = wf_apply.run_wf_apply_result(args)

    assert return_code == 3
    assert calls == [f"ensure:{phase}", f"apply:{phase}"]


def test_wf_next_auto_apply_ensures_database_evidence_before_gate_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    prepare_workflow_repo(repo_root, result_file=VERIFY_RESULT)
    assert initialize_workflow_fixture(repo_root, "Fixture verify workflow").returncode == 0
    mark_workflow_prerequisites_passed(repo_root)
    calls: list[str] = []
    monkeypatch.setattr(
        wf_commands,
        "_ensure_database_evidence_before_apply",
        lambda root, phase: calls.append(f"ensure:{phase}"),
    )
    monkeypatch.setattr(
        wf_commands,
        "apply_workflow_result",
        lambda root, phase, result_path, **kwargs: (
            calls.append(f"apply:{phase}") or (repo_root / "verification-report.md", False)
        ),
    )
    args = build_parser().parse_args(
        [
            "wf",
            "next",
            "--phase",
            "verify",
            "--provider",
            "fixture",
            "--non-interactive",
            "--yolo",
            "--repo-root",
            str(repo_root),
        ]
    )

    return_code = wf_commands.run_wf_next(args)

    assert return_code == 3
    assert calls == ["ensure:verify", "apply:verify"]


def test_wf_next_auto_apply_continues_when_database_check_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    prepare_workflow_repo(repo_root, result_file=VERIFY_RESULT)
    assert initialize_workflow_fixture(repo_root, "Fixture verify workflow").returncode == 0
    mark_workflow_prerequisites_passed(repo_root)
    calls: list[str] = []
    monkeypatch.setattr(
        wf_commands,
        "evaluate_database_gate",
        lambda root, phase: [{"passed": False, "status": "fail"}],
    )
    monkeypatch.setattr(
        wf_commands,
        "run_database_check",
        lambda root, phase: (
            calls.append(f"db:{phase}")
            or (_ for _ in ()).throw(
                RuntimeError("RAW_COMMAND_OUTPUT DATABASE_URL=postgres://secret@example.test")
            )
        ),
    )
    monkeypatch.setattr(
        wf_commands,
        "apply_workflow_result",
        lambda root, phase, result_path, **kwargs: (
            calls.append(f"apply:{phase}") or (repo_root / "verification-report.md", False)
        ),
    )
    args = build_parser().parse_args(
        [
            "wf",
            "next",
            "--phase",
            "verify",
            "--provider",
            "fixture",
            "--non-interactive",
            "--yolo",
            "--repo-root",
            str(repo_root),
        ]
    )

    return_code = wf_commands.run_wf_next(args)

    captured = capsys.readouterr()
    assert return_code == 3
    assert calls == ["db:verify", "apply:verify"]
    assert "RAW_COMMAND_OUTPUT" not in captured.out
    assert "postgres://secret" not in captured.out
    assert "RAW_COMMAND_OUTPUT" not in captured.err
    assert "postgres://secret" not in captured.err


def _python_json_command(payload: dict[str, object]) -> list[str]:
    return [
        sys.executable,
        "-c",
        f"import json; print(json.dumps({payload!r}, sort_keys=True))",
    ]

def _write_state(repo_root: Path) -> None:
    workflow_dir = repo_root / ".workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "state.json").write_text(
        json.dumps(
            {
                "id": "db-check-test",
                "changeClass": "small",
                "phases": {},
                "gates": {},
                "history": [],
            }
        ),
        encoding="utf-8",
    )


def _write_valid_database_workflow(repo_root: Path) -> None:
    workflow_dir = repo_root / ".workflow"
    artifacts_dir = workflow_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "concept.md").write_text(
        "Update the database query plan",
        encoding="utf-8",
    )
    (artifacts_dir / "allowed-files.json").write_text("{}", encoding="utf-8")
    _write_state(repo_root)

    schema = {
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
    schema_command = [
        sys.executable,
        "-c",
        f"print({json.dumps(schema)!r})",
    ]
    (workflow_dir / "manifest.json").write_text(
        json.dumps(
            {
                "database_validation": {
                    "enabled": True,
                    "schema_command": schema_command,
                    "verify_command": [],
                    "test_command": [],
                    "command_timeout_seconds": 2,
                    "max_schema_age_hours": 24,
                    "allow_production_replica_sample": False,
                }
            }
        ),
        encoding="utf-8",
    )

    baseline = {
        "id": "maintain-current",
        "kind": "maintain",
        "applicable": True,
        "summary": "Keep the current query and schema",
        "equivalence_plan": "Use the current result set as the baseline",
        "integrity_plan": "Verify current constraints",
        "normalization_assessment": "No model change",
        "read_write_cost": "Measure the production-shaped workload",
        "operational_risks": [],
        "transition_risks": [],
        "rollback_or_exit": "No change",
        "unavailable_reason": None,
        "covered_surfaces": ["index", "query"],
        "surface_assessments": {
            "index": "Assess index maintenance and lookup cost.",
            "query": "Assess result equivalence and latency.",
        },
        "denormalization_assessment": None,
        "physical_design_assessment": None,
    }
    query_change = {
        "id": "rewrite-query",
        "kind": "query_change",
        "applicable": True,
        "summary": "Rewrite the slow aggregation query",
        "equivalence_plan": "Compare result sets with the baseline",
        "integrity_plan": "Verify constraints before and after the query",
        "normalization_assessment": "No model change",
        "read_write_cost": "Measure latency on the production-shaped workload",
        "operational_risks": [],
        "transition_risks": [],
        "rollback_or_exit": "Restore the current query",
        "unavailable_reason": None,
        "covered_surfaces": ["index", "query"],
        "surface_assessments": {
            "index": "Assess index maintenance and lookup cost.",
            "query": "Assess result equivalence and latency.",
        },
        "denormalization_assessment": None,
        "physical_design_assessment": None,
    }
    physical_design = {
        "id": "add-query-index",
        "kind": "physical_design",
        "applicable": True,
        "summary": "Add a targeted index for the query path",
        "equivalence_plan": "Compare result sets with the baseline",
        "integrity_plan": "Verify constraints before and after the build",
        "normalization_assessment": "No model change",
        "read_write_cost": "Measure read benefit and write amplification",
        "operational_risks": [],
        "transition_risks": [],
        "rollback_or_exit": "Drop the index online",
        "unavailable_reason": None,
        "covered_surfaces": ["index", "query"],
        "surface_assessments": {
            "index": "Assess index maintenance and lookup cost.",
            "query": "Assess result equivalence and latency.",
        },
        "denormalization_assessment": None,
        "physical_design_assessment": {
            "read_benefit": "The index narrows lookup work.",
            "write_amplification": "Each write updates one index.",
            "storage": "Storage is bounded by projected keys.",
            "build_or_lock": "Build online with a bounded metadata lock.",
            "rollback": "Drop the index online.",
        },
    }
    (artifacts_dir / "database-decision.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "selected",
                "change_surfaces": ["query", "index"],
                "baseline_option_id": "maintain-current",
                "recommended_option_id": "rewrite-query",
                "selected_option_id": "rewrite-query",
                "candidates": [baseline, query_change, physical_design],
                "recommendation_rationale": "It preserves correctness at the lowest cost.",
            }
        ),
        encoding="utf-8",
    )


def _write_database_lifecycle_smoke_fixture(repo_root: Path) -> tuple[Path, Path]:
    """Create a hermetic database workflow that can traverse G1, G5, and G6."""
    _write_valid_database_workflow(repo_root)
    workflow_dir = repo_root / ".workflow"
    artifacts_dir = workflow_dir / "artifacts"
    schema_hash = "a" * 64
    schema = {
        "schema_version": 1,
        "kind": "production_schema",
        "target_class": "production_metadata",
        "read_only": True,
        "schema_only": True,
        "engine": "mysql",
        "engine_version": "8.0",
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "schema_hash": schema_hash,
        "object_counts": {"tables": 1, "columns": 8, "indexes": 2, "constraints": 3},
    }
    verify = {
        "schema_version": 1,
        "kind": "database_verify",
        "production_schema_hash": schema_hash,
        "selected_option_id": "rewrite-query",
        "engine": "mysql",
        "execution_target": "local_same_engine",
        "production_primary_queries": False,
        "raw_production_rows": False,
        "equivalence": "pass",
        "integrity": "pass",
        "query_plan": "pass",
        "migration": "pass",
        "rollback": "pass",
    }
    test = {
        "schema_version": 1,
        "kind": "database_test",
        "production_schema_hash": schema_hash,
        "selected_option_id": "rewrite-query",
        "local_target": "sanitized_snapshot",
        "masked": True,
        "raw_production_rows": False,
        "equivalence": "pass",
        "integrity": "pass",
        "performance": "pass",
    }
    manifest_path = workflow_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    profile = manifest["database_validation"]
    profile["schema_command"] = _python_json_command(schema)
    profile["verify_command"] = _python_json_command(verify)
    profile["test_command"] = _python_json_command(test)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    for filename, content in {
        "spec.md": "# Spec\n\nFR-001: Validate the database query workflow.\n",
        "plan.md": "# Plan\n\nRun the database validation sequence. [FR-001]\n",
        "tasks.md": "- [ ] T001 Run the database lifecycle smoke. [FR-001]\n",
        "test-criteria.md": "# Criteria\n\nThe gate path succeeds. [FR-001]\n",
    }.items():
        (artifacts_dir / filename).write_text(content, encoding="utf-8")

    agent_cards = workflow_dir / "agent-cards"
    agent_cards.mkdir()
    (agent_cards / "verify.json").write_text(
        json.dumps(
            {
                "gate": {
                    "pass_conditions": [
                        "scope.violations == 0",
                        "compliance.fail == 0",
                        "compliance.percentage >= 90",
                        "quality.critical == 0",
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (agent_cards / "test.json").write_text(
        json.dumps(
            {
                "gate": {
                    "pass_conditions": [
                        "suites.failed == 0",
                        "regressions.count == 0",
                        "acceptance.passed == acceptance.total",
                        "coverage.percentage >= 70",
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    verify_result = artifacts_dir / "verify-gate-result.json"
    verify_result.write_text(
        json.dumps(
            {
                "scope": {"violations": 0},
                "compliance": {"fail": 0, "percentage": 100},
                "quality": {"critical": 0},
            }
        ),
        encoding="utf-8",
    )
    test_result = artifacts_dir / "test-gate-result.json"
    test_result.write_text(
        json.dumps(
            {
                "suites": [{"failed": 0}],
                "regressions": [],
                "acceptance": {"passed": 1, "total": 1},
                "coverage": {"percentage": 100},
            }
        ),
        encoding="utf-8",
    )

    small_policy = "policy:change_class=small"
    state_path = workflow_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        {
            "currentPhase": "plan",
            "phases": {
                "plan": {"status": "pending", "retries": 0},
                **{
                    phase: {


                        "status": "skipped",
                        "retries": 0,
                        "skipReason": small_policy,
                    }
                    for phase in ("review", "approve", "verify")
                },
                "impl": {"status": "pending", "retries": 0},
                "test": {"status": "pending", "retries": 0},
                "done": {"status": "pending", "retries": 0},
            },
            "gates": {
                "G1": {"passed": None},
                **{
                    gate: {
                        "passed": True,
                        "auto_pass": True,
                        "provider": "policy",
                        "provider_status": "skipped",
                        "skip_reason": small_policy,
                    }
                    for gate in ("G2", "G3", "G5")
                },
                "G4": {"passed": None},
                "G6": {"passed": None},
            },
            "history": [],
        }
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return verify_result, test_result



def test_db_check_cli_smoke_completes_database_lifecycle_gates(tmp_path: Path) -> None:
    verify_result, test_result = _write_database_lifecycle_smoke_fixture(tmp_path)
    state_path = tmp_path / ".workflow" / "state.json"
    initial_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert all(
        initial_state["phases"][phase]["status"] == "skipped"
        for phase in ("review", "approve", "verify")
    )
    assert all(
        initial_state["gates"][gate]["auto_pass"] is True
        for gate in ("G2", "G3", "G5")
    )

    plan_code, plan_stdout, plan_stderr = capture_main(
        ["wf", "db-check", "--stage", "plan", "--repo-root", str(tmp_path), "--json"]
    )

    assert plan_code == 0
    assert plan_stderr == ""
    plan_check = json.loads(plan_stdout)
    assert plan_check["stage"] == "plan"
    assert plan_check["status"] == "pass"
    assert plan_check["evidence_path"] == _DATABASE_EVIDENCE_PATH
    assert isinstance(plan_check["evidence_hash"], str)
    assert plan_check["blockers"] == []
    assert plan_check["signal_reasons"]

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["changeClass"] == "high_risk"
    assert all(
        state["phases"][phase] == {"status": "pending", "retries": 0}
        for phase in ("review", "approve", "verify")
    )
    assert state["gates"]["G2"] == {
        "passed": None,
        "provider": None,
        "provider_status": None,
    }
    assert state["gates"]["G3"] == {"passed": None, "scope_hash": None}
    assert state["gates"]["G5"] == {
        "passed": None,
        "provider": None,
        "provider_status": None,
    }
    escalation_events = [
        event
        for event in state["history"]
        if event.get("action") == "database_risk_escalated"
    ]
    assert len(escalation_events) == 1
    assert escalation_events[0]["reasons"] == plan_check["signal_reasons"]

    decision = json.loads(
        (tmp_path / ".workflow" / "artifacts" / "database-decision.json").read_text(
            encoding="utf-8"
        )
    )
    assert [candidate["kind"] for candidate in decision["candidates"]] == [
        "maintain",
        "query_change",
        "physical_design",
    ]
    assert decision["selected_option_id"] == "rewrite-query"

    g1_code, g1_stdout, g1_stderr = capture_main(
        ["wf", "gate", "plan", "--repo-root", str(tmp_path)]
    )

    assert g1_code == 0
    assert g1_stderr == ""
    assert "database.risk_class" in g1_stdout
    assert "G-plan: PASS" in g1_stdout

    verify_code, verify_stdout, verify_stderr = capture_main(
        ["wf", "db-check", "--stage", "verify", "--repo-root", str(tmp_path), "--json"]
    )

    assert verify_code == 0
    assert verify_stderr == ""
    verify_check = json.loads(verify_stdout)
    assert verify_check["stage"] == "verify"
    assert verify_check["status"] == "pass"
    assert verify_check["evidence_path"] == _DATABASE_EVIDENCE_PATH
    assert verify_check["blockers"] == []

    g5_code, g5_stdout, g5_stderr = capture_main(
        [
            "wf",
            "gate",
            "verify",
            "--repo-root",
            str(tmp_path),
            "--result-file",
            str(verify_result),
        ]
    )

    assert g5_code == 0
    assert g5_stderr == ""
    assert "database.equivalence" in g5_stdout
    assert "database.query_plan" in g5_stdout
    assert "G-verify: PASS" in g5_stdout

    test_code, test_stdout, test_stderr = capture_main(
        ["wf", "db-check", "--stage", "test", "--repo-root", str(tmp_path), "--json"]
    )

    assert test_code == 0
    assert test_stderr == ""
    test_check = json.loads(test_stdout)
    assert test_check["stage"] == "test"
    assert test_check["status"] == "pass"
    assert test_check["evidence_path"] == _DATABASE_EVIDENCE_PATH
    assert test_check["blockers"] == []

    g6_code, g6_stdout, g6_stderr = capture_main(
        [
            "wf",
            "gate",
            "test",
            "--repo-root",
            str(tmp_path),
            "--result-file",
            str(test_result),
        ]
    )

    assert g6_code == 0
    assert g6_stderr == ""
    assert "database.local_test" in g6_stdout
    assert "G-test: PASS" in g6_stdout

    evidence_path = tmp_path / _DATABASE_EVIDENCE_PATH
    evidence_text = evidence_path.read_text(encoding="utf-8")
    evidence = json.loads(evidence_text)
    assert evidence["schema_version"] == 1
    assert evidence["database_signal"] is True
    assert evidence["change_class"] == "high_risk"
    assert set(evidence["stages"]) == {"plan", "verify", "test"}
    assert evidence["stages"]["plan"]["status"] == "pass"
    assert evidence["stages"]["plan"]["schema"]["schema_hash"] == "a" * 64
    assert evidence["stages"]["verify"]["verify"] == {
        "production_schema_hash": "a" * 64,
        "selected_option_id": "rewrite-query",
        "engine": "mysql",
        "execution_target": "local_same_engine",
        "production_primary_queries": False,
        "raw_production_rows": False,
        "equivalence": "pass",
        "integrity": "pass",
        "query_plan": "pass",
        "migration": "pass",
        "rollback": "pass",
    }
    assert evidence["stages"]["test"]["test"] == {
        "status": "pass",
        "production_schema_hash": "a" * 64,
        "selected_option_id": "rewrite-query",
        "local_target": "sanitized_snapshot",
        "masked": True,
        "raw_production_rows": False,
        "equivalence": "pass",
        "integrity": "pass",
        "performance": "pass",
    }
    assert "secret" not in evidence_text.casefold()


@pytest.mark.parametrize("stage", ["plan", "verify", "test"])
def test_db_check_parser_accepts_stage_root_and_json(stage: str, tmp_path: Path) -> None:
    args = build_parser().parse_args(
        ["wf", "db-check", "--stage", stage, "--repo-root", str(tmp_path), "--json"]
    )

    assert args.stage == stage
    assert args.repo_root == str(tmp_path)
    assert args.json is True
    assert args.handler is wf_commands.run_wf_db_check


def test_db_check_no_signal_returns_not_applicable_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_state(tmp_path)

    return_code = main(["wf", "db-check", "--stage", "plan", "--repo-root", str(tmp_path), "--json"])

    expected = {
        "schema_version": 1,
        "stage": "plan",
        "status": "not_applicable",
        "evidence_path": None,
        "evidence_hash": None,
        "signal_reasons": [],
        "blockers": [],
    }
    assert return_code == 0
    assert capsys.readouterr().out == json.dumps(expected, ensure_ascii=False) + "\n"
    assert json.loads((tmp_path / ".workflow" / "state.json").read_text(encoding="utf-8"))["changeClass"] == "small"


def test_db_check_no_signal_text_output_is_exact(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_state(tmp_path)

    return_code = main(["wf", "db-check", "--stage", "plan", "--repo-root", str(tmp_path)])

    assert return_code == 0
    assert capsys.readouterr().out == "database check: stage=plan status=not_applicable\n"


def test_db_check_plan_promotes_valid_database_workflow_to_high_risk(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_valid_database_workflow(tmp_path)

    return_code = main(["wf", "db-check", "--stage", "plan", "--repo-root", str(tmp_path), "--json"])

    assert return_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "schema_version",
        "stage",
        "status",
        "evidence_path",
        "evidence_hash",
        "signal_reasons",
        "blockers",
    }
    assert payload["stage"] == "plan"
    assert payload["status"] == "pass"
    assert payload["evidence_path"] == ".workflow/artifacts/database-validation-evidence.json"
    assert isinstance(payload["evidence_hash"], str)
    assert payload["blockers"] == []
    assert json.loads((tmp_path / ".workflow" / "state.json").read_text(encoding="utf-8"))["changeClass"] == "high_risk"


_DATABASE_EVIDENCE_PATH = ".workflow/artifacts/database-validation-evidence.json"


def _result_for_status(stage: str, status: str) -> DatabaseCheckResult:
    if status == "pass":
        return DatabaseCheckResult(
            stage=stage,
            status=status,
            evidence_path=_DATABASE_EVIDENCE_PATH,
            evidence_hash="a" * 64,
            signal_reasons=("text:database",),
            blockers=(),
        )
    if status == "not_applicable":
        return DatabaseCheckResult(
            stage=stage,
            status=status,
            evidence_path=None,
            evidence_hash=None,
            signal_reasons=(),
            blockers=(),
        )
    return DatabaseCheckResult(
        stage=stage,
        status=status,
        evidence_path=None,
        evidence_hash=None,
        signal_reasons=("text:database",),
        blockers=("profile_disabled",),
    )


def test_db_check_promotes_plan_signal_before_core_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(tmp_path)
    calls: list[str] = []

    def check_database(
        repo_root: Path,
        stage: str,
        *,
        on_database_signal,
    ) -> DatabaseCheckResult:
        calls.append("signal")
        assert on_database_signal is not None
        on_database_signal(("text:database",))
        state = json.loads((repo_root / ".workflow" / "state.json").read_text(encoding="utf-8"))
        assert state["changeClass"] == "high_risk"
        calls.append("command")
        return _result_for_status(stage, "pass")

    monkeypatch.setattr(wf_commands, "run_database_check", check_database)

    return_code = main(["wf", "db-check", "--stage", "plan", "--repo-root", str(tmp_path), "--json"])

    assert return_code == 0
    assert calls == ["signal", "command"]
    assert json.loads(capsys.readouterr().out)["status"] == "pass"


def test_db_check_promotion_failure_blocks_core_commands_and_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(tmp_path)
    command_ran = False

    def fail_promotion(repo_root: str, reasons: tuple[str, ...]) -> dict:
        raise RuntimeError("DATABASE_URL=postgres://secret@example.test")

    def check_database(
        repo_root: Path,
        stage: str,
        *,
        on_database_signal,
    ) -> DatabaseCheckResult:
        nonlocal command_ran
        assert on_database_signal is not None
        try:
            on_database_signal(("text:database",))
        except Exception:
            return DatabaseCheckResult(
                stage=stage,
                status="fail",
                evidence_path=None,
                evidence_hash=None,
                signal_reasons=("text:database",),
                blockers=("signal_callback_failed",),
            )
        command_ran = True
        return _result_for_status(stage, "pass")

    monkeypatch.setattr(wf_commands, "promote_database_change_to_high_risk", fail_promotion)
    monkeypatch.setattr(wf_commands, "run_database_check", check_database)

    return_code = main(["wf", "db-check", "--stage", "plan", "--repo-root", str(tmp_path), "--json"])

    assert return_code == 2
    assert command_ran is False
    assert not (tmp_path / _DATABASE_EVIDENCE_PATH).exists()
    assert json.loads(capsys.readouterr().out)["blockers"] == ["operational_error"]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="requires symlink support")
def test_db_check_uses_one_canonical_root_when_symlink_retargets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    _write_state(root_a)
    _write_state(root_b)
    selected_root = tmp_path / "selected-root"
    selected_root.symlink_to(root_a, target_is_directory=True)
    calls: list[Path] = []

    def promote(repo_root: str, reasons: tuple[str, ...]) -> dict:
        calls.append(Path(repo_root))
        return {}

    def check_database(
        repo_root: Path,
        stage: str,
        *,
        on_database_signal,
    ) -> DatabaseCheckResult:
        calls.append(repo_root)
        selected_root.unlink()
        selected_root.symlink_to(root_b, target_is_directory=True)
        assert on_database_signal is not None
        on_database_signal(("text:database",))
        return _result_for_status(stage, "pass")

    monkeypatch.setattr(wf_commands, "promote_database_change_to_high_risk", promote)
    monkeypatch.setattr(wf_commands, "run_database_check", check_database)

    return_code = main(["wf", "db-check", "--stage", "plan", "--repo-root", str(selected_root), "--json"])

    assert return_code == 0
    assert calls == [root_a.resolve(), root_a.resolve()]
    assert json.loads(capsys.readouterr().out)["status"] == "pass"


@pytest.mark.parametrize(
    "result_factory",
    [
        lambda: object(),
        lambda: DatabaseCheckResult(
            stage="verify",
            status="pass",
            evidence_path="RAW_COMMAND_OUTPUT DATABASE_URL=postgres://secret@example.test",
            evidence_hash="a" * 64,
            signal_reasons=("DATABASE_URL=postgres://secret@example.test",),
            blockers=(),
        ),
        lambda: DatabaseCheckResult(
            stage="verify",
            status="fail",
            evidence_path=None,
            evidence_hash=None,
            signal_reasons=("text:supersecretapikey",),
            blockers=("profile_disabled",),
        ),
    ],
    ids=["wrong_type", "secret_fields", "semantic_secret"],
)
def test_db_check_rejects_malformed_core_results_without_leaking_them(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    result_factory,
) -> None:
    _write_state(tmp_path)
    monkeypatch.setattr(
        wf_commands,
        "run_database_check",
        lambda repo_root, stage, **kwargs: result_factory(),
    )

    return_code = main(["wf", "db-check", "--stage", "verify", "--repo-root", str(tmp_path), "--json"])

    captured = capsys.readouterr()
    expected = {
        "schema_version": 1,
        "stage": "verify",
        "status": "fail",
        "evidence_path": None,
        "evidence_hash": None,
        "signal_reasons": [],
        "blockers": ["operational_error"],
    }
    assert return_code == 2
    assert captured.out == json.dumps(expected, ensure_ascii=False) + "\n"
    assert "RAW_COMMAND_OUTPUT" not in captured.out
    assert "postgres://secret" not in captured.out
    assert captured.err == ""

def test_db_check_text_output_rejects_semantic_secret_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(tmp_path)
    monkeypatch.setattr(
        wf_commands,
        "run_database_check",
        lambda repo_root, stage, **kwargs: DatabaseCheckResult(
            stage=stage,
            status="fail",
            evidence_path=None,
            evidence_hash=None,
            signal_reasons=("text:supersecretapikey",),
            blockers=("profile_disabled",),
        ),
    )

    return_code = main(["wf", "db-check", "--stage", "verify", "--repo-root", str(tmp_path)])

    captured = capsys.readouterr()
    assert return_code == 2
    assert captured.out == "database check: stage=verify status=fail\nblockers: operational_error\n"
    assert "supersecretapikey" not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("as_json", [True, False])
def test_db_check_redacts_secret_unicode_and_long_database_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    as_json: bool,
) -> None:
    _write_valid_database_workflow(tmp_path)
    raw_path = f"src/database/migrations/비밀-supersecretapikey-{'x' * 1024}.sql"
    (tmp_path / ".workflow" / "artifacts" / "allowed-files.json").write_text(
        json.dumps({"planned_files": [raw_path]}, ensure_ascii=False),
        encoding="utf-8",
    )
    argv = ["wf", "db-check", "--stage", "plan", "--repo-root", str(tmp_path)]
    if as_json:
        argv.append("--json")

    return_code = main(argv)

    captured = capsys.readouterr()
    state_text = (tmp_path / ".workflow" / "state.json").read_text(encoding="utf-8")
    assert return_code == 0
    assert "supersecretapikey" not in captured.out
    assert "비밀" not in captured.out
    assert "supersecretapikey" not in state_text
    assert "비밀" not in state_text
    if as_json:
        reasons = json.loads(captured.out)["signal_reasons"]
        assert any(reason.startswith("path:migration:") for reason in reasons)
    else:
        assert "signal_reasons: path:migration:" in captured.out


@pytest.mark.parametrize("stage", ["verify", "test"])
@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [("pass", 0), ("not_applicable", 0), ("fail", 1)],
)
def test_db_check_verify_and_test_exit_matrix(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    status: str,
    expected_exit: int,
) -> None:
    _write_state(tmp_path)
    monkeypatch.setattr(
        wf_commands,
        "run_database_check",
        lambda repo_root, requested_stage, **kwargs: _result_for_status(requested_stage, status),
    )

    return_code = main(["wf", "db-check", "--stage", stage, "--repo-root", str(tmp_path), "--json"])

    assert return_code == expected_exit
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_db_check_invalid_profile_returns_stable_blocker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow_dir = tmp_path / ".workflow"
    workflow_dir.mkdir()
    _write_state(tmp_path)
    (workflow_dir / "concept.md").write_text("Update the database query", encoding="utf-8")
    (workflow_dir / "manifest.json").write_text(
        json.dumps(
            {
                "database_validation": {
                    "enabled": False,
                    "schema_command": [],
                    "verify_command": [],
                    "test_command": [],
                    "command_timeout_seconds": 2,
                    "max_schema_age_hours": 24,
                    "allow_production_replica_sample": False,
                }
            }
        ),
        encoding="utf-8",
    )

    return_code = main(["wf", "db-check", "--stage", "plan", "--repo-root", str(tmp_path), "--json"])

    assert return_code == 1
    assert json.loads(capsys.readouterr().out)["blockers"] == ["profile_disabled"]
    assert json.loads((tmp_path / ".workflow" / "state.json").read_text(encoding="utf-8"))["changeClass"] == "high_risk"


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_db_check_invalid_root_is_an_operational_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    root_kind: str,
) -> None:
    repo_root = tmp_path / "invalid-root"
    if root_kind == "file":
        repo_root.write_text("not a directory", encoding="utf-8")

    return_code = main(["wf", "db-check", "--stage", "plan", "--repo-root", str(repo_root), "--json"])

    assert return_code == 2
    assert json.loads(capsys.readouterr().out)["blockers"] == ["repo_root_invalid"]


def test_db_check_malformed_stage_uses_argparse_exit_code_two() -> None:
    with pytest.raises(SystemExit) as raised:
        main(["wf", "db-check", "--stage", "invalid"])

    assert raised.value.code == 2


def test_db_check_operational_exception_never_emits_exception_contents(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(tmp_path)

    def raise_with_secret(repo_root: Path, stage: str, **kwargs):
        raise RuntimeError("RAW_COMMAND_OUTPUT DATABASE_URL=postgres://secret@example.test")

    monkeypatch.setattr(wf_commands, "run_database_check", raise_with_secret)

    return_code = main(["wf", "db-check", "--stage", "verify", "--repo-root", str(tmp_path), "--json"])

    captured = capsys.readouterr()
    assert return_code == 2
    assert json.loads(captured.out) == {
        "schema_version": 1,
        "stage": "verify",
        "status": "fail",
        "evidence_path": None,
        "evidence_hash": None,
        "signal_reasons": [],
        "blockers": ["operational_error"],
    }
    assert "RAW_COMMAND_OUTPUT" not in captured.out
    assert "postgres://secret" not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("blocker", ["decision_missing", "database_signal_changed"])
def test_db_check_stable_database_validation_blockers_exit_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    blocker: str,
) -> None:
    _write_valid_database_workflow(tmp_path)
    if blocker == "decision_missing":
        (tmp_path / ".workflow" / "artifacts" / "database-decision.json").unlink()
        stage = "plan"
    else:
        assert main(
            ["wf", "db-check", "--stage", "plan", "--repo-root", str(tmp_path), "--json"]
        ) == 0
        capsys.readouterr()
        (tmp_path / ".workflow" / "concept.md").write_text(
            "Update the database query plan with a new index",
            encoding="utf-8",
        )
        stage = "verify"

    return_code = main(
        ["wf", "db-check", "--stage", stage, "--repo-root", str(tmp_path), "--json"]
    )

    assert return_code == 1
    assert json.loads(capsys.readouterr().out)["blockers"] == [blocker]
