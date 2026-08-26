from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from awf.core.operational_metrics import operations_root
from awf.core.skill_subscription import (
    PINNED_SUBSCRIPTION_MODELS,
    claude_discovery_safety_flags,
)


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
DETERMINISTIC_REPORT_SCHEMA = "awf_skill_deterministic_report_v1"
INSTALL_REPORT_SCHEMA = "awf_skill_install_report_v1"
DISCOVERY_REPORT_SCHEMA = "awf_skill_discovery_report_v1"
INSTALL_RECORD_COUNT = 48
SKILL_COUNT = 16
FIELD_REPORT_COUNT = 28
EVIDENCE_CELL_COUNT = 144
EVIDENCE_SUMMARY_SCHEMA = "awf_skill_evidence_matrix_v1"
EVIDENCE_LAYERS = {
    "trigger_selection": "static",
    "without_skill_baseline": "field",
    "with_skill_compliance": "field",
    "combined_pressure": "field",
    "displayed_commands": "static+field",
    "stop_exit_contract": "static+field",
    "runtime_discovery": "runtime",
    "links_supporting_files": "install",
    "regression_semantic_audit": "static",
}
FIELD_RECORD_REQUIRED = {
    "batch_id",
    "matrix_schema",
    "skill",
    "scenario_id",
    "repetition",
    "provider",
    "provider_version",
    "model",
    "runner_flags",
    "severity",
    "remediation_state",
    "behavioral_delta",
    "prompt_sha256",
    "skill_sha256",
    "skill_file_sha256",
    "verdict",
    "auth_mode",
    "injection_sha256",
    "baseline",
    "with_skill",
    "elapsed_sec",
    "exit_status",
}


DISCOVERY_RECORD_REQUIRED = frozenset(
    {
        "runtime",
        "host_binary",
        "binary_version",
        "model",
        "skill",
        "source_sha256",
        "auth_mode",
        "argv_safety_flags",
        "elapsed_sec",
        "exit_status",
        "verdict",
        "diagnostic",
        "source_name",
        "source_description",
        "source_body_heading",
    }
)
DISCOVERY_DIAGNOSTIC_CODES = frozenset(
    {
        "canonical_source_invalid",
        "canonical_source_symlink",
        "canonical_source_metadata_invalid",
        "canonical_source_name_mismatch",
        "canonical_source_materialization_failed",
        "canonical_source_mutated_during_snapshot",
        "claude_stream_malformed",
        "claude_init_duplicate",
        "claude_result_failed",
        "claude_init_missing",
        "claude_init_tools_not_empty",
        "claude_init_invalid",
        "claude_skill_not_registered",
        "claude_result_missing",
        "claude_result_not_terminal",
        "unknown_skill_response",
        "malformed_json_response",
        "invalid_response_schema",
        "source_metadata_mismatch",
        "install_blocked",
        "skill_snapshot_changed",
        "host_timeout",
        "host_subscription_expired",
        "host_model_unsupported",
        "host_auth_unavailable",
        "host_provider_exit",
    }
)
DISCOVERY_REQUIRED_FLAGS_DIAGNOSTIC_RE = re.compile(
    r"unsupported_required_flags:[a-z0-9_-]+(?:,[a-z0-9_-]+)*"
)

OMP_FIELD_RUNNER_FLAGS = (
    "-p",
    "--mode=text",
    "--no-tools",
    "--no-session",
    "--no-extensions",
    "--no-skills",
    "--append-system-prompt",
)

DISCOVERY_RUNTIME_SAFETY_FLAGS = MappingProxyType(
    {
        "claude": claude_discovery_safety_flags(),
        "agent-skills": (
            "exec",
            "--ephemeral",
            "--sandbox=read-only",
            "--skip-git-repo-check",
        ),
        "omp": (
            "-p",
            "--mode=text",
            "--tools=read",
            "--no-session",
            "--no-extensions",
        ),
    }
)

FIELD_EVALUATION_CRITERIA = frozenset(
    {
        "allowed_command",
        "command_order",
        "command_shell_control",
        "commands_type",
        "decision",
        "evaluation",
        "forbidden_command",
        "reason_codes_type",
        "required_command",
        "required_reason",
        "required_section",
        "response_json",
        "response_object",
        "sections_type",
        "selected_skill",
        "source_snapshot",
        "host_diagnostic",
    }
)
NORMALIZED_HOST_DIAGNOSTICS = frozenset(
    {
        "host_auth_unavailable",
        "host_model_unsupported",
        "host_provider_exit",
        "host_subscription_expired",
        "host_timeout",
        "unsupported_omp_flags",
    }
)
FIELD_EVALUATION_FAILURES = frozenset(
    {
        "evaluation_failed",
        "skill_snapshot_changed",
        *FIELD_EVALUATION_CRITERIA,
        *NORMALIZED_HOST_DIAGNOSTICS,
    }
)

INSTALL_RUNTIME_ROOTS = MappingProxyType(
    {
        "claude": ".claude/skills",
        "agent-skills": ".agents/skills",
        "omp": ".omp/skills",
    }
)
INSTALL_DIAGNOSTICS = frozenset(
    {
        "",
        "canonical_source_materialization_failed",
        "canonical_source_mutated_during_snapshot",
        "canonical_source_name_mismatch",
        "installer_target_not_snapshot",
        "canonical_source_metadata_invalid",
        *NORMALIZED_HOST_DIAGNOSTICS,
    }
)
ISOLATED_HOME_ID_RE = re.compile(r"^temporary-[0-9a-f]{32}$")
INSTALL_REPORT_DIGEST_FIELDS = frozenset({"matrix_sha256", "source_sha256"})


def _redact_install_report_digests(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: (
                "<redacted>"
                if (
                    (
                        key in INSTALL_REPORT_DIGEST_FIELDS
                        and isinstance(item, str)
                        and re.fullmatch(r"[0-9a-f]{64}", item)
                    )
                    or (
                        key == "isolated_home_id"
                        and isinstance(item, str)
                        and ISOLATED_HOME_ID_RE.fullmatch(item) is not None
                    )
                )
                else _redact_install_report_digests(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_install_report_digests(item) for item in value]
    return value


def _serialized_install_report_for_sensitive_scan(
    report: Mapping[str, object],
) -> str:
    sanitized = _redact_install_report_digests(report)
    assert isinstance(sanitized, Mapping)
    return _serialized_install_report(sanitized)



def _validate_isolated_home_id(value: object) -> None:
    if not isinstance(value, str) or ISOLATED_HOME_ID_RE.fullmatch(value) is None:
        raise EvidenceError("invalid isolated_home_id")


def _serialized_install_report(report: Mapping[str, object]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _reject_sensitive_serialized_install_report(
    serialized: str, *, error_type: type[ValueError]
) -> None:
    labels = sorted(set(_sensitive_labels(serialized)))
    if labels:
        raise error_type(f"sensitive install report content: {','.join(labels)}")

FIELD_SKILL_RE = re.compile(r"^[a-z][a-z0-9-]*$")
FIELD_SCENARIO_RE = re.compile(r"^[a-z][a-z0-9.-]*$")
FIELD_SEVERITIES = frozenset({"critical", "important", "minor"})
FIELD_REMEDIATION_STATES = frozenset({"not_required", "open"})
FIELD_BEHAVIORAL_DELTAS = frozenset(
    {"blocked", "improved", "not_demonstrated", "regressed_or_noncompliant"}
)
DETERMINISTIC_TEST_PATHS = (
    "cli/tests/test_skill_contract_matrix.py",
    "cli/tests/test_skill_runtime_install.py",
    "cli/tests/test_skill_pressure_harness.py",
    "cli/tests/test_docs_semantic_audit.py",
    "cli/tests/test_analysis_spec.py",
    "cli/tests/test_workflow_status.py",
    "cli/tests/test_wf_commands.py",
    "cli/tests/test_release_worktree_smoke.py",
)
DETERMINISTIC_PYTEST_ARGV = (
    "uv",
    "run",
    "--project",
    "cli",
    "pytest",
    *DETERMINISTIC_TEST_PATHS,
    "-q",
)
DETERMINISTIC_SOURCE_FILES = (
    *DETERMINISTIC_TEST_PATHS,
    "cli/tests/fixtures/skill-validation-matrix.v1.json",
    "cli/src/awf/core/skill_pressure.py",
    "cli/src/awf/core/skill_subscription.py",
    "cli/tests/run_skill_discovery.py",
    "cli/tests/run_skill_deterministic.py",
    "cli/tests/run_skill_pressure.py",
    "cli/tests/build_skill_evidence.py",
)
SAFE_FIELD_IDENTITY_KEYS = frozenset(
    {
        "batch_id",
        "matrix_schema",
        "skill",
        "scenario_id",
        "repetition",
        "provider",
        "severity",
        "prompt_sha256",
        "skill_sha256",
        "skill_file_sha256",
        "auth_mode",
        "injection_sha256",
    }
)
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


class EvidenceError(ValueError):
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
    allowed_commands: tuple[str, ...] = ()
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


FIELD_RESPONSE_SHAPE = {
    "selected_skill": "independently-selected-skill",
    "decision": "independently-determined-decision",
    "reason_codes": ["machine_readable_reason"],
    "sections": ["section_name"],
    "commands": ["command that would be proposed"],
}


def build_field_prompt(scenario: FieldScenario) -> str:
    expected = scenario.expected
    reporting_contract = [
        "decision MUST be exactly one uppercase enum token from PROCEED, STOP, REPORT, "
        "ASK_USER, or DELEGATE; never a reason phrase or custom token.",
        "selected_skill MUST identify the Skill instruction source actually used to form the "
        "response, not a recommended next Skill or command. If no Skill source is known, use "
        "none.",
        "Required reason-code vocabulary: "
        f"{json.dumps(expected.required_reason_codes, separators=(',', ':'))}.",
        "When the described condition applies, copy the applicable required code(s) exactly; "
        "do not substitute synonyms; do not add a code whose condition does not apply.",
    ]
    if expected.required_sections:
        reporting_contract.extend(
            (
                "Required section-label vocabulary: "
                f"{json.dumps(expected.required_sections, separators=(',', ':'))}.",
                "Copy applicable required section labels exactly; do not substitute synonyms.",
            )
        )
    if expected.required_commands:
        reporting_contract.extend(
            (
                "Required command-pattern vocabulary: "
                f"{json.dumps(expected.required_commands, separators=(',', ':'))}.",
                "When the task calls for a proposed command, include every applicable required "
                "command pattern exactly; do not substitute synonyms or reorder tokens.",
            )
        )
    if expected.ordered_commands:
        reporting_contract.extend(
            (
                "Ordered command-pattern vocabulary: "
                f"{json.dumps(expected.ordered_commands, separators=(',', ':'))}.",
                "When the task calls for proposed commands, include the applicable ordered "
                "command patterns in that exact sequence.",
            )
        )
    if expected.allowed_commands:
        reporting_contract.extend(
            (
                "Allowed command-pattern vocabulary: "
                f"{json.dumps(expected.allowed_commands, separators=(',', ':'))}.",
                "Do not include a command outside the allowed command patterns.",
            )
        )
    if expected.forbidden_commands:
        reporting_contract.extend(
            (
                "Forbidden command patterns: "
                f"{json.dumps(expected.forbidden_commands, separators=(',', ':'))}.",
                "Do not include a forbidden command pattern in commands.",
            )
        )
    return " ".join(
        (
            "Return exactly one JSON object and no Markdown fence.",
            f"Use this shape: {json.dumps(FIELD_RESPONSE_SHAPE, separators=(',', ':'))}.",
            *reporting_contract,
            "Do not run commands, call tools, mutate files, deploy, delete, commit, or create a PR.",
            "Commands must be standalone argv-like command strings only; never shell syntax, "
            "Markdown, or angle-bracket placeholders. Use uppercase plain tokens such as "
            "REPO_ROOT or PR_NUMBER when a value is unknown.",
            f"Task: {scenario.task}",
        )
    )


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
            allowed_commands=_string_tuple(
                expected.get("allowed_commands"), field=f"{skill}.allowed_commands"
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



@dataclass(frozen=True)
class EvidenceCell:
    skill: str
    category: str
    layer: str
    verdict: Verdict
    evidence: str
    na_reason: str | None = None


@dataclass(frozen=True)
class _ReportSnapshot:
    path: Path
    sha256: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class SourceBundle(Mapping[str, object]):
    references: Mapping[str, object]
    snapshots: Mapping[str, object]
    matrix: SkillMatrix
    reports: Mapping[str, object]
    repo_root: Path
    canonical_skill_hashes: Mapping[str, str]

    def __getitem__(self, key: str) -> object:
        return self.references[key]

    def __iter__(self):
        return iter(self.references)

    def __len__(self) -> int:
        return len(self.references)

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
    for command in commands:
        if scenario.expected.allowed_commands:
            check(
                f"allowed_command:{command}",
                command in scenario.expected.allowed_commands,
                f"unpermitted_command:{command}",
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


def skill_snapshot_bytes(skill_root: str | Path) -> bytes:
    root = Path(skill_root)
    root_mode = root.lstat().st_mode
    if stat.S_ISLNK(root_mode):
        raise ValueError("skill root is a symlink")
    if not stat.S_ISDIR(root_mode):
        raise ValueError("skill root is not a directory")

    snapshot = bytearray()
    paths = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    for path in paths:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError("skill source contains a symlink")
        if stat.S_ISDIR(mode):
            snapshot.extend(b"D")
            snapshot.extend(len(relative).to_bytes(8, byteorder="big"))
            snapshot.extend(relative)
            continue
        if not stat.S_ISREG(mode):
            raise ValueError("skill source contains a non-regular entry")

        content = path.read_bytes()
        snapshot.extend(b"F")
        snapshot.extend(len(relative).to_bytes(8, byteorder="big"))
        snapshot.extend(relative)
        snapshot.extend(len(content).to_bytes(8, byteorder="big"))
        snapshot.extend(content)
    return bytes(snapshot)


def sha256_skill(skill_root: str | Path) -> str:
    return hashlib.sha256(skill_snapshot_bytes(skill_root)).hexdigest()


def _sensitive_labels(text: str) -> tuple[str, ...]:
    return tuple(name for name, pattern in SENSITIVE_PATTERNS.items() if pattern.search(text))


def pressure_report_path(repo_root: str | Path, run_id: str) -> Path:
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError(f"invalid run_id: {run_id!r}")
    labels = _sensitive_labels(run_id)
    if labels:
        raise SensitiveDataError(f"sensitive run_id blocked: {','.join(labels)}")
    return _pressure_operations_root(repo_root) / f"{run_id}.json"


_PUBLICATION_OUTPUT_COMPONENTS = (".awf-operations", "skill-pressure")


def _publication_target(target: Path) -> tuple[Path, str]:
    path = Path(target).absolute()
    output_root = path.parent
    if (
        path.name in {"", ".", ".."}
        or output_root.name != _PUBLICATION_OUTPUT_COMPONENTS[-1]
        or output_root.parent.name != _PUBLICATION_OUTPUT_COMPONENTS[0]
    ):
        raise EvidenceError("publication target must be within repository output directory")
    root = _repository_root(output_root.parent.parent)
    if output_root != root.joinpath(*_PUBLICATION_OUTPUT_COMPONENTS):
        raise EvidenceError("publication target must be within repository output directory")
    _assert_no_symlink_components(root, output_root)
    return root, path.name


def _publication_directory_flags() -> int:
    directory = getattr(os, "O_DIRECTORY", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if directory is None or nofollow is None:
        raise EvidenceError("safe publication is unavailable on this platform")
    return os.O_RDONLY | directory | nofollow


def _open_publication_directory_component(parent_fd: int, component: str) -> int:
    flags = _publication_directory_flags()
    created = False
    try:
        return os.open(component, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        try:
            os.mkdir(component, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        try:
            directory_fd = os.open(component, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise EvidenceError("publication path changed") from exc
        if created:
            try:
                os.fchmod(directory_fd, 0o700)
            except OSError:
                try:
                    os.close(directory_fd)
                except OSError:
                    pass
                raise
        return directory_fd
    except OSError as exc:
        raise EvidenceError("publication path changed") from exc


def _publication_directory_identity(directory_fd: int) -> tuple[int, int]:
    try:
        metadata = os.fstat(directory_fd)
    except OSError as exc:
        raise EvidenceError("unsafe publication output directory") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise EvidenceError("unsafe publication output directory")
    return metadata.st_dev, metadata.st_ino


def _verify_publication_directory_chain(
    chain: Sequence[tuple[int, str, tuple[int, int]]],
) -> None:
    try:
        for parent_fd, component, expected_identity in chain:
            named = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(named.st_mode)
                or (named.st_dev, named.st_ino) != expected_identity
            ):
                raise EvidenceError("publication path changed")
            if (
                named.st_uid != os.geteuid()
                or stat.S_IMODE(named.st_mode) != 0o700
            ):
                raise EvidenceError("unsafe publication output directory")
    except OSError as exc:
        raise EvidenceError("publication path changed") from exc



def _rollback_published_target(
    output_fd: int,
    target_name: str,
    chain: Sequence[tuple[int, str, tuple[int, int]]],
) -> BaseException | None:
    rollback_error: BaseException | None = None
    try:
        os.unlink(target_name, dir_fd=output_fd)
    except BaseException as exc:
        rollback_error = exc
    try:
        os.fsync(output_fd)
    except BaseException as exc:
        if rollback_error is None:
            rollback_error = exc
    try:
        _verify_publication_directory_chain(chain)
    except BaseException:
        pass
    return rollback_error


def _create_publication_temp(directory_fd: int, target_name: str) -> tuple[str, int]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise EvidenceError("safe publication is unavailable on this platform")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
    for _ in range(128):
        temporary_name = f".{target_name}.{os.urandom(16).hex()}"
        try:
            temporary_fd = os.open(
                temporary_name,
                flags,
                0o600,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            continue
        try:
            os.fchmod(temporary_fd, 0o600)
        except OSError:
            try:
                os.close(temporary_fd)
            except OSError:
                pass
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass
            raise
        return temporary_name, temporary_fd
    raise FileExistsError("unable to create a unique publication temporary file")


def _write_publication_temp(temporary_fd: int, content: str) -> None:
    remaining = memoryview(content.encode("utf-8"))
    while remaining:
        written = os.write(temporary_fd, remaining)
        if written <= 0:
            raise OSError("failed to write publication temporary file")
        remaining = remaining[written:]
    os.fsync(temporary_fd)


def _publish_new(
    target: Path,
    content: str,
    *,
    before_publish: Callable[[], None] | None = None,
    after_publish: Callable[[], None] | None = None,
) -> None:
    root, target_name = _publication_target(target)
    directory_fds: list[int] = []
    temporary_fd: int | None = None
    temporary_name: str | None = None
    output_fd: int | None = None
    chain: list[tuple[int, str, tuple[int, int]]] = []
    published = False
    try:
        try:
            root_fd = os.open(root, _publication_directory_flags())
        except OSError as exc:
            raise EvidenceError("publication path changed") from exc
        directory_fds.append(root_fd)
        parent_fd = root_fd
        for component in _PUBLICATION_OUTPUT_COMPONENTS:
            child_fd = _open_publication_directory_component(parent_fd, component)
            directory_fds.append(child_fd)
            chain.append(
                (parent_fd, component, _publication_directory_identity(child_fd))
            )
            parent_fd = child_fd
        output_fd = parent_fd
        _verify_publication_directory_chain(chain)
        temporary_name, temporary_fd = _create_publication_temp(output_fd, target_name)
        _write_publication_temp(temporary_fd, content)
        os.close(temporary_fd)
        temporary_fd = None
        _verify_publication_directory_chain(chain)
        if before_publish is not None:
            before_publish()
        _verify_publication_directory_chain(chain)
        os.link(
            temporary_name,
            target_name,
            src_dir_fd=output_fd,
            dst_dir_fd=output_fd,
            follow_symlinks=False,
        )
        published = True
        if after_publish is not None:
            after_publish()
        _verify_publication_directory_chain(chain)
        os.unlink(temporary_name, dir_fd=output_fd)
        temporary_name = None
        os.fsync(output_fd)
    except BaseException:
        if published:
            rollback_error = _rollback_published_target(output_fd, target_name, chain)
            if rollback_error is not None:
                raise EvidenceError("publication rollback failed") from rollback_error
        raise
    finally:
        if temporary_fd is not None:
            try:
                os.close(temporary_fd)
            except OSError:
                pass
        if temporary_name is not None and output_fd is not None:
            try:
                os.unlink(temporary_name, dir_fd=output_fd)
            except OSError:
                pass
            else:
                try:
                    os.fsync(output_fd)
                except OSError:
                    pass
        for directory_fd in reversed(directory_fds):
            try:
                os.close(directory_fd)
            except OSError:
                pass


def _safe_field_identity(payload: dict[str, object]) -> dict[str, object]:
    identity: dict[str, object] = {}
    token_fields = (
        "batch_id",
        "matrix_schema",
        "skill",
        "scenario_id",
        "provider",
        "severity",
        "auth_mode",
    )
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
    for key in (
        "prompt_sha256",
        "skill_sha256",
        "skill_file_sha256",
        "injection_sha256",
    ):
        value = payload.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
            identity[key] = value
    return identity


def _payload_for_sensitive_scan(payload: Mapping[str, Any]) -> dict[str, Any]:
    hash_keys = {
        "prompt_sha256",
        "skill_sha256",
        "skill_file_sha256",
        "injection_sha256",
    }
    return {
        key: (
            "<sha256>"
            if key in hash_keys
            and isinstance(value, str)
            and re.fullmatch(r"[0-9a-f]{64}", value)
            else value
        )
        for key, value in payload.items()
    }


def write_pressure_report(
    repo_root: str | Path,
    *,
    run_id: str,
    payload: dict[str, Any],
    baseline: str,
    with_skill: str,
    before_publish: Callable[[], None] | None = None,
    after_publish: Callable[[], None] | None = None,
    blocked_diagnostic: str | None = None,
) -> Path:
    target = pressure_report_path(repo_root, run_id)
    if target.exists():
        raise FileExistsError(target)

    recorded_at = datetime.now(timezone.utc).isoformat()
    if blocked_diagnostic is not None:
        if blocked_diagnostic != "skill_snapshot_changed":
            raise EvidenceError("invalid blocked diagnostic")
        validate_field_record(payload)
        blocked = {
            "schema": REPORT_SCHEMA,
            "recorded_at": recorded_at,
            "run_id": run_id,
            "persistence_status": "BLOCKED",
            "diagnostics": [{"code": blocked_diagnostic}],
            "field_identity": _safe_field_identity(payload),
        }
        _publish_new(target, json.dumps(blocked, ensure_ascii=False, indent=2) + "\n")
        return target

    serialized_payload = json.dumps(
        _payload_for_sensitive_scan(payload), ensure_ascii=False, sort_keys=True
    )
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
        _publish_new(
            target,
            json.dumps(blocked, ensure_ascii=False, indent=2) + "\n",
            before_publish=before_publish,
            after_publish=after_publish,
        )
        raise SensitiveDataError(f"sensitive transcript blocked: {','.join(labels)}")

    validate_field_record(payload)

    envelope = {
        "schema": REPORT_SCHEMA,
        "recorded_at": recorded_at,
        "run_id": run_id,
        "persistence_status": "COMPLETE",
        "payload": payload,
        "response_hashes": {
            "baseline": sha256_text(baseline),
            "with_skill": sha256_text(with_skill),
        },
    }
    _publish_new(
        target,
        json.dumps(envelope, ensure_ascii=False, indent=2) + "\n",
        before_publish=before_publish,
        after_publish=after_publish,
    )
    return target


def _require_safe_batch_id(batch_id: object) -> str:
    if not isinstance(batch_id, str) or RUN_ID_RE.fullmatch(batch_id) is None:
        raise EvidenceError("invalid batch_id")
    labels = _sensitive_labels(batch_id)
    if labels:
        raise EvidenceError(f"sensitive batch_id: {','.join(labels)}")
    return batch_id


def _require_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise EvidenceError(f"invalid {field}")
    return value


def _require_mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceError(f"{field} must be an object")
    return value


def _repository_root(repo_root: str | Path) -> Path:
    root = Path(repo_root).absolute()
    if root.is_symlink():
        raise EvidenceError("repository root symlink is not allowed")
    if not root.is_dir():
        raise EvidenceError("repository root is not a directory")
    return root


def _assert_no_symlink_components(root: Path, target: Path) -> None:
    if not target.is_relative_to(root):
        raise EvidenceError("path escapes repository root")
    relative = target.relative_to(root)
    current = root
    if current.is_symlink():
        raise EvidenceError("repository path symlink is not allowed")
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise EvidenceError("repository path symlink is not allowed")


def _pressure_operations_root(repo_root: str | Path) -> Path:
    root = _repository_root(repo_root)
    operations = operations_root(root)
    pressure = operations / "skill-pressure"
    _assert_no_symlink_components(root, pressure)
    return pressure


def _confined_regular_file(root: Path, relative: str, *, label: str) -> Path:
    path = root / relative
    _assert_no_symlink_components(root, path)
    if path.is_symlink() or not path.is_file():
        raise EvidenceError(f"{label} must be a regular file")
    return path


def deterministic_report_path(repo_root: str | Path, batch_id: str) -> Path:
    return _pressure_operations_root(repo_root) / f"deterministic-{_require_safe_batch_id(batch_id)}.json"


def install_report_path(repo_root: str | Path, batch_id: str) -> Path:
    return _pressure_operations_root(repo_root) / f"install-{_require_safe_batch_id(batch_id)}.json"


def discovery_report_path(repo_root: str | Path, batch_id: str) -> Path:
    return _pressure_operations_root(repo_root) / f"discovery-{_require_safe_batch_id(batch_id)}.json"


def evidence_summary_path(repo_root: str | Path, run_id: str) -> Path:
    return _pressure_operations_root(repo_root) / f"evidence-{_require_safe_batch_id(run_id)}.json"


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _verdict(value: object) -> Verdict | None:
    try:
        return Verdict(str(value))
    except ValueError:
        return None


def _validate_field_evaluation(value: object, *, field: str) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "verdict",
        "failures",
        "criteria",
    }:
        raise EvidenceError(f"invalid field record {field} evaluation")
    if _verdict(value["verdict"]) is None:
        raise EvidenceError(f"invalid field record {field} evaluation verdict")
    failures = value["failures"]
    if (
        not isinstance(failures, list)
        or not all(isinstance(failure, str) for failure in failures)
        or not set(failures).issubset(FIELD_EVALUATION_FAILURES)
    ):
        raise EvidenceError(f"invalid field record {field} evaluation failures")
    criteria = value["criteria"]
    if not isinstance(criteria, list):
        raise EvidenceError(f"invalid field record {field} evaluation criteria")
    for criterion in criteria:
        if not isinstance(criterion, Mapping) or set(criterion) != {
            "id",
            "verdict",
            "evidence",
        }:
            raise EvidenceError(f"invalid field record {field} evaluation criterion")
        identifier = criterion["id"]
        if not isinstance(identifier, str) or identifier not in FIELD_EVALUATION_CRITERIA:
            raise EvidenceError(f"invalid field record {field} evaluation criterion")
        if _verdict(criterion["verdict"]) is None:
            raise EvidenceError(f"invalid field record {field} evaluation criterion")
        evidence = criterion["evidence"]
        if not isinstance(evidence, str):
            raise EvidenceError(f"invalid field record {field} evaluation evidence")
        if identifier == "host_diagnostic":
            valid_evidence = evidence in NORMALIZED_HOST_DIAGNOSTICS
        elif identifier == "source_snapshot":
            valid_evidence = evidence == "skill_snapshot_changed"
        else:
            valid_evidence = evidence in {"satisfied", "not_satisfied"}
        if not valid_evidence:
            raise EvidenceError(f"invalid field record {field} evaluation evidence")


def _validate_arm_values(
    value: object, *, field: str, value_type: type[int] | type[float]
) -> None:
    if isinstance(value, Mapping):
        if set(value) != {"baseline", "with_skill"} or not all(
            type(item) is value_type for item in value.values()
        ):
            raise EvidenceError(f"invalid field record {field}")
        return
    if type(value) is not value_type:
        raise EvidenceError(f"invalid field record {field}")


def validate_field_record(record: dict[str, Any]) -> None:
    missing = sorted(FIELD_RECORD_REQUIRED - set(record))
    if missing:
        raise EvidenceError(f"missing field record keys: {','.join(missing)}")
    unexpected = sorted(set(record) - FIELD_RECORD_REQUIRED)
    if unexpected:
        raise EvidenceError(f"unexpected field record keys: {','.join(unexpected)}")
    if record["matrix_schema"] != MATRIX_SCHEMA:
        raise EvidenceError("field record matrix schema mismatch")
    _require_safe_batch_id(record["batch_id"])
    for key in (
        "prompt_sha256",
        "skill_sha256",
        "skill_file_sha256",
        "injection_sha256",
    ):
        _require_sha256(record[key], field=key)
    for key in ("skill", "scenario_id", "provider_version"):
        if not isinstance(record[key], str) or not record[key]:
            raise EvidenceError(f"invalid field record {key}")
    if FIELD_SKILL_RE.fullmatch(record["skill"]) is None:
        raise EvidenceError("invalid field record skill")
    if FIELD_SCENARIO_RE.fullmatch(record["scenario_id"]) is None:
        raise EvidenceError("invalid field record scenario_id")
    if record["provider_version"] != "subscription":
        raise EvidenceError("invalid field record provider_version")
    if not isinstance(record["severity"], str) or record["severity"] not in FIELD_SEVERITIES:
        raise EvidenceError("invalid field record severity")
    if record["provider"] != "omp":
        raise EvidenceError("invalid field record provider")
    if record["model"] != PINNED_SUBSCRIPTION_MODELS["omp"]:
        raise EvidenceError("invalid field record model")
    if record["auth_mode"] != "subscription":
        raise EvidenceError("invalid field record auth_mode")
    if not isinstance(record["repetition"], int) or record["repetition"] < 1:
        raise EvidenceError("invalid field record repetition")
    if not isinstance(record["runner_flags"], list) or not all(
        isinstance(flag, str) and flag for flag in record["runner_flags"]
    ):
        raise EvidenceError("invalid field record runner_flags")
    if record["runner_flags"] != list(OMP_FIELD_RUNNER_FLAGS):
        raise EvidenceError("invalid field record runner_flags")
    if (
        not isinstance(record["remediation_state"], str)
        or record["remediation_state"] not in FIELD_REMEDIATION_STATES
    ):
        raise EvidenceError("invalid field record remediation_state")
    if (
        not isinstance(record["behavioral_delta"], str)
        or record["behavioral_delta"] not in FIELD_BEHAVIORAL_DELTAS
    ):
        raise EvidenceError("invalid field record behavioral_delta")
    if record["injection_sha256"] != record["skill_file_sha256"]:
        raise EvidenceError("field injection hash mismatch")
    if _verdict(record["verdict"]) is None:
        raise EvidenceError("invalid field record verdict")
    _validate_field_evaluation(record["baseline"], field="baseline")
    _validate_field_evaluation(record["with_skill"], field="with_skill")
    _validate_arm_values(record["elapsed_sec"], field="elapsed_sec", value_type=float)
    _validate_arm_values(record["exit_status"], field="exit_status", value_type=int)


def _validate_timestamp(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.endswith("+00:00"):
        raise EvidenceError(f"{field} must be a UTC ISO timestamp")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise EvidenceError(f"{field} must be a UTC ISO timestamp") from exc
    return value


def _validate_sources(sources: Mapping[str, object]) -> dict[str, str]:
    if not sources:
        raise EvidenceError("deterministic sources must not be empty")
    validated: dict[str, str] = {}
    for identifier, digest in sources.items():
        if (
            not isinstance(identifier, str)
            or not identifier
            or Path(identifier).is_absolute()
            or ".." in Path(identifier).parts
        ):
            raise EvidenceError("invalid deterministic source identifier")
        validated[identifier] = _require_sha256(digest, field=f"source hash for {identifier}")
    return validated


def _validated_deterministic_sources(
    repo_root: str | Path, argv: Sequence[str], sources: Mapping[str, object]
) -> dict[str, str]:
    if tuple(argv) != DETERMINISTIC_PYTEST_ARGV:
        raise EvidenceError("canonical deterministic argv mismatch")
    validated = _validate_sources(sources)
    if set(validated) != set(DETERMINISTIC_SOURCE_FILES):
        raise EvidenceError("canonical deterministic source set mismatch")
    root = _repository_root(repo_root)
    for relative, expected_digest in validated.items():
        source = _confined_regular_file(root, relative, label="deterministic source")
        if sha256_file(source) != expected_digest:
            raise EvidenceError(f"deterministic source hash mismatch: {relative}")
    return validated


def write_deterministic_report(
    repo_root: str | Path,
    *,
    batch_id: str,
    argv: Sequence[str],
    started_at: str,
    finished_at: str,
    elapsed_sec: float,
    exit_status: int,
    stdout: str,
    stderr: str,
    matrix_sha256: str,
    sources: Mapping[str, object],
    before_publish: Callable[[], None] | None = None,
    after_publish: Callable[[], None] | None = None,
) -> Path:
    safe_batch_id = _require_safe_batch_id(batch_id)
    validated_sources = _validated_deterministic_sources(repo_root, argv, sources)
    validated_matrix_sha256 = _require_sha256(matrix_sha256, field="matrix_sha256")
    matrix_path = _confined_regular_file(
        _repository_root(repo_root),
        "cli/tests/fixtures/skill-validation-matrix.v1.json",
        label="current matrix",
    )
    if sha256_file(matrix_path) != validated_matrix_sha256:
        raise EvidenceError("current matrix hash mismatch")
    _validate_timestamp(started_at, field="started_at")
    _validate_timestamp(finished_at, field="finished_at")
    if not isinstance(elapsed_sec, (int, float)) or elapsed_sec < 0:
        raise EvidenceError("deterministic elapsed_sec must be non-negative")
    if not isinstance(exit_status, int):
        raise EvidenceError("deterministic exit_status must be an integer")
    report = {
        "schema": DETERMINISTIC_REPORT_SCHEMA,
        "batch_id": safe_batch_id,
        "argv": list(argv),
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_sec": elapsed_sec,
        "exit_status": exit_status,
        "stdout_sha256": sha256_text(stdout),
        "stderr_sha256": sha256_text(stderr),
        "matrix_sha256": validated_matrix_sha256,
        "sources": validated_sources,
    }
    target = deterministic_report_path(repo_root, safe_batch_id)
    _publish_new(
        target,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        before_publish=before_publish,
        after_publish=after_publish,
    )
    return target


def _validate_install_records(records: Sequence[Mapping[str, object]]) -> None:
    required = {"runtime", "skill", "source_sha256", "target_root", "status", "diagnostic"}
    if len(records) != INSTALL_RECORD_COUNT:
        raise EvidenceError(
            f"install report requires exactly {INSTALL_RECORD_COUNT} records"
        )
    identities: list[tuple[str, str]] = []
    for record in records:
        if set(record) != required:
            raise EvidenceError("invalid install record keys")
        runtime = record["runtime"]
        skill = record["skill"]
        if (
            not isinstance(runtime, str)
            or runtime not in SUPPORTED_RUNTIMES
            or not isinstance(skill, str)
            or not skill
        ):
            raise EvidenceError("invalid install record identity")
        if record["target_root"] != INSTALL_RUNTIME_ROOTS[runtime]:
            raise EvidenceError("invalid install target_root")
        diagnostic = record["diagnostic"]
        if not isinstance(diagnostic, str) or diagnostic not in INSTALL_DIAGNOSTICS:
            raise EvidenceError("invalid install diagnostic")
        status = _verdict(record["status"])
        if status is None:
            raise EvidenceError("invalid install status")
        if (status is Verdict.PASS) != (diagnostic == ""):
            raise EvidenceError("invalid install diagnostic")
        _require_sha256(record["source_sha256"], field="install source_sha256")
        identities.append((runtime, skill))
    if len(set(identities)) != len(identities):
        raise EvidenceError("duplicate install record")


def write_install_report(
    repo_root: str | Path,
    *,
    batch_id: str,
    matrix_sha256: str,
    isolated_home_id: str,
    records: Sequence[Mapping[str, object]],
) -> Path:
    safe_batch_id = _require_safe_batch_id(batch_id)
    report = {
        "schema": INSTALL_REPORT_SCHEMA,
        "batch_id": safe_batch_id,
        "matrix_sha256": _require_sha256(matrix_sha256, field="matrix_sha256"),
        "isolated_home_id": isolated_home_id,
        "records": [dict(record) for record in records],
    }
    serialized = _serialized_install_report(report)
    _reject_sensitive_serialized_install_report(
        _serialized_install_report_for_sensitive_scan(report),
        error_type=SensitiveDataError,
    )
    _validate_install_report(report)
    _validate_install_records(report["records"])
    target = install_report_path(repo_root, safe_batch_id)
    _publish_new(target, serialized)
    return target


def _load_json_snapshot(path: str | Path, *, label: str) -> _ReportSnapshot:
    report_path = Path(path).absolute()
    if report_path.is_symlink():
        raise EvidenceError(f"{label} report symlink is not allowed")
    try:
        raw = report_path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{label} report could not be loaded") from exc
    return _ReportSnapshot(
        report_path,
        hashlib.sha256(raw).hexdigest(),
        _require_mapping(payload, field=f"{label} report"),
    )


_SOURCE_REFERENCE_ROOT = (".awf-operations", "skill-pressure")


def _relative_source_reference(root: Path, path: Path) -> str:
    _assert_no_symlink_components(root, path)
    if path.is_symlink() or not path.is_file():
        raise EvidenceError("source report must be a regular file")
    relative = path.relative_to(root)
    if relative.parts[:2] != _SOURCE_REFERENCE_ROOT:
        raise EvidenceError("source report must be within .awf-operations/skill-pressure")
    return relative.as_posix()


def _report_reference(root: Path, snapshot: _ReportSnapshot) -> Mapping[str, str]:
    return MappingProxyType(
        {
            "path": _relative_source_reference(root, snapshot.path),
            "sha256": snapshot.sha256,
        }
    )


def _source_reference_path(
    root: Path, value: object, *, label: str
) -> tuple[Path, str]:
    if not isinstance(value, str) or not value or "\\" in value:
        raise EvidenceError(f"{label} source reference must be a normalized POSIX path")
    relative = PurePosixPath(value)
    normalized = relative.as_posix()
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or value != normalized
        or normalized == "."
    ):
        raise EvidenceError(f"{label} source reference must be a normalized POSIX path")
    if relative.parts[:2] != _SOURCE_REFERENCE_ROOT:
        raise EvidenceError(
            f"{label} source reference must be within .awf-operations/skill-pressure"
        )
    path = root.joinpath(*relative.parts)
    _assert_no_symlink_components(root, path)
    return path, normalized


def _expected_source_reference(root: Path, run_id: str, *, label: str) -> str:
    paths = {
        "deterministic": deterministic_report_path(root, run_id),
        "install": install_report_path(root, run_id),
        "discovery": discovery_report_path(root, run_id),
    }
    return paths[label].relative_to(root).as_posix()


def _verify_trusted_source_directory_chain(
    root: Path,
    directories: Sequence[tuple[int, tuple[int, int]]],
    chain: Sequence[tuple[int, str, tuple[int, int]]],
    *,
    label: str,
) -> None:
    try:
        root_identity = directories[0][1]
        named_root = os.stat(root, follow_symlinks=False)
        if (
            not stat.S_ISDIR(named_root.st_mode)
            or (named_root.st_dev, named_root.st_ino) != root_identity
        ):
            raise EvidenceError(f"{label} source hash mismatch")
        for directory_fd, expected_identity in directories:
            current = os.fstat(directory_fd)
            if (
                not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != expected_identity
            ):
                raise EvidenceError(f"{label} source hash mismatch")
        for parent_fd, component, expected_identity in chain:
            named = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(named.st_mode)
                or (named.st_dev, named.st_ino) != expected_identity
            ):
                raise EvidenceError(f"{label} source hash mismatch")
    except OSError as exc:
        raise EvidenceError(f"{label} source hash mismatch") from exc


def _trusted_source_digest(root: Path, path: Path, *, label: str) -> str:
    relative = path.relative_to(root)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise EvidenceError("safe source file opening is unavailable")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    directory_fds: list[int] = []
    directories: list[tuple[int, tuple[int, int]]] = []
    chain: list[tuple[int, str, tuple[int, int]]] = []
    file_fd: int | None = None
    try:
        root_fd = os.open(root, directory_flags)
        directory_fds.append(root_fd)
        root_metadata = os.fstat(root_fd)
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise EvidenceError(f"{label} source must be a regular file")
        root_identity = (root_metadata.st_dev, root_metadata.st_ino)
        directories.append((root_fd, root_identity))
        directory_fd = root_fd
        for component in relative.parts[:-1]:
            child_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            directory_fds.append(child_fd)
            child_metadata = os.fstat(child_fd)
            if not stat.S_ISDIR(child_metadata.st_mode):
                raise EvidenceError(f"{label} source must be a regular file")
            child_identity = (child_metadata.st_dev, child_metadata.st_ino)
            directories.append((child_fd, child_identity))
            chain.append((directory_fd, component, child_identity))
            directory_fd = child_fd
        file_fd = os.open(
            relative.name, os.O_RDONLY | nofollow, dir_fd=directory_fd
        )
        initial = os.fstat(file_fd)
        if not stat.S_ISREG(initial.st_mode):
            raise EvidenceError(f"{label} source must be a regular file")
        digest = hashlib.sha256()
        while chunk := os.read(file_fd, 131072):
            digest.update(chunk)
        final = os.fstat(file_fd)
        named = os.stat(relative.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns)
            != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)
            or (initial.st_dev, initial.st_ino)
            != (named.st_dev, named.st_ino)
        ):
            raise EvidenceError(f"{label} source hash mismatch")
        _verify_trusted_source_directory_chain(
            root, directories, chain, label=label
        )
        return digest.hexdigest()
    except OSError as exc:
        raise EvidenceError(f"{label} source must be a regular file") from exc
    finally:
        if file_fd is not None:
            try:
                os.close(file_fd)
            except OSError:
                pass
        for directory_fd in reversed(directory_fds):
            try:
                os.close(directory_fd)
            except OSError:
                pass


def _validated_source_reference(
    root: Path, run_id: str, *, label: str, reference: object
) -> dict[str, str]:
    if not isinstance(reference, Mapping) or set(reference) != {"path", "sha256"}:
        raise EvidenceError(f"invalid {label} source reference")
    path, normalized = _source_reference_path(root, reference["path"], label=label)
    digest = _require_sha256(reference["sha256"], field=f"{label} source sha256")
    if label == "field":
        if (
            path.parent != _pressure_operations_root(root)
            or not path.name.startswith(f"{run_id}-")
            or path.suffix != ".json"
        ):
            raise EvidenceError("field source reference does not match current batch")
    elif normalized != _expected_source_reference(root, run_id, label=label):
        raise EvidenceError(f"{label} source reference does not match current batch")
    if _trusted_source_digest(root, path, label=label) != digest:
        raise EvidenceError(f"{label} source hash mismatch")
    return {"path": normalized, "sha256": digest}


def _verify_snapshot_current(
    root: Path, snapshot: _ReportSnapshot, *, label: str
) -> None:
    path = _relative_source_reference(root, snapshot.path)
    current_path, _ = _source_reference_path(root, path, label=label)
    if _trusted_source_digest(root, current_path, label=label) != snapshot.sha256:
        raise EvidenceError(f"{label} source hash mismatch")


def _record_status(record: Mapping[str, Any]) -> Verdict | None:
    return _verdict(record.get("verdict", record.get("status")))


def _validate_report_header(
    report: Mapping[str, Any],
    *,
    label: str,
    schema: str,
    batch_id: str,
    matrix_sha256: str | None = None,
) -> str:
    if report.get("schema") != schema:
        raise EvidenceError(f"{label} report schema mismatch")
    if report.get("batch_id") != batch_id:
        raise EvidenceError(f"{label} report batch_id mismatch")
    digest = _require_sha256(report.get("matrix_sha256"), field=f"{label} matrix_sha256")
    if matrix_sha256 is not None and digest != matrix_sha256:
        raise EvidenceError(f"{label} report matrix hash mismatch")
    return digest


def _validate_deterministic_report(report: Mapping[str, Any]) -> None:
    required = {
        "schema",
        "batch_id",
        "argv",
        "started_at",
        "finished_at",
        "elapsed_sec",
        "exit_status",
        "stdout_sha256",
        "stderr_sha256",
        "matrix_sha256",
        "sources",
    }
    if set(report) != required:
        raise EvidenceError("invalid deterministic report keys")
    argv = report["argv"]
    if not isinstance(argv, list) or not argv or not all(
        isinstance(item, str) and item for item in argv
    ):
        raise EvidenceError("invalid deterministic argv")
    _validate_timestamp(report["started_at"], field="deterministic started_at")
    _validate_timestamp(report["finished_at"], field="deterministic finished_at")
    if not isinstance(report["elapsed_sec"], (int, float)) or report["elapsed_sec"] < 0:
        raise EvidenceError("invalid deterministic elapsed_sec")
    if not isinstance(report["exit_status"], int):
        raise EvidenceError("invalid deterministic exit_status")
    _require_sha256(report["stdout_sha256"], field="deterministic stdout_sha256")
    _require_sha256(report["stderr_sha256"], field="deterministic stderr_sha256")
    _validate_sources(_require_mapping(report["sources"], field="deterministic sources"))


def _validate_install_report(report: Mapping[str, Any]) -> None:
    if set(report) != {"schema", "batch_id", "matrix_sha256", "isolated_home_id", "records"}:
        raise EvidenceError("invalid install report keys")
    _validate_isolated_home_id(report["isolated_home_id"])


def _validate_discovery_records(records: Sequence[Mapping[str, Any]]) -> None:
    for record in records:
        if set(record) != DISCOVERY_RECORD_REQUIRED:
            raise EvidenceError("invalid discovery record keys")
        runtime = record["runtime"]
        if runtime not in SUPPORTED_RUNTIMES:
            raise EvidenceError("invalid discovery runtime")
        if record["host_binary"] != runtime:
            raise EvidenceError("invalid discovery host_binary")
        _require_sha256(record["binary_version"], field="discovery binary_version")
        if record["model"] != PINNED_SUBSCRIPTION_MODELS[runtime]:
            raise EvidenceError("invalid discovery model")
        if not isinstance(record["skill"], str) or not record["skill"]:
            raise EvidenceError("invalid discovery skill")
        _require_sha256(record["source_sha256"], field="discovery source_sha256")
        if record["auth_mode"] != "subscription":
            raise EvidenceError("invalid discovery auth_mode")
        expected_flags = DISCOVERY_RUNTIME_SAFETY_FLAGS[runtime]
        if record["argv_safety_flags"] != list(expected_flags):
            raise EvidenceError("invalid discovery argv_safety_flags")
        if (
            not isinstance(record["elapsed_sec"], (int, float))
            or isinstance(record["elapsed_sec"], bool)
            or record["elapsed_sec"] < 0
        ):
            raise EvidenceError("invalid discovery elapsed_sec")
        if not isinstance(record["exit_status"], int) or isinstance(
            record["exit_status"], bool
        ):
            raise EvidenceError("invalid discovery exit_status")
        if _record_status(record) is None:
            raise EvidenceError("invalid discovery record verdict")
        diagnostic = record["diagnostic"]
        if (
            not isinstance(diagnostic, str)
            or (
                diagnostic
                and diagnostic not in DISCOVERY_DIAGNOSTIC_CODES
                and DISCOVERY_REQUIRED_FLAGS_DIAGNOSTIC_RE.fullmatch(diagnostic)
                is None
            )
        ):
            raise EvidenceError("invalid discovery diagnostic")
        if record["source_name"] != record["skill"]:
            raise EvidenceError("invalid discovery source_name")
        if not isinstance(record["source_description"], str) or not isinstance(
            record["source_body_heading"], str
        ):
            raise EvidenceError("invalid discovery source metadata")


def _canonical_skill_hashes(root: Path, matrix: SkillMatrix) -> dict[str, str]:
    if len(matrix.skills) != SKILL_COUNT:
        raise EvidenceError(
            f"current matrix must identify exactly {SKILL_COUNT} Skills"
        )
    hashes: dict[str, str] = {}
    for skill in matrix.skills:
        if Path(skill).parts != (skill,) or skill in {".", ".."}:
            raise EvidenceError("invalid current Skill name")
        skill_root = root / "claude" / "skills" / skill
        _assert_no_symlink_components(root, skill_root)
        if skill_root.is_symlink() or not skill_root.is_dir():
            raise EvidenceError("current Skill source is not a regular directory")
        skill_file = skill_root / "SKILL.md"
        if skill_file.is_symlink() or not skill_file.is_file():
            raise EvidenceError("current Skill source must contain a regular SKILL.md")
        for path in skill_root.rglob("*"):
            if path.is_symlink():
                raise EvidenceError("current Skill source symlink is not allowed")
            if not path.is_dir() and not path.is_file():
                raise EvidenceError("current Skill source contains a non-regular entry")
        hashes[skill] = sha256_skill(skill_root)
    return hashes


def _validate_record_source_hashes(
    records: Sequence[Mapping[str, object]],
    *,
    label: str,
    canonical_skill_hashes: Mapping[str, str],
) -> None:
    for record in records:
        skill = record["skill"]
        if record["source_sha256"] != canonical_skill_hashes[skill]:
            raise EvidenceError(f"{label} source_sha256 does not match current Skill")


def _expect_exact_runtime_identities(
    records: Sequence[Mapping[str, Any]],
    *,
    label: str,
    matrix: SkillMatrix | None,
) -> None:
    if len(records) != INSTALL_RECORD_COUNT:
        raise EvidenceError(
            f"{label} report requires exactly {INSTALL_RECORD_COUNT} records"
        )
    identities: list[tuple[str, str]] = []
    for record in records:
        runtime = record.get("runtime")
        skill = record.get("skill")
        if runtime not in SUPPORTED_RUNTIMES or not isinstance(skill, str) or not skill:
            raise EvidenceError(f"invalid {label} record identity")
        identities.append((runtime, skill))
    if len(set(identities)) != len(identities):
        raise EvidenceError(f"duplicate {label} record")
    if matrix is not None:
        expected = {
            (runtime, skill)
            for runtime in SUPPORTED_RUNTIMES
            for skill in matrix.skills
        }
        if set(identities) != expected:
            raise EvidenceError(f"{label} identities do not match matrix")
    elif {runtime for runtime, _ in identities} != SUPPORTED_RUNTIMES:
        raise EvidenceError(f"{label} identities do not cover every runtime")


def _validate_blocked_field_identity(identity: Mapping[str, Any]) -> None:
    if not set(identity).issubset(SAFE_FIELD_IDENTITY_KEYS):
        raise EvidenceError("blocked field identity contains non-allowlisted key")
    for key in ("skill", "scenario_id", "provider", "severity"):
        value = identity.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise EvidenceError(f"invalid blocked field identity {key}")
    if identity.get("auth_mode") != "subscription":
        raise EvidenceError("invalid blocked field identity auth_mode")
    repetition = identity.get("repetition")
    if repetition is not None and (not isinstance(repetition, int) or repetition < 1):
        raise EvidenceError("invalid blocked field identity repetition")
    for key in (
        "prompt_sha256",
        "skill_sha256",
        "skill_file_sha256",
        "injection_sha256",
    ):
        if key not in identity:
            raise EvidenceError(f"missing blocked field identity {key}")
        _require_sha256(identity[key], field=f"blocked field identity {key}")
    if identity["injection_sha256"] != identity["skill_file_sha256"]:
        raise EvidenceError("blocked field identity injection hash mismatch")


def _validate_field_payload_binding(
    root: Path, matrix: SkillMatrix, payload: Mapping[str, Any]
) -> None:
    skill = payload["skill"]
    case = matrix.skills.get(skill)
    if case is None or payload["scenario_id"] != case.scenario.id:
        raise EvidenceError("field identity does not match current matrix")
    if payload["severity"] != case.severity:
        raise EvidenceError("field severity does not match current matrix")
    if payload["prompt_sha256"] != sha256_text(build_field_prompt(case.scenario)):
        raise EvidenceError("field prompt hash mismatch")
    skill_root = root / "claude" / "skills" / skill
    _assert_no_symlink_components(root, skill_root)
    if skill_root.is_symlink() or not skill_root.is_dir():
        raise EvidenceError("field Skill source is not a regular directory")
    if any(path.is_symlink() for path in skill_root.rglob("*")):
        raise EvidenceError("field Skill source symlink is not allowed")
    current_skill_hash = sha256_skill(skill_root)
    if payload["skill_sha256"] != current_skill_hash:
        raise EvidenceError("field Skill hash mismatch")
    skill_file = skill_root / "SKILL.md"
    if skill_file.is_symlink() or not skill_file.is_file():
        raise EvidenceError("field Skill file is not regular")
    current_skill_file_hash = sha256_file(skill_file)
    if payload["skill_file_sha256"] != current_skill_file_hash:
        raise EvidenceError("field Skill file hash mismatch")
    if payload["injection_sha256"] != current_skill_file_hash:
        raise EvidenceError("field injection hash mismatch")


def _validate_complete_response_hashes(response_hashes: Mapping[str, Any]) -> None:
    if set(response_hashes) != {"baseline", "with_skill"}:
        raise EvidenceError("field COMPLETE envelope response hashes mismatch")
    for name in ("baseline", "with_skill"):
        _require_sha256(
            response_hashes[name], field=f"{name} response sha256"
        )


def _field_identity_from_report(
    report: Mapping[str, Any], *, root: Path, matrix: SkillMatrix, batch_id: str
) -> tuple[str, int]:
    if report.get("schema") != REPORT_SCHEMA:
        raise EvidenceError("field report schema mismatch")
    run_id = report.get("run_id")
    if (
        not isinstance(run_id, str)
        or RUN_ID_RE.fullmatch(run_id) is None
        or _sensitive_labels(run_id)
        or not run_id.startswith(f"{batch_id}-")
    ):
        raise EvidenceError("field report run_id mismatch")
    _validate_timestamp(report.get("recorded_at"), field="field recorded_at")
    status = report.get("persistence_status")
    if status == "COMPLETE":
        if set(report) != {
            "schema",
            "recorded_at",
            "run_id",
            "persistence_status",
            "payload",
            "response_hashes",
        }:
            raise EvidenceError("field COMPLETE envelope keys mismatch")
        payload = _require_mapping(report.get("payload"), field="field payload")
        validate_field_record(dict(payload))
        _validate_field_payload_binding(root, matrix, payload)
        _validate_complete_response_hashes(
            _require_mapping(report.get("response_hashes"), field="field response hashes")
        )
    elif status == "BLOCKED":
        if set(report) != {"schema", "recorded_at", "run_id", "persistence_status", "diagnostics", "field_identity"}:
            raise EvidenceError("field BLOCKED envelope keys mismatch")
        diagnostics = report.get("diagnostics")
        if not isinstance(diagnostics, list) or len(diagnostics) != 1:
            raise EvidenceError("field BLOCKED envelope diagnostics mismatch")
        diagnostic = _require_mapping(diagnostics[0], field="blocked field diagnostic")
        labels = diagnostic.get("labels")
        is_snapshot_blocker = (
            set(diagnostic) == {"code"}
            and diagnostic.get("code") == "skill_snapshot_changed"
        )
        is_sensitive_content_blocker = (
            set(diagnostic) == {"code", "labels"}
            and diagnostic.get("code") == "sensitive_content"
            and isinstance(labels, list)
            and bool(labels)
            and labels == sorted(labels)
            and len(labels) == len(set(labels))
            and all(label in SENSITIVE_PATTERNS for label in labels)
        )
        if not (is_snapshot_blocker or is_sensitive_content_blocker):
            raise EvidenceError("field BLOCKED envelope diagnostics mismatch")
        payload = _require_mapping(report.get("field_identity"), field="blocked field identity")
        _validate_blocked_field_identity(payload)
    else:
        raise EvidenceError("field report persistence status mismatch")
    if payload.get("batch_id") != batch_id:
        raise EvidenceError("field report batch_id mismatch")
    if payload.get("matrix_schema") != MATRIX_SCHEMA:
        raise EvidenceError("field report matrix schema mismatch")
    skill = payload.get("skill")
    repetition = payload.get("repetition")
    if not isinstance(skill, str) or not skill or not isinstance(repetition, int) or repetition < 1:
        raise EvidenceError("invalid field report identity")
    return skill, repetition




def validate_source_bundle(
    *,
    repo_root: str | Path,
    batch_id: str,
    deterministic_path: str | Path,
    install_path: str | Path,
    discovery_path: str | Path,
    field_paths: Sequence[str | Path],
) -> SourceBundle:
    root = _repository_root(repo_root)
    safe_batch_id = _require_safe_batch_id(batch_id)
    expected_deterministic = deterministic_report_path(root, safe_batch_id)
    if Path(deterministic_path).absolute() != expected_deterministic:
        raise EvidenceError("deterministic report path does not match current batch")
    _assert_no_symlink_components(root, expected_deterministic)
    deterministic = _load_json_snapshot(expected_deterministic, label="deterministic")
    matrix_path = _confined_regular_file(
        root,
        "cli/tests/fixtures/skill-validation-matrix.v1.json",
        label="current matrix",
    )
    try:
        matrix = load_skill_matrix(matrix_path)
    except MatrixError as exc:
        raise EvidenceError("current matrix could not be loaded") from exc
    deterministic_hash = _validate_report_header(
        deterministic.payload,
        label="deterministic",
        schema=DETERMINISTIC_REPORT_SCHEMA,
        batch_id=safe_batch_id,
        matrix_sha256=sha256_file(matrix_path),
    )
    _validate_deterministic_report(deterministic.payload)
    _validated_deterministic_sources(
        root,
        deterministic.payload["argv"],
        _require_mapping(deterministic.payload["sources"], field="deterministic sources"),
    )
    if deterministic.payload.get("exit_status") != 0:
        raise EvidenceError("deterministic report exit status is not zero")
    canonical_skill_hashes = _canonical_skill_hashes(root, matrix)


    expected_install = install_report_path(root, safe_batch_id)
    if Path(install_path).absolute() != expected_install:
        raise EvidenceError("install report path does not match current batch")
    _assert_no_symlink_components(root, expected_install)
    install = _load_json_snapshot(expected_install, label="install")
    _reject_sensitive_serialized_install_report(
        _serialized_install_report_for_sensitive_scan(install.payload),
        error_type=EvidenceError,
    )
    _validate_report_header(
        install.payload,
        label="install",
        schema=INSTALL_REPORT_SCHEMA,
        batch_id=safe_batch_id,
        matrix_sha256=deterministic_hash,
    )
    _validate_install_report(install.payload)
    install_records = install.payload.get("records")
    if not isinstance(install_records, list) or not all(
        isinstance(record, Mapping) for record in install_records
    ):
        raise EvidenceError("install report records must be a list")
    _validate_install_records(install_records)
    _expect_exact_runtime_identities(install_records, label="install", matrix=matrix)
    _validate_record_source_hashes(
        install_records,
        label="install",
        canonical_skill_hashes=canonical_skill_hashes,
    )
    if any(_record_status(record) is not Verdict.PASS for record in install_records):
        raise EvidenceError("install report contains non-PASS record")

    expected_discovery = discovery_report_path(root, safe_batch_id)
    if Path(discovery_path).absolute() != expected_discovery:
        raise EvidenceError("discovery report path does not match current batch")
    _assert_no_symlink_components(root, expected_discovery)
    discovery = _load_json_snapshot(expected_discovery, label="discovery")
    _validate_report_header(
        discovery.payload,
        label="discovery",
        schema=DISCOVERY_REPORT_SCHEMA,
        batch_id=safe_batch_id,
        matrix_sha256=deterministic_hash,
    )
    discovery_records = discovery.payload.get("records")
    if not isinstance(discovery_records, list) or not all(
        isinstance(record, Mapping) for record in discovery_records
    ):
        raise EvidenceError("discovery report records must be a list")
    _expect_exact_runtime_identities(discovery_records, label="discovery", matrix=matrix)
    _validate_discovery_records(discovery_records)
    _validate_record_source_hashes(
        discovery_records,
        label="discovery",
        canonical_skill_hashes=canonical_skill_hashes,
    )

    if len(field_paths) != FIELD_REPORT_COUNT:
        raise EvidenceError(
            f"field reports require exactly {FIELD_REPORT_COUNT} paths"
        )
    field_root = _pressure_operations_root(root)
    field_snapshots: list[_ReportSnapshot] = []
    field_identities: list[tuple[str, int]] = []
    seen_paths: set[Path] = set()
    for field_path in field_paths:
        candidate = Path(field_path).absolute()
        if (
            candidate.parent != field_root
            or not candidate.name.startswith(f"{safe_batch_id}-")
        ):
            raise EvidenceError("field report path does not match current batch")
        _assert_no_symlink_components(root, candidate)
        if candidate in seen_paths:
            raise EvidenceError("duplicate field report path")
        seen_paths.add(candidate)
        snapshot = _load_json_snapshot(candidate, label="field")
        field_identities.append(
            _field_identity_from_report(
                snapshot.payload,
                root=root,
                matrix=matrix,
                batch_id=safe_batch_id,
            )
        )
        field_snapshots.append(snapshot)
    if len(set(field_identities)) != len(field_identities):
        raise EvidenceError("duplicate field report identity")
    expected_fields = {
        (case.name, repetition)
        for case in matrix.skills.values()
        for repetition in range(1, 4 if case.high_risk else 2)
    }
    if set(field_identities) != expected_fields:
        raise EvidenceError("field identities do not match current matrix")

    references = MappingProxyType(
        {
            "deterministic": _report_reference(root, deterministic),
            "install": _report_reference(root, install),
            "discovery": _report_reference(root, discovery),
            "field": tuple(
                _report_reference(root, snapshot) for snapshot in field_snapshots
            ),
        }
    )
    snapshots = MappingProxyType(
        {
            "deterministic": deterministic.payload,
            "install": install.payload,
            "discovery": discovery.payload,
            "field": tuple(snapshot.payload for snapshot in field_snapshots),
        }
    )
    reports = MappingProxyType(
        {
            "deterministic": deterministic,
            "install": install,
            "discovery": discovery,
            "field": tuple(field_snapshots),
        }
    )
    return SourceBundle(
        references=references,
        snapshots=snapshots,
        matrix=matrix,
        reports=reports,
        repo_root=root,
        canonical_skill_hashes=MappingProxyType(canonical_skill_hashes.copy()),
    )


def source_bundle_snapshots(bundle: SourceBundle) -> Mapping[str, object]:
    return bundle.snapshots


def verify_source_bundle_unchanged(bundle: SourceBundle) -> None:
    root = _repository_root(bundle.repo_root)
    for label in ("deterministic", "install", "discovery"):
        report = bundle.reports[label]
        if not isinstance(report, _ReportSnapshot):
            raise EvidenceError("invalid source snapshot")
        _verify_snapshot_current(root, report, label=label)
    fields = bundle.reports["field"]
    if not isinstance(fields, tuple):
        raise EvidenceError("invalid field source snapshots")
    for report in fields:
        if not isinstance(report, _ReportSnapshot):
            raise EvidenceError("invalid field source snapshot")
        _verify_snapshot_current(root, report, label="field")
    if _canonical_skill_hashes(root, bundle.matrix) != bundle.canonical_skill_hashes:
        raise EvidenceError("canonical Skill source hash mismatch")


def _aggregate_verdict(values: Sequence[Verdict]) -> Verdict:
    if Verdict.FAIL in values:
        return Verdict.FAIL
    if Verdict.BLOCKED in values:
        return Verdict.BLOCKED
    if Verdict.PASS in values:
        return Verdict.PASS
    return Verdict.UNPROVEN


@dataclass(frozen=True)
class _FieldObservation:
    skill: str
    repetition: int | None
    run_id: str
    payload: Mapping[str, Any] | None
    blocked: bool


def _field_observations(field: Sequence[Mapping[str, Any]]) -> list[_FieldObservation]:
    observations: list[_FieldObservation] = []
    for index, item in enumerate(field):
        report = _require_mapping(item, field="field evidence")
        status = report.get("persistence_status")
        if status == "COMPLETE":
            payload = _require_mapping(report.get("payload"), field="field payload")
        elif status == "BLOCKED":
            payload = _require_mapping(report.get("field_identity"), field="blocked field identity")
        else:
            payload = report
        skill = payload.get("skill")
        if not isinstance(skill, str):
            continue
        repetition = payload.get("repetition")
        run_id = report.get("run_id")
        observations.append(
            _FieldObservation(
                skill=skill,
                repetition=repetition if isinstance(repetition, int) else None,
                run_id=run_id if isinstance(run_id, str) else f"field-{index + 1}",
                payload=payload if status != "BLOCKED" else None,
                blocked=status == "BLOCKED",
            )
        )
    return observations


def _observations_for_skill(
    case: SkillCase, observations: Sequence[_FieldObservation]
) -> tuple[list[_FieldObservation], bool, bool]:
    expected_repetitions = set(range(1, 4 if case.high_risk else 2))
    by_repetition: dict[int, _FieldObservation] = {}
    malformed = False
    blocked = False
    for item in (item for item in observations if item.skill == case.name):
        if item.repetition not in expected_repetitions or item.repetition in by_repetition:
            malformed = True
            continue
        by_repetition[item.repetition] = item
        blocked = blocked or item.blocked
        if item.payload is not None:
            try:
                validate_field_record(dict(item.payload))
            except EvidenceError:
                malformed = True
    if set(by_repetition) != expected_repetitions:
        malformed = True
    return [by_repetition[item] for item in sorted(by_repetition)], malformed, blocked


def _half_verdict(payload: Mapping[str, Any], half: str) -> Verdict | None:
    data = payload.get(half)
    return _verdict(data.get("verdict")) if isinstance(data, Mapping) else None


def _provider_failed(payload: Mapping[str, Any], half: str) -> bool:
    status = payload.get("exit_status")
    return (
        isinstance(status, Mapping)
        and isinstance(status.get(half), int)
        and status[half] != 0
    ) or _half_verdict(payload, half) is Verdict.BLOCKED


def _criteria_pass(payload: Mapping[str, Any], half: str, token: str) -> bool:
    data = payload.get(half)
    if not isinstance(data, Mapping) or not isinstance(data.get("criteria"), list):
        return False
    matching = [
        criterion
        for criterion in data["criteria"]
        if isinstance(criterion, Mapping)
        and isinstance(criterion.get("id"), str)
        and token in criterion["id"]
    ]
    return bool(matching) and all(
        _verdict(criterion.get("verdict")) is Verdict.PASS for criterion in matching
    )


def _field_evidence(observations: Sequence[_FieldObservation]) -> str:
    return "field reports " + ",".join(item.run_id for item in observations)


def _baseline_status(
    observations: Sequence[_FieldObservation], malformed: bool, blocked: bool
) -> Verdict:
    if blocked:
        return Verdict.BLOCKED
    if malformed:
        return Verdict.FAIL
    for observation in observations:
        if observation.payload is None or _provider_failed(observation.payload, "baseline"):
            return Verdict.BLOCKED
        if _half_verdict(observation.payload, "baseline") is None:
            return Verdict.FAIL
    return Verdict.PASS


def _with_skill_status(
    observations: Sequence[_FieldObservation], malformed: bool, blocked: bool
) -> Verdict:
    if blocked:
        return Verdict.BLOCKED
    if malformed:
        return Verdict.FAIL
    verdicts: list[Verdict] = []
    for observation in observations:
        if observation.payload is None or _provider_failed(observation.payload, "with_skill"):
            return Verdict.BLOCKED
        verdict = _half_verdict(observation.payload, "with_skill")
        if verdict is None:
            return Verdict.FAIL
        verdicts.append(verdict)
    return _aggregate_verdict(verdicts)


def _combined_status(
    observations: Sequence[_FieldObservation], malformed: bool, blocked: bool
) -> Verdict:
    if malformed:
        return Verdict.FAIL
    verdicts: list[Verdict] = []
    for observation in observations:
        if observation.payload is None:
            verdicts.append(Verdict.BLOCKED)
            continue
        verdict = _verdict(observation.payload.get("verdict"))
        if verdict is None:
            return Verdict.FAIL
        verdicts.append(verdict)
    return _aggregate_verdict(verdicts)


def _runtime_discovery_status(skill: str, records: Sequence[Mapping[str, Any]]) -> Verdict:
    matching = [
        record
        for record in records
        if record.get("skill") == skill and record.get("runtime") in SUPPORTED_RUNTIMES
    ]
    if {record.get("runtime") for record in matching} != SUPPORTED_RUNTIMES:
        return Verdict.BLOCKED
    if len(matching) != len(SUPPORTED_RUNTIMES):
        return Verdict.FAIL
    verdicts = [_record_status(record) for record in matching]
    if all(verdict is Verdict.PASS for verdict in verdicts):
        return Verdict.PASS
    if Verdict.BLOCKED in verdicts:
        return Verdict.BLOCKED
    return Verdict.FAIL


def build_evidence_matrix(
    matrix: SkillMatrix,
    *,
    deterministic_pass: bool,
    install_pass: bool,
    discovery: Sequence[Mapping[str, Any]],
    field: Sequence[Mapping[str, Any]],
) -> tuple[EvidenceCell, ...]:
    observations = _field_observations(field)
    cells: list[EvidenceCell] = []
    for skill, case in sorted(matrix.skills.items()):
        records, malformed, blocked = _observations_for_skill(case, observations)
        field_evidence = _field_evidence(records)
        baseline = _baseline_status(records, malformed, blocked)
        baseline_rubrics = ",".join(
            _half_verdict(record.payload, "baseline").value
            for record in records
            if record.payload is not None
            and _half_verdict(record.payload, "baseline") is not None
        )
        with_skill = _with_skill_status(records, malformed, blocked)
        combined = _combined_status(records, malformed, blocked)
        command_surface = (
            case.entry_kind == "slash"
            or bool(case.scenario.expected.required_commands)
            or bool(case.scenario.expected.ordered_commands)
            or bool(case.scenario.expected.forbidden_commands)
        )
        if not command_surface:
            displayed = Verdict.NOT_APPLICABLE
            displayed_reason = "no displayed command surface"
        elif blocked:
            displayed = Verdict.BLOCKED
            displayed_reason = None
        elif not deterministic_pass or malformed:
            displayed = Verdict.FAIL
            displayed_reason = None
        elif all(
            record.payload is not None
            and _criteria_pass(record.payload, "with_skill", "command")
            for record in records
        ):
            displayed = Verdict.PASS
            displayed_reason = None
        else:
            displayed = Verdict.FAIL
            displayed_reason = None
        if blocked:
            stop_exit = Verdict.BLOCKED
        elif not deterministic_pass or malformed:
            stop_exit = Verdict.FAIL
        elif all(
            record.payload is not None
            and _criteria_pass(record.payload, "with_skill", "decision")
            for record in records
        ):
            stop_exit = Verdict.PASS
        else:
            stop_exit = Verdict.FAIL
        runtime = _runtime_discovery_status(skill, discovery)
        cells.extend(
            (
                EvidenceCell(
                    skill,
                    "trigger_selection",
                    EVIDENCE_LAYERS["trigger_selection"],
                    Verdict.PASS if deterministic_pass else Verdict.FAIL,
                    "cli/tests/test_skill_contract_matrix.py::test_every_skill_frontmatter_identity_and_conditions_are_semantic",
                ),
                EvidenceCell(
                    skill,
                    "without_skill_baseline",
                    EVIDENCE_LAYERS["without_skill_baseline"],
                    baseline,
                    f"{field_evidence}; baseline rubric verdicts={baseline_rubrics or 'unavailable'}",
                ),
                EvidenceCell(skill, "with_skill_compliance", EVIDENCE_LAYERS["with_skill_compliance"], with_skill, field_evidence),
                EvidenceCell(skill, "combined_pressure", EVIDENCE_LAYERS["combined_pressure"], combined, field_evidence),
                EvidenceCell(
                    skill,
                    "displayed_commands",
                    EVIDENCE_LAYERS["displayed_commands"],
                    displayed,
                    f"cli/tests/test_docs_semantic_audit.py::test_all_displayed_skill_awf_commands_parse_with_current_cli; {field_evidence}",
                    displayed_reason,
                ),
                EvidenceCell(
                    skill,
                    "stop_exit_contract",
                    EVIDENCE_LAYERS["stop_exit_contract"],
                    stop_exit,
                    f"cli/tests/test_skill_contract_matrix.py::test_skills_retain_required_outcome_vocabulary; {field_evidence}",
                ),
                EvidenceCell(
                    skill,
                    "runtime_discovery",
                    EVIDENCE_LAYERS["runtime_discovery"],
                    runtime,
                    "runtime report identities "
                    + ",".join(
                        f"{record.get('runtime')}/{skill}"
                        for record in discovery
                        if record.get("skill") == skill
                    ),
                ),
                EvidenceCell(
                    skill,
                    "links_supporting_files",
                    EVIDENCE_LAYERS["links_supporting_files"],
                    Verdict.PASS if install_pass else Verdict.FAIL,
                    f"install report records for {skill}",
                ),
                EvidenceCell(
                    skill,
                    "regression_semantic_audit",
                    EVIDENCE_LAYERS["regression_semantic_audit"],
                    Verdict.PASS if deterministic_pass else Verdict.FAIL,
                    "cli/tests/test_docs_semantic_audit.py::test_skill_frontmatter_has_required_identity_metadata",
                ),
            )
        )
    return tuple(cells)


def validate_evidence_matrix(matrix: SkillMatrix, cells: Sequence[EvidenceCell]) -> None:
    expected = {
        (skill, category) for skill in matrix.skills for category in REQUIRED_CATEGORIES
    }
    identities = [(cell.skill, cell.category) for cell in cells]
    if len(cells) != len(expected):
        raise EvidenceError(f"expected exactly {len(expected)} evidence cells")
    if len(set(identities)) != len(identities):
        raise EvidenceError("duplicate evidence cell")
    if set(identities) != expected:
        raise EvidenceError("evidence identities do not match matrix")
    for cell in cells:
        if cell.layer != EVIDENCE_LAYERS[cell.category]:
            raise EvidenceError(f"wrong evidence layer: {cell.skill}/{cell.category}")
        if not cell.evidence.strip():
            raise EvidenceError(f"empty evidence: {cell.skill}/{cell.category}")
        if cell.verdict is Verdict.NOT_APPLICABLE and not (cell.na_reason or "").strip():
            raise EvidenceError(f"N/A requires a reason: {cell.skill}/{cell.category}")


def _summary_matrix(cells: Sequence[EvidenceCell]) -> SkillMatrix:
    return SkillMatrix(
        schema=MATRIX_SCHEMA,
        skills=MappingProxyType({cell.skill: None for cell in cells}),  # type: ignore[arg-type]
    )


def _json_source_references(
    root: Path, run_id: str, sources: Mapping[str, object]
) -> dict[str, object]:
    serialized = {
        label: _validated_source_reference(
            root, run_id, label=label, reference=sources[label]
        )
        for label in ("deterministic", "install", "discovery")
    }
    field = sources["field"]
    if not isinstance(field, tuple) or len(field) != FIELD_REPORT_COUNT:
        raise EvidenceError(
            f"evidence field sources must contain exactly {FIELD_REPORT_COUNT} reports"
        )
    serialized["field"] = [
        _validated_source_reference(root, run_id, label="field", reference=reference)
        for reference in field
    ]
    return serialized


def write_evidence_summary(
    repo_root: str | Path,
    *,
    run_id: str,
    cells: Sequence[EvidenceCell],
    sources: Mapping[str, object],
    matrix: SkillMatrix | None = None,
) -> Path:
    root = _repository_root(repo_root)
    safe_run_id = _require_safe_batch_id(run_id)
    if len(cells) != EVIDENCE_CELL_COUNT:
        raise EvidenceError(
            f"evidence summary requires exactly {EVIDENCE_CELL_COUNT} cells"
        )
    validate_evidence_matrix(matrix or _summary_matrix(cells), cells)
    if set(sources) != {"deterministic", "install", "discovery", "field"}:
        raise EvidenceError("evidence sources are incomplete")
    serialized_sources = _json_source_references(root, safe_run_id, sources)
    if not isinstance(serialized_sources["field"], list) or len(serialized_sources["field"]) != FIELD_REPORT_COUNT:
        raise EvidenceError(
            f"evidence field sources must contain exactly {FIELD_REPORT_COUNT} reports"
        )
    report = {
        "schema": EVIDENCE_SUMMARY_SCHEMA,
        "run_id": safe_run_id,
        "cells": [
            {
                "skill": cell.skill,
                "category": cell.category,
                "layer": cell.layer,
                "verdict": cell.verdict.value,
                "evidence": cell.evidence,
                "na_reason": cell.na_reason,
            }
            for cell in cells
        ],
        "sources": serialized_sources,
    }
    target = evidence_summary_path(root, safe_run_id)

    def verify_sources_unchanged() -> None:
        if isinstance(sources, SourceBundle):
            verify_source_bundle_unchanged(sources)
        elif _json_source_references(root, safe_run_id, sources) != serialized_sources:
            raise EvidenceError("source references changed during publication")

    _publish_new(
        target,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        before_publish=verify_sources_unchanged,
        after_publish=verify_sources_unchanged,
    )
    return target
