"""Focused database workflow signal tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import errno
import os
import sys
from pathlib import Path
import shutil
import time
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import awf.core.db_validation as db_validation

from awf.core.db_validation import (
    DatabaseDecision,
    DatabaseSignal,
    DatabaseValidationError,
    detect_database_signal,
    evaluate_database_gate,
    load_database_decision,
    load_database_profile,
    run_database_check,
)


def write_workflow_artifacts(
    repo_root: Path,
    *,
    concept: str = "Improve fan log ordering",
    spec: str = "",
    plan: str = "",
    tasks: str = "",
    test_criteria: str = "",
    allowed_files: list[str] | None = None,
    legacy_files: list[str] | None = None,
    expanded_files: list[str] | None = None,
) -> None:
    workflow_dir = repo_root / ".workflow"
    artifacts_dir = workflow_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    (workflow_dir / "concept.md").write_text(concept, encoding="utf-8")
    for name, content in {
        "spec.md": spec,
        "plan.md": plan,
        "tasks.md": tasks,
        "test-criteria.md": test_criteria,
    }.items():
        (artifacts_dir / name).write_text(content, encoding="utf-8")
    allowed_payload = {}
    if allowed_files is not None:
        allowed_payload["planned_files"] = allowed_files
    if legacy_files is not None:
        allowed_payload["files"] = legacy_files
    if expanded_files is not None:
        allowed_payload["expanded_files"] = expanded_files
    (artifacts_dir / "allowed-files.json").write_text(
        json.dumps(allowed_payload),
        encoding="utf-8",
    )

_ARTIFACT_CATEGORY = {
    ".workflow": "workflow",
    ".workflow/concept.md": "concept",
    ".workflow/artifacts/spec.md": "spec",
    ".workflow/artifacts/plan.md": "plan",
    ".workflow/artifacts/tasks.md": "tasks",
    ".workflow/artifacts/test-criteria.md": "test_criteria",
    ".workflow/artifacts/allowed-files.json": "allowed_files",
}


def _redacted_path_reason(path: str, category: str) -> str:
    return f"path:{category}:{hashlib.sha256(path.encode('utf-8')).hexdigest()[:16]}"


def _artifact_error_reason(relative_path: str, error: str) -> str:
    return f"artifact_error:{_ARTIFACT_CATEGORY[relative_path]}:{error}"


def _assert_redacted_path_reason(reasons: tuple[str, ...], path: str, category: str) -> None:
    assert _redacted_path_reason(path, category) in reasons
    assert path not in reasons



def _assert_database_signal(
    signal: DatabaseSignal,
    *,
    detected: bool,
    reasons: tuple[str, ...],
) -> None:
    assert signal.detected is detected
    assert signal.reasons == reasons
    assert len(signal.snapshot_hash) == 64
    assert all(character in "0123456789abcdef" for character in signal.snapshot_hash)


def test_database_signal_detects_concept_database_term(tmp_path: Path) -> None:
    write_workflow_artifacts(tmp_path, concept="Update the database retention policy")

    signal = detect_database_signal(tmp_path)

    _assert_database_signal(
        signal,
        detected=True,
        reasons=("text:database",),
    )



@pytest.mark.parametrize(
    ("concept", "reason"),
    [
        ("CREATE TABLE audit_log (id bigint)", "text:table_ddl"),
        ("ALTER TABLE audit_log ADD COLUMN actor_id bigint", "text:table_ddl"),
        ("DROP INDEX audit_log_actor_idx", "text:index_ddl"),
        ("Migrate the Snowflake warehouse schema", "text:database engine"),
    ],
)
def test_database_signal_detects_strong_ddl_and_warehouse_terms(
    tmp_path: Path,
    concept: str,
    reason: str,
) -> None:
    write_workflow_artifacts(tmp_path, concept=concept)

    assert reason in detect_database_signal(tmp_path).reasons


def test_database_signal_snapshot_hash_is_stable_and_tracks_artifact_content(
    tmp_path: Path,
) -> None:
    write_workflow_artifacts(
        tmp_path,
        concept="Update the database retention policy",
        spec="Initial planning context",
    )

    initial = detect_database_signal(tmp_path)
    repeated = detect_database_signal(tmp_path)
    (tmp_path / ".workflow" / "artifacts" / "spec.md").write_text(
        "Revised planning context",
        encoding="utf-8",
    )
    revised = detect_database_signal(tmp_path)

    assert repeated.snapshot_hash == initial.snapshot_hash
    assert revised.reasons == initial.reasons
    assert revised.snapshot_hash != initial.snapshot_hash

def test_database_signal_normalizes_korean_denormalization(tmp_path: Path) -> None:
    write_workflow_artifacts(tmp_path, tasks="- [ ] 비정규화 모델 검토")

    signal = detect_database_signal(tmp_path)

    _assert_database_signal(
        signal,
        detected=True,
        reasons=("text:denormalization",),
    )


def test_database_signal_ignores_standalone_db_token(tmp_path: Path) -> None:
    write_workflow_artifacts(tmp_path, concept="Reduce by 3 dB")

    assert detect_database_signal(tmp_path).detected is False


def test_database_signal_detects_db_with_relational_context(tmp_path: Path) -> None:
    write_workflow_artifacts(tmp_path, concept="Refresh DB schema")

    assert "text:database" in detect_database_signal(tmp_path).reasons


def test_database_signal_uses_expanded_files_with_canonical_planned_files(
    tmp_path: Path,
) -> None:
    write_workflow_artifacts(
        tmp_path,
        allowed_files=["src/index.ts"],
        legacy_files=["src/models/fan_log.py"],
        expanded_files=[
            "src/database/migrations/001_add_index.sql",
            "src/database/migrations/001_add_index.sql",
        ],
    )

    path = "src/database/migrations/001_add_index.sql"
    signal = detect_database_signal(tmp_path)
    assert signal.detected is True
    _assert_redacted_path_reason(signal.reasons, path, "migration")

def test_database_signal_bounds_many_database_paths_with_a_safe_marker(tmp_path: Path) -> None:
    paths = [f"src/database/migrations/{number:03d}.sql" for number in range(65)]
    write_workflow_artifacts(tmp_path, allowed_files=paths)

    signal = detect_database_signal(tmp_path)

    assert signal.detected is True
    assert len(signal.reasons) == 64
    assert "path:truncated:2" in signal.reasons
    assert all(path not in signal.reasons for path in paths)


def test_database_signal_ignores_stale_legacy_files_when_planned_exist(
    tmp_path: Path,
) -> None:
    write_workflow_artifacts(
        tmp_path,
        allowed_files=["src/index.ts"],
        legacy_files=["src/database/stale_schema.sql"],
    )

    assert detect_database_signal(tmp_path).detected is False


def test_database_signal_falls_back_to_legacy_files_when_planned_absent(
    tmp_path: Path,
) -> None:
    write_workflow_artifacts(
        tmp_path,
        legacy_files=["src/database/migrations/001_add_index.sql"],
    )

    path = "src/database/migrations/001_add_index.sql"
    _assert_redacted_path_reason(
        detect_database_signal(tmp_path).reasons,
        path,
        "migration",
    )


def test_database_signal_detects_query_and_migration_path(tmp_path: Path) -> None:
    write_workflow_artifacts(
        tmp_path,
        tasks="- [ ] T001 Update ORDER BY [FR-001]",
        allowed_files=["src/database/migrations/add_fan_log_index.sql"],
    )

    signal = detect_database_signal(tmp_path)

    assert signal.detected is True
    assert "text:order by" in signal.reasons
    _assert_redacted_path_reason(
        signal.reasons,
        "src/database/migrations/add_fan_log_index.sql",
        "migration",
    )


def test_database_signal_does_not_scan_unlisted_repository_files(tmp_path: Path) -> None:
    write_workflow_artifacts(tmp_path)
    schema_path = tmp_path / "src" / "database" / "schema.sql"
    schema_path.parent.mkdir(parents=True)
    schema_path.write_text("SELECT * FROM users", encoding="utf-8")

    assert detect_database_signal(tmp_path).detected is False


def test_database_signal_ignores_frontend_index_file(tmp_path: Path) -> None:
    write_workflow_artifacts(tmp_path, allowed_files=["src/index.ts"])

    assert detect_database_signal(tmp_path).detected is False


def test_database_signal_weak_terms_require_database_context(tmp_path: Path) -> None:
    write_workflow_artifacts(
        tmp_path,
        spec="\n".join(
            [
                "- Update src/index.ts",
                "- Publish an OpenAPI and JSON schema",
                "- Preserve the URL query parameter",
                "- Render an HTML table",
                "- Train the ML model",
            ]
        ),
    )

    assert detect_database_signal(tmp_path).detected is False


def test_database_signal_detects_weak_terms_with_database_context(tmp_path: Path) -> None:
    write_workflow_artifacts(
        tmp_path,
        tasks="- [ ] Update the database schema and query [FR-001]",
    )

    signal = detect_database_signal(tmp_path)

    assert {"text:database", "text:schema", "text:query"} <= set(signal.reasons)


@pytest.mark.parametrize(
    "relative_path",
    [
        ".workflow/concept.md",
        ".workflow/artifacts/spec.md",
        ".workflow/artifacts/plan.md",
        ".workflow/artifacts/tasks.md",
        ".workflow/artifacts/test-criteria.md",
        ".workflow/artifacts/allowed-files.json",
    ],
)
def test_database_signal_reports_oversized_known_artifacts(
    tmp_path: Path,
    relative_path: str,
) -> None:
    write_workflow_artifacts(tmp_path)
    (tmp_path / relative_path).write_bytes(b"x" * (128 * 1024 + 1))

    _assert_database_signal(
        detect_database_signal(tmp_path),
        detected=True,
        reasons=(_artifact_error_reason(relative_path, "oversize"),),
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        ".workflow/concept.md",
        ".workflow/artifacts/spec.md",
        ".workflow/artifacts/plan.md",
        ".workflow/artifacts/tasks.md",
        ".workflow/artifacts/test-criteria.md",
        ".workflow/artifacts/allowed-files.json",
    ],
)
def test_database_signal_reports_invalid_utf8_known_artifacts(
    tmp_path: Path,
    relative_path: str,
) -> None:
    write_workflow_artifacts(tmp_path)
    (tmp_path / relative_path).write_bytes(b"\xff")

    _assert_database_signal(
        detect_database_signal(tmp_path),
        detected=True,
        reasons=(_artifact_error_reason(relative_path, "invalid_utf8"),),
    )


@pytest.mark.parametrize(
    ("allowed_files", "error"),
    [
        ("{", "invalid_json"),
        (
            '{"planned_files": [], "planned_files": ["src/database/schema.sql"]}',
            "invalid_json",
        ),
        ("[]", "invalid_shape"),
        ('{"planned_files": "src/database/schema.sql"}', "invalid_shape"),
        ('{"expanded_files": {"path": "src/database/schema.sql"}}', "invalid_shape"),
    ],
)
def test_database_signal_reports_invalid_allowed_files_artifact(
    tmp_path: Path,
    allowed_files: str,
    error: str,
) -> None:
    write_workflow_artifacts(tmp_path)
    allowed_path = tmp_path / ".workflow" / "artifacts" / "allowed-files.json"
    allowed_path.write_text(allowed_files, encoding="utf-8")

    _assert_database_signal(
        detect_database_signal(tmp_path),
        detected=True,
        reasons=(
            _artifact_error_reason(".workflow/artifacts/allowed-files.json", error),
        ),
    )


def test_database_signal_canonicalizes_windows_and_dot_paths(tmp_path: Path) -> None:
    write_workflow_artifacts(
        tmp_path,
        allowed_files=[r".\src\.\database\migrations\001_add_index.sql"],
    )

    _assert_redacted_path_reason(
        detect_database_signal(tmp_path).reasons,
        "src/database/migrations/001_add_index.sql",
        "migration",
    )


def test_database_signal_preserves_hidden_directories_and_collapses_parent_paths(
    tmp_path: Path,
) -> None:
    write_workflow_artifacts(
        tmp_path,
        allowed_files=[".models/inference.py", "database/../ui/component.ts"],
    )

    assert detect_database_signal(tmp_path).detected is False


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "/var/lib/schema.sql",
        r"C:\database\schema.sql",
        "../../database/schema.sql",
    ],
)
def test_database_signal_rejects_unsafe_allowed_paths(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    write_workflow_artifacts(tmp_path, allowed_files=[unsafe_path])

    _assert_database_signal(
        detect_database_signal(tmp_path),
        detected=True,
        reasons=(
            _artifact_error_reason(
                ".workflow/artifacts/allowed-files.json",
                "unsafe_path",
            ),
        ),
    )


def database_command(payload: object, *, exit_code: int = 0) -> list[str]:
    script = (
        "import json, sys; "
        f"print(json.dumps({payload!r})); "
        f"raise SystemExit({exit_code})"
    )
    return [sys.executable, "-c", script]


def write_database_manifest(
    repo_root: Path,
    *,
    schema_command: object,
    verify_command: object = (),
    test_command: object = (),
    enabled: bool = True,
    timeout_seconds: int = 2,
    max_schema_age_hours: int = 24,
    allow_production_replica_sample: bool = False,
    extra_profile: dict[str, object] | None = None,
) -> None:
    profile = {
        "enabled": enabled,
        "schema_command": schema_command,
        "verify_command": verify_command,
        "test_command": test_command,
        "command_timeout_seconds": timeout_seconds,
        "max_schema_age_hours": max_schema_age_hours,
        "allow_production_replica_sample": allow_production_replica_sample,
    }
    if extra_profile:
        profile.update(extra_profile)
    manifest_path = repo_root / ".workflow" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"database_validation": profile}),
        encoding="utf-8",
    )


def schema_evidence(
    *,
    schema_hash: str = "a" * 64,
    captured_at: datetime | None = None,
    **updates: object,
) -> dict[str, object]:
    captured = captured_at or datetime.now(timezone.utc)
    payload: dict[str, object] = {
        "schema_version": 1,
        "kind": "production_schema",
        "target_class": "production_metadata",
        "read_only": True,
        "schema_only": True,
        "engine": "mysql",
        "engine_version": "8.0",
        "captured_at": captured.isoformat().replace("+00:00", "Z"),
        "schema_hash": schema_hash,
        "object_counts": {
            "tables": 1,
            "columns": 8,
            "indexes": 2,
            "constraints": 3,
        },
    }
    payload.update(updates)
    return payload


def database_decision(
    *,
    change_surfaces: list[str] | None = None,
    candidates: list[dict[str, object]] | None = None,
    selected_option_id: str = "rewrite-query",
    **updates: object,
) -> dict[str, object]:
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
    rewrite = {
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
    physical = {
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
        "denormalization_assessment": None,
        "physical_design_assessment": {
            "read_benefit": "The index narrows lookup work.",
            "write_amplification": "Each write updates one index.",
            "storage": "Storage is bounded by projected keys.",
            "build_or_lock": "Build online with a bounded metadata lock.",
            "rollback": "Drop the index online.",
        },
    }
    payload: dict[str, object] = {
        "schema_version": 1,
        "status": "selected",
        "change_surfaces": change_surfaces or ["query", "index"],
        "baseline_option_id": "maintain-current",
        "recommended_option_id": "rewrite-query",
        "selected_option_id": selected_option_id,
        "candidates": candidates or [baseline, rewrite, physical],
        "recommendation_rationale": "It preserves correctness at the lowest cost.",
    }
    payload.update(updates)
    return payload


def write_database_decision(repo_root: Path, payload: dict[str, object]) -> None:
    decision_path = repo_root / ".workflow" / "artifacts" / "database-decision.json"
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    decision_path.write_text(json.dumps(payload), encoding="utf-8")


def verify_evidence(
    *,
    schema_hash: str = "a" * 64,
    selected_option_id: str = "rewrite-query",
    **updates: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "kind": "database_verify",
        "production_schema_hash": schema_hash,
        "selected_option_id": selected_option_id,
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
    payload.update(updates)
    return payload


def database_test_payload(
    *,
    schema_hash: str = "a" * 64,
    selected_option_id: str = "rewrite-query",
    **updates: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "kind": "database_test",
        "production_schema_hash": schema_hash,
        "selected_option_id": selected_option_id,
        "local_target": "both",
        "masked": True,
        "raw_production_rows": False,
        "equivalence": "pass",
        "integrity": "pass",
        "performance": "pass",
    }
    payload.update(updates)
    return payload


def prepare_database_workflow(
    repo_root: Path,
    *,
    schema_command: object | None = None,
    verify_command: object = (),
    test_command: object = (),
    decision: dict[str, object] | None = None,
) -> None:
    write_workflow_artifacts(repo_root, concept="Update the database query plan")
    write_database_manifest(
        repo_root,
        schema_command=schema_command or database_command(schema_evidence()),
        verify_command=verify_command,
        test_command=test_command,
    )
    write_database_decision(repo_root, decision or database_decision())


def evidence_path(repo_root: Path) -> Path:
    return repo_root / ".workflow" / "artifacts" / "database-validation-evidence.json"


def test_profile_rejects_disabled_incomplete_unknown_and_shell_string_commands(
    tmp_path: Path,
) -> None:
    write_workflow_artifacts(tmp_path, concept="Update the database query")
    for profile_updates in (
        {"enabled": False},
        {"schema_command": "python schema.py"},
        {
            "schema_command": [
                "database-inspector",
                "--dsn=postgresql://user:password@db.internal/service",
            ]
        },
        {"extra_profile": {"password": "never-store-me"}},
    ):
        write_database_manifest(
            tmp_path,
            schema_command=profile_updates.get(
                "schema_command",
                database_command(schema_evidence()),
            ),
            enabled=profile_updates.get("enabled", True),
            extra_profile=profile_updates.get("extra_profile"),
        )

        with pytest.raises(DatabaseValidationError):
            load_database_profile(tmp_path)


@pytest.mark.parametrize(
    "secret_option",
    [
        "--password",
        "--token",
        "--dsn",
        "--secret",
        "--credential",
        "--password=from-env",
        "--token=from-env",
        "--dsn=from-env",
        "--secret=from-env",
        "--credential=from-env",
    ],
)
def test_profile_rejects_split_and_equals_secret_options(
    tmp_path: Path,
    secret_option: str,
) -> None:
    write_workflow_artifacts(tmp_path, concept="Update the database query")
    write_database_manifest(
        tmp_path,
        schema_command=["database-inspector", secret_option, "$DATABASE_SECRET"],
    )

    with pytest.raises(DatabaseValidationError):
        load_database_profile(tmp_path)


@pytest.mark.parametrize(
    "argument",
    [
        "inspect postgresql://user:password@db.internal/service",
        "DATABASE_PASSWORD=$DATABASE_PASSWORD",
        "DATABASE_TOKEN=literal-token",
        "--connection=postgresql://user:password@db.internal/service",
        "-p",
        "-psecret",
        "-t",
        "-ttoken",
        "inspect --credential=$DATABASE_CREDENTIAL",
    ],
)
def test_profile_rejects_sensitive_values_anywhere_in_argv(
    tmp_path: Path,
    argument: str,
) -> None:
    write_workflow_artifacts(tmp_path, concept="Update the database query")
    write_database_manifest(
        tmp_path,
        schema_command=["database-inspector", argument],
    )

    with pytest.raises(DatabaseValidationError):
        load_database_profile(tmp_path)


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh"])
def test_profile_rejects_shell_interpreter_commands(tmp_path: Path, shell: str) -> None:
    write_workflow_artifacts(tmp_path, concept="Update the database query")
    write_database_manifest(
        tmp_path,
        schema_command=[shell, "-c", "echo $DATABASE_SECRET"],
    )

    with pytest.raises(DatabaseValidationError):
        load_database_profile(tmp_path)


@pytest.mark.parametrize(
    "missing_field",
    [
        "enabled",
        "schema_command",
        "verify_command",
        "test_command",
        "command_timeout_seconds",
        "max_schema_age_hours",
        "allow_production_replica_sample",
    ],
)
def test_profile_rejects_each_missing_required_field(
    tmp_path: Path,
    missing_field: str,
) -> None:
    write_workflow_artifacts(tmp_path, concept="Update the database query")
    write_database_manifest(tmp_path, schema_command=database_command(schema_evidence()))
    manifest_path = tmp_path / ".workflow" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["database_validation"][missing_field]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DatabaseValidationError):
        load_database_profile(tmp_path)


def test_profile_returns_canonical_hash_for_a_safe_complete_profile(tmp_path: Path) -> None:
    write_workflow_artifacts(tmp_path, concept="Update the database query")
    write_database_manifest(
        tmp_path,
        schema_command=database_command(schema_evidence()),
    )

    profile = load_database_profile(tmp_path)

    assert profile.enabled is True
    assert profile.schema_command[0] == sys.executable
    assert len(profile.profile_hash) == 64


def test_decision_requires_candidates_baseline_references_and_selected_plans(
    tmp_path: Path,
) -> None:
    write_workflow_artifacts(tmp_path, concept="Update the database query")
    invalid_decisions = [
        database_decision(candidates=[database_decision()["candidates"][0]]),
        database_decision(baseline_option_id="not-a-candidate"),
        database_decision(selected_option_id="not-a-candidate"),
        database_decision(
            candidates=[
                database_decision()["candidates"][0],
                {
                    **database_decision()["candidates"][1],
                    "equivalence_plan": "",
                },
            ]
        ),
    ]
    for payload in invalid_decisions:
        write_database_decision(tmp_path, payload)

        with pytest.raises(DatabaseValidationError):
            load_database_decision(tmp_path)


@pytest.mark.parametrize(
    "decision_updates",
    [
        {
            "candidates": database_decision()["candidates"]
            + [
                {
                    **database_decision()["candidates"][1],
                    "id": "alternate-query-one",
                },
                {
                    **database_decision()["candidates"][1],
                    "id": "alternate-query-two",
                },
            ]
        },
        {"recommended_option_id": "broken-id"},
        {
            "candidates": [
                database_decision()["candidates"][0],
                {
                    **database_decision()["candidates"][1],
                    "integrity_plan": "",
                },
            ]
        },
        {
            "candidates": [
                database_decision()["candidates"][0],
                {
                    **database_decision()["candidates"][1],
                    "applicable": False,
                    "unavailable_reason": None,
                },
            ],
            "recommended_option_id": "maintain-current",
            "selected_option_id": "maintain-current",
        },
        {
            "candidates": [
                database_decision()["candidates"][0],
                {
                    **database_decision()["candidates"][1],
                    "unavailable_reason": "This option is available.",
                },
            ]
        },
    ],
    ids=[
        "four-candidates",
        "broken-recommendation-id",
        "missing-integrity-plan",
        "missing-unavailable-reason",
        "available-candidate-has-unavailable-reason",
    ],
)
def test_decision_rejects_required_candidate_mutations(
    tmp_path: Path,
    decision_updates: dict[str, object],
) -> None:
    write_workflow_artifacts(tmp_path, concept="Update the database query")
    write_database_decision(tmp_path, database_decision(**decision_updates))

    with pytest.raises(DatabaseValidationError):
        load_database_decision(tmp_path)


def test_decision_allows_materially_distinct_same_kind_candidates(tmp_path: Path) -> None:
    write_workflow_artifacts(tmp_path, concept="Update the database schema")
    baseline = database_decision()["candidates"][0]
    alternative_maintain = {
        **baseline,
        "id": "maintain-copy",
        "equivalence_plan": "Compare a restored backup against the current ledger.",
        "operational_risks": ["The backup restore extends the maintenance window."],
    }
    write_database_decision(
        tmp_path,
        database_decision(
            change_surfaces=["column"],
            candidates=[baseline, alternative_maintain],
            recommended_option_id="maintain-copy",
            selected_option_id="maintain-copy",
        ),
    )

    assert load_database_decision(tmp_path).selected_option_id == "maintain-copy"


def test_decision_rejects_candidate_that_only_changes_its_id(tmp_path: Path) -> None:
    write_workflow_artifacts(tmp_path, concept="Update the database query")
    baseline = database_decision()["candidates"][0]
    id_only_copy = {**baseline, "id": "maintain-copy"}
    write_database_decision(
        tmp_path,
        database_decision(
            candidates=[baseline, id_only_copy],
            recommended_option_id="maintain-copy",
            selected_option_id="maintain-copy",
        ),
    )

    with pytest.raises(DatabaseValidationError):
        load_database_decision(tmp_path)


def test_decision_rejects_summary_format_and_risk_order_only_clone(
    tmp_path: Path,
) -> None:
    write_workflow_artifacts(tmp_path, concept="Update the database query")
    baseline = {
        **database_decision()["candidates"][0],
        "summary": "Keep the baseline current.",
        "equivalence_plan": "Compare the current result-set.",
        "operational_risks": ["Write amplification.", "Lock wait"],
    }
    formatted_copy = {
        **baseline,
        "id": "maintain-copy",
        "summary": "A different label.",
        "equivalence_plan": "  compare the CURRENT result set!!! ",
        "operational_risks": [" lock wait ", "write-amplification"],
    }
    write_database_decision(
        tmp_path,
        database_decision(
            candidates=[baseline, formatted_copy],
            recommended_option_id="maintain-copy",
            selected_option_id="maintain-copy",
        ),
    )

    with pytest.raises(DatabaseValidationError):
        load_database_decision(tmp_path)


@pytest.mark.parametrize(
    ("kind", "assessment_field", "assessment", "change_surfaces"),
    [
        (
            "denormalize",
            "denormalization_assessment",
            {
                "source_of_truth": "The ledger is authoritative.",
                "consistency_window": "Updates converge within one minute.",
                "reconciliation": "A scheduled job compares source and summary.",
                "rollback": "Stop summary writes and rebuild from the ledger.",
            },
            ["column"],
        ),
        (
            "physical_design",
            "physical_design_assessment",
            {
                "read_benefit": "The index narrows lookup work.",
                "write_amplification": "Each write updates one index.",
                "storage": "Storage is bounded by projected keys.",
                "build_or_lock": "Build online with a bounded metadata lock.",
                "rollback": "Drop the index online.",
            },
            ["index"],
        ),
    ],
)
def test_decision_accepts_exact_structured_kind_assessments(
    tmp_path: Path,
    kind: str,
    assessment_field: str,
    assessment: dict[str, str],
    change_surfaces: list[str],
) -> None:
    write_workflow_artifacts(tmp_path, concept="Update the database schema")
    baseline, alternative = database_decision()["candidates"][:2]
    candidate = {
        **alternative,
        "id": f"{kind}-option",
        "kind": kind,
        assessment_field: assessment,
    }
    write_database_decision(
        tmp_path,
        database_decision(
            change_surfaces=change_surfaces,
            candidates=[baseline, candidate],
            recommended_option_id=candidate["id"],
            selected_option_id=candidate["id"],
        ),
    )

    assert load_database_decision(tmp_path).selected_option_id == candidate["id"]


def test_decision_enforces_surface_denormalization_and_physical_design_assessments(
    tmp_path: Path,
) -> None:
    write_workflow_artifacts(tmp_path, concept="Update the database query")
    baseline = database_decision()["candidates"][0]
    denormalized = {
        **database_decision()["candidates"][1],
        "id": "denormalize-summary",
        "kind": "denormalize",
        "denormalization_assessment": {
            "source_of_truth": "The base ledger remains authoritative.",
            "consistency_window": "Updates converge within one minute.",
            "reconciliation": "A nightly reconciliation compares aggregates.",
        },
    }
    physical = {
        **database_decision()["candidates"][1],
        "id": "add-index",
        "kind": "physical_design",
        "physical_design_assessment": {
            "read_benefit": "The index narrows the read path.",
            "write_amplification": "Each write updates one index.",
            "storage": "The index is bounded by the projected key count.",
            "build_or_lock": "Build online with a bounded metadata lock.",
        },
    }
    for payload in (
        database_decision(
            change_surfaces=["column"],
            candidates=[baseline, {**database_decision()["candidates"][1], "normalization_assessment": ""}],
        ),
        database_decision(
            candidates=[baseline, denormalized],
            selected_option_id="denormalize-summary",
            recommended_option_id="denormalize-summary",
        ),
        database_decision(
            candidates=[baseline, physical],
            selected_option_id="add-index",
            recommended_option_id="add-index",
        ),
    ):
        write_database_decision(tmp_path, payload)

        with pytest.raises(DatabaseValidationError):
            load_database_decision(tmp_path)


def test_decision_returns_selected_id_and_normalized_surfaces(tmp_path: Path) -> None:
    write_workflow_artifacts(tmp_path, concept="Update the database query")
    write_database_decision(tmp_path, database_decision(change_surfaces=["Query", "INDEX"]))

    decision = load_database_decision(tmp_path)

    assert isinstance(decision, DatabaseDecision)
    assert decision.selected_option_id == "rewrite-query"
    assert decision.change_surfaces == ("index", "query")
    assert len(decision.decision_hash) == 64
def test_decision_requires_applicable_physical_candidate_for_index_surface(
    tmp_path: Path,
) -> None:
    write_workflow_artifacts(tmp_path, concept="Update the database query")
    decision = database_decision(
        change_surfaces=["query", "index"],
        candidates=database_decision()["candidates"][:2],
    )
    write_database_decision(tmp_path, decision)

    with pytest.raises(DatabaseValidationError):
        load_database_decision(tmp_path)

@pytest.mark.parametrize(
    ("concept", "allowed_files", "underdeclared", "declared"),
    [
        ("CREATE TABLE audit_log (id bigint)", None, ["query"], ["column"]),
        (
            "Coordinate the application release",
            ["src/database/migrations/001_create_audit_log.sql"],
            ["query"],
            ["constraint"],
        ),
        (
            "Coordinate the application release",
            ["src/database/schema.prisma"],
            ["query"],
            ["erd"],
        ),
        ("Add a database index for audit lookup", None, ["query"], ["index"]),
        ("Add a database column for audit actor", None, ["query"], ["column"]),
        ("Tune the database query plan", None, ["index"], ["query"]),
        (
            "Coordinate the application release",
            ["src/database/indexes/audit_lookup.py"],
            ["query"],
            ["index"],
        ),
        (
            "Coordinate the application release",
            ["src/database/columns/audit_actor.py"],
            ["query"],
            ["column"],
        ),
        (
            "Coordinate the application release",
            ["src/database/queries/audit_lookup.py"],
            ["index"],
            ["query"],
        ),
    ],
    ids=[
        "text-ddl",
        "migration-path",
        "prisma-path",
        "text-index",
        "text-column",
        "text-query",
        "index-path",
        "column-path",
        "query-path",
    ],
)
def test_decision_binds_database_signals_to_change_surfaces(
    tmp_path: Path,
    concept: str,
    allowed_files: list[str] | None,
    underdeclared: list[str],
    declared: list[str],
) -> None:
    write_workflow_artifacts(
        tmp_path,
        concept=concept,
        allowed_files=allowed_files,
    )
    write_database_decision(
        tmp_path,
        database_decision(change_surfaces=underdeclared),
    )

    with pytest.raises(DatabaseValidationError) as raised:
        load_database_decision(tmp_path)

    assert raised.value.code == "decision_invalid"
    write_database_decision(tmp_path, database_decision(change_surfaces=declared))

    assert load_database_decision(tmp_path).change_surfaces == tuple(declared)


@pytest.mark.parametrize(
    ("concept", "allowed_files"),
    [
        ("ALTER TABLE audit_log ADD COLUMN actor_id bigint", None),
        (
            "Coordinate the application release",
            ["src/database/migrations/001_add_actor.sql"],
        ),
    ],
    ids=["ddl", "migration"],
)
def test_underdeclared_decision_blocks_schema_command(
    tmp_path: Path,
    concept: str,
    allowed_files: list[str] | None,
) -> None:
    command_marker = tmp_path / "schema-command-ran"
    schema_command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; "
            f"Path({str(command_marker)!r}).write_text('ran', encoding='utf-8'); "
            f"print({json.dumps(schema_evidence())!r})"
        ),
    ]
    write_workflow_artifacts(
        tmp_path,
        concept=concept,
        allowed_files=allowed_files,
    )
    write_database_manifest(tmp_path, schema_command=schema_command)
    write_database_decision(tmp_path, database_decision(change_surfaces=["query"]))

    result = run_database_check(tmp_path, "plan")

    assert result.status == "fail"
    assert result.blockers == ("decision_invalid",)
    assert not command_marker.exists()
    assert not evidence_path(tmp_path).exists()


def test_artifact_error_blocks_database_commands_without_guessing(tmp_path: Path) -> None:
    command_marker = tmp_path / "schema-command-ran"
    schema_command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; "
            f"Path({str(command_marker)!r}).write_text('ran', encoding='utf-8'); "
            f"print({json.dumps(schema_evidence())!r})"
        ),
    ]
    prepare_database_workflow(tmp_path, schema_command=schema_command)
    (tmp_path / ".workflow" / "artifacts" / "allowed-files.json").write_text(
        "[]",
        encoding="utf-8",
    )

    result = run_database_check(tmp_path, "plan")

    assert result.status == "fail"
    assert result.blockers == ("artifact_invalid",)
    assert not command_marker.exists()
    assert not evidence_path(tmp_path).exists()

def test_table_ddl_rejects_index_only_change_surface(tmp_path: Path) -> None:
    write_workflow_artifacts(tmp_path, concept="CREATE TABLE audit_log (id bigint)")
    write_database_decision(tmp_path, database_decision(change_surfaces=["index"]))

    with pytest.raises(DatabaseValidationError) as raised:
        load_database_decision(tmp_path)

    assert raised.value.code == "decision_invalid"
    write_database_decision(tmp_path, database_decision(change_surfaces=["column"]))

    assert load_database_decision(tmp_path).change_surfaces == ("column",)


@pytest.mark.parametrize(
    ("ddl", "underdeclared", "declared", "required_reason"),
    [
        (
            "CREATE INDEX audit_actor_idx ON audit_log (actor_id)",
            ["column"],
            ["index"],
            "text:index_ddl",
        ),
        (
            "ALTER TABLE audit_log ADD COLUMN actor_id bigint",
            ["constraint"],
            ["column"],
            "text:column_ddl",
        ),
    ],
    ids=["index-ddl", "column-ddl"],
)
def test_specific_ddl_signals_require_their_declared_surface(
    tmp_path: Path,
    ddl: str,
    underdeclared: list[str],
    declared: list[str],
    required_reason: str,
) -> None:
    write_workflow_artifacts(tmp_path, concept=ddl)
    assert required_reason in detect_database_signal(tmp_path).reasons
    write_database_decision(
        tmp_path,
        database_decision(change_surfaces=underdeclared),
    )

    with pytest.raises(DatabaseValidationError) as raised:
        load_database_decision(tmp_path)

    assert raised.value.code == "decision_invalid"
    write_database_decision(tmp_path, database_decision(change_surfaces=declared))

    assert load_database_decision(tmp_path).change_surfaces == tuple(declared)


def test_alter_table_add_index_emits_table_and_index_ddl_signals(
    tmp_path: Path,
) -> None:
    write_workflow_artifacts(
        tmp_path,
        concept="ALTER TABLE audit_log ADD INDEX audit_actor_idx (actor_id)",
    )

    assert {
        "text:table_ddl",
        "text:index_ddl",
    } <= set(detect_database_signal(tmp_path).reasons)


def test_migration_index_verification_requires_migration_and_rollback(
    tmp_path: Path,
) -> None:
    write_workflow_artifacts(
        tmp_path,
        concept="Coordinate the application release",
        allowed_files=["src/database/migrations/001_add_audit_index.sql"],
    )
    write_database_manifest(
        tmp_path,
        schema_command=database_command(schema_evidence()),
        verify_command=database_command(
            verify_evidence(
                migration="not_applicable",
                rollback="not_applicable",
            )
        ),
    )
    write_database_decision(tmp_path, database_decision(change_surfaces=["index"]))
    assert run_database_check(tmp_path, "plan").status == "pass"

    result = run_database_check(tmp_path, "verify")

    assert result.status == "fail"
    assert result.blockers == ("verify_evidence_invalid",)


def test_query_only_verification_allows_not_applicable_schema_migration(
    tmp_path: Path,
) -> None:
    prepare_database_workflow(
        tmp_path,
        decision=database_decision(change_surfaces=["query"]),
        verify_command=database_command(
            verify_evidence(
                migration="not_applicable",
                rollback="not_applicable",
            )
        ),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"

    assert run_database_check(tmp_path, "verify").status == "pass"


@pytest.mark.parametrize("schema_version", [True, 1.0, "1"])
def test_schema_version_requires_integer_one_everywhere(
    tmp_path: Path,
    schema_version: object,
) -> None:
    decision_root = tmp_path / "decision"
    write_workflow_artifacts(decision_root, concept="Update the database query")
    write_database_decision(
        decision_root,
        database_decision(schema_version=schema_version),
    )
    with pytest.raises(DatabaseValidationError):
        load_database_decision(decision_root)

    schema_root = tmp_path / "schema"
    prepare_database_workflow(
        schema_root,
        schema_command=database_command(schema_evidence(schema_version=schema_version)),
    )
    assert run_database_check(schema_root, "plan").blockers == (
        "schema_evidence_invalid",
    )

    verify_root = tmp_path / "verify"
    prepare_database_workflow(
        verify_root,
        verify_command=database_command(verify_evidence(schema_version=schema_version)),
    )
    assert run_database_check(verify_root, "plan").status == "pass"
    assert run_database_check(verify_root, "verify").blockers == (
        "verify_evidence_invalid",
    )

    test_root = tmp_path / "test"
    prepare_database_workflow(
        test_root,
        verify_command=database_command(verify_evidence()),
        test_command=database_command(
            database_test_payload(schema_version=schema_version)
        ),
    )
    assert run_database_check(test_root, "plan").status == "pass"
    assert run_database_check(test_root, "verify").status == "pass"
    assert run_database_check(test_root, "test").blockers == (
        "test_evidence_invalid",
    )

    evidence_root = tmp_path / "evidence"
    prepare_database_workflow(evidence_root)
    assert run_database_check(evidence_root, "plan").status == "pass"
    path = evidence_path(evidence_root)
    evidence = json.loads(path.read_text(encoding="utf-8"))
    evidence["schema_version"] = schema_version
    path.write_text(json.dumps(evidence), encoding="utf-8")
    assert run_database_check(evidence_root, "plan").blockers == (
        "evidence_invalid",
    )




@pytest.mark.parametrize(
    ("schema_command", "expected_blocker"),
    [
        (
            [sys.executable, "-c", "import time; time.sleep(3)"],
            "command_timeout",
        ),
        (
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * (128 * 1024 + 1))"],
            "command_output_oversize",
        ),
        (database_command(schema_evidence(), exit_code=7), "command_nonzero"),
        (
            [sys.executable, "-c", "print('{}'); print('{}')"],
            "command_output_invalid",
        ),
        (
            [
                sys.executable,
                "-c",
                "print('{\"schema_version\": 1, \"schema_version\": 1}')",
            ],
            "command_output_invalid",
        ),
        (
            database_command(schema_evidence(diagnostic="unapproved")),
            "command_output_unsafe",
        ),
        (
            database_command(
                schema_evidence(object_counts={"tables": 1, "password": "RAW_SECRET"})
            ),
            "command_output_unsafe",
        ),
    ],
)
def test_command_rejections_fail_closed_without_persisting_evidence(
    tmp_path: Path,
    schema_command: list[str],
    expected_blocker: str,
) -> None:
    prepare_database_workflow(tmp_path, schema_command=schema_command)

    result = run_database_check(tmp_path, "plan")

    assert result.status == "fail"
    assert result.blockers == (expected_blocker,)
    assert not evidence_path(tmp_path).exists()
    assert "RAW_SECRET" not in "\n".join(result.blockers)


def leaking_process_group_command(mode: str, pid_path: Path) -> list[str]:
    payload = schema_evidence()
    script = f"""
import json
import os
from pathlib import Path
import signal
import sys
import time

child = os.fork()
if child == 0:
    Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding="utf-8")
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.01)

if {mode!r} == "reader":
    sys.stdout.write(json.dumps({payload!r}))
    sys.stdout.flush()
    os._exit(0)
if {mode!r} == "nonzero":
    sys.stdout.write(json.dumps({payload!r}))
    sys.stdout.flush()
    os._exit(7)
if {mode!r} == "oversize":
    sys.stdout.write("x" * (128 * 1024 + 1))
    sys.stdout.flush()
    os._exit(0)
time.sleep(3)
"""
    return [sys.executable, "-c", script]


def pid_is_reaped(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as error:
        return error.errno == errno.ESRCH
    return False


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
@pytest.mark.parametrize(
    ("mode", "expected_blocker"),
    [
        ("reader", "command_output_invalid"),
        ("nonzero", "command_nonzero"),
        ("oversize", "command_output_oversize"),
        ("timeout", "command_timeout"),
    ],
)
def test_command_failure_reaps_ignored_process_group_children(
    tmp_path: Path,
    mode: str,
    expected_blocker: str,
) -> None:
    pid_path = tmp_path / f"{mode}.pid"
    prepare_database_workflow(
        tmp_path,
        schema_command=leaking_process_group_command(mode, pid_path),
    )
    write_database_manifest(
        tmp_path,
        schema_command=leaking_process_group_command(mode, pid_path),
        timeout_seconds=1,
    )
    try:
        result = run_database_check(tmp_path, "plan")

        assert result.blockers == (expected_blocker,)
        deadline = time.monotonic() + 2
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_path.exists()
        child_pid = int(pid_path.read_text(encoding="utf-8"))
        while not pid_is_reaped(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_is_reaped(child_pid)
    finally:
        if pid_path.exists():
            try:
                os.kill(int(pid_path.read_text(encoding="utf-8")), 9)
            except OSError:
                pass


@pytest.mark.parametrize(
    "updates",
    [
        {"target_class": "production_primary"},
        {"read_only": False},
        {"schema_only": False},
        {"schema_hash": "not-a-hash"},
        {"captured_at": datetime.now(timezone.utc) - timedelta(hours=25)},
        {"captured_at": datetime.now(timezone.utc) + timedelta(minutes=5)},
    ],
)
def test_schema_evidence_requires_safe_target_hash_and_fresh_timestamp(
    tmp_path: Path,
    updates: dict[str, object],
) -> None:
    prepare_database_workflow(
        tmp_path,
        schema_command=database_command(schema_evidence(**updates)),
    )

    result = run_database_check(tmp_path, "plan")

    assert result.status == "fail"
    assert result.blockers == ("schema_evidence_invalid",)
    assert not evidence_path(tmp_path).exists()


def test_plan_evidence_persists_only_sanitized_schema_metadata(tmp_path: Path) -> None:
    prepare_database_workflow(tmp_path)

    result = run_database_check(tmp_path, "plan")

    persisted = json.loads(evidence_path(tmp_path).read_text(encoding="utf-8"))
    assert result.status == "pass"
    assert persisted["stages"]["plan"]["schema"] == {
        "schema_hash": "a" * 64,
        "engine": "mysql",
        "engine_version": "8.0",
        "captured_at": persisted["stages"]["plan"]["schema"]["captured_at"],
        "object_counts": {"tables": 1, "columns": 8, "indexes": 2, "constraints": 3},
    }
    assert "command" not in json.dumps(persisted)


def test_verify_evidence_requires_selected_option_hash_and_query_surface_checks(
    tmp_path: Path,
) -> None:
    prepare_database_workflow(
        tmp_path,
        verify_command=database_command(verify_evidence(query_plan="not_applicable")),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"

    result = run_database_check(tmp_path, "verify")

    assert result.status == "fail"
    assert result.blockers == ("verify_evidence_invalid",)
    assert set(json.loads(evidence_path(tmp_path).read_text())["stages"]) == {"plan"}


def test_verify_evidence_requires_migration_and_rollback_for_column_surfaces(
    tmp_path: Path,
) -> None:
    prepare_database_workflow(
        tmp_path,
        decision=database_decision(change_surfaces=["query", "column"]),
        verify_command=database_command(verify_evidence(migration="not_applicable")),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"

    result = run_database_check(tmp_path, "verify")

    assert result.status == "fail"
    assert result.blockers == ("verify_evidence_invalid",)


@pytest.mark.parametrize(
    "verification",
    [
        verify_evidence(equivalence="fail"),
        verify_evidence(integrity="fail"),
        verify_evidence(rollback="fail"),
    ],
    ids=["equivalence", "integrity", "rollback"],
)
def test_verify_evidence_rejects_failed_required_outcomes(
    tmp_path: Path,
    verification: dict[str, object],
) -> None:
    prepare_database_workflow(
        tmp_path,
        verify_command=database_command(verification),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"
    before = evidence_path(tmp_path).read_bytes()

    result = run_database_check(tmp_path, "verify")

    assert result.status == "fail"
    assert result.blockers == ("verify_evidence_invalid",)
    assert evidence_path(tmp_path).read_bytes() == before

@pytest.mark.parametrize(
    "verification",
    [
        verify_evidence(selected_option_id="maintain-current"),
        verify_evidence(schema_hash="b" * 64),
    ],
    ids=["selected-option-id", "production-schema-hash"],
)
def test_verify_evidence_rejects_identity_mismatches_without_overwrite(
    tmp_path: Path,
    verification: dict[str, object],
) -> None:
    prepare_database_workflow(
        tmp_path,
        verify_command=database_command(verification),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"
    before = evidence_path(tmp_path).read_bytes()

    result = run_database_check(tmp_path, "verify")

    assert result.status == "fail"
    assert result.blockers == ("verify_evidence_invalid",)
    assert evidence_path(tmp_path).read_bytes() == before

def test_verify_persists_exact_safe_same_engine_target_metadata(
    tmp_path: Path,
) -> None:
    prepare_database_workflow(
        tmp_path,
        verify_command=database_command(verify_evidence()),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"

    result = run_database_check(tmp_path, "verify")

    persisted = json.loads(evidence_path(tmp_path).read_text(encoding="utf-8"))
    assert result.status == "pass"
    assert persisted["stages"]["verify"]["verify"] == {
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


def test_query_verify_allows_approved_read_replica_same_engine_target(
    tmp_path: Path,
) -> None:
    prepare_database_workflow(
        tmp_path,
        decision=database_decision(change_surfaces=["query"]),
        verify_command=database_command(
            verify_evidence(execution_target="approved_read_replica")
        ),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"

    result = run_database_check(tmp_path, "verify")

    assert result.status == "pass"


@pytest.mark.parametrize(
    "updates",
    [
        {"engine": "duckdb"},
        {"engine": "postgres"},
        {"execution_target": "production_primary"},
        {"execution_target": "approved_read_replica"},
        {"production_primary_queries": True},
        {"raw_production_rows": True},
    ],
    ids=[
        "duckdb",
        "cross-engine",
        "production-primary",
        "structural-read-replica",
        "production-primary-queries",
        "raw-production-rows",
    ],
)
def test_verify_evidence_rejects_unsafe_targets_engines_and_production_rows(
    tmp_path: Path,
    updates: dict[str, object],
) -> None:
    prepare_database_workflow(
        tmp_path,
        verify_command=database_command(verify_evidence(**updates)),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"

    result = run_database_check(tmp_path, "verify")

    assert result.status == "fail"
    assert result.blockers == ("verify_evidence_invalid",)




def test_verify_merges_sanitized_evidence_without_replacing_plan(tmp_path: Path) -> None:
    prepare_database_workflow(
        tmp_path,
        verify_command=database_command(verify_evidence()),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"
    plan_record = json.loads(evidence_path(tmp_path).read_text())["stages"]["plan"]

    result = run_database_check(tmp_path, "verify")

    persisted = json.loads(evidence_path(tmp_path).read_text())
    assert result.status == "pass"
    assert persisted["stages"]["plan"] == plan_record
    assert persisted["stages"]["verify"]["verify"]["equivalence"] == "pass"


def delayed_database_command(payload: dict[str, object]) -> list[str]:
    return [
        sys.executable,
        "-c",
        f"import json, time; time.sleep(0.2); print(json.dumps({payload!r}))",
    ]


def test_concurrent_verify_and_test_preserve_all_valid_stage_records(
    tmp_path: Path,
) -> None:
    prepare_database_workflow(
        tmp_path,
        schema_command=delayed_database_command(schema_evidence()),
        verify_command=delayed_database_command(verify_evidence()),
        test_command=delayed_database_command(database_test_payload()),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"
    start = threading.Barrier(3)
    results: dict[str, object] = {}

    def check(stage: str) -> None:
        start.wait()
        results[stage] = run_database_check(tmp_path, stage)

    verify_thread = threading.Thread(target=check, args=("verify",))
    test_thread = threading.Thread(target=check, args=("test",))
    verify_thread.start()
    test_thread.start()
    start.wait()
    verify_thread.join()
    test_thread.join()

    assert results["verify"].status == "pass"
    assert results["test"].status == "pass"
    persisted = json.loads(evidence_path(tmp_path).read_text(encoding="utf-8"))
    assert set(persisted["stages"]) == {"plan", "verify", "test"}


def test_test_evidence_requires_masked_safe_local_target_and_all_passes(tmp_path: Path) -> None:
    prepare_database_workflow(
        tmp_path,
        verify_command=database_command(verify_evidence()),
        test_command=database_command(
            database_test_payload(
                masked=False,
                raw_production_rows=True,
                local_target="production_primary",
            )
        ),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"
    assert run_database_check(tmp_path, "verify").status == "pass"

    result = run_database_check(tmp_path, "test")

    assert result.status == "fail"
    assert result.blockers == ("test_evidence_invalid",)
    assert set(json.loads(evidence_path(tmp_path).read_text())["stages"]) == {"plan", "verify"}

@pytest.mark.parametrize(
    "test_payload",
    [
        database_test_payload(equivalence="fail"),
        database_test_payload(integrity="fail"),
        database_test_payload(performance="fail"),
    ],
    ids=["equivalence", "integrity", "performance"],
)
def test_test_evidence_rejects_failed_outcomes_without_overwrite(
    tmp_path: Path,
    test_payload: dict[str, object],
) -> None:
    prepare_database_workflow(
        tmp_path,
        verify_command=database_command(verify_evidence()),
        test_command=database_command(test_payload),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"
    assert run_database_check(tmp_path, "verify").status == "pass"
    before = evidence_path(tmp_path).read_bytes()

    result = run_database_check(tmp_path, "test")

    assert result.status == "fail"
    assert result.blockers == ("test_evidence_invalid",)
    assert evidence_path(tmp_path).read_bytes() == before

@pytest.mark.parametrize(
    "test_payload",
    [
        database_test_payload(selected_option_id="maintain-current"),
        database_test_payload(schema_hash="b" * 64),
    ],
    ids=["selected-option-id", "production-schema-hash"],
)
def test_test_evidence_rejects_identity_mismatches_without_overwrite(
    tmp_path: Path,
    test_payload: dict[str, object],
) -> None:
    prepare_database_workflow(
        tmp_path,
        verify_command=database_command(verify_evidence()),
        test_command=database_command(test_payload),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"
    assert run_database_check(tmp_path, "verify").status == "pass"
    before = evidence_path(tmp_path).read_bytes()

    result = run_database_check(tmp_path, "test")

    assert result.status == "fail"
    assert result.blockers == ("test_evidence_invalid",)
    assert evidence_path(tmp_path).read_bytes() == before

def test_persisted_test_record_requires_raw_production_rows_false(
    tmp_path: Path,
) -> None:
    prepare_database_workflow(
        tmp_path,
        verify_command=database_command(verify_evidence()),
        test_command=database_command(database_test_payload()),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"
    assert run_database_check(tmp_path, "verify").status == "pass"
    assert run_database_check(tmp_path, "test").status == "pass"

    path = evidence_path(tmp_path)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["stages"]["test"]["test"]["raw_production_rows"] is False
    del persisted["stages"]["test"]["test"]["raw_production_rows"]
    path.write_text(json.dumps(persisted), encoding="utf-8")

    result = run_database_check(tmp_path, "plan")

    assert result.status == "fail"
    assert result.blockers == ("evidence_invalid",)






@pytest.mark.parametrize(
    ("allow_replica", "expected_status"),
    [(False, "fail"), (True, "pass")],
)
def test_read_replica_test_target_requires_profile_opt_in(
    tmp_path: Path,
    allow_replica: bool,
    expected_status: str,
) -> None:
    write_workflow_artifacts(tmp_path, concept="Update the database query")
    write_database_manifest(
        tmp_path,
        schema_command=database_command(schema_evidence()),
        verify_command=database_command(verify_evidence()),
        test_command=database_command(database_test_payload(local_target="read_replica")),
        allow_production_replica_sample=allow_replica,
    )
    write_database_decision(tmp_path, database_decision())
    assert run_database_check(tmp_path, "plan").status == "pass"
    assert run_database_check(tmp_path, "verify").status == "pass"

    result = run_database_check(tmp_path, "test")

    assert result.status == expected_status
    if expected_status == "fail":
        assert result.blockers == ("test_evidence_invalid",)
        assert set(json.loads(evidence_path(tmp_path).read_text())["stages"]) == {
            "plan",
            "verify",
        }


def test_test_stage_requires_waiver_when_no_test_command_is_configured(
    tmp_path: Path,
) -> None:
    prepare_database_workflow(
        tmp_path,
        verify_command=database_command(verify_evidence()),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"
    before = evidence_path(tmp_path).read_bytes()

    result = run_database_check(tmp_path, "test")

    assert result.status == "fail"
    assert result.blockers == ("test_waiver_missing",)
    assert evidence_path(tmp_path).read_bytes() == before


def test_test_stage_accepts_explicit_valid_local_data_waiver(tmp_path: Path) -> None:
    decision = database_decision(
        local_data_test_waiver={
            "reason": "No approved masked production-shaped fixture is available.",
            "approver": "database-owner",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    prepare_database_workflow(
        tmp_path,
        verify_command=database_command(verify_evidence()),
        decision=decision,
    )
    assert run_database_check(tmp_path, "plan").status == "pass"
    assert run_database_check(tmp_path, "verify").status == "pass"

    result = run_database_check(tmp_path, "test")

    persisted = json.loads(evidence_path(tmp_path).read_text())
    assert result.status == "pass"
    assert persisted["stages"]["test"]["test"]["status"] == "waived"
    assert persisted["stages"]["test"]["test"]["waiver"]["approver"] == "database-owner"

def test_stored_test_waiver_requires_empty_current_test_command(tmp_path: Path) -> None:
    decision = database_decision(
        local_data_test_waiver={
            "reason": "No approved masked production-shaped fixture is available.",
            "approver": "database-owner",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    prepare_database_workflow(
        tmp_path,
        verify_command=database_command(verify_evidence()),
        decision=decision,
    )
    assert run_database_check(tmp_path, "plan").status == "pass"
    assert run_database_check(tmp_path, "verify").status == "pass"
    assert run_database_check(tmp_path, "test").status == "pass"

    manifest_path = tmp_path / ".workflow" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["database_validation"]["test_command"] = database_command(
        database_test_payload()
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    profile_hash = load_database_profile(tmp_path).profile_hash

    path = evidence_path(tmp_path)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    persisted["profile_hash"] = profile_hash
    for record in persisted["stages"].values():
        record["profile_hash"] = profile_hash
    path.write_text(json.dumps(persisted), encoding="utf-8")

    result = run_database_check(tmp_path, "plan")

    assert result.status == "fail"
    assert result.blockers == ("evidence_invalid",)
    assert all(
        condition["passed"] is False
        for condition in evaluate_database_gate(tmp_path, "test")
        if condition["condition"] == "database.local_test"
    )


def test_waiver_timestamp_is_canonical_utc_in_hash_and_evidence(tmp_path: Path) -> None:
    instant = datetime.now(timezone.utc).replace(microsecond=0)
    decision = database_decision(
        local_data_test_waiver={
            "reason": "No approved masked production-shaped fixture is available.",
            "approver": "database-owner",
            "timestamp": instant.astimezone(timezone(timedelta(hours=9))).isoformat(),
        }
    )
    prepare_database_workflow(
        tmp_path,
        verify_command=database_command(verify_evidence()),
        decision=decision,
    )
    assert load_database_decision(tmp_path).local_data_test_waiver == {
        "reason": "No approved masked production-shaped fixture is available.",
        "approver": "database-owner",
        "timestamp": instant.isoformat().replace("+00:00", "Z"),
    }
    assert run_database_check(tmp_path, "plan").status == "pass"
    assert run_database_check(tmp_path, "verify").status == "pass"
    assert run_database_check(tmp_path, "test").status == "pass"

    persisted = json.loads(evidence_path(tmp_path).read_text(encoding="utf-8"))
    assert persisted["stages"]["test"]["test"]["waiver"]["timestamp"] == (
        instant.isoformat().replace("+00:00", "Z")
    )


def test_future_stage_checked_at_blocks_evidence_merge(tmp_path: Path) -> None:
    prepare_database_workflow(tmp_path)
    assert run_database_check(tmp_path, "plan").status == "pass"
    path = evidence_path(tmp_path)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    persisted["stages"]["plan"]["checked_at"] = (
        datetime.now(timezone.utc) + timedelta(minutes=1)
    ).isoformat().replace("+00:00", "Z")
    path.write_text(json.dumps(persisted), encoding="utf-8")
    before = path.read_bytes()

    result = run_database_check(tmp_path, "plan")

    assert result.status == "fail"
    assert result.blockers == ("evidence_invalid",)
    assert path.read_bytes() == before


@pytest.mark.parametrize("changed_artifact", ["profile", "decision"])
def test_changed_profile_or_decision_does_not_overwrite_valid_plan_evidence(
    tmp_path: Path,
    changed_artifact: str,
) -> None:
    prepare_database_workflow(
        tmp_path,
        verify_command=database_command(verify_evidence()),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"
    before = evidence_path(tmp_path).read_bytes()
    if changed_artifact == "profile":
        write_database_manifest(
            tmp_path,
            schema_command=database_command(schema_evidence()),
            verify_command=database_command(verify_evidence()),
            timeout_seconds=1,
        )
    else:
        write_database_decision(
            tmp_path,
            database_decision(recommendation_rationale="The selected option remains correct."),
        )

    result = run_database_check(tmp_path, "verify")

    assert result.status == "fail"
    assert result.blockers == (f"{changed_artifact}_changed",)
    assert evidence_path(tmp_path).read_bytes() == before


def test_plan_refresh_accepts_changed_profile_and_invalidates_downstream(
    tmp_path: Path,
) -> None:
    prepare_database_workflow(
        tmp_path,
        verify_command=database_command(verify_evidence()),
        test_command=database_command(database_test_payload()),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"
    assert run_database_check(tmp_path, "verify").status == "pass"
    assert run_database_check(tmp_path, "test").status == "pass"
    write_database_manifest(
        tmp_path,
        schema_command=database_command(schema_evidence()),
        verify_command=database_command(verify_evidence()),
        test_command=database_command(database_test_payload()),
        timeout_seconds=1,
    )

    result = run_database_check(tmp_path, "plan")

    assert result.status == "pass"
    assert set(json.loads(evidence_path(tmp_path).read_text())["stages"]) == {"plan"}


def test_schema_drift_does_not_overwrite_valid_plan_evidence(tmp_path: Path) -> None:
    schema_hash_path = tmp_path / "current-schema-hash"
    schema_hash_path.write_text("a" * 64, encoding="utf-8")
    schema_command = [
        sys.executable,
        "-c",
        (
            "import datetime, json, pathlib, sys; "
            "schema_hash = pathlib.Path(sys.argv[1]).read_text().strip(); "
            "print(json.dumps({"
            "'schema_version': 1, 'kind': 'production_schema', "
            "'target_class': 'production_metadata', 'read_only': True, "
            "'schema_only': True, 'engine': 'mysql', 'engine_version': '8.0', "
            "'captured_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), "
            "'schema_hash': schema_hash, "
            "'object_counts': {'tables': 1, 'columns': 8, 'indexes': 2, "
            "'constraints': 3}}))"
        ),
        str(schema_hash_path),
    ]
    prepare_database_workflow(
        tmp_path,
        schema_command=schema_command,
        verify_command=database_command(verify_evidence(schema_hash="b" * 64)),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"
    before = evidence_path(tmp_path).read_bytes()
    schema_hash_path.write_text("b" * 64, encoding="utf-8")

    result = run_database_check(tmp_path, "verify")

    assert result.status == "fail"
    assert result.blockers == ("production_schema_changed",)
    assert evidence_path(tmp_path).read_bytes() == before


def test_unsafe_verify_output_preserves_prior_evidence_without_raw_stdout(tmp_path: Path) -> None:
    prepare_database_workflow(
        tmp_path,
        verify_command=database_command(
            verify_evidence(
                rollback={"password": "RAW_SECRET"},
            )
        ),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"
    before = evidence_path(tmp_path).read_bytes()

    result = run_database_check(tmp_path, "verify")

    assert result.status == "fail"
    assert result.blockers == ("command_output_unsafe",)
    assert evidence_path(tmp_path).read_bytes() == before
    assert b"RAW_SECRET" not in before


@pytest.mark.parametrize(
    ("stage", "mutation"),
    [
        ("plan", "status"),
        ("plan", "missing_checked_at"),
        ("plan", "non_utc_checked_at"),
        ("plan", "stale_schema"),
        ("verify", "selected_option"),
        ("verify", "missing_checked_at"),
        ("verify", "schema_hash"),
        ("test", "selected_option"),
        ("test", "unexpected_field"),
        ("test", "missing_checked_at"),
    ],
)
def test_malformed_prior_stage_evidence_blocks_atomic_merge(
    tmp_path: Path,
    stage: str,
    mutation: str,
) -> None:
    prepare_database_workflow(
        tmp_path,
        verify_command=database_command(verify_evidence()),
        test_command=database_command(database_test_payload()),
    )
    assert run_database_check(tmp_path, "plan").status == "pass"
    assert run_database_check(tmp_path, "verify").status == "pass"
    assert run_database_check(tmp_path, "test").status == "pass"

    path = evidence_path(tmp_path)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    record = persisted["stages"][stage]
    if mutation == "missing_checked_at":
        del record["checked_at"]
    elif mutation == "status":
        record["status"] = "waived"
    elif mutation == "non_utc_checked_at":
        record["checked_at"] = datetime.now(
            timezone(timedelta(hours=9))
        ).isoformat()
    elif mutation == "stale_schema":
        record["schema"]["captured_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=25)
        ).isoformat()
    elif mutation == "selected_option":
        record[stage]["selected_option_id"] = "broken-id"
    elif mutation == "schema_hash":
        record["schema"]["schema_hash"] = "b" * 64
    else:
        record["unexpected"] = "corrupt"
    path.write_text(json.dumps(persisted), encoding="utf-8")
    before = path.read_bytes()

    result = run_database_check(tmp_path, "plan")

    assert result.status == "fail"
    assert result.blockers == ("evidence_invalid",)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("escaped_path", "expected_blocker"),
    [
        ("workflow", "artifact_invalid"),
        ("manifest", "profile_invalid"),
        ("decision", "decision_invalid"),
        ("artifacts", "artifact_invalid"),
        ("evidence", "evidence_invalid"),
    ],
)
def test_workflow_artifacts_reject_symlink_escape(
    tmp_path: Path,
    escaped_path: str,
    expected_blocker: str,
) -> None:
    prepare_database_workflow(tmp_path)
    workflow = tmp_path / ".workflow"
    artifacts = workflow / "artifacts"
    outside = tmp_path.parent / f"{tmp_path.name}-{escaped_path}-outside"
    outside.mkdir()
    outside_evidence = outside / "database-validation-evidence.json"
    outside_evidence_before: bytes | None = None
    if escaped_path == "workflow":
        shutil.copytree(workflow, outside / "workflow")
        shutil.rmtree(workflow)
        workflow.symlink_to(outside / "workflow", target_is_directory=True)
    elif escaped_path == "manifest":
        source = workflow / "manifest.json"
        target = outside / "manifest.json"
        target.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(target)
    elif escaped_path == "decision":
        source = artifacts / "database-decision.json"
        target = outside / "database-decision.json"
        target.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(target)
    elif escaped_path == "artifacts":
        shutil.copytree(artifacts, outside / "artifacts")
        shutil.rmtree(artifacts)
        artifacts.symlink_to(outside / "artifacts", target_is_directory=True)
    else:
        assert run_database_check(tmp_path, "plan").status == "pass"
        source = evidence_path(tmp_path)
        target = outside / "database-validation-evidence.json"
        target.write_bytes(source.read_bytes())
        outside_evidence_before = target.read_bytes()
        source.unlink()
        source.symlink_to(target)

    result = run_database_check(tmp_path, "plan")

    assert result.status == "fail"
    assert result.blockers == (expected_blocker,)
    if outside_evidence_before is None:
        assert not outside_evidence.exists()
    else:
        assert outside_evidence.read_bytes() == outside_evidence_before


def test_bounded_reader_retries_eintr_and_accumulates_short_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_workflow_artifacts(tmp_path, concept="Update the database query")
    real_read = db_validation.os.read
    actions: list[object] = ["eintr", 3, 3, 3]

    def short_read(file_descriptor: int, size: int) -> bytes:
        if actions:
            action = actions.pop(0)
            if action == "eintr":
                raise InterruptedError(errno.EINTR, "interrupted")
            return real_read(file_descriptor, min(size, int(action)))
        return real_read(file_descriptor, size)

    monkeypatch.setattr(db_validation.os, "read", short_read)

    signal = detect_database_signal(tmp_path)
    assert signal.detected is True
    assert "text:database" in signal.reasons


def test_bounded_reader_detects_oversize_after_short_read_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_workflow_artifacts(tmp_path)
    real_read = db_validation.os.read
    chunks = [b"x" * (64 * 1024), b"x" * (64 * 1024 + 1)]

    def short_read(file_descriptor: int, size: int) -> bytes:
        if chunks:
            return chunks.pop(0)
        return real_read(file_descriptor, size)

    monkeypatch.setattr(db_validation.os, "read", short_read)

    _assert_database_signal(
        detect_database_signal(tmp_path),
        detected=True,
        reasons=(_artifact_error_reason(".workflow/concept.md", "oversize"),),
    )


def test_plan_signal_callback_precedes_hermetic_command_and_redacts_path_reasons(
    tmp_path: Path,
) -> None:
    callback_marker = tmp_path / "callback-ran"
    command_marker = tmp_path / "schema-command-ran"
    payload = schema_evidence()
    script = (
        "from pathlib import Path; "
        f"callback = Path({str(callback_marker)!r}); "
        "assert callback.is_file(); "
        f"Path({str(command_marker)!r}).write_text('ran', encoding='utf-8'); "
        f"print({json.dumps(payload)!r})"
    )
    prepare_database_workflow(
        tmp_path,
        schema_command=[sys.executable, "-c", script],
    )
    allowed_path = "src/database/migrations/supersecretapikey.sql"
    (tmp_path / ".workflow" / "artifacts" / "allowed-files.json").write_text(
        json.dumps({"planned_files": [allowed_path]}),
        encoding="utf-8",
    )
    callback_reasons: list[tuple[str, ...]] = []

    def callback(reasons: tuple[str, ...]) -> None:
        callback_reasons.append(reasons)
        callback_marker.write_text("callback", encoding="utf-8")

    result = run_database_check(tmp_path, "plan", on_database_signal=callback)

    assert result.status == "pass"
    assert command_marker.read_text(encoding="utf-8") == "ran"
    assert evidence_path(tmp_path).exists()
    assert any(reason.startswith("path:migration:") for reason in callback_reasons[0])
    assert all("supersecretapikey" not in reason for reason in callback_reasons[0])
    assert all("supersecretapikey" not in reason for reason in result.signal_reasons)


def test_plan_signal_callback_failure_blocks_hermetic_command_and_evidence(
    tmp_path: Path,
) -> None:
    command_marker = tmp_path / "schema-command-ran"
    payload = schema_evidence()
    script = (
        "from pathlib import Path; "
        f"Path({str(command_marker)!r}).write_text('ran', encoding='utf-8'); "
        f"print({json.dumps(payload)!r})"
    )
    prepare_database_workflow(
        tmp_path,
        schema_command=[sys.executable, "-c", script],
    )

    result = run_database_check(
        tmp_path,
        "plan",
        on_database_signal=lambda reasons: (_ for _ in ()).throw(
            RuntimeError("DATABASE_URL=postgres://secret@example.test")
        ),
    )

    assert result.status == "fail"
    assert result.blockers == ("signal_callback_failed",)
    assert not command_marker.exists()
    assert not evidence_path(tmp_path).exists()


def test_no_signal_does_not_invoke_database_callback(tmp_path: Path) -> None:
    write_workflow_artifacts(tmp_path)
    callback_reasons: list[tuple[str, ...]] = []

    result = run_database_check(
        tmp_path,
        "plan",
        on_database_signal=lambda reasons: callback_reasons.append(reasons),
    )

    assert result.status == "not_applicable"
    assert callback_reasons == []


def test_no_database_signal_is_not_applicable_without_profile_or_evidence(
    tmp_path: Path,
) -> None:
    write_workflow_artifacts(tmp_path)

    result = run_database_check(tmp_path, "plan")

    assert result.status == "not_applicable"
    assert result.blockers == ()
    assert not evidence_path(tmp_path).exists()


def test_each_persisted_stage_records_the_exact_evidence_identities(
    tmp_path: Path,
) -> None:
    prepare_database_workflow(
        tmp_path,
        verify_command=database_command(verify_evidence()),
        test_command=database_command(database_test_payload()),
    )

    assert run_database_check(tmp_path, "plan").status == "pass"
    assert run_database_check(tmp_path, "verify").status == "pass"
    assert run_database_check(tmp_path, "test").status == "pass"

    persisted = json.loads(evidence_path(tmp_path).read_text(encoding="utf-8"))
    expected = {
        field: persisted[field]
        for field in ("signal_hash", "profile_hash", "decision_hash")
    }
    for record in persisted["stages"].values():
        assert {
            field: record[field]
            for field in ("signal_hash", "profile_hash", "decision_hash")
        } == expected


def test_plan_rejects_malformed_stale_evidence_before_refreshing(
    tmp_path: Path,
) -> None:
    prepare_database_workflow(tmp_path)
    assert run_database_check(tmp_path, "plan").status == "pass"

    path = evidence_path(tmp_path)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    persisted["profile_hash"] = "b" * 64
    del persisted["stages"]["plan"]["status"]
    path.write_text(json.dumps(persisted), encoding="utf-8")
    before = path.read_bytes()

    result = run_database_check(tmp_path, "plan")

    assert result.status == "fail"
    assert result.blockers == ("evidence_invalid",)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("engine", "postgres://readonly@example.test/database"),
        ("engine_version", "8.0\ninjected"),
    ],
)
def test_plan_rejects_unsafe_persisted_schema_strings(
    tmp_path: Path,
    field: str,
    unsafe_value: str,
) -> None:
    payload = schema_evidence(**{field: unsafe_value})
    payload_path = tmp_path / "unsafe-schema-evidence.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    prepare_database_workflow(
        tmp_path,
        schema_command=[
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text())",
            str(payload_path),
        ],
    )

    result = run_database_check(tmp_path, "plan")

    assert result.status == "fail"
    assert result.blockers == ("schema_evidence_invalid",)
    assert not evidence_path(tmp_path).exists()


@pytest.mark.parametrize(
    ("target", "unsafe_value"),
    [
        ("id", "rewrite-query\ninjected"),
        ("free_text", "https://readonly@example.test/decision"),
        ("free_text", "password=database-secret"),
        ("free_text", "ALTER TABLE customer ADD COLUMN secret text"),
        ("free_text", "SELECT * FROM customer"),
        ("free_text", "x" * 4097),
        ("waiver", "raw production rows were inspected"),
    ],
)
def test_decision_rejects_unsafe_persisted_ids_and_free_text(
    tmp_path: Path,
    target: str,
    unsafe_value: str,
) -> None:
    decision = database_decision()
    if target == "id":
        decision["candidates"][1]["id"] = unsafe_value
        decision["recommended_option_id"] = unsafe_value
        decision["selected_option_id"] = unsafe_value
    elif target == "waiver":
        decision["local_data_test_waiver"] = {
            "reason": unsafe_value,
            "approver": "database-owner",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    else:
        decision["recommendation_rationale"] = unsafe_value
    write_workflow_artifacts(tmp_path, concept="Update the database query")
    write_database_decision(tmp_path, decision)

    with pytest.raises(DatabaseValidationError) as raised:
        load_database_decision(tmp_path)

    assert raised.value.code == "decision_invalid"
