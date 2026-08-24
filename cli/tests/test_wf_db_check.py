"""Focused CLI contract tests for ``awf wf db-check``."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import awf.commands.wf as wf_commands
from awf.cli import build_parser, main
from awf.core.db_validation import DatabaseCheckResult


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
