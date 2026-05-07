from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Tuple

from awf.core.gates import evaluate_gate
from awf.core.state import apply_gate_result, resolve_repo_root
from awf.core.workflow_envelope import normalize_worker_result
from awf.core.workflow_loop import record_phase_escape


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _recommendation_text(finding: dict[str, Any]) -> str:
    return str(finding.get("recommendation", "") or finding.get("suggestion", "") or "-")


def _parse_result_json(path: str) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"Unable to locate JSON object in {path}")
        return json.loads(raw[start : end + 1])


def load_result_envelope(path: str, *, phase: str, provider: str) -> dict[str, Any]:
    return normalize_worker_result(_parse_result_json(path), phase=phase, provider=provider)


def load_result_json(path: str) -> dict[str, Any]:
    payload = _parse_result_json(path)
    if isinstance(payload, dict) and "status" in payload and "result" in payload:
        normalized = normalize_worker_result(
            payload,
            phase=str(payload.get("phase", "unknown") or "unknown"),
            provider=str(payload.get("provider", "unknown") or "unknown"),
        )
        return dict(normalized.get("result", {}))
    return payload


def _raw_result_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _render_escape_report(
    phase: str,
    reason: str,
    severity: str,
    recommended_action: str,
    escape_data: dict,
    raw_text: str,
) -> str:
    summary = str(escape_data.get("summary", "") or "No summary provided")
    evidence = _as_list(escape_data.get("evidence"))
    affected = _as_list(escape_data.get("affected_files"))
    lines = [
        f"# {'Review' if phase == 'review' else 'Verification'} Report — Worker Escaped",
        "",
        "## Summary",
        f"- Status: ESCAPED",
        f"- Severity: {severity}",
        f"- Reason: {reason}",
        f"- Recommended Action: {recommended_action or 'none'}",
        f"- Description: {summary}",
        "",
    ]
    if affected:
        lines.extend(["## Affected Files"] + [f"- {f}" for f in affected] + [""])
    if evidence:
        lines.extend(["## Evidence"])
        for e in evidence:
            lines.append(f"- [{e.get('kind', 'note')}] {e.get('value', str(e))}")
        lines.append("")
    raw_excerpt = raw_text[:8000]
    lines.extend(["## Raw Result Excerpt", "```text", raw_excerpt.rstrip(), "```", ""])
    return "\n".join(lines)


def _render_malformed_report(phase: str, reason: str, raw_text: str) -> str:
    raw_excerpt = raw_text[:12000]
    return "\n".join(
        [
            f"# {'Review' if phase == 'review' else 'Verification'} Report",
            "",
            "## Summary",
            f"- Conclusion: FAIL",
            f"- Reason: {reason}",
            "",
            "## Gate Checks",
            f"- FAIL: structured_result_shape ({reason})",
            "",
            "## Raw Result Excerpt",
            "```text",
            raw_excerpt.rstrip(),
            "```",
            "",
        ]
    )


def _render_synthesis_summary(synthesis_summary: Optional[dict[str, Any]]) -> list[str]:
    if not synthesis_summary:
        return []
    return [
        "",
        "## Synthesis",
        f"- Selected Provider: {synthesis_summary.get('selected_provider', 'N/A')}",
        f"- Secondary Provider: {synthesis_summary.get('secondary_provider') or 'None'}",
        f"- Judge: {'PASS' if synthesis_summary.get('judge_passed') else 'FAIL'}",
        f"- Synthesis: {'PASS' if synthesis_summary.get('synthesis_passed') else 'FAIL'}",
        f"- Judge Reasons: {', '.join(_as_list(synthesis_summary.get('judge_reasons'))) or 'None'}",
        f"- Synthesis Reasons: {', '.join(_as_list(synthesis_summary.get('synthesis_reasons'))) or 'None'}",
        f"- Selection Basis: {synthesis_summary.get('selection_summary') or 'None'}",
    ]


def render_review_report(
    data: dict[str, Any],
    gate_passed: bool,
    gate_checks: list[dict[str, Any]],
    synthesis_summary: Optional[dict[str, Any]] = None,
) -> tuple[str, bool]:
    findings = _as_list(data.get("findings"))
    coverage = data.get("coverage", {})
    critical = len([item for item in findings if item.get("severity") == "CRITICAL"])
    high = len([item for item in findings if item.get("severity") == "HIGH"])
    medium = len([item for item in findings if item.get("severity") == "MEDIUM"])
    low = len([item for item in findings if item.get("severity") == "LOW"])

    lines = [
        "# Review Report",
        "",
        "## Summary",
        f"- Conclusion: {data.get('conclusion') or ('PASS' if gate_passed else 'FAIL')}",
        f"- Coverage: {coverage.get('percentage', 'N/A')}%",
        f"- CRITICAL: {critical} | HIGH: {high} | MEDIUM: {medium} | LOW: {low}",
        f"- Gate G2: {'PASS' if gate_passed else 'FAIL'}",
    ]
    lines.extend(_render_synthesis_summary(synthesis_summary))
    lines.extend(["", "## Findings"])

    if not findings:
        lines.extend(["", "No findings reported."])
    else:
        lines.extend(["", "| ID | Category | Severity | Location | Summary | Recommendation |", "|----|----------|----------|----------|---------|----------------|"])
        for finding in findings:
            locations = "<br>".join(_as_list(finding.get("locations"))) or "-"
            lines.append(
                f"| {finding.get('id', '-')} | {finding.get('category', '-')} | "
                f"{finding.get('severity', '-')} | {locations} | "
                f"{finding.get('summary', '-')} | {_recommendation_text(finding)} |"
            )

    lines.extend(
        [
            "",
            "## Metrics",
            f"- Total Requirements: {coverage.get('total_requirements', 'N/A')}",
            f"- Mapped Requirements: {coverage.get('mapped_requirements', 'N/A')}",
            f"- Coverage %: {coverage.get('percentage', 'N/A')}",
            f"- Critical Issues: {critical}",
            "",
            "## Gate Checks",
        ]
    )
    lines.extend([f"- {'PASS' if item['passed'] else 'FAIL'}: {item['condition']} ({item['detail']})" for item in gate_checks] or ["- None"])

    lines.extend(["", "## Evidence"])
    evidence = _as_list(data.get("evidence"))
    lines.append("\n".join(f"- {item.get('id', 'evidence')}: {item.get('detail', item)}" for item in evidence) or "- None")

    lines.extend(["", "## Risks"])
    risks = _as_list(data.get("risks"))
    lines.append("\n".join(f"- {item.get('id', 'risk')} [{item.get('severity', 'N/A')}]: {item.get('detail', item)}" for item in risks) or "- None")

    lines.extend(["", "## Action Items"])
    actions = _as_list(data.get("action_items"))
    lines.append("\n".join(f"- {item.get('id', 'action')}: {item.get('action', item)}" for item in actions) or "- None")
    lines.append("")
    return "\n".join(lines), gate_passed


def render_verify_report(
    data: dict[str, Any],
    gate_passed: bool,
    gate_checks: list[dict[str, Any]],
    synthesis_summary: Optional[dict[str, Any]] = None,
) -> tuple[str, bool]:
    scope = data.get("scope", {})
    compliance = data.get("compliance", {})
    quality = data.get("quality", {})

    lines = [
        "# Verification Report",
        "",
        "## Summary",
        f"- Conclusion: {data.get('conclusion') or ('PASS' if gate_passed else 'FAIL')}",
        f"- Scope: {scope.get('changed_files', 'N/A')} changed / {scope.get('planned_files', 'N/A')} planned, {scope.get('violations', 'N/A')} violations",
        f"- Compliance: {compliance.get('pass', 'N/A')}/{compliance.get('total_requirements', 'N/A')} ({compliance.get('percentage', 'N/A')}%)",
        f"- Quality: critical={quality.get('critical', 'N/A')}, high={quality.get('high', 'N/A')}, medium={quality.get('medium', 'N/A')}, low={quality.get('low', 'N/A')}",
        f"- Gate G5: {'PASS' if gate_passed else 'FAIL'}",
    ]
    lines.extend(_render_synthesis_summary(synthesis_summary))
    lines.extend([
        "",
        "## Scope",
        f"- Changed files: {scope.get('changed_files', 'N/A')}",
        f"- Planned files: {scope.get('planned_files', 'N/A')}",
        f"- Violations: {scope.get('violations', 'N/A')}",
        f"- Violation files: {', '.join(_as_list(scope.get('violation_files'))) or 'None'}",
        "",
        "## Compliance",
        f"- Total requirements: {compliance.get('total_requirements', 'N/A')}",
        f"- Pass: {compliance.get('pass', 'N/A')}",
        f"- Warn: {compliance.get('warn', 'N/A')}",
        f"- Fail: {compliance.get('fail', 'N/A')}",
        f"- Failed requirements: {', '.join(_as_list(compliance.get('failed_requirements'))) or 'None'}",
        "",
        "## Quality Issues",
    ])

    issues = _as_list(quality.get("issues"))
    if not issues:
        lines.extend(["", "No quality issues reported."])
    else:
        lines.extend(["", "| ID | Severity | File | Summary |", "|----|----------|------|---------|"])
        for issue in issues:
            lines.append(
                f"| {issue.get('id', '-')} | {issue.get('severity', '-')} | "
                f"{issue.get('file', '-')} | {issue.get('summary', '-')} |"
            )

    lines.extend(["", "## Gate Checks"])
    lines.extend([f"- {'PASS' if item['passed'] else 'FAIL'}: {item['condition']} ({item['detail']})" for item in gate_checks] or ["- None"])

    lines.extend(["", "## Evidence"])
    evidence = _as_list(data.get("evidence"))
    lines.append("\n".join(f"- {item.get('id', 'evidence')}: {item.get('detail', item)}" for item in evidence) or "- None")

    lines.extend(["", "## Risks"])
    risks = _as_list(data.get("risks"))
    lines.append("\n".join(f"- {item.get('id', 'risk')} [{item.get('severity', 'N/A')}]: {item.get('detail', item)}" for item in risks) or "- None")

    lines.extend(["", "## Action Items"])
    actions = _as_list(data.get("action_items"))
    lines.append("\n".join(f"- {item.get('id', 'action')}: {item.get('action', item)}" for item in actions) or "- None")
    lines.append("")
    return "\n".join(lines), gate_passed


def apply_workflow_result(
    explicit_root: Optional[str],
    phase: str,
    result_path: str,
    synthesis_summary: Optional[dict[str, Any]] = None,
    skip_gate_apply: bool = False,
    change_class: str = "standard",
) -> Tuple[Path, bool]:
    root = resolve_repo_root(explicit_root)
    wf_dir = root / ".workflow"
    if phase == "review":
        output_path = wf_dir / "artifacts" / "review-report.md"
    elif phase == "verify":
        output_path = wf_dir / "artifacts" / "verification-report.md"
    else:
        raise ValueError(f"apply-result currently supports review/verify only: {phase}")

    try:
        envelope = load_result_envelope(result_path, phase=phase, provider="unknown")
        status = envelope.get("status")

        # --- Handle escaped/failed envelopes properly ---
        if status == "escaped":
            escape_data = envelope.get("escape") or {}
            record_phase_escape(
                explicit_root,
                phase,
                provider=str(envelope.get("provider", "unknown") or "unknown"),
                escape=escape_data,
            )
            recommended = str(escape_data.get("recommended_action", "") or "")
            reason = str(escape_data.get("reason", "") or "unknown")
            severity = str(escape_data.get("severity", "") or "unknown")
            markdown = _render_escape_report(phase, reason, severity, recommended, escape_data, _raw_result_text(result_path))
            output_path.write_text(markdown, encoding="utf-8")
            return output_path, False

        if status == "failed":
            markdown = _render_malformed_report(phase, f"worker_returned_failed", _raw_result_text(result_path))
            output_path.write_text(markdown, encoding="utf-8")
            apply_gate_result(explicit_root, phase, False)
            return output_path, False

        if status != "completed":
            raise ValueError(f"worker_result_status:{status}")

        data = dict(envelope.get("result", {}))
        gate_passed, gate_checks = evaluate_gate(explicit_root, phase, data, change_class=change_class)

        # --- Build failure context for on_fail routing ---
        failure_context = None
        if not gate_passed:
            findings = _as_list(data.get("findings"))
            failure_context = {
                "has_critical": any(f.get("severity") == "CRITICAL" for f in findings),
                "high_count": len([f for f in findings if f.get("severity") == "HIGH"]),
            }

        malformed = any(
            (item.get("condition") == "structured_result_shape" and not item.get("passed", False))
            for item in gate_checks
        )
        if malformed:
            markdown = _render_malformed_report(
                phase,
                next((str(item.get("detail", "malformed_response")) for item in gate_checks if item.get("condition") == "structured_result_shape"), "malformed_response"),
                _raw_result_text(result_path),
            )
            passed = False
        elif phase == "review":
            markdown, passed = render_review_report(data, gate_passed, gate_checks, synthesis_summary=synthesis_summary)
        else:
            markdown, passed = render_verify_report(data, gate_passed, gate_checks, synthesis_summary=synthesis_summary)
    except Exception as exc:
        markdown = _render_malformed_report(phase, f"invalid_json:{exc}", _raw_result_text(result_path))
        passed = False
        failure_context = None

    output_path.write_text(markdown, encoding="utf-8")
    if not skip_gate_apply:
        apply_gate_result(explicit_root, phase, passed, failure_context=failure_context)
    return output_path, passed
