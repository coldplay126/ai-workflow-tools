from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import stat
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
from awf.core.skill_subscription import (  # noqa: E402
    SubscriptionAuthContext,
    build_subscription_environment,
    normalize_host_diagnostic,
    require_subscription_model,
)

RUNTIMES = ("claude", "agent-skills", "omp")
TARGET_ROOTS = {
    "claude": ".claude/skills",
    "agent-skills": ".agents/skills",
    "omp": ".omp/skills",
}
REQUIRED_FLAGS = {
    "claude": (
        "-p",
        "--output-format",
        "--tools",
        "--no-session-persistence",
        "--setting-sources",
        "--model",
    ),
    "agent-skills": (
        "exec",
        "--ephemeral",
        "--sandbox",
        "--skip-git-repo-check",
        "--model",
    ),
    "omp": ("-p", "--mode=text", "--tools=read", "--no-session", "--no-extensions", "--model", "--skills"),
}
SAFETY_FLAGS = {
    "claude": (
        "-p",
        "--output-format=text",
        "--tools=",
        "--no-session-persistence",
        "--setting-sources=project",
    ),
    "agent-skills": ("exec", "--ephemeral", "--sandbox=read-only", "--skip-git-repo-check"),
    "omp": ("-p", "--mode=text", "--tools=read", "--no-session", "--no-extensions"),
}
UNKNOWN_SKILL_RE = re.compile(
    r"(?:unknown|unrecognized|unsupported)\s+(?:command|skill)|skill\s+.*(?:not found|unknown)",
    re.IGNORECASE,
)
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


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
    source_error: str = ""


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
        "--setting-sources",
        "project",
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
        "--tools=read",
        "--no-session",
        "--no-extensions",
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
    if not value or value in {"|", "|-", "|+", ">", ">-", ">+"}:
        raise ValueError(f"invalid {field} frontmatter")
    if re.fullmatch(r"[-+]?(?:\d+|\d+\.\d+)|true|false|null|~", value, re.IGNORECASE):
        raise ValueError(f"{field} frontmatter must be a string")
    return value


def _literal_scalar(
    lines: list[str], start: int, *, field: str, indicator: str
) -> tuple[str, int]:
    block: list[str] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if line and not line.startswith((" ", "\t")):
            break
        block.append(line)
        index += 1
    indents = [len(line) - len(line.lstrip(" ")) for line in block if line.strip()]
    if not indents:
        raise ValueError(f"invalid {field} frontmatter")
    indent = min(indents)
    if any(line.strip() and len(line) - len(line.lstrip(" ")) < indent for line in block):
        raise ValueError(f"invalid {field} frontmatter")
    value = "\n".join(line[indent:] if line else "" for line in block)
    if indicator != "|-":
        value += "\n"
    return value, index


def _identity_frontmatter(lines: list[str]) -> dict[str, str]:
    frontmatter: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line or line.startswith((" ", "\t", "#")) or ":" not in line:
            index += 1
            continue
        key, value = line.split(":", 1)
        if key not in {"name", "description"}:
            index += 1
            continue
        if key in frontmatter:
            raise ValueError(f"duplicate {key} frontmatter")
        value = value.strip()
        if key == "description" and value in {"|", "|-", "|+"}:
            frontmatter[key], index = _literal_scalar(
                lines, index + 1, field=key, indicator=value
            )
        else:
            frontmatter[key] = _unquote_scalar(value, field=key)
            index += 1
    return frontmatter


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
    frontmatter = _identity_frontmatter(lines[1:closing])
    try:
        name = frontmatter["name"]
        description = frontmatter["description"]
    except KeyError as exc:
        raise ValueError(f"missing {exc.args[0]} frontmatter: {skill_file}") from exc
    heading = next((line for line in lines[closing + 1 :] if line.startswith("# ")), None)
    if heading is None:
        raise ValueError(f"missing Markdown H1: {skill_file}")
    return name, description, heading


def load_expected_skills(repo_root: Path, matrix_path: Path) -> dict[str, ExpectedSkill]:
    root = Path(repo_root).resolve()
    matrix = load_skill_matrix(matrix_path)
    expected: dict[str, ExpectedSkill] = {}
    empty_sha256 = EMPTY_SHA256
    for skill in sorted(matrix.skills):
        source = root / "claude" / "skills" / skill
        source_error = ""
        source_sha256 = empty_sha256
        name = skill
        description = ""
        body_heading = ""
        if source.is_symlink() or not source.is_dir():
            source_error = "canonical_source_invalid"
        elif any(path.is_symlink() for path in source.rglob("*")):
            source_error = "canonical_source_symlink"
        else:
            source_sha256 = sha256_skill(source)
            try:
                name, description, body_heading = _source_metadata(source / "SKILL.md")
            except ValueError:
                source_error = "canonical_source_metadata_invalid"
                name = skill
                description = ""
                body_heading = ""
            else:
                if name != skill:
                    source_error = "canonical_source_name_mismatch"
                    name = skill
                    description = ""
                    body_heading = ""
        expected[skill] = ExpectedSkill(
            name=name,
            description=description,
            body_heading=body_heading,
            source=source,
            source_sha256=source_sha256,
            source_error=source_error,
        )
    if len(expected) != 15:
        raise ValueError("Skill matrix must identify exactly 15 Skills")
    return expected


def _regular_skill_paths(root: Path) -> list[Path]:
    try:
        root_mode = root.lstat().st_mode
    except OSError as exc:
        raise ValueError("unable to inspect Skill source") from exc
    if stat.S_ISLNK(root_mode):
        raise ValueError("Skill source is a symlink")
    if not stat.S_ISDIR(root_mode):
        raise ValueError("Skill source is not a directory")
    try:
        paths = [root, *sorted(root.rglob("*"))]
    except OSError as exc:
        raise ValueError("unable to inspect Skill source") from exc
    for path in paths:
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise ValueError("unable to inspect Skill source") from exc
        if stat.S_ISLNK(mode):
            raise ValueError("Skill source contains a symlink")
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ValueError("Skill source contains a non-regular entry")
    return paths


def _skill_state(root: Path) -> tuple[str, tuple[str, str, str], tuple[tuple[str, str], ...]]:
    paths = _regular_skill_paths(root)
    entries = tuple(
        (
            path.relative_to(root).as_posix(),
            "directory" if stat.S_ISDIR(path.lstat().st_mode) else "file",
        )
        for path in paths
    )
    return sha256_skill(root), _source_metadata(root / "SKILL.md"), entries


def _copy_regular_file(source: Path, destination: Path) -> None:
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    destination_fd: int | None = None
    try:
        source_mode = os.fstat(source_fd).st_mode
        if not stat.S_ISREG(source_mode):
            raise ValueError("Skill source contains a non-regular file")
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            source_mode & 0o777,
        )
        while data := os.read(source_fd, 65536):
            written = 0
            while written < len(data):
                written += os.write(destination_fd, data[written:])
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(source_fd)


def _copy_regular_skill_tree(source: Path, destination: Path) -> None:
    source_paths = _regular_skill_paths(source)
    destination.mkdir()
    for path in source_paths[1:]:
        target = destination / path.relative_to(source)
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            target.mkdir()
        elif stat.S_ISREG(mode):
            _copy_regular_file(path, target)
        else:
            raise ValueError("Skill source contains an unsupported entry")
    _regular_skill_paths(destination)


def _blocked_snapshot_skill(candidate: ExpectedSkill, diagnostic: str) -> ExpectedSkill:
    return ExpectedSkill(
        name=candidate.name,
        description="",
        body_heading="",
        source=candidate.source,
        source_sha256=EMPTY_SHA256,
        source_error=diagnostic,
    )


def _materialize_expected_skills(
    expected: Mapping[str, ExpectedSkill], snapshot_root: Path
) -> dict[str, ExpectedSkill]:
    snapshots: dict[str, ExpectedSkill] = {}
    snapshot_root.mkdir()
    for skill, candidate in expected.items():
        if candidate.source_error:
            snapshots[skill] = candidate
            continue
        snapshot = snapshot_root / skill
        try:
            before = _skill_state(candidate.source)
            _copy_regular_skill_tree(candidate.source, snapshot)
            materialized = _skill_state(snapshot)
            after = _skill_state(candidate.source)
        except (OSError, ValueError):
            snapshots[skill] = _blocked_snapshot_skill(
                candidate, "canonical_source_materialization_failed"
            )
            continue
        if before != materialized or after != materialized:
            snapshots[skill] = _blocked_snapshot_skill(
                candidate, "canonical_source_mutated_during_snapshot"
            )
            continue
        source_sha256, metadata, _ = materialized
        if metadata[0] != skill:
            snapshots[skill] = _blocked_snapshot_skill(
                candidate, "canonical_source_name_mismatch"
            )
            continue
        snapshots[skill] = ExpectedSkill(
            name=metadata[0],
            description=metadata[1],
            body_heading=metadata[2],
            source=snapshot,
            source_sha256=source_sha256,
        )
    return snapshots






def _help_has_flag(help_text: str, flag: str) -> bool:
    if flag == "exec":
        return re.search(r"(?<![a-zA-Z0-9_-])exec(?![a-zA-Z0-9_-])", help_text) is not None
    option = flag.split("=", 1)[0]
    return (
        re.search(
            rf"(?<![a-zA-Z0-9_-]){re.escape(option)}(?=$|[\s=,;:)\]])",
            help_text,
        )
        is not None
    )
def _preflight(
    runtime: str,
    binary: str,
    *,
    workspace: Path,
    env: dict[str, str],
    process_runner: ProcessRunner,
) -> HostPreflight:
    version = _invoke(
        process_runner,
        [binary, "--version"],
        cwd=workspace,
        env=env,
        timeout=10,
    )
    if version.error_kind is not None or version.returncode != 0:
        return HostPreflight(
            runtime,
            binary,
            "",
            False,
            normalize_host_diagnostic(
                version.returncode, version.stdout, version.stderr, version.error_kind
            ),
            version.returncode,
        )
    version_text = version.stdout.strip()
    top_level_help = _invoke(
        process_runner,
        [binary, "--help"],
        cwd=workspace,
        env=env,
        timeout=10,
    )
    if top_level_help.error_kind is not None or top_level_help.returncode != 0:
        return HostPreflight(
            runtime,
            binary,
            version_text,
            False,
            normalize_host_diagnostic(
                top_level_help.returncode,
                top_level_help.stdout,
                top_level_help.stderr,
                top_level_help.error_kind,
            ),
            top_level_help.returncode,
        )
    if runtime == "agent-skills":
        if not _help_has_flag(top_level_help.stdout, "exec"):
            return HostPreflight(
                runtime,
                binary,
                version_text,
                False,
                "unsupported_required_flags:exec",
                78,
            )
        scoped_help = _invoke(
            process_runner,
            [binary, "exec", "--help"],
            cwd=workspace,
            env=env,
            timeout=10,
        )
        if scoped_help.error_kind is not None or scoped_help.returncode != 0:
            return HostPreflight(
                runtime,
                binary,
                version_text,
                False,
                normalize_host_diagnostic(
                    scoped_help.returncode,
                    scoped_help.stdout,
                    scoped_help.stderr,
                    scoped_help.error_kind,
                ),
                scoped_help.returncode,
            )
        help_text = scoped_help.stdout
        flags = tuple(flag for flag in required_flags(runtime) if flag != "exec")
    else:
        help_text = top_level_help.stdout
        flags = required_flags(runtime)
    missing = [flag for flag in flags if not _help_has_flag(help_text, flag)]
    if missing:
        return HostPreflight(
            runtime,
            binary,
            version_text,
            False,
            f"unsupported_required_flags:{','.join(missing)}",
            78,
        )
    return HostPreflight(runtime, binary, version_text, True, "", 0)


def _verify_link(target: Path, source: Path) -> bool:
    try:
        return target.is_symlink() and target.resolve(strict=True) == source.resolve(strict=True)
    except OSError:
        return False


def _install_records(
    expected: Mapping[str, ExpectedSkill],
    *,
    repo_root: Path,
    workspace: Path,
    installer_env: dict[str, str],
    process_runner: ProcessRunner,
) -> list[dict[str, object]]:
    roots = {runtime: workspace / relative for runtime, relative in TARGET_ROOTS.items()}
    installer = repo_root / "scripts" / "install-skill-links.sh"
    records: list[dict[str, object]] = []
    for skill, source in expected.items():
        if source.source_error:
            for runtime in RUNTIMES:
                records.append(
                    {
                        "runtime": runtime,
                        "skill": skill,
                        "source_sha256": source.source_sha256,
                        "target_root": TARGET_ROOTS[runtime],
                        "status": "BLOCKED",
                        "diagnostic": source.source_error,
                    }
                )
            continue
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
            linked = _verify_link(roots[runtime] / skill, source.source)
            if linked:
                status, diagnostic = "PASS", ""
            elif completed.error_kind is not None or completed.returncode != 0:
                status = "BLOCKED"
                diagnostic = normalize_host_diagnostic(
                    completed.returncode,
                    completed.stdout,
                    completed.stderr,
                    completed.error_kind,
                )
            else:
                status, diagnostic = "BLOCKED", "installer_target_not_snapshot"
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


def _prompt(runtime: str) -> str:
    if runtime == "omp":
        return (
            "Use only the read tool to load the selected Skill. Do not read any other path or mutate anything. "
            "Return the decoded YAML frontmatter scalar values for name and description, excluding YAML syntax "
            "such as surrounding quote delimiters, block indicators, and indentation. "
            "Preserve each decoded scalar's content and embedded newlines exactly; do not translate, summarize, "
            "or normalize whitespace. "
            "body_heading is the exact source Markdown H1 line, including the literal leading '# '. "
            "Encode embedded newlines as JSON escapes. "
            'Return exactly one JSON object and no prose. Its schema is {"name":"decoded frontmatter name",'
            '"description":"decoded frontmatter description","body_heading":"exact first Markdown H1"}.'
        )
    return (
        "Do not call tools, access files, or mutate anything. Read the selected Skill only. "
        "Return the decoded YAML frontmatter scalar values for name and description, excluding YAML syntax "
        "such as surrounding quote delimiters, block indicators, and indentation. "
        "Preserve each decoded scalar's content and embedded newlines exactly; do not translate, summarize, "
        "or normalize whitespace. "
        "body_heading is the exact source Markdown H1 line, including the literal leading '# '. "
        "Encode embedded newlines as JSON escapes. "
        'Return exactly one JSON object and no prose. Its schema is {"name":"decoded frontmatter name",'
        '"description":"decoded frontmatter description","body_heading":"exact first Markdown H1"}.'
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
    try:
        payload = _json_object_no_duplicates(stdout)
    except (ValueError, json.JSONDecodeError):
        if UNKNOWN_SKILL_RE.search(stdout):
            return "unknown_skill_response"
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
    workspace: Path,
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
        "auth_mode": "subscription",
        "argv_safety_flags": list(SAFETY_FLAGS[runtime]),
        "elapsed_sec": 0.0,
        "exit_status": preflight.exit_status,
        "verdict": "BLOCKED",
        "diagnostic": "",
        "source_name": expected.name,
        "source_description": expected.description,
        "source_body_heading": expected.body_heading,
    }
    if expected.source_error:
        record["diagnostic"] = expected.source_error
        return record
    if not preflight.available:
        if preflight.diagnostic == "host_model_unsupported":
            record["verdict"] = "FAIL"
        record["diagnostic"] = preflight.diagnostic
        return record
    if install_status != "PASS":
        record["exit_status"] = 78
        record["diagnostic"] = "install_blocked"
        return record

    argv = _argv_for(runtime, preflight.binary, model, expected.name, _prompt(runtime))
    completed = _invoke(
        process_runner,
        argv,
        cwd=workspace,
        env=env,
        timeout=timeout_sec,
    )
    record["elapsed_sec"] = completed.elapsed_sec
    record["exit_status"] = completed.returncode
    if completed.error_kind is not None or completed.returncode != 0:
        diagnostic = normalize_host_diagnostic(
            completed.returncode,
            completed.stdout,
            completed.stderr,
            completed.error_kind,
        )
        if diagnostic == "host_model_unsupported":
            record["verdict"] = "FAIL"
        record["diagnostic"] = diagnostic
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
    auth_context: SubscriptionAuthContext | None = None,
) -> DiscoveryRun:
    if timeout_sec <= 0:
        raise ValueError("timeout_sec must be positive")
    root = Path(repo_root).resolve()
    expected = load_expected_skills(root, Path(matrix_path))
    missing = [runtime for runtime in RUNTIMES if not binaries.get(runtime) or not models.get(runtime)]
    if missing:
        raise ValueError(f"missing runtime binary or model: {','.join(missing)}")
    matrix_sha256 = sha256_file(matrix_path)
    for runtime in RUNTIMES:
        require_subscription_model(runtime, models[runtime])
    auth = auth_context or SubscriptionAuthContext.capture()

    with tempfile.TemporaryDirectory(prefix="awf-skill-discovery-") as temporary:
        temporary_root = Path(temporary)
        temporary_home = temporary_root / "home"
        workspace = temporary_root / "workspace"
        temporary_home.mkdir()
        workspace.mkdir()
        expected = _materialize_expected_skills(
            expected, temporary_root / "skill-snapshots"
        )
        base_environment = dict(os.environ)
        environments = {
            runtime: build_subscription_environment(
                runtime, auth, temporary_home, base_environment
            )
            for runtime in RUNTIMES
        }
        preflights = {
            runtime: _preflight(
                runtime,
                binaries[runtime],
                workspace=workspace,
                env=environments[runtime],
                process_runner=process_runner,
            )
            for runtime in RUNTIMES
        }
        install_records = _install_records(
            expected,
            repo_root=root,
            workspace=workspace,
            installer_env=environments["agent-skills"],
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
                workspace=workspace,
                env=environments[runtime],
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
    exit_code = (
        0
        if all(record.get("verdict", record.get("status")) == "PASS" for record in records)
        else 1
    )
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
