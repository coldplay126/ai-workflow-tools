"""Focused database workflow signal tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.db_validation import (
    DatabaseDecision,
    DatabaseSignal,
    DatabaseValidationError,
    detect_database_signal,
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


def test_database_signal_detects_concept_database_term(tmp_path: Path) -> None:
    write_workflow_artifacts(tmp_path, concept="Update the database retention policy")

    signal = detect_database_signal(tmp_path)

    assert signal == DatabaseSignal(detected=True, reasons=("text:database",))


def test_database_signal_normalizes_korean_denormalization(tmp_path: Path) -> None:
    write_workflow_artifacts(tmp_path, tasks="- [ ] 비정규화 모델 검토")

    signal = detect_database_signal(tmp_path)

    assert signal == DatabaseSignal(
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

    assert detect_database_signal(tmp_path) == DatabaseSignal(
        detected=True,
        reasons=("path:src/database/migrations/001_add_index.sql",),
    )


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

    assert detect_database_signal(tmp_path).reasons == (
        "path:src/database/migrations/001_add_index.sql",
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
    assert "path:src/database/migrations/add_fan_log_index.sql" in signal.reasons


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

    assert detect_database_signal(tmp_path) == DatabaseSignal(
        detected=True,
        reasons=(f"artifact_error:{relative_path}:oversize",),
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

    assert detect_database_signal(tmp_path) == DatabaseSignal(
        detected=True,
        reasons=(f"artifact_error:{relative_path}:invalid_utf8",),
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

    assert detect_database_signal(tmp_path) == DatabaseSignal(
        detected=True,
        reasons=(f"artifact_error:.workflow/artifacts/allowed-files.json:{error}",),
    )


def test_database_signal_canonicalizes_windows_and_dot_paths(tmp_path: Path) -> None:
    write_workflow_artifacts(
        tmp_path,
        allowed_files=[r".\src\.\database\migrations\001_add_index.sql"],
    )

    assert detect_database_signal(tmp_path).reasons == (
        "path:src/database/migrations/001_add_index.sql",
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

    assert detect_database_signal(tmp_path) == DatabaseSignal(
        detected=True,
        reasons=("artifact_error:.workflow/artifacts/allowed-files.json:unsafe_path",),
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
    payload: dict[str, object] = {
        "schema_version": 1,
        "status": "selected",
        "change_surfaces": change_surfaces or ["query", "index"],
        "baseline_option_id": "maintain-current",
        "recommended_option_id": "rewrite-query",
        "selected_option_id": selected_option_id,
        "candidates": candidates or [baseline, rewrite],
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
        "equivalence": "pass",
        "integrity": "pass",
        "query_plan": "pass",
        "migration": "not_applicable",
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
    write_workflow_artifacts(tmp_path, concept="Update the database query")
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
    write_workflow_artifacts(tmp_path, concept="Update the database query")
    baseline, alternative = database_decision()["candidates"]
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
        decision=database_decision(change_surfaces=["column"]),
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
        ("plan", "non_utc_checked_at"),
        ("plan", "stale_schema"),
        ("verify", "selected_option"),
        ("verify", "schema_hash"),
        ("test", "selected_option"),
        ("test", "unexpected_field"),
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
    if mutation == "status":
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


def test_no_database_signal_is_not_applicable_without_profile_or_evidence(
    tmp_path: Path,
) -> None:
    write_workflow_artifacts(tmp_path)

    result = run_database_check(tmp_path, "plan")

    assert result.status == "not_applicable"
    assert result.blockers == ()
    assert not evidence_path(tmp_path).exists()
