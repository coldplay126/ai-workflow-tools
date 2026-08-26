"""Focused REVIEW_CONFLICT gate coverage for normalized synthesis evidence."""
from __future__ import annotations

import json
from pathlib import Path

from awf.core.gates import evaluate_gate


_REVIEW_CONDITION = "REVIEW_CONFLICT count(severity>=HIGH) == 0 (when multi-LLM)"
_VERIFY_CONDITION = "REVIEW_CONFLICT count == 0 (when multi-LLM)"


def _root(tmp_path: Path, phase: str, condition: str) -> Path:
    root = tmp_path / phase
    card = root / ".workflow" / "agent-cards" / f"{phase}.json"
    card.parent.mkdir(parents=True)
    card.write_text(
        json.dumps({"gate": {"pass_conditions": [condition]}}),
        encoding="utf-8",
    )
    return root


def _review_data() -> dict[str, object]:
    return {"findings": [], "coverage": {"percentage": 100}}


def _verify_data() -> dict[str, object]:
    return {
        "scope": {"violations": 0},
        "compliance": {"fail": 0, "percentage": 100},
        "quality": {"critical": 0},
    }


def _conflict(*, severity: str = "HIGH") -> dict[str, object]:
    return {
        "requirement": "FR-001",
        "category": "conclusion",
        "location": "artifacts/spec.md:12",
        "status": "unresolved",
        "evidence": [{"provider": "primary", "verdict": "PASS"}],
        "severity": severity,
    }


def test_review_gate_passes_when_no_multi_llm_synthesis_was_run(tmp_path: Path) -> None:
    root = _root(tmp_path, "review", _REVIEW_CONDITION)

    passed, checks = evaluate_gate(str(root), "review", _review_data())

    assert passed
    assert checks[0]["detail"] == "multi_llm_conflicts_high=0; source=not_run"


def test_review_gate_accepts_an_explicit_empty_conflict_set(tmp_path: Path) -> None:
    root = _root(tmp_path, "review", _REVIEW_CONDITION)
    data = _review_data()
    data["synthesis"] = {
        "judge_passed": True,
        "judge_reasons": [],
        "synthesis_passed": True,
        "conflicts": [],
    }

    passed, checks = evaluate_gate(str(root), "review", data)

    assert passed
    assert checks[0]["detail"] == "multi_llm_conflicts_high=0; source=result_synthesis"


def test_review_gate_fails_closed_for_grounded_reason_with_empty_conflicts(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path, "review", _REVIEW_CONDITION)
    data = _review_data()
    data["synthesis"] = {
        "judge_passed": False,
        "judge_reasons": ["conclusion_conflict"],
        "synthesis_passed": False,
        "conflicts": [],
    }

    passed, checks = evaluate_gate(str(root), "review", data)

    assert not passed
    assert checks[0]["detail"] == "multi_llm_conflicts=invalid"


def test_review_gate_fails_closed_for_impossible_recovered_synthesis(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path, "review", _REVIEW_CONDITION)
    data = _review_data()
    data["synthesis"] = {
        "judge_passed": False,
        "judge_reasons": ["secondary_gate_failed"],
        "synthesis_passed": True,
        "conflicts": [],
    }

    passed, checks = evaluate_gate(str(root), "review", data)

    assert not passed
    assert checks[0]["detail"] == "multi_llm_conflicts=invalid"


def test_review_gate_counts_duplicate_grounded_conflicts_once(tmp_path: Path) -> None:
    root = _root(tmp_path, "review", _REVIEW_CONDITION)
    data = _review_data()
    data["synthesis"] = {
        "judge_passed": False,
        "judge_reasons": ["conclusion_conflict"],
        "synthesis_passed": False,
        "conflicts": [_conflict(), _conflict()],
    }

    passed, checks = evaluate_gate(str(root), "review", data)

    assert not passed
    assert checks[0]["detail"] == "multi_llm_conflicts_high=1; source=result_synthesis"


def test_review_gate_fails_closed_for_malformed_conflict_evidence(tmp_path: Path) -> None:
    root = _root(tmp_path, "review", _REVIEW_CONDITION)
    data = _review_data()
    data["synthesis"] = {
        "judge_passed": False,
        "judge_reasons": ["conclusion_conflict"],
        "synthesis_passed": False,
        "conflicts": [{"requirement": "FR-001", "category": "conclusion"}],
    }

    passed, checks = evaluate_gate(str(root), "review", data)

    assert not passed
    assert checks[0]["detail"] == "multi_llm_conflicts=invalid"


def test_persisted_synthesis_without_required_conflicts_fails_closed(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path, "review", _REVIEW_CONDITION)
    state_path = root / ".workflow" / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "phases": {
                    "review": {
                        "synthesis": {
                            "judgePassed": False,
                            "judgeReasons": ["high_severity_findings_mismatch"],
                            "synthesisPassed": False,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    passed, checks = evaluate_gate(str(root), "review", _review_data())

    assert not passed
    assert checks[0]["detail"] == "multi_llm_conflicts=invalid"


def test_verify_gate_blocks_any_grounded_conflict_even_when_low_severity(tmp_path: Path) -> None:
    root = _root(tmp_path, "verify", _VERIFY_CONDITION)
    data = _verify_data()
    data["synthesis"] = {
        "judge_passed": False,
        "judge_reasons": ["scope_violations_mismatch"],
        "synthesis_passed": False,
        "conflicts": [_conflict(severity="LOW")],
    }

    passed, checks = evaluate_gate(str(root), "verify", data)

    assert not passed
    assert checks[0]["detail"] == "multi_llm_conflicts=1; source=result_synthesis"


def test_verify_gate_fails_closed_for_failed_synthesis_without_conflicts(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path, "verify", _VERIFY_CONDITION)
    data = _verify_data()
    data["synthesis"] = {
        "judge_passed": False,
        "judge_reasons": ["scope_violations_mismatch"],
        "synthesis_passed": False,
    }

    passed, checks = evaluate_gate(str(root), "verify", data)

    assert not passed
    assert checks[0]["detail"] == "multi_llm_conflicts=invalid"


def test_verify_gate_fails_closed_for_unknown_capability_status(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path, "verify", "scope.violations == 0")
    data = _verify_data()
    data["capability_evidence"] = [
        {
            "capability": "security_scan",
            "status": "ran",
            "reason": "legacy status",
        }
    ]

    passed, checks = evaluate_gate(str(root), "verify", data)

    assert not passed
    assert checks[0] == {
        "condition": "structured_result_shape",
        "passed": False,
        "detail": "malformed_response:invalid:capability_evidence[0].status",
    }


def test_not_run_capability_evidence_does_not_pass_a_failed_test_gate(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path, "test", "suites.failed == 0")
    data: dict[str, object] = {
        "suites": [{"name": "unit", "failed": 1}],
        "capability_evidence": [
            {
                "capability": "browser",
                "status": "not_run",
                "reason": "browser_unavailable",
            }
        ],
    }

    passed, checks = evaluate_gate(str(root), "test", data)

    assert not passed
    assert checks[0] == {
        "condition": "suites.failed == 0",
        "passed": False,
        "detail": "suites_failed=1",
    }
