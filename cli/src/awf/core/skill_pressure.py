from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


MATRIX_SCHEMA = "awf_skill_validation_matrix_v1"
REQUIRED_CATEGORIES = {
    "trigger_selection",
    "without_skill_baseline",
    "with_skill_compliance",
    "combined_pressure",
    "displayed_commands",
    "stop_exit_contract",
    "runtime_discovery",
    "links_supporting_files",
    "regression_semantic_audit",
}
SUPPORTED_RUNTIMES = {"claude", "agent-skills", "omp"}
SUPPORTED_DECISIONS = frozenset({"PROCEED", "STOP", "REPORT", "ASK_USER", "DELEGATE"})
HIGH_RISK_SKILLS = frozenset(
    {
        "multi-agent",
        "phase-approve",
        "phase-done",
        "release-worktree-lifecycle",
        "wf-orchestrator",
        "wf-reset",
    }
)


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    UNPROVEN = "UNPROVEN"
    NOT_APPLICABLE = "N/A"


class MatrixError(ValueError):
    pass


@dataclass(frozen=True)
class ScenarioExpectation:
    decisions: tuple[str, ...]
    required_reason_codes: tuple[str, ...] = ()
    required_sections: tuple[str, ...] = ()
    required_commands: tuple[str, ...] = ()
    ordered_commands: tuple[str, ...] = ()
    forbidden_commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class FieldScenario:
    id: str
    skill: str
    layer: str
    category: str
    severity: str
    task: str
    positive_criteria: tuple[str, ...]
    negative_criteria: tuple[str, ...]
    runtimes: tuple[str, ...]
    expected: ScenarioExpectation


@dataclass(frozen=True)
class SkillCase:
    name: str
    type: str
    entry_kind: str
    high_risk: bool
    severity: str
    categories: tuple[str, ...]
    runtimes: tuple[str, ...]
    scenario: FieldScenario


@dataclass(frozen=True)
class SkillMatrix:
    schema: str
    skills: Mapping[str, SkillCase]


def _required_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise MatrixError(f"{field} must be a non-empty string")
    return value


def _string_tuple(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MatrixError(f"{field} must be a list of strings")
    return tuple(value)


def _scenario(raw: Any, *, skill: str, severity: str) -> FieldScenario:
    if not isinstance(raw, dict):
        raise MatrixError(f"{skill}.scenario must be an object")
    expected = raw.get("expected")
    if not isinstance(expected, dict):
        raise MatrixError(f"{skill}.scenario.expected must be an object")
    decisions = _string_tuple(expected.get("decisions"), field=f"{skill}.decisions")
    if not decisions:
        raise MatrixError(f"{skill}.decisions must not be empty")
    unknown_decisions = set(decisions).difference(SUPPORTED_DECISIONS)
    if unknown_decisions:
        raise MatrixError(f"{skill}.decisions contains unknown decision")
    if len(decisions) != len(set(decisions)):
        raise MatrixError(f"{skill}.decisions contains duplicated decision")
    scenario_skill = raw.get("skill")
    if scenario_skill != skill:
        raise MatrixError(f"{skill}.scenario.skill must equal {skill!r}")
    if raw.get("layer") != "field":
        raise MatrixError(f"{skill}.scenario.layer must be 'field'")
    category = str(raw.get("category") or "")
    if category not in REQUIRED_CATEGORIES:
        raise MatrixError(f"{skill}.scenario.category is invalid")
    if raw.get("severity") != severity:
        raise MatrixError(f"{skill}.scenario.severity must equal Skill severity")
    scenario_id = _required_string(raw.get("id"), field=f"{skill}.scenario.id")
    task = _required_string(raw.get("task"), field=f"{skill}.scenario.task")
    positive = _string_tuple(raw.get("positive_criteria"), field=f"{skill}.positive_criteria")
    negative = _string_tuple(raw.get("negative_criteria"), field=f"{skill}.negative_criteria")
    runtimes = _string_tuple(raw.get("runtimes"), field=f"{skill}.scenario.runtimes")
    if not positive or not negative:
        raise MatrixError(f"{skill}.scenario positive and negative criteria must not be empty")
    if not runtimes or not set(runtimes).issubset(SUPPORTED_RUNTIMES):
        raise MatrixError(f"{skill}.scenario.runtimes must be a non-empty supported subset")
    return FieldScenario(
        id=scenario_id,
        skill=skill,
        layer="field",
        category=category,
        severity=severity,
        task=task,
        positive_criteria=positive,
        negative_criteria=negative,
        runtimes=runtimes,
        expected=ScenarioExpectation(
            decisions=decisions,
            required_reason_codes=_string_tuple(
                expected.get("required_reason_codes"), field=f"{skill}.required_reason_codes"
            ),
            required_sections=_string_tuple(
                expected.get("required_sections"), field=f"{skill}.required_sections"
            ),
            required_commands=_string_tuple(
                expected.get("required_commands"), field=f"{skill}.required_commands"
            ),
            ordered_commands=_string_tuple(
                expected.get("ordered_commands"), field=f"{skill}.ordered_commands"
            ),
            forbidden_commands=_string_tuple(
                expected.get("forbidden_commands"), field=f"{skill}.forbidden_commands"
            ),
        ),
    )


def load_skill_matrix(path: str | Path) -> SkillMatrix:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MatrixError(f"matrix could not be loaded: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != MATRIX_SCHEMA:
        raise MatrixError(f"matrix schema must be {MATRIX_SCHEMA!r}")
    rows = raw.get("skills")
    if not isinstance(rows, list):
        raise MatrixError("matrix skills must be a list")

    skills: dict[str, SkillCase] = {}
    scenario_ids: set[str] = set()
    for raw_case in rows:
        if not isinstance(raw_case, dict):
            raise MatrixError("matrix skill entries must be objects")
        name = _required_string(raw_case.get("name"), field="matrix skill name")
        if name in skills:
            raise MatrixError(f"matrix skill name is duplicated: {name!r}")
        type_ = _required_string(raw_case.get("type"), field=f"{name}.type")
        entry_kind = _required_string(raw_case.get("entry_kind"), field=f"{name}.entry_kind")
        categories = _string_tuple(raw_case.get("categories"), field=f"{name}.categories")
        if len(categories) != len(REQUIRED_CATEGORIES) or set(categories) != REQUIRED_CATEGORIES:
            raise MatrixError(f"{name}.categories must contain each required category exactly once")
        runtimes = _string_tuple(raw_case.get("runtimes"), field=f"{name}.runtimes")
        if len(runtimes) != len(SUPPORTED_RUNTIMES) or set(runtimes) != SUPPORTED_RUNTIMES:
            raise MatrixError(f"{name}.runtimes must contain each supported runtime exactly once")
        severity = str(raw_case.get("severity") or "")
        if severity not in {"critical", "important", "minor"}:
            raise MatrixError(f"{name}.severity must be critical, important, or minor")
        high_risk = raw_case.get("high_risk")
        if not isinstance(high_risk, bool) or high_risk != (name in HIGH_RISK_SKILLS):
            raise MatrixError(f"{name}.high_risk does not match the locked risk policy")
        scenario = _scenario(raw_case.get("scenario"), skill=name, severity=severity)
        if scenario.id in scenario_ids:
            raise MatrixError(f"matrix scenario id is duplicated: {scenario.id!r}")
        scenario_ids.add(scenario.id)
        skills[name] = SkillCase(
            name=name,
            type=type_,
            entry_kind=entry_kind,
            high_risk=high_risk,
            severity=severity,
            categories=categories,
            runtimes=runtimes,
            scenario=scenario,
        )
    return SkillMatrix(schema=MATRIX_SCHEMA, skills=MappingProxyType(skills))
