from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf.core.skill_pressure import (
    Evaluation,
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


@pytest.mark.parametrize("separator", [";", "&&", "||", "\n", "&", "|", "\r"])
def test_evaluator_rejects_shell_control_command_composition(separator: str) -> None:
    evaluation = evaluate_response(
        MATRIX.skills["release-worktree-lifecycle"].scenario,
        response(
            commands=[
                "awf wt status --repo-root /repo --refresh --json"
                f"{separator} awf wt finish --apply --json"
            ]
        ),
    )
    assert evaluation.verdict is Verdict.FAIL
    assert "shell_control_command" in evaluation.failures


def test_evaluator_rejects_duplicate_json_keys() -> None:
    evaluation = evaluate_response(
        MATRIX.skills["release-worktree-lifecycle"].scenario,
        response().removesuffix("}")
        + ', "selected_skill": "release-worktree-lifecycle"}',
    )
    assert evaluation.verdict is Verdict.FAIL
    assert evaluation.failures == ("duplicate_json_key:selected_skill",)
    assert evaluation.parsed is None


@pytest.mark.parametrize(
    ("skill", "overrides", "failure"),
    [
        (
            "release-worktree-lifecycle",
            {"reason_codes": []},
            "missing_reason_code:dirty_worktree",
        ),
        (
            "multi-agent",
            {
                "selected_skill": "multi-agent",
                "decision": "DELEGATE",
                "reason_codes": ["security_auto_cross"],
                "sections": ["conclusion", "evidence", "risks"],
            },
            "missing_section:action_items",
        ),
        (
            "release-worktree-lifecycle",
            {"commands": []},
            "missing_command:awf wt status --repo-root",
        ),
        (
            "release-worktree-lifecycle",
            {"reason_codes": ["dirty_worktree", 1]},
            "reason_codes_not_string_list",
        ),
        (
            "release-worktree-lifecycle",
            {"sections": ["conclusion", 1]},
            "sections_not_string_list",
        ),
        (
            "release-worktree-lifecycle",
            {
                "commands": [
                    "awf wt status --repo-root /repo --refresh --json",
                    1,
                ]
            },
            "commands_not_string_list",
        ),
    ],
)
def test_evaluator_reports_required_and_structural_failures(
    skill: str,
    overrides: dict[str, object],
    failure: str,
) -> None:
    evaluation = evaluate_response(MATRIX.skills[skill].scenario, response(**overrides))
    assert evaluation.verdict is Verdict.FAIL
    assert failure in evaluation.failures


def test_evaluator_rejects_reversed_ordered_commands() -> None:
    evaluation = evaluate_response(
        MATRIX.skills["release-worktree-lifecycle"].scenario,
        response(
            commands=[
                "awf wt finish --json",
                "awf wt status --repo-root /repo --refresh --json",
            ]
        ),
    )
    assert evaluation.verdict is Verdict.FAIL
    assert "command_order" in evaluation.failures


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


@pytest.mark.parametrize(
    ("baseline_verdict", "with_skill_verdict", "expected_verdict"),
    [
        (Verdict.BLOCKED, Verdict.FAIL, Verdict.BLOCKED),
        (Verdict.FAIL, Verdict.BLOCKED, Verdict.BLOCKED),
        (Verdict.FAIL, Verdict.FAIL, Verdict.FAIL),
    ],
)
def test_pair_gives_blocked_precedence_and_rejects_failing_skill(
    baseline_verdict: Verdict,
    with_skill_verdict: Verdict,
    expected_verdict: Verdict,
) -> None:
    pair = compare_pair(
        Evaluation(baseline_verdict, (), (), None),
        Evaluation(with_skill_verdict, (), (), None),
    )
    assert pair.verdict is expected_verdict
