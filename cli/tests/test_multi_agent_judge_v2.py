"""Focused tests for deterministic evidence-aware judge v2 rules."""

import pytest

from awf.core.agent_runner import AgentResult
from awf.core.multi_agent import _record_dispatch_complete_safe, judge


def _result(
    name: str,
    conclusion: str,
    *,
    findings: list[dict] | None = None,
    evidence: list[dict] | None = None,
    confidence: str | float | None = None,
    returncode: int = 0,
    timed_out: bool = False,
    parse_error: bool = False,
) -> AgentResult:
    parsed = {
        "conclusion": conclusion,
        "findings": findings or [],
        "evidence": evidence or [],
    }
    if confidence is not None:
        parsed["confidence"] = confidence
    return AgentResult(
        provider_name=name,
        role="review",
        stdout="{}",
        stderr="",
        returncode=returncode,
        elapsed_sec=0.1,
        timed_out=timed_out,
        parse_error=parse_error,
        parsed=parsed,
    )


def test_omp_dispatch_provenance_failure_is_not_swallowed(tmp_path):
    with pytest.raises(RuntimeError, match="initialized .workflow"):
        _record_dispatch_complete_safe(
            str(tmp_path),
            backend="omp",
            strategy="parallel",
            mode="cross",
            worker_count=0,
            agents=[],
            started_at=0.0,
        )


@pytest.mark.parametrize("severity", ["CRITICAL", "HIGH"])
def test_critical_and_high_findings_remain_fail_closed(severity: str):
    invalid = _result(
        "invalid-fail",
        "FAIL",
        findings=[
            {
                "severity": severity,
                "category": "security",
                "location": "auth.py:12",
            }
        ],
        returncode=1,
        parse_error=True,
    )

    verdict, reason = judge([_result("pass", "PASS"), invalid])

    assert verdict == "FAIL"
    assert reason == "critical finding from invalid-fail (review)"


def test_major_threshold_counts_distinct_deduplicated_findings():
    duplicate_findings = [
        {"severity": "MAJOR", "category": "logic", "location": "svc.py:8"},
        {"severity": "MEDIUM", "category": "logic", "location": "svc.py:8"},
    ]
    assert judge([_result("one", "PASS", findings=duplicate_findings)]) == (
        "PASS",
        "all agents agree",
    )

    distinct_findings = duplicate_findings + [
        {"severity": "MEDIUM", "category": "logic", "location": "svc.py:19"}
    ]
    verdict, reason = judge([_result("one", "PASS", findings=distinct_findings)])

    assert verdict == "FAIL"
    assert reason == "major findings total 2 >= 2 (after dedup)"


def test_grounded_disagreement_fails_with_transparent_score():
    failing = _result(
        "grounded-fail",
        "FAIL",
        findings=[
            {
                "severity": "LOW",
                "category": "regression",
                "location": "tests/test_api.py:41",
                "confidence": "high",
                "evidence": {
                    "command": "pytest tests/test_api.py::test_rejects_invalid_token",
                    "result": "1 failed",
                },
            }
        ],
    )

    verdict, reason = judge([_result("pass", "PASS"), failing])

    assert verdict == "FAIL"
    assert reason.startswith("grounded disagreement: grounded-fail FAIL evidence score 5/5")
    assert "valid=1,confidence=2,evidence=1,reproducible=1" in reason


def test_weak_disagreement_escalates_for_revalidation():
    failing = _result(
        "weak-fail",
        "FAIL",
        findings=[
            {
                "severity": "LOW",
                "category": "style",
                "description": "This might be a problem",
                "confidence": "low",
            }
        ],
    )

    verdict, reason = judge([_result("pass", "PASS"), failing])

    assert verdict == "ESCALATE"
    assert reason.startswith("revalidation_required:")
    assert "scored 1/5" in reason


def test_all_pass_preserves_existing_semantics():
    assert judge([_result("first", "PASS"), _result("second", "PASS")]) == (
        "PASS",
        "all agents agree",
    )


def test_explicit_fail_prefix_is_not_misclassified_as_pass():
    failing = _result("fail", "FAIL: tests did not pass")

    assert judge([failing]) == ("FAIL", "all failing agents agree")


def test_explicit_pass_fail_prefixes_are_a_disagreement():
    verdict, reason = judge(
        [
            _result("pass", "PASS: tests passed"),
            _result("fail", "FAIL: tests did not pass"),
        ]
    )

    assert verdict == "ESCALATE"
    assert "PASS=['pass'], FAIL=['fail']" in reason


def test_single_agent_honors_explicit_fail_conclusion():
    failing = _result(
        "only",
        "FAIL",
        findings=[{"severity": "LOW", "category": "style", "location": "a.py"}],
    )

    assert judge([failing]) == ("FAIL", "all failing agents agree")


def test_high_confidence_without_evidence_still_requires_revalidation():
    failing = _result("confidence-only", "FAIL", confidence="high")

    verdict, reason = judge([_result("pass", "PASS"), failing])

    assert verdict == "ESCALATE"
    assert reason.startswith("revalidation_required:")
    assert "evidence=0,reproducible=0" in reason


def test_pass_plus_unparseable_result_requires_revalidation():
    invalid = AgentResult(
        provider_name="unparseable",
        role="review",
        stdout="not json",
        stderr="",
        returncode=0,
        elapsed_sec=0.1,
        parse_error=True,
    )

    verdict, reason = judge([_result("pass", "PASS"), invalid])

    assert verdict == "ESCALATE"
    assert reason == "revalidation_required: incomplete validation from ['unparseable']"


def test_only_invalid_result_without_conclusion_fails_closed():
    invalid = AgentResult(
        provider_name="timed-out",
        role="review",
        stdout="",
        stderr="timeout",
        returncode=124,
        elapsed_sec=1.0,
        timed_out=True,
    )

    assert judge([invalid]) == ("FAIL", "no valid agent conclusion")


@pytest.mark.parametrize(
    ("returncode", "timed_out", "parse_error"),
    [(0, False, True), (124, True, False)],
)
def test_malformed_or_timed_out_failing_agent_cannot_ground_disagreement(
    returncode: int,
    timed_out: bool,
    parse_error: bool,
):
    invalid = _result(
        "invalid-fail",
        "FAIL",
        findings=[
            {
                "severity": "LOW",
                "category": "regression",
                "location": "src/api.py:22",
                "confidence": "high",
                "evidence": {"command": "pytest tests/test_api.py", "result": "failed"},
            }
        ],
        returncode=returncode,
        timed_out=timed_out,
        parse_error=parse_error,
    )

    verdict, reason = judge([_result("pass", "PASS"), invalid])

    assert verdict == "ESCALATE"
    assert reason.startswith("revalidation_required:")
    assert "valid=0" in reason


def test_rule_order_is_deterministic_and_fail_closed_rules_win_first():
    major = _result(
        "major",
        "FAIL",
        findings=[
            {"severity": "MAJOR", "category": "logic", "location": "a.py:1"},
            {"severity": "MEDIUM", "category": "logic", "location": "b.py:2"},
        ],
    )
    critical = _result(
        "critical",
        "FAIL",
        findings=[
            {"severity": "HIGH", "category": "security", "location": "auth.py:3"}
        ],
    )

    verdict, reason = judge([_result("pass", "PASS"), major, critical])

    assert verdict == "FAIL"
    assert reason == "critical finding from critical (review)"


def test_valid_grounded_failure_outranks_higher_scoring_invalid_failure():
    invalid = _result(
        "invalid-fail",
        "FAIL",
        findings=[
            {
                "severity": "LOW",
                "location": "invalid.py:1",
                "confidence": "high",
            }
        ],
        timed_out=True,
        returncode=124,
    )
    valid = _result(
        "valid-fail",
        "FAIL",
        findings=[{"severity": "LOW", "location": "valid.py:2"}],
    )

    verdict, reason = judge([_result("pass", "PASS"), invalid, valid])

    assert verdict == "FAIL"
    assert reason.startswith("grounded disagreement: valid-fail ")


def test_equal_evidence_scores_choose_first_failing_agent_in_input_order():
    first = _result(
        "first-fail",
        "FAIL",
        findings=[
            {
                "severity": "LOW",
                "category": "logic",
                "location": "first.py:1",
            }
        ],
    )
    second = _result(
        "second-fail",
        "FAIL",
        findings=[
            {
                "severity": "LOW",
                "category": "logic",
                "location": "second.py:1",
            }
        ],
    )

    verdict, reason = judge([_result("pass", "PASS"), first, second])

    assert verdict == "FAIL"
    assert reason.startswith("grounded disagreement: first-fail ")
