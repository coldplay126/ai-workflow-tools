from __future__ import annotations

from typing import Any

from awf.core.analysis_outputs import analyze_stage2_output
from awf.core.gates import evaluate_gate
from awf.core.workflow_results import load_result_json

JUDGE_REASON_MESSAGES = {
    "primary_gate_failed": "primary provider result did not satisfy the phase gate",
    "secondary_gate_failed": "secondary provider result did not satisfy the phase gate",
    "conclusion_conflict": "providers reached different PASS/FAIL conclusions",
    "critical_finding_present": "at least one provider reported a CRITICAL finding",
    "high_count_mismatch": "providers reported different numbers of HIGH findings",
    "high_severity_findings_mismatch": "providers disagreed on the high-severity finding set",
    "scope_violation_present": "at least one provider reported scope violations",
    "compliance_fail_present": "at least one provider reported failed requirements",
    "quality_critical_present": "at least one provider reported critical quality issues",
    "scope_violations_mismatch": "providers disagreed on scope violation counts",
    "compliance_fail_mismatch": "providers disagreed on failed requirement counts",
    "quality_critical_mismatch": "providers disagreed on critical quality issue counts",
    "primary_missing_required_outputs": "primary provider did not produce the full required Stage 2 file set",
    "secondary_missing_required_outputs": "secondary provider did not produce the full required Stage 2 file set",
    "required_output_set_mismatch": "providers produced different required Stage 2 file sets",
    "extra_output_set_mismatch": "providers produced different extra Stage 2 files",
    "secondary_provider_failed": "secondary provider execution failed before cross-check completion",
    "secondary_selected_after_primary_gate_failure": "secondary provider result was selected because the primary result failed the gate",
    "secondary_selected_after_primary_missing_outputs": "secondary Stage 2 output was selected because the primary output missed required files",
    "secondary_selected_higher_coverage": "secondary provider result was selected because it achieved better review coverage while still passing",
    "secondary_selected_higher_compliance": "secondary provider result was selected because it achieved better verify compliance while still passing",
    "secondary_selected_cleaner_output_set": "secondary Stage 2 output was selected because it produced a cleaner required file set",
    "primary_retained_higher_or_equal_coverage": "primary provider result was retained because its review coverage was at least as strong",
    "primary_retained_higher_or_equal_compliance": "primary provider result was retained because its verify compliance was at least as strong",
    "primary_retained_cleaner_output_set": "primary Stage 2 output was retained because it produced a cleaner required file set",
    "primary_retained_after_consensus": "primary provider result was retained because both providers agreed closely enough",
    "primary_retained_due_to_conflict": "primary provider result was retained but the cross-check detected a blocking conflict",
}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _normalized_finding_locations(item: dict[str, Any]) -> str:
    values = item.get("locations")
    if not isinstance(values, list):
        values = [item.get("location", "")]
    normalized = {
        str(value).strip().replace("\\", "/")
        for value in values
        if str(value).strip()
    }
    return "|".join(sorted(normalized))


def _finding_signature(item: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(item.get("severity", "")),
        str(item.get("category", "")),
        _normalized_finding_locations(item),
        str(item.get("description") or item.get("summary") or ""),
    )


def _conclusion_status(data: dict[str, Any]) -> str:
    conclusion = str(data.get("conclusion", "")).strip().upper()
    if conclusion.startswith("PASS"):
        return "PASS"
    if conclusion.startswith("FAIL"):
        return "FAIL"
    return ""


def _review_severity_counts(data: dict[str, Any]) -> dict[str, int]:
    findings = _as_list(data.get("findings"))
    return {
        "CRITICAL": sum(1 for item in findings if item.get("severity") == "CRITICAL"),
        "HIGH": sum(1 for item in findings if item.get("severity") == "HIGH"),
    }


def _review_coverage_percentage(data: dict[str, Any]) -> float:
    coverage = data.get("coverage", {})
    if not isinstance(coverage, dict):
        return 0.0
    return float(coverage.get("percentage", 0) or 0)


def _verify_compliance_percentage(data: dict[str, Any]) -> float:
    compliance = data.get("compliance", {})
    if not isinstance(compliance, dict):
        return 0.0
    return float(compliance.get("percentage", 0) or 0)


def judge_workflow_multi_provider_results(
    explicit_root: str | None,
    phase: str,
    primary_result_path: str,
    secondary_result_path: str,
    change_class: str = "standard",
) -> tuple[bool, list[str]]:
    primary = load_result_json(primary_result_path)
    secondary = load_result_json(secondary_result_path)
    primary_gate_passed, _ = evaluate_gate(explicit_root, phase, primary, change_class=change_class)
    secondary_gate_passed, _ = evaluate_gate(explicit_root, phase, secondary, change_class=change_class)

    reasons: list[str] = []
    if not primary_gate_passed:
        reasons.append("primary_gate_failed")
    if not secondary_gate_passed:
        reasons.append("secondary_gate_failed")
    primary_conclusion = _conclusion_status(primary)
    secondary_conclusion = _conclusion_status(secondary)
    if primary_conclusion and secondary_conclusion and primary_conclusion != secondary_conclusion:
        reasons.append("conclusion_conflict")

    if phase == "review":
        primary_counts = _review_severity_counts(primary)
        secondary_counts = _review_severity_counts(secondary)
        if primary_counts["CRITICAL"] > 0 or secondary_counts["CRITICAL"] > 0:
            reasons.append("critical_finding_present")
        if primary_counts["HIGH"] != secondary_counts["HIGH"]:
            reasons.append("high_count_mismatch")
        primary_findings = {
            _finding_signature(item) for item in _as_list(primary.get("findings")) if item.get("severity") in {"CRITICAL", "HIGH"}
        }
        secondary_findings = {
            _finding_signature(item) for item in _as_list(secondary.get("findings")) if item.get("severity") in {"CRITICAL", "HIGH"}
        }
        if primary_findings != secondary_findings:
            reasons.append("high_severity_findings_mismatch")
    elif phase == "verify":
        primary_scope = primary.get("scope", {})
        secondary_scope = secondary.get("scope", {})
        primary_compliance = primary.get("compliance", {})
        secondary_compliance = secondary.get("compliance", {})
        primary_quality = primary.get("quality", {})
        secondary_quality = secondary.get("quality", {})
        if int(primary_scope.get("violations", 0) or 0) > 0 or int(secondary_scope.get("violations", 0) or 0) > 0:
            reasons.append("scope_violation_present")
        if int(primary_compliance.get("fail", 0) or 0) > 0 or int(secondary_compliance.get("fail", 0) or 0) > 0:
            reasons.append("compliance_fail_present")
        if int(primary_quality.get("critical", 0) or 0) > 0 or int(secondary_quality.get("critical", 0) or 0) > 0:
            reasons.append("quality_critical_present")
        if int(primary_scope.get("violations", 0) or 0) != int(secondary_scope.get("violations", 0) or 0):
            reasons.append("scope_violations_mismatch")
        if int(primary_compliance.get("fail", 0) or 0) != int(secondary_compliance.get("fail", 0) or 0):
            reasons.append("compliance_fail_mismatch")
        if int(primary_quality.get("critical", 0) or 0) != int(secondary_quality.get("critical", 0) or 0):
            reasons.append("quality_critical_mismatch")

    return not reasons, reasons


def synthesize_workflow_multi_provider_results(
    explicit_root: str | None,
    phase: str,
    primary_result_path: str,
    secondary_result_path: str,
    *,
    primary_provider: str = "primary",
    secondary_provider: str = "secondary",
    change_class: str = "standard",
) -> dict[str, Any]:
    primary = load_result_json(primary_result_path)
    secondary = load_result_json(secondary_result_path)
    primary_gate_passed, _ = evaluate_gate(explicit_root, phase, primary, change_class=change_class)
    secondary_gate_passed, _ = evaluate_gate(explicit_root, phase, secondary, change_class=change_class)
    judge_passed, judge_reasons = judge_workflow_multi_provider_results(
        explicit_root,
        phase,
        primary_result_path,
        secondary_result_path,
        change_class=change_class,
    )

    selected_provider = primary_provider
    selected_result_path = primary_result_path
    synthesis_reasons: list[str] = []
    final_passed = judge_passed
    selection_summary = ""

    if not primary_gate_passed and secondary_gate_passed:
        selected_provider = secondary_provider
        selected_result_path = secondary_result_path
        synthesis_reasons.append("secondary_selected_after_primary_gate_failure")
        final_passed = True
        selection_summary = "primary gate failed, secondary gate passed"
    elif primary_gate_passed and secondary_gate_passed and judge_passed:
        if phase == "review":
            primary_coverage = _review_coverage_percentage(primary)
            secondary_coverage = _review_coverage_percentage(secondary)
            if secondary_coverage > primary_coverage:
                selected_provider = secondary_provider
                selected_result_path = secondary_result_path
                synthesis_reasons.append("secondary_selected_higher_coverage")
                selection_summary = f"secondary coverage {secondary_coverage:.0f}% > primary {primary_coverage:.0f}%"
            else:
                synthesis_reasons.append("primary_retained_higher_or_equal_coverage")
                selection_summary = f"primary coverage {primary_coverage:.0f}% >= secondary {secondary_coverage:.0f}%"
        elif phase == "verify":
            primary_compliance = _verify_compliance_percentage(primary)
            secondary_compliance = _verify_compliance_percentage(secondary)
            if secondary_compliance > primary_compliance:
                selected_provider = secondary_provider
                selected_result_path = secondary_result_path
                synthesis_reasons.append("secondary_selected_higher_compliance")
                selection_summary = f"secondary compliance {secondary_compliance:.0f}% > primary {primary_compliance:.0f}%"
            else:
                synthesis_reasons.append("primary_retained_higher_or_equal_compliance")
                selection_summary = f"primary compliance {primary_compliance:.0f}% >= secondary {secondary_compliance:.0f}%"
        else:
            synthesis_reasons.append("primary_retained_after_consensus")
            selection_summary = "primary retained after consensus"
    elif judge_passed:
        synthesis_reasons.append("primary_retained_after_consensus")
        selection_summary = "primary retained after consensus"
    else:
        synthesis_reasons.append("primary_retained_due_to_conflict")
        selection_summary = "primary retained because synthesis could not safely recover the conflict"

    return {
        "primary_gate_passed": primary_gate_passed,
        "secondary_gate_passed": secondary_gate_passed,
        "judge_passed": judge_passed,
        "judge_reasons": judge_reasons,
        "selected_provider": selected_provider,
        "selected_result_path": selected_result_path,
        "final_passed": final_passed,
        "synthesis_reasons": synthesis_reasons,
        "selection_summary": selection_summary,
    }


def cross_secondary_candidates(provider_name: str) -> list[str]:
    if provider_name == "fixture":
        return ["fixture"]
    if provider_name == "claude-code":
        return ["codex", "claude-sdk", "openai"]
    if provider_name == "codex":
        return ["claude-code", "claude-sdk", "openai"]
    if provider_name == "claude-sdk":
        return ["claude-code", "codex", "openai"]
    if provider_name == "openai":
        return ["claude-code", "codex", "claude-sdk"]
    return ["claude-code", "codex", "claude-sdk", "openai"]


def judge_cross_stage2(primary_output: str, secondary_output: str) -> tuple[bool, list[str]]:
    primary_summary = analyze_stage2_output(primary_output)
    secondary_summary = analyze_stage2_output(secondary_output)
    reasons: list[str] = []
    if not primary_summary["has_required_set"]:
        reasons.append("primary_missing_required_outputs")
    if not secondary_summary["has_required_set"]:
        reasons.append("secondary_missing_required_outputs")
    if set(primary_summary["written_files"]) != set(secondary_summary["written_files"]):
        reasons.append("required_output_set_mismatch")
    if set(primary_summary["extra_files"]) != set(secondary_summary["extra_files"]):
        reasons.append("extra_output_set_mismatch")
    return not reasons, reasons


def synthesize_cross_stage2(
    primary_output: str,
    secondary_output: str,
    *,
    primary_provider: str = "primary",
    secondary_provider: str = "secondary",
    secondary_failed: bool = False,
) -> dict[str, Any]:
    primary_summary = analyze_stage2_output(primary_output)
    secondary_summary = analyze_stage2_output(secondary_output)
    if secondary_failed:
        judge_passed = False
        judge_reasons = ["secondary_provider_failed"]
    else:
        judge_passed, judge_reasons = judge_cross_stage2(primary_output, secondary_output)

    selected_provider = primary_provider
    selected_output = primary_output
    synthesis_reasons: list[str] = []
    final_passed = judge_passed
    primary_extra_count = len(primary_summary["extra_files"])
    secondary_extra_count = len(secondary_summary["extra_files"])
    selection_summary = ""

    if not primary_summary["has_required_set"] and secondary_summary["has_required_set"] and not secondary_failed:
        selected_provider = secondary_provider
        selected_output = secondary_output
        synthesis_reasons.append("secondary_selected_after_primary_missing_outputs")
        final_passed = True
        selection_summary = "primary missed required outputs, secondary produced the full required set"
    elif primary_summary["has_required_set"] and secondary_summary["has_required_set"] and not secondary_failed:
        recoverable_extra_only = set(judge_reasons).issubset({"extra_output_set_mismatch"})
        if judge_passed or recoverable_extra_only:
            final_passed = True
            if secondary_extra_count < primary_extra_count:
                selected_provider = secondary_provider
                selected_output = secondary_output
                synthesis_reasons.append("secondary_selected_cleaner_output_set")
                selection_summary = f"secondary extra files {secondary_extra_count} < primary {primary_extra_count}"
            elif primary_extra_count <= secondary_extra_count:
                synthesis_reasons.append("primary_retained_cleaner_output_set" if recoverable_extra_only else "primary_retained_after_consensus")
                selection_summary = f"primary extra files {primary_extra_count} <= secondary {secondary_extra_count}"
            else:
                synthesis_reasons.append("primary_retained_after_consensus")
                selection_summary = "primary retained after consensus"
        else:
            synthesis_reasons.append("primary_retained_due_to_conflict")
            selection_summary = "primary retained because cross-check found a non-recoverable conflict"
    elif judge_passed:
        synthesis_reasons.append("primary_retained_after_consensus")
        selection_summary = "primary retained after consensus"
    else:
        synthesis_reasons.append("primary_retained_due_to_conflict")
        selection_summary = "primary retained because cross-check found a non-recoverable conflict"

    return {
        "primary_summary": primary_summary,
        "secondary_summary": secondary_summary,
        "judge_passed": judge_passed,
        "judge_reasons": judge_reasons,
        "selected_provider": selected_provider,
        "selected_output": selected_output,
        "final_passed": final_passed,
        "synthesis_reasons": synthesis_reasons,
        "selection_summary": selection_summary,
    }


def explain_judge_reasons(reasons: list[str]) -> list[str]:
    return [JUDGE_REASON_MESSAGES.get(reason, reason.replace("_", " ")) for reason in reasons]
