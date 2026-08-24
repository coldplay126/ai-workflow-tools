"""Focused CLI contract tests for ``awf wf db-check``."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import awf.commands.wf as wf_commands
from awf.cli import build_parser, main


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
        "denormalization_assessment": None,
        "physical_design_assessment": None,
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
                "candidates": [baseline, query_change],
                "recommendation_rationale": "It preserves correctness at the lowest cost.",
            }
        ),
        encoding="utf-8",
    )


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
    return_code = main(["wf", "db-check", "--stage", "plan", "--repo-root", str(tmp_path), "--json"])

    assert return_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema_version": 1,
        "stage": "plan",
        "status": "not_applicable",
        "evidence_path": None,
        "evidence_hash": None,
        "signal_reasons": [],
        "blockers": [],
    }


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


def test_db_check_invalid_profile_returns_stable_blocker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow_dir = tmp_path / ".workflow"
    workflow_dir.mkdir()
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
    def raise_with_secret(repo_root: Path, stage: str):
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
