from __future__ import annotations

import errno
import html
import json
import os
import re
import stat
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


_MAX_REPORT_TEXT_CHARS = 512
_REPORT_SENSITIVE_TEXT = re.compile(
    r"(?:"
    r"[a-z][a-z0-9+.-]*://"
    r"|(?:^|[^a-z0-9_])(?:[a-z_][a-z0-9_]*_)?"
    r"(?:password|token|secret|dsn|url|key|credential)\s*[:=]"
    r"|(?:^|[\s;])--(?:password|token|dsn|secret|credential)(?:=|\s+\S+|$)"
    r"|(?:^|[\s;])-[pt](?:\S+|\s+\S+|$)"
    r"|\b(?:create|alter|drop|truncate|insert|update|delete)\s+(?:table|index|database|schema|into|from)\b"
    r"|\b(?:ddl|sample|samples|row|rows|record|records|raw[ _-]?data)\b"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r")",
    re.IGNORECASE,
)
_MARKDOWN_ESCAPE = re.compile(r"([\\`*_{}\[\]()|#!])")


def _safe_report_text(value: object) -> str:
    if not isinstance(value, str):
        value = str(value)
    normalized = " ".join(value.split())
    if normalized == "[REDACTED]":
        return normalized
    if _REPORT_SENSITIVE_TEXT.search(normalized):
        return "[REDACTED]"
    bounded = normalized[:_MAX_REPORT_TEXT_CHARS]
    escaped = html.escape(bounded, quote=True)
    return _MARKDOWN_ESCAPE.sub(r"\\\1", escaped)


def _sanitize_report_payload(value: Any) -> Any:
    if isinstance(value, str):
        return _safe_report_text(value)
    if isinstance(value, list):
        return [_sanitize_report_payload(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _sanitize_report_payload(item)
            for key, item in value.items()
            if isinstance(key, str)
        }
    return value


_MAX_RESULT_INPUT_BYTES = 128 * 1024
_STABLE_RESULT_FAILURE_REASONS = frozenset(
    {
        "malformed_response",
        "result_input_directory",
        "result_input_invalid_utf8",
        "result_input_io",
        "result_input_missing",
        "result_input_oversize",
        "result_json_invalid",
        "worker_returned_failed",
    }
)



class ResultInputError(ValueError):
    """Stable failure produced before parsing an untrusted worker result."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _recommendation_text(finding: dict[str, Any]) -> str:
    return _safe_report_text(
        finding.get("recommendation", "") or finding.get("suggestion", "") or "-"
    )


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


def _read_result_input(path: str) -> str:
    """Read one bounded, regular UTF-8 worker result exactly once."""

    descriptor: Optional[int] = None
    raw_bytes = bytearray()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            raise ResultInputError("result_input_missing") from None
        except OSError:
            raise ResultInputError("result_input_io") from None

        try:
            metadata = os.fstat(descriptor)
        except OSError:
            raise ResultInputError("result_input_io") from None
        if not stat.S_ISREG(metadata.st_mode):
            raise ResultInputError("result_input_directory")
        if metadata.st_size > _MAX_RESULT_INPUT_BYTES:
            raise ResultInputError("result_input_oversize")

        while len(raw_bytes) <= _MAX_RESULT_INPUT_BYTES:
            try:
                chunk = os.read(
                    descriptor,
                    _MAX_RESULT_INPUT_BYTES + 1 - len(raw_bytes),
                )
            except OSError as error:
                if error.errno == errno.EINTR:
                    continue
                raise ResultInputError("result_input_io") from None
            if not chunk:
                break
            raw_bytes.extend(chunk)
            if len(raw_bytes) > _MAX_RESULT_INPUT_BYTES:
                raise ResultInputError("result_input_oversize")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    try:
        return bytes(raw_bytes).decode("utf-8")
    except UnicodeDecodeError:
        raise ResultInputError("result_input_invalid_utf8") from None


def _parse_result_text(raw: str) -> dict[str, Any]:
    """Parse a bounded worker result using the supported envelope formats."""

    raw = raw.strip()
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
        raise ValueError("result_json_invalid")
    return json.loads(raw[start : end + 1])


def _parse_result_json(path: str) -> dict[str, Any]:
    return _parse_result_text(_read_result_input(path))


def load_result_envelope(path: str, *, phase: str, provider: str) -> dict[str, Any]:
    return normalize_worker_result(
        _parse_result_json(path),
        phase=phase,
        provider=provider,
    )


def normalize_phase_result(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten shared team/OMP phase metrics into the canonical result shape."""
    normalized = dict(data)
    phase_metrics = normalized.get("phase_metrics")
    if isinstance(phase_metrics, dict):
        for key, value in phase_metrics.items():
            normalized.setdefault(key, value)
    return normalized


def load_result_json(path: str) -> dict[str, Any]:
    payload = _parse_result_json(path)
    if isinstance(payload, dict) and "status" in payload and "result" in payload:
        normalized = normalize_worker_result(
            payload,
            phase=str(payload.get("phase", "unknown") or "unknown"),
            provider=str(payload.get("provider", "unknown") or "unknown"),
        )
        return normalize_phase_result(dict(normalized.get("result", {})))
    return normalize_phase_result(payload)


def _render_escape_report(
    phase: str,
    reason: str,
    severity: str,
    recommended_action: str,
    escape_data: dict[str, Any],
) -> str:
    escape_data = _sanitize_report_payload(escape_data)
    summary = str(escape_data.get("summary", "") or "No summary provided")
    evidence = _as_list(escape_data.get("evidence"))
    affected = _as_list(escape_data.get("affected_files"))
    lines = [
        f"# {'Review' if phase == 'review' else 'Verification'} Report — Worker Escaped",
        "",
        "## Summary",
        "- Status: ESCAPED",
        f"- Severity: {_safe_report_text(severity)}",
        f"- Reason: {_safe_report_text(reason)}",
        f"- Recommended Action: {_safe_report_text(recommended_action or 'none')}",
        f"- Description: {summary}",
        "",
    ]
    if affected:
        lines.extend(["## Affected Files"] + [f"- {item}" for item in affected] + [""])
    if evidence:
        lines.extend(["## Evidence"])
        for item in evidence:
            if isinstance(item, dict):
                lines.append(
                    f"- [{item.get('kind', 'note')}] "
                    f"{item.get('value', item.get('detail', '[REDACTED]'))}"
                )
            else:
                lines.append(f"- [note] {item}")
        lines.append("")
    return "\n".join(lines)


def _render_malformed_report(phase: str, reason: str) -> str:
    safe_reason = (
        reason
        if reason in _STABLE_RESULT_FAILURE_REASONS
        else _safe_report_text(reason)
    )
    return "\n".join(
        [
            f"# {'Review' if phase == 'review' else 'Verification'} Report",
            "",
            "## Summary",
            "- Conclusion: FAIL",
            f"- Reason: {safe_reason}",
            "",
            "## Gate Checks",
            f"- FAIL: structured_result_shape ({safe_reason})",
            "",
        ]
    )


def _render_synthesis_summary(synthesis_summary: Optional[dict[str, Any]]) -> list[str]:
    if not synthesis_summary:
        return []
    synthesis_summary = _sanitize_report_payload(synthesis_summary)
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
    data = _sanitize_report_payload(data)
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


_SAFE_DATABASE_REPORT_VALUE = re.compile(r"^[A-Za-z0-9._:+-]{1,128}$")


def _database_gate_report_detail(check: dict[str, Any]) -> str:
    summary = check.get("database_summary")
    if not isinstance(summary, dict):
        return "status=unavailable"

    detail: list[str] = []
    stage = summary.get("stage")
    if stage in {"plan", "verify", "test"}:
        detail.append(f"stage={stage}")
    status = summary.get("status")
    if status in {"detected", "fail", "not_applicable", "pass", "waived"}:
        detail.append(f"status={status}")

    for key in (
        "schema_hash_prefix",
        "engine",
        "engine_version",
        "selected_option",
        "local_target",
    ):
        value = summary.get(key)
        if isinstance(value, str) and _SAFE_DATABASE_REPORT_VALUE.fullmatch(value):
            detail.append(f"{key}={value}")

    if (
        summary.get("waiver_present") in {True, "true"}
        or isinstance(summary.get("waiver_reason"), str)
    ):
        detail.append("waiver_present=true")
    return ",".join(detail) or "status=unavailable"


def _render_gate_checks(gate_checks: list[dict[str, Any]]) -> list[str]:
    rendered: list[str] = []
    for check in gate_checks:
        condition = str(check.get("condition", "unknown_condition"))
        detail = (
            _database_gate_report_detail(check)
            if condition.startswith("database.")
            else str(check.get("detail", ""))
        )
        rendered.append(
            f"- {'PASS' if check.get('passed') else 'FAIL'}: {condition} ({detail})"
        )
    return rendered


def render_verify_report(
    data: dict[str, Any],
    gate_passed: bool,
    gate_checks: list[dict[str, Any]],
    synthesis_summary: Optional[dict[str, Any]] = None,
) -> tuple[str, bool]:
    data = _sanitize_report_payload(data)
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

    issues = [item for item in _as_list(quality.get("issues")) if isinstance(item, dict)]
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
    lines.extend(_render_gate_checks(gate_checks) or ["- None"])

    lines.extend(["", "## Evidence"])
    evidence = _as_list(data.get("evidence"))
    lines.append(
        "\n".join(
            f"- {item.get('id', 'evidence')}: {item.get('detail', '[REDACTED]')}"
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
            f"- {item.get('id', 'risk')} [{item.get('severity', 'N/A')}]: {item.get('detail', '[REDACTED]')}"
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
            f"- {item.get('id', 'action')}: {item.get('action', '[REDACTED]')}"
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
    data = _sanitize_report_payload(data)
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
    data = _sanitize_report_payload(data)
    suites = [item for item in _as_list(data.get("suites")) if isinstance(item, dict)]
    regressions = [item for item in _as_list(data.get("regressions")) if isinstance(item, dict)]
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
        lines.extend(
            ["", "## Regressions"]
            + [f"- {item.get('id', '-')}: {item.get('detail', '[REDACTED]')}" for item in regressions]
        )
    lines.extend(["", "## Gate Checks"])
    lines.extend(_render_gate_checks(gate_checks) or ["- None"])
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

    failure_context = None
    try:
        raw = _read_result_input(result_path)
        envelope = normalize_worker_result(
            _parse_result_text(raw),
            phase=phase,
            provider="unknown",
        )
        status = envelope.get("status")
        if status == "escaped":
            raw_escape = envelope.get("escape")
            escape_data = (
                _sanitize_report_payload(raw_escape)
                if isinstance(raw_escape, dict)
                else {}
            )
            provider = _safe_report_text(
                envelope.get("provider", "unknown") or "unknown"
            )
            record_phase_escape(
                explicit_root,
                phase,
                provider=provider,
                escape=escape_data,
            )
            markdown = _render_escape_report(
                phase,
                str(escape_data.get("reason", "") or "unknown"),
                str(escape_data.get("severity", "") or "unknown"),
                str(escape_data.get("recommended_action", "") or ""),
                escape_data,
            )
            passed = False
        elif status == "failed":
            markdown = _render_malformed_report(phase, "worker_returned_failed")
            passed = False
        elif status != "completed":
            raise ValueError("worker_result_status_invalid")
        else:
            data = _sanitize_report_payload(
                normalize_phase_result(dict(envelope.get("result", {})))
            )
            gate_passed, gate_checks = evaluate_gate(
                explicit_root,
                phase,
                data,
                change_class=change_class,
            )
            if not gate_passed:
                failure_context = _failure_context_for_result(phase, data)

            malformed = any(
                item.get("condition") == "structured_result_shape"
                and not item.get("passed", False)
                for item in gate_checks
            )
            if malformed:
                markdown = _render_malformed_report(phase, "malformed_response")
                passed = False
            elif phase == "review":
                markdown, passed = render_review_report(
                    data,
                    gate_passed,
                    gate_checks,
                    synthesis_summary=synthesis_summary,
                )
            elif phase == "verify":
                markdown, passed = render_verify_report(
                    data,
                    gate_passed,
                    gate_checks,
                    synthesis_summary=synthesis_summary,
                )
            elif phase == "impl":
                markdown, passed = render_impl_report(
                    data,
                    gate_passed,
                    gate_checks,
                    synthesis_summary=synthesis_summary,
                )
            else:
                markdown, passed = render_test_report(
                    data,
                    gate_passed,
                    gate_checks,
                    synthesis_summary=synthesis_summary,
                )
    except ResultInputError as error:
        markdown = _render_malformed_report(phase, error.code)
        passed = False
    except Exception:
        markdown = _render_malformed_report(phase, "result_json_invalid")
        passed = False

    output_path.write_text(markdown, encoding="utf-8")
    if not skip_gate_apply:
        apply_gate_result(
            explicit_root,
            phase,
            passed,
            failure_context=failure_context,
        )
    return output_path, passed
