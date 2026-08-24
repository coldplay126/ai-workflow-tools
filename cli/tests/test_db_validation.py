"""Focused database workflow signal tests."""
from __future__ import annotations

import json
import sys
from pathlib import Path

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
    allowed_payload = {"planned_files": allowed_files or []}
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


def test_database_signal_unions_expanded_and_legacy_allowed_files(tmp_path: Path) -> None:
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
        reasons=(
            "path:src/database/migrations/001_add_index.sql",
            "path:src/models/fan_log.py",
        ),
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
