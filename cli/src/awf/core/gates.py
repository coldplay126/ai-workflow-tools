from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional, Tuple

from awf.core.db_validation import evaluate_database_gate
from awf.core.paths import find_repo_root
from awf.core.planning_options import (
    PlanningOptionsError,
    resolve_planning_options_policy,
    validate_planning_options_provenance,
)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _is_strict_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0

_CONFLICT_SEVERITY_RANK = {
    "CRITICAL": 0,
    "HIGH": 1,
    "MEDIUM": 2,
    "LOW": 3,
}
_ACTIVE_CONFLICT_STATUSES = frozenset(
    {"conflict", "conflicted", "open", "unresolved"}
)
_INACTIVE_CONFLICT_STATUSES = frozenset(
    {"acknowledged", "dismissed", "resolved"}
)
_REVIEW_JUDGE_CONFLICT_REASONS = frozenset(
    {
        "conclusion_conflict",
        "critical_finding_present",
        "high_count_mismatch",
        "high_severity_findings_mismatch",
    }
)
_VERIFY_JUDGE_CONFLICT_REASONS = frozenset(
    {
        "compliance_fail_mismatch",
        "compliance_fail_present",
        "conclusion_conflict",
        "quality_critical_mismatch",
        "quality_critical_present",
        "scope_violation_present",
        "scope_violations_mismatch",
    }
)
_NON_CONFLICT_JUDGE_REASONS = frozenset(
    {"primary_gate_failed", "secondary_gate_failed"}
)
_CAPABILITY_EVIDENCE_BY_PHASE = {
    "verify": ("security_scan",),
    "test": ("browser", "debug"),
}
_CAPABILITY_EVIDENCE_STATUSES = frozenset(
    {"pass", "not_run", "skipped", "failed"}
)
_SYNTHESIZED_CAPABILITY_NOT_RUN_REASON = (
    "legacy_result_missing_capability_evidence"
)


def canonicalize_capability_evidence(
    phase: str, result_data: dict[str, Any]
) -> dict[str, Any]:
    """Add explicit unavailable optional-capability evidence to legacy results."""
    capabilities = _CAPABILITY_EVIDENCE_BY_PHASE.get(phase)
    if not capabilities:
        return result_data
    if "capability_evidence" not in result_data:
        present: set[str] = set()
        evidence: list[Any] = []
    else:
        raw_evidence = result_data["capability_evidence"]
        if not isinstance(raw_evidence, list) or not raw_evidence:
            return result_data
        present = {
            item["capability"]
            for item in raw_evidence
            if isinstance(item, dict)
            and isinstance(item.get("capability"), str)
            and item["capability"] in capabilities
        }
        evidence = list(raw_evidence)
    normalized = dict(result_data)
    normalized["capability_evidence"] = [
        *evidence,
        *[
            {
                "capability": capability,
                "status": "not_run",
                "reason": _SYNTHESIZED_CAPABILITY_NOT_RUN_REASON,
            }
            for capability in capabilities
            if capability not in present
        ],
    ]
    return normalized


def _validate_capability_evidence(
    phase: str, value: object, *, declared: bool
) -> list[str]:
    capabilities = _CAPABILITY_EVIDENCE_BY_PHASE.get(phase)
    if not capabilities or not declared:
        return []
    if not isinstance(value, list) or not value:
        return ["missing_or_invalid:capability_evidence"]

    errors: list[str] = []
    seen: set[str] = set()
    allowed = set(capabilities)
    for index, item in enumerate(value):
        prefix = f"capability_evidence[{index}]"
        if not isinstance(item, dict):
            errors.append(f"invalid:{prefix}")
            continue
        if set(item) - {"capability", "status", "reason"}:
            errors.append(f"unexpected:{prefix}")
        capability = item.get("capability")
        status = item.get("status")
        reason = item.get("reason")
        if not isinstance(capability, str) or capability not in allowed:
            errors.append(f"invalid:{prefix}.capability")
        elif capability in seen:
            errors.append(f"duplicate:{prefix}.capability")
        else:
            seen.add(capability)
        if not isinstance(status, str) or status not in _CAPABILITY_EVIDENCE_STATUSES:
            errors.append(f"invalid:{prefix}.status")
        if status != "pass" and (
            not isinstance(reason, str) or not reason.strip()
        ):
            errors.append(f"missing_or_invalid:{prefix}.reason")
        elif reason is not None and not isinstance(reason, str):
            errors.append(f"invalid:{prefix}.reason")
    return errors


def _conflict_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized if normalized else None


def _conflict_location(item: dict[str, Any]) -> str | None:
    if "location" in item and "locations" in item:
        return None
    locations = item.get("locations") if "locations" in item else [item.get("location")]
    if not isinstance(locations, list):
        return None
    normalized = {
        value.strip().replace("\\", "/")
        for value in locations
        if isinstance(value, str) and value.strip()
    }
    if not normalized or len(normalized) != len(locations):
        return None
    return "|".join(sorted(normalized))


def _conflict_evidence(value: object) -> str | None:
    if not isinstance(value, list) or not value:
        return None
    try:
        normalized = {
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            for item in value
        }
    except (TypeError, ValueError):
        return None
    return "|".join(sorted(normalized)) if normalized else None


def _structured_conflict_counts(
    values: object,
) -> tuple[bool, int, int]:
    """Return valid, active conflict count, and active HIGH-or-higher count."""
    if not isinstance(values, list):
        return False, 0, 0
    conflicts: dict[tuple[str, str, str, str, str], str] = {}
    for item in values:
        if not isinstance(item, dict):
            return False, 0, 0
        requirement = _conflict_text(item.get("requirement"))
        category = _conflict_text(item.get("category"))
        location = _conflict_location(item)
        status = _conflict_text(item.get("status"))
        evidence = _conflict_evidence(item.get("evidence"))
        severity = _conflict_text(item.get("severity"))
        if (
            requirement is None
            or category is None
            or location is None
            or status is None
            or evidence is None
            or severity is None
        ):
            return False, 0, 0
        status = status.casefold()
        severity = severity.upper()
        if (
            status not in _ACTIVE_CONFLICT_STATUSES | _INACTIVE_CONFLICT_STATUSES
            or severity not in _CONFLICT_SEVERITY_RANK
        ):
            return False, 0, 0
        key = (
            requirement,
            category.casefold(),
            location,
            status,
            evidence,
        )
        existing = conflicts.get(key)
        if (
            existing is None
            or _CONFLICT_SEVERITY_RANK[severity] < _CONFLICT_SEVERITY_RANK[existing]
        ):
            conflicts[key] = severity
    active = [
        severity
        for (_, _, _, status, _), severity in conflicts.items()
        if status in _ACTIVE_CONFLICT_STATUSES
    ]
    return (
        True,
        len(active),
        sum(_CONFLICT_SEVERITY_RANK[severity] <= _CONFLICT_SEVERITY_RANK["HIGH"] for severity in active),
    )


def _state_synthesis(explicit_root: Optional[str], phase: str) -> tuple[bool, object]:
    """Load the persisted synthesis only when this run did not embed one."""
    try:
        from awf.core.state import load_workflow_state

        state = load_workflow_state(explicit_root)
    except FileNotFoundError:
        return False, None
    except Exception:
        return True, None
    phases = state.get("phases") if isinstance(state, dict) else None
    phase_state = phases.get(phase) if isinstance(phases, dict) else None
    if not isinstance(phase_state, dict) or "synthesis" not in phase_state:
        return False, None
    return True, phase_state["synthesis"]


def _synthesis_conflict_counts(
    phase: str,
    synthesis: object,
) -> tuple[bool, int, int]:
    if not isinstance(synthesis, dict):
        return False, 0, 0
    judge_passed = synthesis.get(
        "judge_passed",
        synthesis.get("judgePassed"),
    )
    synthesis_passed = synthesis.get(
        "synthesis_passed",
        synthesis.get("synthesisPassed"),
    )
    judge_reasons = synthesis.get(
        "judge_reasons",
        synthesis.get("judgeReasons"),
    )
    if (
        not isinstance(judge_passed, bool)
        or not isinstance(synthesis_passed, bool)
        or not isinstance(judge_reasons, list)
        or not all(isinstance(reason, str) and reason.strip() for reason in judge_reasons)
        or len(set(judge_reasons)) != len(judge_reasons)
    ):
        return False, 0, 0
    if judge_passed:
        if judge_reasons or not synthesis_passed:
            return False, 0, 0
    elif not judge_reasons:
        return False, 0, 0

    conflict_reasons = (
        _REVIEW_JUDGE_CONFLICT_REASONS
        if phase == "review"
        else _VERIFY_JUDGE_CONFLICT_REASONS
    )
    if any(
        reason not in conflict_reasons | _NON_CONFLICT_JUDGE_REASONS
        for reason in judge_reasons
    ):
        return False, 0, 0
    grounded_reasons = set(judge_reasons) & conflict_reasons
    if grounded_reasons and synthesis_passed:
        return False, 0, 0
    if synthesis_passed and not judge_passed and set(judge_reasons) != {
        "primary_gate_failed"
    }:
        return False, 0, 0

    if "conflicts" not in synthesis:
        if grounded_reasons or not synthesis_passed:
            return False, 0, 0
        return True, 0, 0

    valid, count, high_count = _structured_conflict_counts(synthesis["conflicts"])
    if not valid:
        return False, 0, 0
    if (grounded_reasons or not synthesis_passed) and count == 0:
        return False, 0, 0
    if judge_passed and count:
        return False, 0, 0
    return True, count, high_count


def _multi_llm_conflict_counts(
    explicit_root: Optional[str],
    phase: str,
    result_data: dict[str, Any],
) -> tuple[bool, int, int, str]:
    """Count de-duplicated, evidenced multi-LLM conflicts without auto-passing malformed evidence."""
    if "synthesis" in result_data:
        valid, count, high_count = _synthesis_conflict_counts(
            phase,
            result_data["synthesis"],
        )
        return valid, count, high_count, "result_synthesis"

    has_state_synthesis, state_synthesis = _state_synthesis(explicit_root, phase)
    if has_state_synthesis:
        valid, count, high_count = _synthesis_conflict_counts(
            phase,
            state_synthesis,
        )
        return valid, count, high_count, "state_synthesis"

    embedded_conflicts = [
        finding
        for finding in _as_list(result_data.get("findings"))
        if isinstance(finding, dict)
        and str(finding.get("category", "")).strip().casefold() == "review_conflict"
    ]
    if embedded_conflicts:
        valid, count, high_count = _structured_conflict_counts(embedded_conflicts)
        return valid, count, high_count, "embedded_findings"
    return True, 0, 0, "not_run"


# --- Plan G1 artifact-based evaluation ---

_FR_PATTERN = re.compile(r"FR-\d+")

_SUPPORTED_GATE_CONDITIONS = frozenset(
    {
        "findings.count(severity=CRITICAL) == 0",
        "HIGH issues all have resolution or user acknowledgment",
        "coverage.percentage >= 80",
        "REVIEW_CONFLICT count(severity>=HIGH) == 0 (when multi-LLM)",
        "scope.violations == 0",
        "compliance.fail == 0",
        "compliance.percentage >= 90",
        "quality.critical == 0",
        "REVIEW_CONFLICT count == 0 (when multi-LLM)",
        "tasks.pending == 0",
        "lint_clean == true",
        "build_passed == true",
        "commits.count > 0",
        "suites.failed == 0",
        "regressions.count == 0",
        "acceptance.passed == acceptance.total",
        "coverage.percentage >= 70",
    }
)
_TASK_PATTERN = re.compile(r"^- \[ \] T\d+", re.MULTILINE)

_PLANNING_OPTIONS_CONDITIONS = (
    "planning_options.artifact",
    "planning_options.shape",
    "planning_options.selection",
    "planning_options.recommendation",
    "planning_options.materiality",
    "planning_options.provenance",
)
_PLANNING_OPTIONS_ERROR_DETAILS = frozenset(
    {
        "artifact_invalid",
        "artifact_missing",
        "profile_invalid",
        "provenance_invalid",
        "provenance_missing",
    }
)


def _planning_options_evaluations(
    passed: bool, detail: str
) -> list[dict[str, Any]]:
    return [
        {"condition": condition, "passed": passed, "detail": detail}
        for condition in _PLANNING_OPTIONS_CONDITIONS
    ]


def evaluate_planning_options_gate(root: Path) -> Tuple[bool, list[dict[str, Any]]]:
    """Evaluate the planning-options policy without exposing artifact contents."""
    try:
        policy = resolve_planning_options_policy(root)
    except PlanningOptionsError as exc:
        detail = (
            exc.code
            if exc.code in _PLANNING_OPTIONS_ERROR_DETAILS
            else "planning_options_invalid"
        )
        return False, _planning_options_evaluations(False, detail)

    if policy.required and policy.artifact is None:
        return False, _planning_options_evaluations(False, "artifact_missing")

    detail = f"status={policy.status}"
    evaluations = _planning_options_evaluations(True, detail)
    if policy.status == "selection_required":
        evaluations[2] = {
            "condition": "planning_options.selection",
            "passed": False,
            "detail": "decision_selection_required",
        }
        evaluations[5] = {
            "condition": "planning_options.provenance",
            "passed": False,
            "detail": "selection_required",
        }
        return False, evaluations

    if policy.required:
        try:
            assert policy.artifact is not None
            provenance_passed, provenance_detail = validate_planning_options_provenance(
                root, policy.artifact.artifact_hash
            )
        except PlanningOptionsError as exc:
            provenance_passed = False
            provenance_detail = (
                exc.code
                if exc.code in _PLANNING_OPTIONS_ERROR_DETAILS
                else "planning_options_invalid"
            )
        evaluations[5] = {
            "condition": "planning_options.provenance",
            "passed": provenance_passed,
            "detail": provenance_detail,
        }
        return provenance_passed, evaluations
    return True, evaluations


def _extract_fr_ids(text: str) -> set[str]:
    """Extract all FR-NNN identifiers from text."""
    return set(_FR_PATTERN.findall(text))


def _extract_fr_tags(text: str) -> set[str]:
    """Extract FR-NNN from bracket tags like [FR-001, FR-002]."""
    tags: set[str] = set()
    for match in re.finditer(r"\[([^\]]*)\]", text):
        tags.update(_FR_PATTERN.findall(match.group(1)))
    return tags


def evaluate_plan_gate(root: Path) -> Tuple[bool, list[dict[str, Any]]]:
    """Evaluate G1 gate for plan phase by checking artifact files."""
    artifacts = root / ".workflow" / "artifacts"
    evaluations: list[dict[str, Any]] = []

    # 1. Artifact existence
    required_files = {
        "spec.md": artifacts / "spec.md",
        "plan.md": artifacts / "plan.md",
        "tasks.md": artifacts / "tasks.md",
        "test-criteria.md": artifacts / "test-criteria.md",
    }
    for name, path in required_files.items():
        exists = path.is_file()
        evaluations.append({
            "condition": f"artifacts/{name} exists",
            "passed": exists,
            "detail": f"exists={exists}",
        })

    if not all(e["passed"] for e in evaluations):
        _, planning_evaluations = evaluate_planning_options_gate(root)
        evaluations.extend(planning_evaluations)
        evaluations.extend(evaluate_database_gate(root, "plan"))
        return False, evaluations

    try:
        spec_text = required_files["spec.md"].read_text(encoding="utf-8")
        plan_text = required_files["plan.md"].read_text(encoding="utf-8")
        tasks_text = required_files["tasks.md"].read_text(encoding="utf-8")
        criteria_text = required_files["test-criteria.md"].read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        evaluations.append({
            "condition": "artifact_readable",
            "passed": False,
            "detail": f"read_error:{exc}",
        })
        _, planning_evaluations = evaluate_planning_options_gate(root)
        evaluations.extend(planning_evaluations)
        evaluations.extend(evaluate_database_gate(root, "plan"))
        return False, evaluations

    # 2. No [NEEDS CLARIFICATION] markers
    nc_count = spec_text.count("[NEEDS CLARIFICATION]")
    evaluations.append({
        "condition": "spec.md has 0 [NEEDS CLARIFICATION] markers",
        "passed": nc_count == 0,
        "detail": f"needs_clarification={nc_count}",
    })

    # 3. At least 1 task
    task_count = len(_TASK_PATTERN.findall(tasks_text))
    evaluations.append({
        "condition": "tasks.md has >= 1 task",
        "passed": task_count >= 1,
        "detail": f"task_count={task_count}",
    })

    # 4. FR cross-reference coverage
    spec_frs = _extract_fr_ids(spec_text)
    if spec_frs:
        plan_fr_tags = _extract_fr_tags(plan_text)
        tasks_fr_tags = _extract_fr_tags(tasks_text)
        criteria_fr_tags = _extract_fr_tags(criteria_text)

        plan_missing = spec_frs - plan_fr_tags
        tasks_missing = spec_frs - tasks_fr_tags
        criteria_missing = spec_frs - criteria_fr_tags

        evaluations.append({
            "condition": "all FR in spec.md tagged in plan.md",
            "passed": len(plan_missing) == 0,
            "detail": f"missing={sorted(plan_missing)}" if plan_missing else "full_coverage",
        })
        evaluations.append({
            "condition": "all FR in spec.md tagged in tasks.md",
            "passed": len(tasks_missing) == 0,
            "detail": f"missing={sorted(tasks_missing)}" if tasks_missing else "full_coverage",
        })
        evaluations.append({
            "condition": "all FR in spec.md tagged in test-criteria.md",
            "passed": len(criteria_missing) == 0,
            "detail": f"missing={sorted(criteria_missing)}" if criteria_missing else "full_coverage",
        })

    # 5. Constitution check
    manifest_path = root / ".workflow" / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        constitution_path = manifest.get("constitution_path")
        if constitution_path:
            resolved = Path(constitution_path).expanduser()
            if not resolved.is_absolute():
                resolved = root / resolved
            const_ok = resolved.is_file()
            evaluations.append({
                "condition": "constitution loaded if manifest.constitution_path is set",
                "passed": const_ok,
                "detail": f"path={constitution_path},exists={const_ok}",
            })

    _, planning_evaluations = evaluate_planning_options_gate(root)
    evaluations.extend(planning_evaluations)
    evaluations.extend(evaluate_database_gate(root, "plan"))

    overall = all(e["passed"] for e in evaluations)
    return overall, evaluations


def _severity_count(findings: list[dict[str, Any]], severity: str) -> int:
    return len([item for item in findings if item.get("severity") == severity])


def _high_without_resolution(findings: list[dict[str, Any]]) -> int:
    unresolved = 0
    for item in findings:
        if item.get("severity") != "HIGH":
            continue
        recommendation = str(item.get("recommendation", "") or item.get("suggestion", "") or "").strip()
        if not recommendation:
            unresolved += 1
    return unresolved


def _validate_required_shape(phase: str, result_data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if phase == "review":
        if not isinstance(result_data.get("findings"), list):
            errors.append("missing_or_invalid:findings")
        coverage = result_data.get("coverage")
        if not isinstance(coverage, dict):
            errors.append("missing_or_invalid:coverage")
        else:
            if "percentage" not in coverage:
                errors.append("missing:coverage.percentage")
    elif phase == "verify":
        for key in ("scope", "compliance", "quality"):
            if not isinstance(result_data.get(key), dict):
                errors.append(f"missing_or_invalid:{key}")
        scope = result_data.get("scope", {})
        compliance = result_data.get("compliance", {})
        quality = result_data.get("quality", {})
        if isinstance(scope, dict) and "violations" not in scope:
            errors.append("missing:scope.violations")
        if isinstance(compliance, dict):
            if "fail" not in compliance:
                errors.append("missing:compliance.fail")
            if "percentage" not in compliance:
                errors.append("missing:compliance.percentage")
        if isinstance(quality, dict) and "critical" not in quality:
            errors.append("missing:quality.critical")
    errors.extend(
        _validate_capability_evidence(
            phase,
            result_data.get("capability_evidence"),
            declared="capability_evidence" in result_data,
        )
    )
    return errors


def evaluate_gate(explicit_root: Optional[str], phase: str, result_data: dict[str, Any], change_class: str = "standard") -> Tuple[bool, list[dict[str, Any]]]:
    root = find_repo_root(explicit_root)
    database_evaluations = (
        evaluate_database_gate(root, phase)
        if phase in {"verify", "test"}
        else []
    )


    # Plan phase uses artifact-based evaluation, not result-based
    if phase == "plan":
        return evaluate_plan_gate(root)

    agent_card_path = root / ".workflow" / "agent-cards" / f"{phase}.json"

    if not agent_card_path.is_file():
        return False, [
            {
                "condition": "gate_configuration",
                "passed": False,
                "detail": "agent_card_missing",
            },
            *database_evaluations,
        ]

    try:
        agent_card = json.loads(agent_card_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, [
            {
                "condition": "gate_configuration",
                "passed": False,
                "detail": "agent_card_invalid",
            },
            *database_evaluations,
        ]
    if not isinstance(agent_card, dict) or not isinstance(agent_card.get("gate"), dict):
        return False, [
            {
                "condition": "gate_configuration",
                "passed": False,
                "detail": "agent_card_invalid",
            },
            *database_evaluations,
        ]
    conditions = agent_card["gate"].get("pass_conditions")
    if not isinstance(conditions, list):
        return False, [
            {
                "condition": "gate_configuration",
                "passed": False,
                "detail": "pass_conditions_invalid",
            },
            *database_evaluations,
        ]
    if not conditions:
        return False, [
            {
                "condition": "gate_configuration",
                "passed": False,
                "detail": "pass_conditions_empty",
            },
            *database_evaluations,
        ]
    if any(
        not isinstance(condition, str)
        or condition not in _SUPPORTED_GATE_CONDITIONS
        for condition in conditions
    ):
        return False, [
            {
                "condition": "gate_configuration",
                "passed": False,
                "detail": "pass_conditions_unsupported",
            },
            *database_evaluations,
        ]
    evaluations: list[dict[str, Any]] = []
    shape_errors = _validate_required_shape(phase, result_data)

    if shape_errors:
        evaluations.append(
            {
                "condition": "structured_result_shape",
                "passed": False,
                "detail": "malformed_response:" + ",".join(shape_errors),
            }
        )
        evaluations.extend(database_evaluations)
        return False, evaluations

    findings = _as_list(result_data.get("findings"))
    coverage = result_data.get("coverage", {})
    scope = result_data.get("scope", {})
    compliance = result_data.get("compliance", {})
    quality = result_data.get("quality", {})

    # Risk-aware gate: skip certain conditions based on change class (§2)
    from awf.core.state import get_risk_investment
    risk = get_risk_investment(change_class, phase)
    skip_checks: set[str] = set(risk.get("skip_checks", set()))

    for condition in conditions:
        # Skip conditions relaxed by risk investment policy
        condition_lower = condition.lower()
        if skip_checks and any(check in condition_lower for check in skip_checks):
            evaluations.append({"condition": condition, "passed": True, "detail": f"skipped_by_risk_policy:change_class={change_class}"})
            continue

        passed = True
        detail = ""

        if condition == "findings.count(severity=CRITICAL) == 0":
            count = _severity_count(findings, "CRITICAL")
            passed = count == 0
            detail = f"critical_findings={count}"
        elif condition == "HIGH issues all have resolution or user acknowledgment":
            unresolved = _high_without_resolution(findings)
            passed = unresolved == 0
            detail = f"unresolved_high_findings={unresolved}"
        elif condition == "coverage.percentage >= 80":
            percentage = float(coverage.get("percentage", 0) or 0)
            passed = percentage >= 80
            detail = f"coverage_percentage={percentage}"
        elif condition == "REVIEW_CONFLICT count(severity>=HIGH) == 0 (when multi-LLM)":
            valid, _, high_count, source = _multi_llm_conflict_counts(
                explicit_root,
                phase,
                result_data,
            )
            passed = valid and high_count == 0
            detail = (
                f"multi_llm_conflicts_high={high_count}; source={source}"
                if valid
                else "multi_llm_conflicts=invalid"
            )
        elif condition == "scope.violations == 0":
            violations = int(scope.get("violations", 0) or 0)
            passed = violations == 0
            detail = f"scope_violations={violations}"
        elif condition == "compliance.fail == 0":
            fail = int(compliance.get("fail", 0) or 0)
            passed = fail == 0
            detail = f"compliance_fail={fail}"
        elif condition == "compliance.percentage >= 90":
            percentage = float(compliance.get("percentage", 0) or 0)
            passed = percentage >= 90
            detail = f"compliance_percentage={percentage}"
        elif condition == "quality.critical == 0":
            critical = int(quality.get("critical", 0) or 0)
            passed = critical == 0
            detail = f"quality_critical={critical}"
        elif condition == "REVIEW_CONFLICT count == 0 (when multi-LLM)":
            valid, count, _, source = _multi_llm_conflict_counts(
                explicit_root,
                phase,
                result_data,
            )
            passed = valid and count == 0
            detail = (
                f"multi_llm_conflicts={count}; source={source}"
                if valid
                else "multi_llm_conflicts=invalid"
            )
        # --- §1.1 impl gate (G4) conditions ---
        elif condition == "tasks.pending == 0":
            pending = result_data.get("tasks_pending")
            passed = isinstance(pending, list) and not pending
            detail = (
                f"tasks_pending={len(pending)}"
                if isinstance(pending, list)
                else "tasks_pending=invalid"
            )
        elif condition == "lint_clean == true":
            passed = result_data.get("lint_clean") is True
            detail = "lint_clean=true" if passed else "lint_clean=invalid_or_false"
        elif condition == "build_passed == true":
            passed = result_data.get("build_passed") is True
            detail = "build_passed=true" if passed else "build_passed=invalid_or_false"
        elif condition == "commits.count > 0":
            commits = result_data.get("commits")
            passed = isinstance(commits, list) and bool(commits)
            detail = (
                f"commit_count={len(commits)}"
                if isinstance(commits, list)
                else "commit_count=invalid"
            )
        # --- §1.1 test gate (G6) conditions ---
        elif condition == "suites.failed == 0":
            suites = result_data.get("suites")
            if not isinstance(suites, list) or not all(
                isinstance(suite, dict)
                and _is_strict_nonnegative_int(suite.get("failed"))
                for suite in suites
            ):
                passed = False
                detail = "suites_failed=invalid"
            else:
                total_failed = sum(suite["failed"] for suite in suites)
                passed = total_failed == 0
                detail = f"suites_failed={total_failed}"
        elif condition == "regressions.count == 0":
            regressions = result_data.get("regressions")
            passed = isinstance(regressions, list) and not regressions
            detail = (
                f"regression_count={len(regressions)}"
                if isinstance(regressions, list)
                else "regression_count=invalid"
            )
        elif condition == "acceptance.passed == acceptance.total":
            acceptance = result_data.get("acceptance") or {}
            if not isinstance(acceptance, dict):
                acceptance = {}
            ap = int(acceptance.get("passed", 0) or 0)
            at = int(acceptance.get("total", 0) or 0)
            passed = ap == at and at > 0  # require at least one acceptance item
            detail = f"acceptance={ap}/{at}"
        elif condition == "coverage.percentage >= 70":
            percentage = float(coverage.get("percentage", 0) or 0)
            passed = percentage >= 70
            detail = f"coverage_percentage={percentage}"
        else:
            passed = False
            detail = "unsupported_condition_requires_evaluator_update"

        evaluations.append({"condition": condition, "passed": passed, "detail": detail})
    evaluations.extend(database_evaluations)

    if not evaluations:
        # No conditions defined — pass with warning
        import sys
        print(f"warning: no gate conditions defined for {phase}, defaulting to PASS", file=sys.stderr)
        return True, [{"condition": "no_conditions_defined", "passed": True, "detail": "empty_pass_conditions"}]

    overall = all(item["passed"] for item in evaluations)
    return overall, evaluations
