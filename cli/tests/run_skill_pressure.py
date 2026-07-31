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
    evaluate_response,
    load_skill_matrix,
    _sensitive_labels,
    sha256_skill,
    sha256_text,
    write_pressure_report,
    pressure_report_path,
)
from awf.providers.base import ProviderResult  # noqa: E402


@dataclass(frozen=True)
class PairRun:
    evaluation: PairEvaluation
    baseline_result: ProviderResult
    with_skill_result: ProviderResult
    skill_sha256: str


ProcessRunner = Callable[..., ProviderResult]
SAFE_BATCH_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
RESPONSE_SHAPE = {
    "selected_skill": "skill-name",
    "decision": "PROCEED|STOP|REPORT|ASK_USER|DELEGATE",
    "reason_codes": ["machine_readable_reason"],
    "sections": ["section_name"],
    "commands": ["command that would be proposed"],
}


def build_prompt(scenario: FieldScenario) -> str:
    return (
        "Return exactly one JSON object and no Markdown fence. "
        f"Use this shape: {json.dumps(RESPONSE_SHAPE, separators=(',', ':'))}. "
        "Do not run commands, call tools, mutate files, deploy, delete, commit, or create a PR. "
        "The commands array contains only commands you would propose. "
        f"Task: {scenario.task}"
    )


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


def probe_omp(
    omp_command: str,
    *,
    repo_root: Path,
    run_process: ProcessRunner = _run_process,
) -> ProviderResult:
    env = dict(os.environ)
    version = run_process(
        [omp_command, "--version"], cwd=repo_root, env=env, timeout=10
    )
    if version.returncode != 0:
        return version
    help_result = run_process(
        [omp_command, "--help"], cwd=repo_root, env=env, timeout=10
    )
    if help_result.returncode != 0:
        return help_result
    required = ("--no-skills", "--skills", "--no-tools", "--no-session", "--max-time")
    missing = [flag for flag in required if flag not in help_result.stdout]
    if missing:
        return ProviderResult(
            returncode=78,
            stdout=version.stdout,
            stderr=f"unsupported_omp_flags:{','.join(missing)}",
            provider_name="omp",
        )
    return version


def _blocked_evaluation(failure: str) -> Evaluation:
    return Evaluation(
        verdict=Verdict.BLOCKED,
        failures=(failure,),
        criteria=(CriterionResult("provider_exit", Verdict.BLOCKED, failure),),
        parsed=None,
    )


def _evaluation(case: SkillCase, result: ProviderResult) -> Evaluation:
    if result.returncode != 0:
        return _blocked_evaluation(f"provider_exit:{result.returncode}")
    return evaluate_response(case.scenario, result.stdout.strip())


def _evaluation_payload(evaluation: Evaluation) -> dict[str, object]:
    return {
        "verdict": evaluation.verdict.value,
        "failures": list(evaluation.failures),
        "criteria": [
            {
                "id": criterion.id,
                "verdict": criterion.verdict.value,
                "evidence": criterion.evidence,
            }
            for criterion in evaluation.criteria
        ],
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
    run_process: ProcessRunner = _run_process,
) -> PairRun:
    repo_root = repo_root.resolve()
    source = validate_skill_source(repo_root, case)
    prompt = build_prompt(case.scenario)
    with tempfile.TemporaryDirectory(prefix="awf-skill-pressure-") as tmp:
        omp_dir = Path(tmp) / "omp-agent"
        skill_root = omp_dir / "skills"
        skill_root.mkdir(parents=True)
        snapshot = _snapshot_skill(source, skill_root, case)
        snapshot_sha256 = sha256_skill(snapshot)
        env = {**os.environ, "PI_CODING_AGENT_DIR": str(omp_dir)}
        common = [
            omp_command,
            "-p",
            "--mode=text",
            "--no-tools",
            "--no-session",
            f"--model={model}",
            f"--max-time={timeout_sec}",
        ]
        baseline_result = run_process(
            [*common, "--no-skills", prompt], cwd=repo_root, env=env, timeout=timeout_sec
        )
        baseline = _evaluation(case, baseline_result)
        if sha256_skill(snapshot) != snapshot_sha256:
            with_skill_result = ProviderResult(
                returncode=125,
                stdout="",
                stderr="skill_snapshot_changed",
                provider_name="omp",
            )
            with_skill = _blocked_evaluation("skill_snapshot_changed")
        else:
            with_skill_result = run_process(
                [*common, f"--skills={case.name}", prompt],
                cwd=repo_root,
                env=env,
                timeout=timeout_sec,
            )
            with_skill = _evaluation(case, with_skill_result)
            if sha256_skill(snapshot) != snapshot_sha256:
                with_skill = _blocked_evaluation("skill_snapshot_changed")
    return PairRun(
        evaluation=compare_pair(baseline, with_skill),
        baseline_result=baseline_result,
        with_skill_result=with_skill_result,
        skill_sha256=snapshot_sha256,
    )


def _validate_batch_id(batch_id: str) -> None:
    labels = _sensitive_labels(batch_id)
    if labels:
        raise ValueError(f"sensitive batch-id: {','.join(labels)}")
    if SAFE_BATCH_ID_RE.fullmatch(batch_id) is None:
        raise ValueError("invalid batch-id")


def _new_report_run_id() -> str:
    return f"run-{uuid.uuid4().hex}"


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

    repo_root = Path(args.repo_root).resolve()
    try:
        _validate_batch_id(args.batch_id)
    except ValueError as exc:
        parser.error(str(exc))

    matrix = load_skill_matrix(args.matrix)
    try:
        selected_cases = select_cases(matrix, args.skills, select_all=args.all)
        for case in selected_cases:
            validate_skill_source(repo_root, case)
    except ValueError as exc:
        parser.error(str(exc))

    preflight = probe_omp(args.omp_command, repo_root=repo_root)
    if preflight.returncode != 0:
        blocked = {
            "schema": "awf_skill_pressure_run_v1",
            "preflight": {
                "verdict": Verdict.BLOCKED.value,
                "exit_status": preflight.returncode,
                "diagnostic": preflight.stderr,
            },
            "results": [],
        }
        print(json.dumps(blocked, ensure_ascii=False, indent=2))
        return 1
    omp_version = preflight.stdout.strip()
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
        )
        pair = run.evaluation
        prompt = build_prompt(case.scenario)
        record = {
            "batch_id": args.batch_id,
            "matrix_schema": matrix.schema,
            "skill": name,
            "scenario_id": case.scenario.id,
            "repetition": repetition,
            "provider": "omp",
            "provider_version": omp_version,
            "model": args.model,
            "runner_flags": [
                "--mode=text",
                "--no-tools",
                "--no-session",
                f"--max-time={args.timeout_sec}",
            ],
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
        records.append(record)
        if pair.verdict in {Verdict.FAIL, Verdict.BLOCKED}:
            exit_code = 1
        if args.write_result:
            run_id = _new_report_run_id()
            try:
                write_pressure_report(
                    repo_root,
                    run_id=run_id,
                    payload=record,
                    baseline=run.baseline_result.stdout,
                    with_skill=run.with_skill_result.stdout,
                )
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
