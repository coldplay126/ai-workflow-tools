from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from awf.core.skill_pressure import (
    HIGH_RISK_SKILLS,
    REQUIRED_CATEGORIES,
    SUPPORTED_RUNTIMES,
    MatrixError,
    load_skill_matrix,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX_PATH = REPO_ROOT / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json"
SKILLS_ROOT = REPO_ROOT / "claude" / "skills"
EXPECTED_SKILLS = {
    "analysis",
    "multi-agent",
    "phase-approve",
    "phase-done",
    "phase-impl",
    "phase-plan",
    "phase-review",
    "phase-test",
    "phase-verify",
    "release-worktree-lifecycle",
    "wf",
    "wf-discovery",
    "wf-orchestrator",
    "wf-reset",
    "wf-status",
}


def test_matrix_locks_exact_first_party_skill_inventory() -> None:
    matrix = load_skill_matrix(MATRIX_PATH)
    source_names = {
        path.parent.name for path in SKILLS_ROOT.glob("*/SKILL.md")
    }

    assert set(matrix.skills) == EXPECTED_SKILLS
    assert source_names == EXPECTED_SKILLS
    assert {name for name, case in matrix.skills.items() if case.high_risk} == HIGH_RISK_SKILLS
    assert "chat" not in matrix.skills


def test_every_skill_declares_all_categories_and_runtimes() -> None:
    matrix = load_skill_matrix(MATRIX_PATH)

    for case in matrix.skills.values():
        assert set(case.categories) == REQUIRED_CATEGORIES
        assert set(case.runtimes) == SUPPORTED_RUNTIMES
        assert case.severity in {"critical", "important", "minor"}
        assert case.scenario.skill == case.name


def test_matrix_rejects_an_unknown_verdict_category(tmp_path: Path) -> None:
    path = tmp_path / "matrix.json"
    path.write_text(
        '{"schema":"awf_skill_validation_matrix_v1","skills":['
        '{"name":"x","type":"test","entry_kind":"conditions",'
        '"high_risk":false,"categories":["unknown"],'
        '"runtimes":["claude","agent-skills","omp"],'
        '"scenario":{"id":"x.test","skill":"x","task":"x",'
        '"expected":{"decisions":["REPORT"]}}}]}'
    )

    with pytest.raises(MatrixError, match="categories"):
        load_skill_matrix(path)
