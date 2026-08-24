"""Focused database workflow signal tests."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.db_validation import DatabaseSignal, detect_database_signal


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
