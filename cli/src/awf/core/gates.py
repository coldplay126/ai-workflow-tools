from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional, Tuple

from awf.core.db_validation import evaluate_database_gate
from awf.core.paths import find_repo_root


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _is_strict_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


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
            passed = True
            detail = "multi_llm_conflicts=0 (not yet modeled)"
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
            passed = True
            detail = "multi_llm_conflicts=0 (not yet modeled)"
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
