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


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _recommendation_text(finding: dict[str, Any]) -> str:
    return str(finding.get("recommendation", "") or finding.get("suggestion", "") or "-")


def _is_stream_result_event(payload: Any) -> bool:
    """Recognise a claude-code stream-json result event."""
    return (
        isinstance(payload, dict)
        and payload.get("type") == "result"
        and isinstance(payload.get("result"), str)
    )


def _unwrap_stream_result_payload(result_text: str) -> dict[str, Any] | None:
    """Extract the worker envelope embedded in a stream-json result event's text."""
    stripped = result_text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    inner_start = stripped.find("{")
    inner_end = stripped.rfind("}")
    if inner_start == -1 or inner_end <= inner_start:
        return None
    try:
        parsed = json.loads(stripped[inner_start : inner_end + 1])
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        return None
    return None


def _parse_result_json(path: str) -> dict[str, Any]:
    """Parse a worker result file into a single dict envelope.

    Supports three formats:
    1. A single JSON document (legacy path).
    2. Claude Code stream-json output (one JSON event per line, ending with
       a `{"type": "result", "result": "<text>", ...}` envelope). When the
       worker ran with `--json-schema`, the `result` field is itself a JSON
       document — we unwrap one level. The same unwrapping applies when the
       whole file happens to be a single result-event JSON document.
    3. JSON object embedded in surrounding prose (extract by first `{` /
       last `}`); kept as a last-resort fallback.

    §1.3 fix: the verify executor runs claude with
    `--output-format stream-json --include-partial-messages`, so the result
    file is multi-line line-delimited JSON. Previously json.loads on the
    whole file raised `Extra data` and the substring fallback merged events
    into invalid JSON; the gate evaluator then marked PASS results as FAIL.
    """
    raw = Path(path).read_text(encoding="utf-8").strip()
    try:
        outer = json.loads(raw)
        if _is_stream_result_event(outer):
            unwrapped = _unwrap_stream_result_payload(outer["result"])
            if unwrapped is not None:
                return unwrapped
        return outer
    except json.JSONDecodeError:
        pass

    final_result_text: str | None = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _is_stream_result_event(event):
            final_result_text = event["result"]

    if final_result_text is not None:
        unwrapped = _unwrap_stream_result_payload(final_result_text)
        if unwrapped is not None:
            return unwrapped

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
            locations = "<br>".join(_as_list(finding.get("locations")))
            if not locations:
                locations = str(finding.get("location", "-") or "-")
            summary = str(
                finding.get("summary", "")
                or finding.get("description", "")
                or "-"
            )
            lines.append(
                f"| {finding.get('id', '-')} | {finding.get('category', '-')} | "
                f"{finding.get('severity', '-')} | {locations} | "
                f"{summary} | {_recommendation_text(finding)} |"
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
    lines.append(
        "\n".join(
            f"- {item.get('id', 'evidence')}: {item.get('detail', item)}"
            if isinstance(item, dict)
            else f"- evidence: {item}"
            for item in evidence
        )
        or "- None"
    )

    lines.extend(["", "## Risks"])
    risks = _as_list(data.get("risks"))
    lines.append(
        "\n".join(
            f"- {item.get('id', 'risk')} [{item.get('severity', 'N/A')}]: {item.get('detail', item)}"
            if isinstance(item, dict)
            else f"- risk: {item}"
            for item in risks
        )
        or "- None"
    )

    lines.extend(["", "## Action Items"])
    actions = _as_list(data.get("action_items"))
    lines.append(
        "\n".join(
            f"- {item.get('id', 'action')}: {item.get('action', item)}"
            if isinstance(item, dict)
            else f"- action: {item}"
            for item in actions
        )
        or "- None"
    )
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
    lines.append(
        "\n".join(
            f"- {item.get('id', 'evidence')}: {item.get('detail', item)}"
            if isinstance(item, dict)
            else f"- evidence: {item}"
            for item in evidence
        )
        or "- None"
    )

    lines.extend(["", "## Risks"])
    risks = _as_list(data.get("risks"))
    lines.append(
        "\n".join(
            f"- {item.get('id', 'risk')} [{item.get('severity', 'N/A')}]: {item.get('detail', item)}"
            if isinstance(item, dict)
            else f"- risk: {item}"
            for item in risks
        )
        or "- None"
    )

    lines.extend(["", "## Action Items"])
    actions = _as_list(data.get("action_items"))
    lines.append(
        "\n".join(
            f"- {item.get('id', 'action')}: {item.get('action', item)}"
            if isinstance(item, dict)
            else f"- action: {item}"
            for item in actions
        )
        or "- None"
    )
    lines.append("")
    return "\n".join(lines), gate_passed


def render_impl_report(
    data: dict[str, Any],
    gate_passed: bool,
    gate_checks: list[dict[str, Any]],
    synthesis_summary: Optional[dict[str, Any]] = None,
) -> tuple[str, bool]:
    """Render the impl phase markdown report.

    Schema is intentionally permissive — agent cards can add stricter
    `gate.pass_conditions`. Defaults reflect the §1.1 baseline criteria
    (lint clean / build PASS / tasks complete / commits exist).
    """
    tasks_completed = _as_list(data.get("tasks_completed"))
    tasks_pending = _as_list(data.get("tasks_pending"))
    commits = _as_list(data.get("commits"))
    findings = _as_list(data.get("findings"))

    lines = [
        "# Implementation Report",
        "",
        "## Summary",
        f"- Conclusion: {data.get('conclusion') or ('PASS' if gate_passed else 'FAIL')}",
        f"- Tasks completed: {len(tasks_completed)}",
        f"- Tasks pending: {len(tasks_pending)}",
        f"- Commits: {len(commits)}",
        f"- Lint clean: {data.get('lint_clean', 'N/A')}",
        f"- Build passed: {data.get('build_passed', 'N/A')}",
        f"- Gate G4: {'PASS' if gate_passed else 'FAIL'}",
    ]
    lines.extend(_render_synthesis_summary(synthesis_summary))
    if tasks_completed:
        lines.extend(["", "## Tasks Completed"] + [f"- {t}" for t in tasks_completed])
    if tasks_pending:
        lines.extend(["", "## Tasks Pending"] + [f"- {t}" for t in tasks_pending])
    if commits:
        lines.extend(["", "## Commits"] + [f"- {c}" for c in commits])
    if findings:
        lines.extend(["", "## Findings", "", "| ID | Severity | File | Summary |", "|----|----------|------|---------|"])
        for f in findings:
            lines.append(
                f"| {f.get('id', '-')} | {f.get('severity', '-')} | "
                f"{f.get('file', '-')} | {f.get('summary', '-')} |"
            )
    lines.extend(["", "## Gate Checks"])
    lines.extend([f"- {'PASS' if item['passed'] else 'FAIL'}: {item['condition']} ({item['detail']})" for item in gate_checks] or ["- None"])
    lines.append("")
    return "\n".join(lines), gate_passed


def render_test_report(
    data: dict[str, Any],
    gate_passed: bool,
    gate_checks: list[dict[str, Any]],
    synthesis_summary: Optional[dict[str, Any]] = None,
) -> tuple[str, bool]:
    """Render the test phase markdown report."""
    suites = _as_list(data.get("suites"))
    regressions = _as_list(data.get("regressions"))
    acceptance = data.get("acceptance", {}) if isinstance(data.get("acceptance"), dict) else {}
    coverage = data.get("coverage", {}) if isinstance(data.get("coverage"), dict) else {}

    suite_passed = sum(_int_value(s.get("passed")) for s in suites)
    suite_failed = sum(_int_value(s.get("failed")) for s in suites)

    lines = [
        "# Test Report",
        "",
        "## Summary",
        f"- Conclusion: {data.get('conclusion') or ('PASS' if gate_passed else 'FAIL')}",
        f"- Suites: {len(suites)} (passed={suite_passed}, failed={suite_failed})",
        f"- Regressions: {len(regressions)}",
        f"- Acceptance: {acceptance.get('passed', 'N/A')}/{acceptance.get('total', 'N/A')}",
        f"- Coverage %: {coverage.get('percentage', 'N/A')}",
        f"- Gate G6: {'PASS' if gate_passed else 'FAIL'}",
    ]
    lines.extend(_render_synthesis_summary(synthesis_summary))
    if suites:
        lines.extend(["", "## Suites", "", "| Name | Passed | Failed | Duration |", "|------|--------|--------|----------|"])
        for s in suites:
            lines.append(
                f"| {s.get('name', '-')} | {s.get('passed', '-')} | "
                f"{s.get('failed', '-')} | {s.get('duration_sec', '-')} |"
            )
    if regressions:
        lines.extend(["", "## Regressions"] + [f"- {r.get('id', '-')}: {r.get('detail', r)}" for r in regressions])
    lines.extend(["", "## Gate Checks"])
    lines.extend([f"- {'PASS' if item['passed'] else 'FAIL'}: {item['condition']} ({item['detail']})" for item in gate_checks] or ["- None"])
    lines.append("")
    return "\n".join(lines), gate_passed


def _failure_context_for_result(phase: str, data: dict[str, Any]) -> dict[str, Any]:
    findings = _as_list(data.get("findings"))
    context: dict[str, Any] = {
        "has_critical": any(f.get("severity") == "CRITICAL" for f in findings),
        "high_count": len([f for f in findings if f.get("severity") == "HIGH"]),
    }

    explicit_failure_type = (
        data.get("failure_type")
        or data.get("failureType")
        or data.get("reason")
    )
    if isinstance(explicit_failure_type, str) and explicit_failure_type.strip():
        context["failure_type"] = explicit_failure_type.strip()
        return context

    if phase == "verify":
        scope = data.get("scope", {})
        if isinstance(scope, dict) and _int_value(scope.get("violations")) > 0:
            context["failure_type"] = "scope_violation"
            return context

        compliance = data.get("compliance", {})
        if isinstance(compliance, dict) and _int_value(compliance.get("fail")) > 0:
            context["failure_type"] = "impl_bug"

    return context


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
    artifact_names = {
        "review": "review-report.md",
        "verify": "verification-report.md",
        "impl": "implementation-report.md",
        "test": "test-report.md",
    }
    if phase not in artifact_names:
        raise ValueError(
            f"apply-result supports {sorted(artifact_names)}: got {phase}"
        )
    output_path = wf_dir / "artifacts" / artifact_names[phase]

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
        # Team/OMP worker schemas group gate metrics under `phase_metrics`,
        # while provider-direct schemas expose them at the result root.
        # Accept both at this boundary so either execution surface feeds the
        # same deterministic gate evaluator.
        phase_metrics = data.get("phase_metrics")
        if isinstance(phase_metrics, dict):
            for key, value in phase_metrics.items():
                data.setdefault(key, value)
        gate_passed, gate_checks = evaluate_gate(explicit_root, phase, data, change_class=change_class)

        # --- Build failure context for on_fail routing ---
        failure_context = None
        if not gate_passed:
            failure_context = _failure_context_for_result(phase, data)

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
        elif phase == "verify":
            markdown, passed = render_verify_report(data, gate_passed, gate_checks, synthesis_summary=synthesis_summary)
        elif phase == "impl":
            markdown, passed = render_impl_report(data, gate_passed, gate_checks, synthesis_summary=synthesis_summary)
        else:
            markdown, passed = render_test_report(data, gate_passed, gate_checks, synthesis_summary=synthesis_summary)
    except Exception as exc:
        markdown = _render_malformed_report(phase, f"invalid_json:{exc}", _raw_result_text(result_path))
        passed = False
        failure_context = None

    output_path.write_text(markdown, encoding="utf-8")
    if not skip_gate_apply:
        apply_gate_result(explicit_root, phase, passed, failure_context=failure_context)
    return output_path, passed
