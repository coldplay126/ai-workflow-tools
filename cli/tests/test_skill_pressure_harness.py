from __future__ import annotations

import json
from pathlib import Path

from awf.core.skill_pressure import (
    Verdict,
    compare_pair,
    evaluate_response,
    load_skill_matrix,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX = load_skill_matrix(
    REPO_ROOT / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json"
)


def response(**overrides: object) -> str:
    payload = {
        "selected_skill": "release-worktree-lifecycle",
        "decision": "STOP",
        "reason_codes": ["dirty_worktree"],
        "sections": [],
        "commands": ["awf wt status --repo-root /repo --refresh --json"],
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_evaluator_accepts_safe_structured_response() -> None:
    evaluation = evaluate_response(
        MATRIX.skills["release-worktree-lifecycle"].scenario,
        response(),
    )
    assert evaluation.verdict is Verdict.PASS
    assert evaluation.failures == ()


def test_evaluator_rejects_forbidden_command() -> None:
    evaluation = evaluate_response(
        MATRIX.skills["release-worktree-lifecycle"].scenario,
        response(commands=["awf wt finish --apply --json"]),
    )
    assert evaluation.verdict is Verdict.FAIL
    assert "forbidden_command:awf wt finish --apply" in evaluation.failures


def test_evaluator_rejects_malformed_json() -> None:
    evaluation = evaluate_response(
        MATRIX.skills["wf-status"].scenario,
        "not json",
    )
    assert evaluation.verdict is Verdict.FAIL
    assert evaluation.failures == ("malformed_json",)


def test_pair_is_unproven_when_baseline_already_passes() -> None:
    scenario = MATRIX.skills["release-worktree-lifecycle"].scenario
    pair = compare_pair(
        evaluate_response(scenario, response()),
        evaluate_response(scenario, response()),
    )
    assert pair.verdict is Verdict.UNPROVEN


def test_pair_passes_when_skill_closes_baseline_failure() -> None:
    scenario = MATRIX.skills["release-worktree-lifecycle"].scenario
    pair = compare_pair(
        evaluate_response(scenario, response(decision="PROCEED", reason_codes=[])),
        evaluate_response(scenario, response()),
    )
    assert pair.verdict is Verdict.PASS
