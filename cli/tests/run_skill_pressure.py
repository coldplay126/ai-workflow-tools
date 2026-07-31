from __future__ import annotations

import argparse
import json
import os
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
    sha256_skill,
    sha256_text,
    write_pressure_report,
)
from awf.providers.base import ProviderResult  # noqa: E402


@dataclass(frozen=True)
class PairRun:
    evaluation: PairEvaluation
    baseline_result: ProviderResult
    with_skill_result: ProviderResult


ProcessRunner = Callable[..., ProviderResult]
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


def _evaluation(case: SkillCase, result: ProviderResult) -> Evaluation:
    if result.returncode != 0:
        failure = f"provider_exit:{result.returncode}"
        return Evaluation(
            verdict=Verdict.BLOCKED,
            failures=(failure,),
            criteria=(CriterionResult("provider_exit", Verdict.BLOCKED, failure),),
            parsed=None,
        )
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


def execute_pair(
    case: SkillCase,
    *,
    repo_root: Path,
    omp_command: str,
    model: str,
    timeout_sec: int,
    run_process: ProcessRunner = _run_process,
) -> PairRun:
    prompt = build_prompt(case.scenario)
    with tempfile.TemporaryDirectory(prefix="awf-skill-pressure-") as tmp:
        omp_dir = Path(tmp) / "omp-agent"
        skill_root = omp_dir / "skills"
        skill_root.mkdir(parents=True)
        source = repo_root / "claude" / "skills" / case.name
        (skill_root / case.name).symlink_to(source.resolve(), target_is_directory=True)
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
        with_skill_result = run_process(
            [*common, f"--skills={case.name}", prompt],
            cwd=repo_root,
            env=env,
            timeout=timeout_sec,
        )
    return PairRun(
        evaluation=compare_pair(
            _evaluation(case, baseline_result),
            _evaluation(case, with_skill_result),
        ),
        baseline_result=baseline_result,
        with_skill_result=with_skill_result,
    )


def select_cases(
    matrix: object, selected: list[str] | None, *, select_all: bool
) -> list[SkillCase]:
    skills = matrix.skills
    names = sorted(skills) if select_all else list(selected or [])
    unknown = [name for name in names if name not in skills]
    if not names:
        raise ValueError("select at least one Skill")
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
    parser.add_argument("--skill", action="append", dest="skills")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--omp-command", default="omp")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout-sec", type=int, default=120)
    parser.add_argument("--write-result", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.timeout_sec <= 0:
        parser.error("--timeout-sec must be positive")

    matrix = load_skill_matrix(args.matrix)
    try:
        selected_cases = select_cases(matrix, args.skills, select_all=args.all)
    except ValueError as exc:
        parser.error(str(exc))

    repo_root = Path(args.repo_root).resolve()
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
            "skill_sha256": sha256_skill(repo_root / "claude" / "skills" / name),
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
            run_id = f"{args.batch_id}-{case.scenario.id}-{repetition}-{uuid.uuid4().hex[:8]}"
            try:
                write_pressure_report(
                    repo_root,
                    run_id=run_id,
                    payload=record,
                    baseline=run.baseline_result.stdout,
                    with_skill=run.with_skill_result.stdout,
                )
            except SensitiveDataError:
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
                        "status": "BLOCKED",
                        "run_id": run_id,
                        "diagnostic": "sensitive_data_redacted",
                    },
                }
                records[-1] = record
                exit_code = 1

    output = {"schema": "awf_skill_pressure_run_v1", "results": records}
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    else:
        for record in records:
            print(f"{record['skill']}#{record['repetition']}: {record['verdict']}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
