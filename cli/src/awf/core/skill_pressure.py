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
from typing import Any, Mapping, Sequence

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
DETERMINISTIC_REPORT_SCHEMA = "awf_skill_deterministic_report_v1"
INSTALL_REPORT_SCHEMA = "awf_skill_install_report_v1"
DISCOVERY_REPORT_SCHEMA = "awf_skill_discovery_report_v1"
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
    "verdict",
    "baseline",
    "with_skill",
    "elapsed_sec",
    "exit_status",
}
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



@dataclass(frozen=True)
class EvidenceCell:
    skill: str
    category: str
    layer: str
    verdict: Verdict
    evidence: str
    na_reason: str | None = None

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
    labels = _sensitive_labels(run_id)
    if labels:
        raise SensitiveDataError(f"sensitive run_id blocked: {','.join(labels)}")
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

    validate_field_record(payload)

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


def _pressure_operations_root(repo_root: str | Path) -> Path:
    return operations_root(repo_root) / "skill-pressure"


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


def validate_field_record(record: dict[str, Any]) -> None:
    missing = sorted(FIELD_RECORD_REQUIRED - set(record))
    if missing:
        raise EvidenceError(f"missing field record keys: {','.join(missing)}")
    if record["matrix_schema"] != MATRIX_SCHEMA:
        raise EvidenceError("field record matrix schema mismatch")
    _require_safe_batch_id(record["batch_id"])
    for key in ("prompt_sha256", "skill_sha256"):
        _require_sha256(record[key], field=key)
    for key in ("skill", "scenario_id", "provider", "provider_version", "model", "severity"):
        if not isinstance(record[key], str) or not record[key]:
            raise EvidenceError(f"invalid field record {key}")
    if not isinstance(record["repetition"], int) or record["repetition"] < 1:
        raise EvidenceError("invalid field record repetition")
    if not isinstance(record["runner_flags"], list) or not all(
        isinstance(flag, str) and flag for flag in record["runner_flags"]
    ):
        raise EvidenceError("invalid field record runner_flags")
    for key in ("remediation_state", "behavioral_delta"):
        if not isinstance(record[key], str) or not record[key]:
            raise EvidenceError(f"invalid field record {key}")
    if _verdict(record["verdict"]) is None:
        raise EvidenceError("invalid field record verdict")
    if not isinstance(record["baseline"], Mapping) or not isinstance(record["with_skill"], Mapping):
        raise EvidenceError("invalid field record evaluations")
    if not isinstance(record["elapsed_sec"], (int, float, Mapping)):
        raise EvidenceError("invalid field record elapsed_sec")
    if not isinstance(record["exit_status"], (int, Mapping)):
        raise EvidenceError("invalid field record exit_status")


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
) -> Path:
    safe_batch_id = _require_safe_batch_id(batch_id)
    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise EvidenceError("deterministic argv must be a non-empty string sequence")
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
        "matrix_sha256": _require_sha256(matrix_sha256, field="matrix_sha256"),
        "sources": _validate_sources(sources),
    }
    target = deterministic_report_path(repo_root, safe_batch_id)
    _publish_new(target, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return target


def _validate_install_records(records: Sequence[Mapping[str, object]]) -> None:
    required = {"runtime", "skill", "source_sha256", "target_root", "status", "diagnostic"}
    if len(records) != 45:
        raise EvidenceError("install report requires exactly 45 records")
    identities: list[tuple[str, str]] = []
    for record in records:
        if set(record) != required:
            raise EvidenceError("invalid install record keys")
        runtime = record["runtime"]
        skill = record["skill"]
        if runtime not in SUPPORTED_RUNTIMES or not isinstance(skill, str) or not skill:
            raise EvidenceError("invalid install record identity")
        target_root = record["target_root"]
        if (
            not isinstance(target_root, str)
            or not target_root
            or Path(target_root).is_absolute()
            or ".." in Path(target_root).parts
        ):
            raise EvidenceError("invalid install target_root")
        if not isinstance(record["diagnostic"], str):
            raise EvidenceError("invalid install diagnostic")
        _require_sha256(record["source_sha256"], field="install source_sha256")
        if _verdict(record["status"]) is None:
            raise EvidenceError("invalid install status")
        identities.append((str(runtime), skill))
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
    if (
        not isinstance(isolated_home_id, str)
        or not isolated_home_id
        or Path(isolated_home_id).is_absolute()
        or "/" in isolated_home_id
        or "\\" in isolated_home_id
    ):
        raise EvidenceError("invalid isolated_home_id")
    _validate_install_records(records)
    report = {
        "schema": INSTALL_REPORT_SCHEMA,
        "batch_id": safe_batch_id,
        "matrix_sha256": _require_sha256(matrix_sha256, field="matrix_sha256"),
        "isolated_home_id": isolated_home_id,
        "records": [dict(record) for record in records],
    }
    target = install_report_path(repo_root, safe_batch_id)
    _publish_new(target, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return target


def _load_json_report(path: str | Path, *, label: str) -> tuple[Path, Mapping[str, Any]]:
    report_path = Path(path)
    try:
        raw = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{label} report could not be loaded") from exc
    return report_path, _require_mapping(raw, field=f"{label} report")


def _report_reference(path: Path) -> Mapping[str, str]:
    return MappingProxyType({"path": str(path), "sha256": sha256_file(path)})


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
    isolated_home_id = report["isolated_home_id"]
    if (
        not isinstance(isolated_home_id, str)
        or not isolated_home_id
        or Path(isolated_home_id).is_absolute()
        or "/" in isolated_home_id
        or "\\" in isolated_home_id
    ):
        raise EvidenceError("invalid isolated_home_id")


def _validate_discovery_records(records: Sequence[Mapping[str, Any]]) -> None:
    for record in records:
        _require_sha256(record.get("source_sha256"), field="discovery source_sha256")
        if _record_status(record) is None:
            raise EvidenceError("invalid discovery record verdict")
        if not isinstance(record.get("diagnostic"), str):
            raise EvidenceError("invalid discovery diagnostic")


def _expect_exact_runtime_identities(
    records: Sequence[Mapping[str, Any]],
    *,
    label: str,
    matrix: SkillMatrix | None,
) -> None:
    if len(records) != 45:
        raise EvidenceError(f"{label} report requires exactly 45 records")
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


def _field_identity_from_report(
    report: Mapping[str, Any], *, batch_id: str
) -> tuple[str, int]:
    if report.get("schema") != REPORT_SCHEMA:
        raise EvidenceError("field report schema mismatch")
    if report.get("persistence_status") == "COMPLETE":
        payload = _require_mapping(report.get("payload"), field="field payload")
        validate_field_record(dict(payload))
    elif report.get("persistence_status") == "BLOCKED":
        payload = _require_mapping(report.get("field_identity"), field="blocked field identity")
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
    batch_id: str,
    deterministic_path: str | Path,
    install_path: str | Path,
    discovery_path: str | Path,
    field_paths: Sequence[str | Path],
    matrix: SkillMatrix | None = None,
    matrix_sha256: str | None = None,
) -> Mapping[str, object]:
    safe_batch_id = _require_safe_batch_id(batch_id)
    deterministic_file, deterministic = _load_json_report(
        deterministic_path, label="deterministic"
    )
    deterministic_hash = _validate_report_header(
        deterministic,
        label="deterministic",
        schema=DETERMINISTIC_REPORT_SCHEMA,
        batch_id=safe_batch_id,
        matrix_sha256=matrix_sha256,
    )
    _validate_deterministic_report(deterministic)
    if deterministic.get("exit_status") != 0:
        raise EvidenceError("deterministic report exit status is not zero")
    _validate_sources(_require_mapping(deterministic.get("sources"), field="deterministic sources"))

    install_file, install = _load_json_report(install_path, label="install")
    _validate_report_header(
        install,
        label="install",
        schema=INSTALL_REPORT_SCHEMA,
        batch_id=safe_batch_id,
        matrix_sha256=deterministic_hash,
    )
    _validate_install_report(install)
    install_records = install.get("records")
    if not isinstance(install_records, list) or not all(
        isinstance(record, Mapping) for record in install_records
    ):
        raise EvidenceError("install report records must be a list")
    _validate_install_records(install_records)
    _expect_exact_runtime_identities(install_records, label="install", matrix=matrix)
    if any(_record_status(record) is not Verdict.PASS for record in install_records):
        raise EvidenceError("install report contains non-PASS record")

    discovery_file, discovery = _load_json_report(discovery_path, label="discovery")
    _validate_report_header(
        discovery,
        label="discovery",
        schema=DISCOVERY_REPORT_SCHEMA,
        batch_id=safe_batch_id,
        matrix_sha256=deterministic_hash,
    )
    discovery_records = discovery.get("records")
    if not isinstance(discovery_records, list) or not all(
        isinstance(record, Mapping) for record in discovery_records
    ):
        raise EvidenceError("discovery report records must be a list")
    _expect_exact_runtime_identities(discovery_records, label="discovery", matrix=matrix)
    _validate_discovery_records(discovery_records)

    if len(field_paths) != 27:
        raise EvidenceError("field reports require exactly 27 paths")
    field_references: list[Mapping[str, str]] = []
    field_identities: list[tuple[str, int]] = []
    seen_paths: set[Path] = set()
    for field_path in field_paths:
        field_file, field_report = _load_json_report(field_path, label="field")
        if field_file in seen_paths:
            raise EvidenceError("duplicate field report path")
        seen_paths.add(field_file)
        field_identities.append(_field_identity_from_report(field_report, batch_id=safe_batch_id))
        field_references.append(_report_reference(field_file))
    if len(set(field_identities)) != len(field_identities):
        raise EvidenceError("duplicate field report identity")
    if matrix is not None:
        expected_fields = {
            (case.name, repetition)
            for case in matrix.skills.values()
            for repetition in range(1, 4 if case.high_risk else 2)
        }
        if set(field_identities) != expected_fields:
            raise EvidenceError("field identities do not match matrix")

    return MappingProxyType(
        {
            "deterministic": _report_reference(deterministic_file),
            "install": _report_reference(install_file),
            "discovery": _report_reference(discovery_file),
            "field": tuple(field_references),
        }
    )


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
    if blocked:
        return Verdict.BLOCKED
    if malformed:
        return Verdict.FAIL
    verdicts: list[Verdict] = []
    for observation in observations:
        if observation.payload is None:
            return Verdict.BLOCKED
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
    if any(verdict is None for verdict in verdicts):
        return Verdict.FAIL
    return _aggregate_verdict([verdict for verdict in verdicts if verdict is not None])


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


def _json_source_references(sources: Mapping[str, object]) -> dict[str, object]:
    serialized: dict[str, object] = {}
    for label, reference in sources.items():
        if isinstance(reference, Mapping):
            serialized[label] = dict(reference)
        elif isinstance(reference, tuple):
            serialized[label] = [dict(item) for item in reference if isinstance(item, Mapping)]
        else:
            raise EvidenceError(f"invalid {label} source reference")
    return serialized


def write_evidence_summary(
    repo_root: str | Path,
    *,
    run_id: str,
    cells: Sequence[EvidenceCell],
    sources: Mapping[str, object],
    matrix: SkillMatrix | None = None,
) -> Path:
    safe_run_id = _require_safe_batch_id(run_id)
    if len(cells) != 135:
        raise EvidenceError("evidence summary requires exactly 135 cells")
    validate_evidence_matrix(matrix or _summary_matrix(cells), cells)
    if set(sources) != {"deterministic", "install", "discovery", "field"}:
        raise EvidenceError("evidence sources are incomplete")
    serialized_sources = _json_source_references(sources)
    if not isinstance(serialized_sources["field"], list) or len(serialized_sources["field"]) != 27:
        raise EvidenceError("evidence field sources must contain exactly 27 reports")
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
    target = evidence_summary_path(repo_root, safe_run_id)
    _publish_new(target, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return target
