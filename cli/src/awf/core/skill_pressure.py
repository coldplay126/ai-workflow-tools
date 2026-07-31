from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from awf.core.operational_metrics import operations_root


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

REPORT_SCHEMA = "awf_skill_pressure_report_v1"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SENSITIVE_PATTERNS = {
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "phone": re.compile(r"(?<!\d)(?:\+?82[- ]?)?0?1[016789][- ]?\d{3,4}[- ]?\d{4}(?!\d)"),
    "bearer_token": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    UNPROVEN = "UNPROVEN"
    NOT_APPLICABLE = "N/A"


class MatrixError(ValueError):
    pass


class SensitiveDataError(ValueError):
    pass


class _DuplicateJsonKeyError(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key



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


@dataclass(frozen=True)
class CriterionResult:
    id: str
    verdict: Verdict
    evidence: str


@dataclass(frozen=True)
class Evaluation:
    verdict: Verdict
    failures: tuple[str, ...]
    criteria: tuple[CriterionResult, ...]
    parsed: dict[str, Any] | None


@dataclass(frozen=True)
class PairEvaluation:
    verdict: Verdict
    baseline: Evaluation
    with_skill: Evaluation


def _command_matches(command: str, expected_prefix: str) -> bool:
    return command == expected_prefix or command.startswith(f"{expected_prefix} ")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(key)
        result[key] = value
    return result


def _has_shell_control(command: str) -> bool:
    return any(control in command for control in ";&|$`<>\r\n")



def evaluate_response(scenario: FieldScenario, raw: str) -> Evaluation:
    try:
        parsed = json.loads(raw, object_pairs_hook=_object_without_duplicate_keys)
    except _DuplicateJsonKeyError as exc:
        failure = f"duplicate_json_key:{exc.key}"
        criterion = CriterionResult("response_json", Verdict.FAIL, failure)
        return Evaluation(Verdict.FAIL, (failure,), (criterion,), None)
    except json.JSONDecodeError:
        criterion = CriterionResult("response_json", Verdict.FAIL, "malformed_json")
        return Evaluation(Verdict.FAIL, ("malformed_json",), (criterion,), None)
    if not isinstance(parsed, dict):
        criterion = CriterionResult("response_object", Verdict.FAIL, "response_not_object")
        return Evaluation(Verdict.FAIL, ("response_not_object",), (criterion,), None)

    failures: list[str] = []
    criteria: list[CriterionResult] = []

    def check(identifier: str, condition: bool, failure: str) -> None:
        criteria.append(
            CriterionResult(
                identifier,
                Verdict.PASS if condition else Verdict.FAIL,
                "satisfied" if condition else failure,
            )
        )
        if not condition:
            failures.append(failure)

    check(
        "selected_skill",
        parsed.get("selected_skill") == scenario.skill,
        f"selected_skill:{parsed.get('selected_skill')!r}",
    )
    check(
        "decision",
        parsed.get("decision") in scenario.expected.decisions,
        f"decision:{parsed.get('decision')!r}",
    )

    reasons = parsed.get("reason_codes")
    reasons_valid = isinstance(reasons, list) and all(isinstance(item, str) for item in reasons)
    check("reason_codes_type", reasons_valid, "reason_codes_not_string_list")
    reasons = reasons if reasons_valid else []
    for required in scenario.expected.required_reason_codes:
        check(
            f"required_reason:{required}",
            required in reasons,
            f"missing_reason_code:{required}",
        )

    sections = parsed.get("sections")
    sections_valid = isinstance(sections, list) and all(
        isinstance(item, str) for item in sections
    )
    check("sections_type", sections_valid, "sections_not_string_list")
    sections = sections if sections_valid else []
    for required in scenario.expected.required_sections:
        check(
            f"required_section:{required}",
            required in sections,
            f"missing_section:{required}",
        )

    commands = parsed.get("commands")
    commands_valid = isinstance(commands, list) and all(isinstance(item, str) for item in commands)
    check("commands_type", commands_valid, "commands_not_string_list")
    commands = commands if commands_valid else []
    for command in commands:
        check(
            "command_shell_control",
            not _has_shell_control(command),
            "shell_control_command",
        )
    for required in scenario.expected.required_commands:
        check(
            f"required_command:{required}",
            any(_command_matches(command, required) for command in commands),
            f"missing_command:{required}",
        )
    for forbidden in scenario.expected.forbidden_commands:
        check(
            f"forbidden_command:{forbidden}",
            not any(_command_matches(command, forbidden) for command in commands),
            f"forbidden_command:{forbidden}",
        )

    ordered = scenario.expected.ordered_commands
    if ordered:
        positions = [
            next(
                (
                    index
                    for index, command in enumerate(commands)
                    if _command_matches(command, prefix)
                ),
                -1,
            )
            for prefix in ordered
        ]
        present = [position for position in positions if position >= 0]
        check("command_order", len(present) <= 1 or present == sorted(present), "command_order")

    return Evaluation(
        Verdict.FAIL if failures else Verdict.PASS,
        tuple(failures),
        tuple(criteria),
        parsed,
    )


def compare_pair(baseline: Evaluation, with_skill: Evaluation) -> PairEvaluation:
    if Verdict.BLOCKED in {baseline.verdict, with_skill.verdict}:
        return PairEvaluation(Verdict.BLOCKED, baseline, with_skill)
    if with_skill.verdict is not Verdict.PASS:
        return PairEvaluation(Verdict.FAIL, baseline, with_skill)
    if baseline.verdict is Verdict.PASS:
        return PairEvaluation(Verdict.UNPROVEN, baseline, with_skill)
    return PairEvaluation(Verdict.PASS, baseline, with_skill)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_skill(skill_root: str | Path) -> str:
    root = Path(skill_root)
    digest = hashlib.sha256()
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _sensitive_labels(text: str) -> tuple[str, ...]:
    return tuple(name for name, pattern in SENSITIVE_PATTERNS.items() if pattern.search(text))


def pressure_report_path(repo_root: str | Path, run_id: str) -> Path:
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError(f"invalid run_id: {run_id!r}")
    return operations_root(repo_root) / "skill-pressure" / f"{run_id}.json"


def _publish_new(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        delete=False,
    ) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    try:
        os.link(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)


def _safe_field_identity(payload: dict[str, object]) -> dict[str, object]:
    identity: dict[str, object] = {}
    token_fields = ("batch_id", "matrix_schema", "skill", "scenario_id", "provider", "severity")
    for key in token_fields:
        value = payload.get(key)
        if (
            isinstance(value, str)
            and re.fullmatch(r"[a-z0-9_.-]+", value)
            and not _sensitive_labels(value)
        ):
            identity[key] = value
    repetition = payload.get("repetition")
    if isinstance(repetition, int) and repetition > 0:
        identity["repetition"] = repetition
    for key in ("prompt_sha256", "skill_sha256"):
        value = payload.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
            identity[key] = value
    return identity


def write_pressure_report(
    repo_root: str | Path,
    *,
    run_id: str,
    payload: dict[str, Any],
    baseline: str,
    with_skill: str,
) -> Path:
    target = pressure_report_path(repo_root, run_id)
    if target.exists():
        raise FileExistsError(target)

    recorded_at = datetime.now(timezone.utc).isoformat()
    serialized_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    labels = sorted(
        set(
            _sensitive_labels(serialized_payload)
            + _sensitive_labels(baseline)
            + _sensitive_labels(with_skill)
        )
    )
    if labels:
        blocked = {
            "schema": REPORT_SCHEMA,
            "recorded_at": recorded_at,
            "run_id": run_id,
            "persistence_status": "BLOCKED",
            "diagnostics": [{"code": "sensitive_content", "labels": labels}],
            "field_identity": _safe_field_identity(payload),
        }
        _publish_new(target, json.dumps(blocked, ensure_ascii=False, indent=2) + "\n")
        raise SensitiveDataError(f"sensitive transcript blocked: {','.join(labels)}")

    transcript_root = operations_root(repo_root) / "skill-pressure" / "transcripts" / run_id
    baseline_path = transcript_root / "baseline.txt"
    with_skill_path = transcript_root / "with-skill.txt"
    created: list[Path] = []
    try:
        _publish_new(baseline_path, baseline)
        created.append(baseline_path)
        _publish_new(with_skill_path, with_skill)
        created.append(with_skill_path)
        envelope = {
            "schema": REPORT_SCHEMA,
            "recorded_at": recorded_at,
            "run_id": run_id,
            "persistence_status": "COMPLETE",
            "payload": payload,
            "transcripts": {
                "baseline": {"path": str(baseline_path), "sha256": sha256_text(baseline)},
                "with_skill": {"path": str(with_skill_path), "sha256": sha256_text(with_skill)},
            },
        }
        _publish_new(target, json.dumps(envelope, ensure_ascii=False, indent=2) + "\n")
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        try:
            transcript_root.rmdir()
        except OSError:
            pass
        raise
    return target
