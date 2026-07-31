from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "cli" / "src"))

from awf.core.skill_pressure import (  # noqa: E402
    DISCOVERY_REPORT_SCHEMA,
    EvidenceError,
    _publish_new,
    _sensitive_labels,
    discovery_report_path,
    load_skill_matrix,
    sha256_file,
    sha256_skill,
    write_install_report,
)

RUNTIMES = ("claude", "agent-skills", "omp")
TARGET_ROOTS = {
    "claude": ".claude/skills",
    "agent-skills": ".agents/skills",
    "omp": ".omp/agent/skills",
}
REQUIRED_FLAGS = {
    "claude": (
        "-p",
        "--output-format",
        "--tools",
        "--no-session-persistence",
        "--model",
    ),
    "agent-skills": (
        "exec",
        "--ephemeral",
        "--sandbox",
        "--skip-git-repo-check",
        "--model",
    ),
    "omp": ("-p", "--mode=text", "--no-tools", "--no-session", "--model", "--skills"),
}
SAFETY_FLAGS = {
    "claude": (
        "-p",
        "--output-format=text",
        "--tools=",
        "--no-session-persistence",
    ),
    "agent-skills": ("exec", "--ephemeral", "--sandbox=read-only", "--skip-git-repo-check"),
    "omp": ("-p", "--mode=text", "--no-tools", "--no-session"),
}
UNKNOWN_SKILL_RE = re.compile(
    r"(?:unknown|unrecognized|unsupported)\s+(?:command|skill)|skill\s+.*(?:not found|unknown)",
    re.IGNORECASE,
)
AUTH_FAILURE_RE = re.compile(
    r"(?:credential|authentication|authorize|login|api[ _-]?key|not authenticated)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed_sec: float
    error_kind: str | None = None


@dataclass(frozen=True)
class ExpectedSkill:
    name: str
    description: str
    body_heading: str
    source: Path
    source_sha256: str


@dataclass(frozen=True)
class HostPreflight:
    runtime: str
    binary: str
    binary_version: str
    available: bool
    diagnostic: str
    exit_status: int


@dataclass(frozen=True)
class DiscoveryRun:
    repo_root: Path
    install_records: tuple[dict[str, object], ...]
    discovery_records: tuple[dict[str, object], ...]
    install_report: Path | None
    discovery_report: Path | None
    exit_code: int


ProcessRunner = Callable[..., ProcessResult]


def claude_argv(binary: str, model: str, skill: str, prompt: str) -> list[str]:
    return [
        binary,
        "-p",
        "--output-format",
        "text",
        "--tools",
        "",
        "--no-session-persistence",
        "--model",
        model,
        f"/{skill}\n{prompt}",
    ]


def agent_skills_argv(binary: str, model: str, skill: str, prompt: str) -> list[str]:
    return [
        binary,
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--model",
        model,
        f"${skill}\n{prompt}",
    ]


def omp_argv(binary: str, model: str, skill: str, prompt: str) -> list[str]:
    return [
        binary,
        "-p",
        "--mode=text",
        "--no-tools",
        "--no-session",
        f"--model={model}",
        f"--skills={skill}",
        prompt,
    ]


def required_flags(runtime: str) -> tuple[str, ...]:
    try:
        return REQUIRED_FLAGS[runtime]
    except KeyError as exc:
        raise ValueError(f"unsupported runtime: {runtime}") from exc


def _text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _run_process(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
) -> ProcessResult:
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
    except subprocess.TimeoutExpired as exc:
        return ProcessResult(
            124,
            _text(exc.stdout),
            _text(exc.stderr),
            time.monotonic() - started,
            "timeout",
        )
    except OSError as exc:
        return ProcessResult(
            127,
            "",
            "",
            time.monotonic() - started,
            f"unavailable:{type(exc).__name__}",
        )
    return ProcessResult(
        completed.returncode,
        _text(completed.stdout),
        _text(completed.stderr),
        time.monotonic() - started,
    )


def _invoke(
    process_runner: ProcessRunner,
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> ProcessResult:
    started = time.monotonic()
    try:
        return process_runner(argv, cwd=cwd, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ProcessResult(124, "", "", time.monotonic() - started, "timeout")
    except OSError as exc:
        return ProcessResult(
            127,
            "",
            "",
            time.monotonic() - started,
            f"unavailable:{type(exc).__name__}",
        )


def _unquote_scalar(value: str, *, field: str) -> str:
    value = value.strip()
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid {field} frontmatter") from exc
        if isinstance(decoded, str):
            return decoded
        raise ValueError(f"invalid {field} frontmatter")
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if not value:
        raise ValueError(f"missing {field} frontmatter")
    return value


def _source_metadata(skill_file: Path) -> tuple[str, str, str]:
    if skill_file.is_symlink() or not skill_file.is_file():
        raise ValueError(f"missing regular SKILL.md: {skill_file}")
    lines = skill_file.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"missing frontmatter: {skill_file}")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError(f"unterminated frontmatter: {skill_file}") from exc
    frontmatter: dict[str, str] = {}
    for line in lines[1:closing]:
        if ":" not in line or line.startswith((" ", "\t", "#")):
            continue
        key, value = line.split(":", 1)
        if key in {"name", "description"}:
            frontmatter[key] = _unquote_scalar(value, field=key)
    try:
        name = frontmatter["name"]
        description = frontmatter["description"]
    except KeyError as exc:
        raise ValueError(f"missing {exc.args[0]} frontmatter: {skill_file}") from exc
    heading = next(
        (line for line in lines[closing + 1 :] if re.fullmatch(r"#{1,6} .+", line)),
        None,
    )
    if heading is None:
        raise ValueError(f"missing Markdown body heading: {skill_file}")
    return name, description, heading


def load_expected_skills(repo_root: Path, matrix_path: Path) -> dict[str, ExpectedSkill]:
    root = Path(repo_root).resolve()
    matrix = load_skill_matrix(matrix_path)
    expected: dict[str, ExpectedSkill] = {}
    for skill in sorted(matrix.skills):
        source = root / "claude" / "skills" / skill
        if source.is_symlink() or not source.is_dir():
            raise ValueError(f"invalid canonical Skill source: {skill}")
        if any(path.is_symlink() for path in source.rglob("*")):
            raise ValueError(f"canonical Skill source has symlink: {skill}")
        name, description, body_heading = _source_metadata(source / "SKILL.md")
        if name != skill:
            raise ValueError(f"canonical Skill name mismatch: {skill}")
        expected[skill] = ExpectedSkill(
            name=name,
            description=description,
            body_heading=body_heading,
            source=source,
            source_sha256=sha256_skill(source),
        )
    if len(expected) != 15:
        raise ValueError("Skill matrix must identify exactly 15 Skills")
    return expected


def _base_environment(isolated_home: Path, *, runtime: str | None) -> dict[str, str]:
    env = dict(os.environ)
    codex_home = env.pop("CODEX_HOME", None)
    for key in ("HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR"):
        env.pop(key, None)
    env["HOME"] = str(isolated_home)
    if runtime == "claude":
        env["CLAUDE_CONFIG_DIR"] = str(isolated_home / ".claude")
    elif runtime == "omp":
        env["PI_CODING_AGENT_DIR"] = str(isolated_home / ".omp" / "agent")
    elif runtime == "agent-skills" and codex_home:
        env["CODEX_HOME"] = codex_home
    return env


def _preflight(
    runtime: str,
    binary: str,
    *,
    repo_root: Path,
    env: dict[str, str],
    process_runner: ProcessRunner,
) -> HostPreflight:
    version = _invoke(
        process_runner,
        [binary, "--version"],
        cwd=repo_root,
        env=env,
        timeout=10,
    )
    if version.error_kind is not None:
        return HostPreflight(runtime, binary, "", False, version.error_kind, version.returncode)
    if version.returncode != 0:
        return HostPreflight(
            runtime, binary, "", False, f"preflight_version_exit:{version.returncode}", version.returncode
        )
    help_result = _invoke(
        process_runner,
        [binary, "--help"],
        cwd=repo_root,
        env=env,
        timeout=10,
    )
    if help_result.error_kind is not None:
        return HostPreflight(
            runtime, binary, version.stdout.strip(), False, help_result.error_kind, help_result.returncode
        )
    if help_result.returncode != 0:
        return HostPreflight(
            runtime,
            binary,
            version.stdout.strip(),
            False,
            f"preflight_help_exit:{help_result.returncode}",
            help_result.returncode,
        )
    missing = [flag for flag in required_flags(runtime) if flag not in help_result.stdout]
    if missing:
        return HostPreflight(
            runtime,
            binary,
            version.stdout.strip(),
            False,
            f"unsupported_required_flags:{','.join(missing)}",
            78,
        )
    return HostPreflight(runtime, binary, version.stdout.strip(), True, "", 0)


def _verify_link(target: Path, source: Path) -> bool:
    try:
        return target.is_symlink() and target.resolve(strict=True) == source.resolve(strict=True)
    except OSError:
        return False


def _install_records(
    expected: Mapping[str, ExpectedSkill],
    *,
    repo_root: Path,
    isolated_home: Path,
    process_runner: ProcessRunner,
) -> list[dict[str, object]]:
    roots = {runtime: isolated_home / relative for runtime, relative in TARGET_ROOTS.items()}
    installer = repo_root / "scripts" / "install-skill-links.sh"
    records: list[dict[str, object]] = []
    installer_env = _base_environment(isolated_home, runtime=None)
    for skill, source in expected.items():
        completed = _invoke(
            process_runner,
            [
                "sh",
                str(installer),
                str(source.source),
                *(str(roots[runtime]) for runtime in RUNTIMES),
            ],
            cwd=repo_root,
            env=installer_env,
            timeout=30,
        )
        for runtime in RUNTIMES:
            linked = completed.returncode == 0 and _verify_link(roots[runtime] / skill, source.source)
            if linked:
                status, diagnostic = "PASS", ""
            elif completed.error_kind is not None:
                status, diagnostic = "BLOCKED", completed.error_kind
            elif completed.returncode != 0:
                status, diagnostic = "BLOCKED", f"installer_exit:{completed.returncode}"
            else:
                status, diagnostic = "BLOCKED", "installer_target_not_canonical"
            records.append(
                {
                    "runtime": runtime,
                    "skill": skill,
                    "source_sha256": source.source_sha256,
                    "target_root": TARGET_ROOTS[runtime],
                    "status": status,
                    "diagnostic": diagnostic,
                }
            )
    return records


def _prompt() -> str:
    return (
        "Do not call tools, access files, or mutate anything. Return exactly one JSON object and no prose. "
        'Its schema is {"name":"exact Skill name","description":"exact frontmatter description",'
        '"body_heading":"exact first Markdown H1"}. Read the selected Skill only.'
    )


def _argv_for(runtime: str, binary: str, model: str, skill: str, prompt: str) -> list[str]:
    builders = {
        "claude": claude_argv,
        "agent-skills": agent_skills_argv,
        "omp": omp_argv,
    }
    return builders[runtime](binary, model, skill, prompt)


def _json_object_no_duplicates(text: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    parsed = json.loads(text, object_pairs_hook=reject_duplicates)
    if not isinstance(parsed, dict):
        raise ValueError("response is not an object")
    return parsed


def _response_diagnostic(stdout: str, expected: ExpectedSkill) -> str:
    if UNKNOWN_SKILL_RE.search(stdout):
        return "unknown_skill_response"
    try:
        payload = _json_object_no_duplicates(stdout)
    except (ValueError, json.JSONDecodeError):
        return "malformed_json_response"
    required = {"name", "description", "body_heading"}
    if set(payload) != required or not all(isinstance(payload[key], str) for key in required):
        return "invalid_response_schema"
    actual = (payload["name"], payload["description"], payload["body_heading"])
    source = (expected.name, expected.description, expected.body_heading)
    if actual != source:
        return "source_metadata_mismatch"
    return ""


def _discovery_record(
    *,
    runtime: str,
    preflight: HostPreflight,
    expected: ExpectedSkill,
    model: str,
    install_status: str,
    repo_root: Path,
    env: dict[str, str],
    timeout_sec: int,
    process_runner: ProcessRunner,
) -> dict[str, object]:
    record = {
        "runtime": runtime,
        "host_binary": preflight.binary,
        "binary_version": preflight.binary_version,
        "model": model,
        "skill": expected.name,
        "source_sha256": expected.source_sha256,
        "argv_safety_flags": list(SAFETY_FLAGS[runtime]),
        "elapsed_sec": 0.0,
        "exit_status": preflight.exit_status,
        "verdict": "BLOCKED",
        "diagnostic": "",
        "source_name": expected.name,
        "source_description": expected.description,
        "source_body_heading": expected.body_heading,
    }
    if not preflight.available:
        record["diagnostic"] = preflight.diagnostic
        return record
    if install_status != "PASS":
        record["exit_status"] = 78
        record["diagnostic"] = "install_blocked"
        return record

    argv = _argv_for(runtime, preflight.binary, model, expected.name, _prompt())
    completed = _invoke(
        process_runner,
        argv,
        cwd=repo_root,
        env=env,
        timeout=timeout_sec,
    )
    record["elapsed_sec"] = completed.elapsed_sec
    record["exit_status"] = completed.returncode
    if completed.error_kind is not None:
        record["diagnostic"] = completed.error_kind
        return record
    if completed.returncode != 0:
        if AUTH_FAILURE_RE.search(completed.stderr):
            record["diagnostic"] = "host_auth_unavailable"
        else:
            record["verdict"] = "FAIL"
            record["diagnostic"] = f"host_exit:{completed.returncode}"
        return record
    diagnostic = _response_diagnostic(completed.stdout, expected)
    if diagnostic:
        record["verdict"] = "FAIL"
        record["diagnostic"] = diagnostic
        return record
    record["verdict"] = "PASS"
    record["diagnostic"] = ""
    return record


def _write_discovery_report(
    repo_root: Path,
    *,
    batch_id: str,
    matrix_sha256: str,
    records: Sequence[Mapping[str, object]],
) -> Path:
    report = {
        "schema": DISCOVERY_REPORT_SCHEMA,
        "batch_id": batch_id,
        "matrix_sha256": matrix_sha256,
        "records": [dict(record) for record in records],
    }
    content = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    labels = _sensitive_labels(content)
    if labels:
        raise EvidenceError(f"sensitive discovery report blocked: {','.join(labels)}")
    target = discovery_report_path(repo_root, batch_id)
    _publish_new(target, content)
    return target


def run_discovery(
    *,
    repo_root: Path,
    matrix_path: Path,
    batch_id: str,
    binaries: Mapping[str, str],
    models: Mapping[str, str],
    timeout_sec: int,
    write_result: bool,
    process_runner: ProcessRunner = _run_process,
) -> DiscoveryRun:
    if timeout_sec <= 0:
        raise ValueError("timeout_sec must be positive")
    root = Path(repo_root).resolve()
    expected = load_expected_skills(root, Path(matrix_path))
    missing = [runtime for runtime in RUNTIMES if not binaries.get(runtime) or not models.get(runtime)]
    if missing:
        raise ValueError(f"missing runtime binary or model: {','.join(missing)}")
    matrix_sha256 = sha256_file(matrix_path)

    with tempfile.TemporaryDirectory(prefix="awf-skill-discovery-") as temporary:
        isolated_home = Path(temporary)
        preflights = {
            runtime: _preflight(
                runtime,
                binaries[runtime],
                repo_root=root,
                env=_base_environment(isolated_home, runtime=runtime),
                process_runner=process_runner,
            )
            for runtime in RUNTIMES
        }
        install_records = _install_records(
            expected,
            repo_root=root,
            isolated_home=isolated_home,
            process_runner=process_runner,
        )
        install_by_identity = {
            (str(record["runtime"]), str(record["skill"])): str(record["status"])
            for record in install_records
        }
        install_report = None
        if write_result:
            install_report = write_install_report(
                root,
                batch_id=batch_id,
                matrix_sha256=matrix_sha256,
                isolated_home_id=f"temporary-{uuid.uuid4().hex}",
                records=install_records,
            )

        discovery_records = [
            _discovery_record(
                runtime=runtime,
                preflight=preflights[runtime],
                expected=source,
                model=models[runtime],
                install_status=install_by_identity[(runtime, skill)],
                repo_root=root,
                env=_base_environment(isolated_home, runtime=runtime),
                timeout_sec=timeout_sec,
                process_runner=process_runner,
            )
            for runtime in RUNTIMES
            for skill, source in expected.items()
        ]
        discovery_report = None
        if write_result:
            discovery_report = _write_discovery_report(
                root,
                batch_id=batch_id,
                matrix_sha256=matrix_sha256,
                records=discovery_records,
            )

    records = [*install_records, *discovery_records]
    exit_code = 0 if all(record.get("verdict", record.get("status")) == "PASS" for record in records) else 1
    return DiscoveryRun(
        repo_root=root,
        install_records=tuple(install_records),
        discovery_records=tuple(discovery_records),
        install_report=install_report,
        discovery_report=discovery_report,
        exit_code=exit_code,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe all 15 canonical AWF Skills in isolated Claude, Agent Skills, and OMP homes."
    )
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument(
        "--matrix",
        default=str(REPO_ROOT / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json"),
    )
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--all", action="store_true", required=True)
    parser.add_argument("--claude-command", default="claude")
    parser.add_argument("--agent-skills-command", default="codex")
    parser.add_argument("--omp-command", default="omp")
    parser.add_argument("--claude-model", required=True)
    parser.add_argument("--agent-skills-model", required=True)
    parser.add_argument("--omp-model", required=True)
    parser.add_argument("--timeout-sec", type=int, default=120)
    parser.add_argument("--write-result", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = run_discovery(
            repo_root=Path(args.repo_root),
            matrix_path=Path(args.matrix),
            batch_id=args.batch_id,
            binaries={
                "claude": args.claude_command,
                "agent-skills": args.agent_skills_command,
                "omp": args.omp_command,
            },
            models={
                "claude": args.claude_model,
                "agent-skills": args.agent_skills_model,
                "omp": args.omp_model,
            },
            timeout_sec=args.timeout_sec,
            write_result=args.write_result,
        )
    except (EvidenceError, ValueError) as exc:
        parser.error(str(exc))

    payload = {
        "schema": "awf_skill_discovery_run_v1",
        "install_report": str(result.install_report) if result.install_report else None,
        "discovery_report": str(result.discovery_report) if result.discovery_report else None,
        "install_records": list(result.install_records),
        "discovery_records": list(result.discovery_records),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"install records: {len(result.install_records)}")
        print(f"discovery records: {len(result.discovery_records)}")
        if result.install_report:
            print(f"install report: {result.install_report}")
        if result.discovery_report:
            print(f"discovery report: {result.discovery_report}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
