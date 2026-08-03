from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "cli" / "src"))

from awf.core.skill_pressure import (  # noqa: E402
    CriterionResult,
    Evaluation,
    FieldScenario,
    HIGH_RISK_SKILLS,
    PairEvaluation,
    SensitiveDataError,
    SkillCase,
    Verdict,
    compare_pair,
    build_field_prompt,
    evaluate_response,
    load_skill_matrix,
    _sensitive_labels,
    sha256_file,
    sha256_skill,
    sha256_text,
    write_pressure_report,
    pressure_report_path,
)
from awf.core.skill_subscription import (  # noqa: E402
    SubscriptionAuthContext,
    build_subscription_environment,
    normalize_host_diagnostic,
    require_subscription_model,
)
from awf.providers.base import ProviderResult  # noqa: E402


@dataclass(frozen=True)
class PairRun:
    evaluation: PairEvaluation
    baseline_result: ProviderResult
    with_skill_result: ProviderResult
    preflight_result: ProviderResult
    skill_sha256: str
    skill_file_sha256: str
    injection_sha256: str


class SourceSnapshotChangedError(RuntimeError):
    pass


_NORMALIZED_HOST_DIAGNOSTICS = frozenset(
    {
        "host_auth_unavailable",
        "host_model_unsupported",
        "host_provider_exit",
        "host_subscription_expired",
        "host_timeout",
        "unsupported_omp_flags",
    }
)


def _safe_criterion_id(identifier: str) -> str:
    if identifier in {"host_diagnostic", "source_snapshot"}:
        return identifier
    for prefix in (
        "allowed_command:",
        "required_reason:",
        "required_section:",
        "required_command:",
        "forbidden_command:",
    ):
        if identifier.startswith(prefix):
            return prefix.removesuffix(":")
    if identifier in {
        "selected_skill",
        "decision",
        "reason_codes_type",
        "sections_type",
        "commands_type",
        "command_shell_control",
        "command_order",
        "response_json",
        "response_object",
    }:
        return identifier
    return "evaluation"

ProcessRunner = Callable[..., ProviderResult]
SAFE_BATCH_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def build_prompt(scenario: FieldScenario) -> str:
    return build_field_prompt(scenario)


def _run_process(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
) -> ProviderResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ProviderResult(
            returncode=124,
            stdout="",
            stderr=f"provider_timeout after {timeout}s",
            provider_name="omp",
            elapsed_sec=time.monotonic() - started,
        )
    except OSError as exc:
        return ProviderResult(
            returncode=127,
            stdout="",
            stderr=f"provider_unavailable:{type(exc).__name__}",
            provider_name="omp",
            elapsed_sec=time.monotonic() - started,
        )
    return ProviderResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        provider_name="omp",
        elapsed_sec=time.monotonic() - started,
    )


def _help_supports_option(help_text: str, option: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9_-]){re.escape(option)}(?![A-Za-z0-9_-])"
    return re.search(pattern, help_text) is not None


def probe_omp(
    omp_command: str,
    *,
    repo_root: Path | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    run_process: ProcessRunner = _run_process,
) -> ProviderResult:
    launch_cwd = cwd or repo_root
    if launch_cwd is None:
        raise ValueError("probe_omp requires cwd or repo_root")
    launch_env = dict(os.environ if env is None else env)
    version = run_process(
        [omp_command, "--version"], cwd=launch_cwd, env=launch_env, timeout=10
    )
    if version.returncode != 0:
        return version
    help_result = run_process(
        [omp_command, "--help"], cwd=launch_cwd, env=launch_env, timeout=10
    )
    if help_result.returncode != 0:
        return help_result
    required = (
        "-p",
        "--mode",
        "--no-tools",
        "--no-skills",
        "--append-system-prompt",
        "--no-session",
        "--no-extensions",
        "--model",
    )
    missing = [flag for flag in required if not _help_supports_option(help_result.stdout, flag)]
    if missing:
        return ProviderResult(
            returncode=78,
            stdout=version.stdout,
            stderr=f"unsupported_omp_flags:{','.join(missing)}",
            provider_name="omp",
        )
    return version


def _diagnostic_evaluation(diagnostic: str) -> Evaluation:
    verdict = (
        Verdict.FAIL
        if diagnostic in {"host_model_unsupported", "unsupported_omp_flags"}
        else Verdict.BLOCKED
    )
    return Evaluation(
        verdict=verdict,
        failures=(diagnostic,),
        criteria=(CriterionResult("host_diagnostic", verdict, diagnostic),),
        parsed=None,
    )


def _blocked_evaluation(failure: str) -> Evaluation:
    return Evaluation(
        verdict=Verdict.BLOCKED,
        failures=(failure,),
        criteria=(CriterionResult("source_snapshot", Verdict.BLOCKED, failure),),
        parsed=None,
    )


def _evaluation(case: SkillCase, result: ProviderResult) -> Evaluation:
    if result.returncode != 0:
        diagnostic = (
            "unsupported_omp_flags"
            if result.returncode == 78
            and result.stderr.startswith("unsupported_omp_flags:")
            else normalize_host_diagnostic(
                result.returncode, result.stdout, result.stderr
            )
        )
        return _diagnostic_evaluation(diagnostic)
    return evaluate_response(case.scenario, result.stdout.strip())

def _evaluation_payload(evaluation: Evaluation) -> dict[str, object]:
    criteria: list[dict[str, str]] = []
    failures: list[str] = []
    for criterion in evaluation.criteria:
        identifier = _safe_criterion_id(criterion.id)
        if identifier == "host_diagnostic":
            evidence = (
                criterion.evidence
                if criterion.evidence in _NORMALIZED_HOST_DIAGNOSTICS
                else "host_provider_exit"
            )
        elif identifier == "source_snapshot":
            evidence = "skill_snapshot_changed"
        else:
            evidence = "satisfied" if criterion.verdict is Verdict.PASS else "not_satisfied"
        criteria.append(
            {
                "id": identifier,
                "verdict": criterion.verdict.value,
                "evidence": evidence,
            }
        )
        if criterion.verdict is not Verdict.PASS:
            failures.append(evidence if identifier in {"host_diagnostic", "source_snapshot"} else identifier)
    if evaluation.verdict is not Verdict.PASS and not failures:
        failures.append("evaluation_failed")
    return {
        "verdict": evaluation.verdict.value,
        "failures": failures,
        "criteria": criteria,
    }


def validate_skill_source(repo_root: Path, case: SkillCase) -> Path:
    if Path(case.name).parts != (case.name,) or case.name in {".", ".."}:
        raise ValueError(f"invalid Skill name: {case.name!r}")

    canonical_repo_root = repo_root.resolve()
    claude_root = canonical_repo_root / "claude"
    skill_root = claude_root / "skills"
    if claude_root.is_symlink() or skill_root.is_symlink():
        raise ValueError("Skill root symlink is not allowed")
    if not skill_root.is_dir():
        raise ValueError("Skill root is not a directory")

    canonical_skill_root = skill_root.resolve(strict=True)
    candidate = skill_root / case.name
    if candidate.is_symlink():
        raise ValueError("Skill source symlink is not allowed")
    try:
        source = candidate.resolve(strict=True)
    except FileNotFoundError:
        raise ValueError("Skill source is not a directory") from None
    if not source.is_relative_to(canonical_skill_root):
        raise ValueError("Skill source escapes the Skill root")
    if not source.is_dir():
        raise ValueError("Skill source is not a directory")

    skill_definition = source / "SKILL.md"
    if skill_definition.is_symlink() or not skill_definition.is_file():
        raise ValueError("Skill source must contain a regular SKILL.md")
    if any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("Skill source symlink is not allowed")
    return source


def _snapshot_skill(source: Path, skill_root: Path, case: SkillCase) -> Path:
    snapshot = skill_root / case.name
    shutil.copytree(source, snapshot)
    return snapshot


def execute_pair(
    case: SkillCase,
    *,
    repo_root: Path,
    omp_command: str,
    model: str,
    timeout_sec: int,
    auth_context: SubscriptionAuthContext | None = None,
    run_process: ProcessRunner = _run_process,
) -> PairRun:
    require_subscription_model("omp", model)
    auth = auth_context or SubscriptionAuthContext.capture()
    repo_root = repo_root.resolve()
    source = validate_skill_source(repo_root, case)
    prompt = build_prompt(case.scenario)
    expected_skill_sha256 = sha256_skill(source)
    expected_skill_file_sha256 = sha256_file(source / "SKILL.md")

    def snapshot_failure() -> tuple[Evaluation, ProviderResult]:
        return (
            _blocked_evaluation("skill_snapshot_changed"),
            ProviderResult(
                returncode=125,
                stdout="",
                stderr="skill_snapshot_changed",
                provider_name="omp",
            ),
        )

    with tempfile.TemporaryDirectory(prefix="awf-skill-pressure-") as tmp:
        temporary_root = Path(tmp)
        temporary_home = temporary_root / "home"
        workspace = temporary_root / "workspace"
        temporary_home.mkdir()
        workspace.mkdir()
        skill_root = workspace / ".omp" / "skills"
        skill_root.mkdir(parents=True)
        try:
            snapshot = _snapshot_skill(source, skill_root, case)
            injection = snapshot / "SKILL.md"
            if injection.is_symlink() or not injection.is_file():
                raise OSError("snapshot SKILL.md is not a regular file")
            if (
                sha256_skill(snapshot) != expected_skill_sha256
                or sha256_file(injection) != expected_skill_file_sha256
            ):
                raise OSError("snapshot hash mismatch")
        except (OSError, shutil.Error, ValueError):
            baseline, baseline_result = snapshot_failure()
            with_skill, with_skill_result = snapshot_failure()
            return PairRun(
                evaluation=compare_pair(baseline, with_skill),
                baseline_result=baseline_result,
                with_skill_result=with_skill_result,
                preflight_result=baseline_result,
                skill_sha256=expected_skill_sha256,
                skill_file_sha256=expected_skill_file_sha256,
                injection_sha256=expected_skill_file_sha256,
            )
        environment = build_subscription_environment(
            "omp", auth, temporary_home, os.environ
        )

        def snapshot_unchanged() -> bool:
            try:
                return (
                    sha256_skill(source) == expected_skill_sha256
                    and sha256_file(source / "SKILL.md") == expected_skill_file_sha256
                    and sha256_skill(snapshot) == expected_skill_sha256
                    and sha256_file(injection) == expected_skill_file_sha256
                )
            except (OSError, ValueError):
                return False

        if not snapshot_unchanged():
            baseline, baseline_result = snapshot_failure()
            with_skill, with_skill_result = snapshot_failure()
            preflight_result = baseline_result
        else:
            preflight_result = probe_omp(
                omp_command,
                cwd=workspace,
                env=environment,
                run_process=run_process,
            )
            if not snapshot_unchanged():
                baseline, baseline_result = snapshot_failure()
                with_skill, with_skill_result = snapshot_failure()
            elif preflight_result.returncode != 0:
                baseline_result = preflight_result
                with_skill_result = preflight_result
                baseline = _evaluation(case, preflight_result)
                with_skill = _evaluation(case, preflight_result)
            elif not snapshot_unchanged():
                baseline, baseline_result = snapshot_failure()
                with_skill, with_skill_result = snapshot_failure()
            else:
                common = [
                    omp_command,
                    "-p",
                    "--mode=text",
                    "--no-tools",
                    "--no-session",
                    "--no-extensions",
                    f"--model={model}",
                ]
                baseline_result = run_process(
                    [*common, "--no-skills", prompt],
                    cwd=workspace,
                    env=environment,
                    timeout=timeout_sec,
                )
                baseline = _evaluation(case, baseline_result)
                if not snapshot_unchanged():
                    baseline, baseline_result = snapshot_failure()
                    with_skill, with_skill_result = snapshot_failure()
                else:
                    with_skill_result = run_process(
                        [
                            *common,
                            "--no-skills",
                            "--append-system-prompt",
                            str(injection),
                            prompt,
                        ],
                        cwd=workspace,
                        env=environment,
                        timeout=timeout_sec,
                    )
                    with_skill = _evaluation(case, with_skill_result)
                    if not snapshot_unchanged():
                        baseline, baseline_result = snapshot_failure()
                        with_skill, with_skill_result = snapshot_failure()

        return PairRun(
            evaluation=compare_pair(baseline, with_skill),
            baseline_result=baseline_result,
            with_skill_result=with_skill_result,
            preflight_result=preflight_result,
            skill_sha256=expected_skill_sha256,
            skill_file_sha256=expected_skill_file_sha256,
            injection_sha256=expected_skill_file_sha256,
        )


def _validate_batch_id(batch_id: str) -> None:
    labels = _sensitive_labels(batch_id)
    if labels:
        raise ValueError(f"sensitive batch-id: {','.join(labels)}")
    if SAFE_BATCH_ID_RE.fullmatch(batch_id) is None:
        raise ValueError("invalid batch-id")


def _new_report_run_id(batch_id: str) -> str:
    return f"{batch_id}-run-{uuid.uuid4().hex}"


def select_cases(
    matrix: object, selected: list[str] | None, *, select_all: bool
) -> list[SkillCase]:
    if select_all and selected:
        raise ValueError("select either --all or --skill")
    skills = matrix.skills
    names = sorted(skills) if select_all else list(selected or [])
    if not names:
        raise ValueError("select at least one Skill")
    duplicate_names = sorted({name for name in names if names.count(name) > 1})
    if duplicate_names:
        raise ValueError(f"duplicate Skills: {','.join(duplicate_names)}")
    unknown = [name for name in names if name not in skills]
    if unknown:
        raise ValueError(f"unknown Skills: {','.join(unknown)}")
    return [skills[name] for name in names]


def repetitions_for(case: SkillCase) -> int:
    return 3 if case.name in HIGH_RISK_SKILLS else 1


def expanded_runs(cases: list[SkillCase]) -> list[tuple[SkillCase, int]]:
    return [
        (case, repetition)
        for case in cases
        for repetition in range(1, repetitions_for(case) + 1)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run opt-in AWF Skill pressure pairs with OMP.")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument(
        "--matrix",
        default=str(REPO_ROOT / "cli/tests/fixtures/skill-validation-matrix.v1.json"),
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--skill", action="append", dest="skills")
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--omp-command", default="omp")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout-sec", type=int, default=120)
    parser.add_argument("--write-result", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.timeout_sec <= 0:
        parser.error("--timeout-sec must be positive")

    repo_root = Path(args.repo_root).absolute()
    try:
        _validate_batch_id(args.batch_id)
    except ValueError as exc:
        parser.error(str(exc))

    matrix = load_skill_matrix(args.matrix)
    try:
        selected_cases = select_cases(matrix, args.skills, select_all=args.all)
        for case in selected_cases:
            validate_skill_source(repo_root, case)
        require_subscription_model("omp", args.model)
        auth_context = SubscriptionAuthContext.capture()
    except ValueError as exc:
        parser.error(str(exc))

    records: list[dict[str, object]] = []
    exit_code = 0
    for case, repetition in expanded_runs(selected_cases):
        name = case.name
        run = execute_pair(
            case,
            repo_root=repo_root,
            omp_command=args.omp_command,
            model=args.model,
            timeout_sec=args.timeout_sec,
            auth_context=auth_context,
        )
        pair = run.evaluation
        def source_unchanged() -> bool:
            try:
                current_source = validate_skill_source(repo_root, case)
                return (
                    sha256_skill(current_source) == run.skill_sha256
                    and sha256_file(current_source / "SKILL.md")
                    == run.skill_file_sha256
                )
            except (OSError, ValueError):
                return False

        if not source_unchanged():
            blocked = _blocked_evaluation("skill_snapshot_changed")
            pair = PairEvaluation(Verdict.BLOCKED, blocked, blocked)
        provider_version = "subscription"
        prompt = build_prompt(case.scenario)
        record = {
            "batch_id": args.batch_id,
            "matrix_schema": matrix.schema,
            "skill": name,
            "scenario_id": case.scenario.id,
            "repetition": repetition,
            "provider": "omp",
            "provider_version": provider_version,
            "model": args.model,
            "runner_flags": [
                "-p",
                "--mode=text",
                "--no-tools",
                "--no-session",
                "--no-extensions",
                "--no-skills",
                "--append-system-prompt",
            ],
            "auth_mode": "subscription",
            "severity": case.severity,
            "remediation_state": (
                "open" if pair.verdict in {Verdict.FAIL, Verdict.BLOCKED} else "not_required"
            ),
            "behavioral_delta": {
                Verdict.PASS: "improved",
                Verdict.UNPROVEN: "not_demonstrated",
                Verdict.FAIL: "regressed_or_noncompliant",
                Verdict.BLOCKED: "blocked",
            }[pair.verdict],
            "prompt_sha256": sha256_text(prompt),
            "skill_sha256": run.skill_sha256,
            "skill_file_sha256": run.skill_file_sha256,
            "injection_sha256": run.injection_sha256,
            "verdict": pair.verdict.value,
            "baseline": _evaluation_payload(pair.baseline),
            "with_skill": _evaluation_payload(pair.with_skill),
            "elapsed_sec": {
                "baseline": run.baseline_result.elapsed_sec,
                "with_skill": run.with_skill_result.elapsed_sec,
            },
            "exit_status": {
                "baseline": run.baseline_result.returncode,
                "with_skill": run.with_skill_result.returncode,
            },
        }

        def snapshot_blocked_record() -> dict[str, object]:
            blocked = _blocked_evaluation("skill_snapshot_changed")
            return {
                **record,
                "remediation_state": "open",
                "behavioral_delta": "blocked",
                "verdict": Verdict.BLOCKED.value,
                "baseline": _evaluation_payload(blocked),
                "with_skill": _evaluation_payload(blocked),
            }

        def verify_source_snapshot() -> None:
            if not source_unchanged():
                raise SourceSnapshotChangedError("skill_snapshot_changed")
        records.append(record)
        if pair.verdict in {Verdict.FAIL, Verdict.BLOCKED}:
            exit_code = 1
        if args.write_result:
            run_id = _new_report_run_id(args.batch_id)
            try:
                write_pressure_report(
                    repo_root,
                    run_id=run_id,
                    payload=record,
                    baseline=run.baseline_result.stdout,
                    with_skill=run.with_skill_result.stdout,
                    before_publish=verify_source_snapshot,
                    after_publish=verify_source_snapshot,
                )
            except SourceSnapshotChangedError:
                record = snapshot_blocked_record()
                records[-1] = record
                try:
                    write_pressure_report(
                        repo_root,
                        run_id=run_id,
                        payload=record,
                        baseline="",
                        with_skill="",
                        blocked_diagnostic="skill_snapshot_changed",
                    )
                except (OSError, ValueError):
                    report_written = False
                else:
                    report_written = True
                record["persistence"] = {
                    "status": "BLOCKED" if report_written else "REJECTED",
                    "run_id": run_id,
                    "report_written": report_written,
                    "diagnostic": (
                        "skill_snapshot_changed"
                        if report_written
                        else "report_rejected"
                    ),
                }
                exit_code = 1
            except SensitiveDataError:
                report_written = pressure_report_path(repo_root, run_id).is_file()
                record = {
                    **record,
                    "remediation_state": "blocked_sensitive_data",
                    "behavioral_delta": "blocked",
                    "verdict": Verdict.BLOCKED.value,
                    "baseline": {
                        "verdict": Verdict.BLOCKED.value,
                        "evidence": "redacted_sensitive_data",
                    },
                    "with_skill": {
                        "verdict": Verdict.BLOCKED.value,
                        "evidence": "redacted_sensitive_data",
                    },
                    "persistence": {
                        "status": "REDACTED" if report_written else "REJECTED",
                        "run_id": run_id,
                        "report_written": report_written,
                        "diagnostic": (
                            "sensitive_data_redacted"
                            if report_written
                            else "report_rejected"
                        ),
                    },
                }
                records[-1] = record
                exit_code = 1
            except ValueError:
                record = {
                    **record,
                    "remediation_state": "blocked_persistence",
                    "behavioral_delta": "blocked",
                    "verdict": Verdict.BLOCKED.value,
                    "persistence": {
                        "status": "REJECTED",
                        "run_id": run_id,
                        "report_written": False,
                        "diagnostic": "run_id_rejected",
                    },
                }
                records[-1] = record
                exit_code = 1
            else:
                record["persistence"] = {
                    "status": "COMPLETE",
                    "run_id": run_id,
                    "report_written": True,
                }

    output = {"schema": "awf_skill_pressure_run_v1", "results": records}
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    else:
        for record in records:
            print(f"{record['skill']}#{record['repetition']}: {record['verdict']}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
