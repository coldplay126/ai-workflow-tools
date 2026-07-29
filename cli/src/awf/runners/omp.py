from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from awf.core.agent_runner import AgentResult, _try_parse_json
SchemaMode = Literal["permissive", "strict"]
CoordinationSurface = Literal["native", "print"]
JsonSchema = dict[str, Any] | bool | str | None


@dataclass(frozen=True)
class OmpRunnerConfig:
    command: str = "omp"
    model: str | None = None
    extra_args: tuple[str, ...] = field(default_factory=tuple)
    timeout_sec: int = 300
    no_session: bool = True
    coordination_surface: CoordinationSurface = "native"
    capacity: int = 8
    termination_grace_sec: float = 1.0

    @classmethod
    def from_env(cls) -> "OmpRunnerConfig":
        command = os.environ.get("AWF_OMP_COMMAND", "omp").strip() or "omp"
        model = os.environ.get("AWF_OMP_MODEL", "").strip() or None
        extra_args = tuple(shlex.split(os.environ.get("AWF_OMP_EXTRA_ARGS", "")))
        timeout_sec = _positive_int(os.environ.get("AWF_OMP_TIMEOUT_SEC"), 300)
        no_session = os.environ.get("AWF_OMP_NO_SESSION", "1").strip().lower() not in {
            "0",
            "false",
            "no",
        }
        surface = os.environ.get("AWF_OMP_COORDINATION_SURFACE", "native").strip().lower()
        coordination_surface: CoordinationSurface = "print" if surface == "print" else "native"
        capacity = _positive_int(os.environ.get("AWF_OMP_CAPACITY"), 8)
        try:
            grace = max(
                0.05,
                float(os.environ.get("AWF_OMP_TERMINATION_GRACE_SEC", "1")),
            )
        except ValueError:
            grace = 1.0
        return cls(
            command=command,
            model=model,
            extra_args=extra_args,
            timeout_sec=timeout_sec,
            no_session=no_session,
            coordination_surface=coordination_surface,
            capacity=capacity,
            termination_grace_sec=grace,
        )


@dataclass(frozen=True)
class OmpExecutionResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed_sec: float
    timed_out: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class OmpWorkerTask:
    """One exact worker descriptor passed to the native OMP coordinator."""

    name: str
    role: str
    prompt: str
    agent_type: str
    output_schema: JsonSchema = None
    schema_mode: SchemaMode = "permissive"
    isolated: bool | None = None
    require_json: bool = False


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def omp_worker_name(index: int, role: str) -> str:
    """Return a deterministic OMP-compatible CamelCase task name."""
    words = re.findall(r"[A-Za-z0-9]+", role)
    suffix = "".join(word[:1].upper() + word[1:] for word in words) or "Worker"
    return f"Awf{index:03d}{suffix}"[:32]


def build_omp_print_command(
    prompt: str,
    config: OmpRunnerConfig | None = None,
    *,
    model: str | None = None,
) -> list[str]:
    cfg = config or OmpRunnerConfig.from_env()
    cmd = [cfg.command, *cfg.extra_args, "--mode", "json"]
    if cfg.no_session:
        cmd.append("--no-session")
    selected_model = model or cfg.model
    if selected_model:
        cmd.extend(["--model", selected_model])
    cmd.extend(["-p", prompt])
    return cmd


def build_omp_native_command(
    prompt_path: str | os.PathLike[str],
    config: OmpRunnerConfig | None = None,
    *,
    model: str | None = None,
) -> list[str]:
    """Build a bounded argv that asks OMP to expand a UTF-8 prompt file."""
    return build_omp_print_command(f"@{os.fspath(prompt_path)}", config, model=model)


def build_omp_coordinator_prompt(
    workers: Sequence[OmpWorkerTask],
    *,
    capacity: int,
) -> str:
    """Serialize one exact native ``task`` batch and host envelope contract."""
    descriptors = [
        {
            "name": worker.name,
            "agent": worker.agent_type,
            "task": worker.prompt,
            "outputSchema": worker.output_schema,
            "schemaMode": worker.schema_mode,
            **(
                {"isolated": worker.isolated}
                if worker.isolated is not None
                else {}
            ),
        }
        for worker in workers
    ]
    payload = json.dumps(
        {"capacity": capacity, "tasks": descriptors},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    names = json.dumps([worker.name for worker in workers], separators=(",", ":"))
    return f"""You are the OMP host coordinator for one AWF worker batch.
Call `task` exactly once using the native tool. Its `tasks` array MUST preserve the order and exact `name`, `agent`, `task`, `outputSchema`, and `schemaMode` values in COORDINATOR_INPUT. Include `isolated` only when it is not null. Do not rename tasks, invent task IDs, replace agent types, split the batch, or use another execution backend.
The configured capacity is {capacity}; this input has {len(workers)} workers and has already been capacity-checked.
Wait for every task to settle using native async delivery or `hub wait`; do not start successor tasks.
Respond with JSON only: {{"awf_omp_batch":1,"workers":[{{"name":"<stable input name>","status":"completed","result":<exact yielded data>}},{{"name":"<stable input name>","status":"failed","error":"<explicit error>"}}]}}
Return exactly one entry for each name, in any completion order: {names}. Never put task IDs in the envelope; AWF obtains authentic IDs from native task events.
COORDINATOR_INPUT={payload}
"""


def _message_text(message: Any) -> str:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    content = message.get("content", [])
    if not isinstance(content, list):
        return ""
    return "".join(
        str(item.get("text", ""))
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    )


def _json_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            event = json.loads(line.strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _nonnegative_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return value


def _sanitized_usage(value: Any) -> dict[str, int | float]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): number
        for key, raw in value.items()
        if (number := _nonnegative_number(raw)) is not None
    }


def _isolated_patch_path(candidate: dict[str, Any]) -> str | None:
    direct = candidate.get("patchPath") or candidate.get("patch_path")
    if direct:
        return str(direct)
    result_text = candidate.get("resultText") or candidate.get("result_text")
    if not isinstance(result_text, str):
        return None
    match = re.search(
        r"Isolation: changes captured at `([^`]+[.]patch)`",
        result_text,
    )
    return match.group(1) if match else None


def parse_omp_task_events(text: str) -> list[dict[str, Any]]:
    """Collect authentic task handles without retaining assignments or results."""
    records: dict[tuple[str, int | str], dict[str, Any]] = {}
    next_order = 0
    for event in _json_events(text):
        if event.get("toolName") not in {"task", "hub"} or event.get("type") not in {
            "tool_execution_update",
            "tool_execution_end",
        }:
            continue
        container = event.get(
            "partialResult"
            if event.get("type") == "tool_execution_update"
            else "result"
        )
        details = container.get("details") if isinstance(container, dict) else None
        candidates: list[Any] = []
        for node in (event, container, details):
            if not isinstance(node, dict):
                continue
            for field_name in ("progress", "results", "agents", "jobs"):
                value = node.get(field_name)
                if isinstance(value, list):
                    candidates.extend(value)
                elif isinstance(value, dict):
                    for nested_name in ("agents", "results", "progress", "jobs"):
                        nested = value.get(nested_name)
                        if isinstance(nested, list):
                            candidates.extend(nested)

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            raw_task_id = (
                candidate.get("taskId")
                or candidate.get("task_id")
                or candidate.get("id")
                or candidate.get("jobId")
            )
            if raw_task_id in (None, ""):
                continue
            raw_index = candidate.get("index")
            index = (
                raw_index
                if isinstance(raw_index, int) and not isinstance(raw_index, bool)
                else None
            )
            task_id = str(raw_task_id)
            key = ("index", index) if index is not None else ("id", task_id)
            if index is None:
                matching_key = next(
                    (
                        existing_key
                        for existing_key, existing in records.items()
                        if task_id
                        in {
                            existing.get("task_id"),
                            existing.get("task_name"),
                        }
                    ),
                    None,
                )
                if matching_key is not None:
                    key = matching_key
            record = records.get(key, {"_order": next_order})
            if key not in records:
                next_order += 1
            updates: dict[str, Any] = {
                "index": index,
                "task_id": task_id,
                "agent_name": (
                    str(
                        candidate.get("agentName")
                        or candidate.get("agent_name")
                        or candidate.get("agent")
                        or ""
                    )
                    or None
                ),
                "task_name": (
                    str(
                        candidate.get("name")
                        or candidate.get("label")
                        or candidate.get("id")
                        or ""
                    )
                    or None
                ),
                "status": str(candidate.get("status") or "") or None,
                "resolved_model": (
                    str(
                        candidate.get("resolvedModel")
                        or candidate.get("resolved_model")
                        or ""
                    )
                    or None
                ),
                "resolved_model_is_fallback": (
                    candidate.get("resolvedModelIsFallback")
                    if isinstance(candidate.get("resolvedModelIsFallback"), bool)
                    else candidate.get("resolved_model_is_fallback")
                    if isinstance(candidate.get("resolved_model_is_fallback"), bool)
                    else None
                ),
                "duration_ms": _nonnegative_number(
                    candidate.get("durationMs", candidate.get("duration_ms"))
                ),
                "tokens": _nonnegative_number(candidate.get("tokens")),
                "cost": _nonnegative_number(candidate.get("cost")),
                "usage": _sanitized_usage(candidate.get("usage")),
                "patch_path": _isolated_patch_path(candidate),
            }
            record.update(
                {
                    name: value
                    for name, value in updates.items()
                    if value is not None
                    or (name == "index" and "index" not in record)
                }
            )
            records[key] = record
    ordered = sorted(
        records.values(),
        key=lambda item: (
            item.get("index") is None,
            item.get("index")
            if item.get("index") is not None
            else item["_order"],
        ),
    )
    for record in ordered:
        record.pop("_order", None)
    return ordered


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_omp_json_stream(
    text: str,
    *,
    session_persisted: bool,
) -> tuple[str, dict[str, Any], int, int]:
    """Extract the final host response, task handles, and provenance from NDJSON."""
    events = _json_events(text)
    session_id: str | None = None
    final_message: dict[str, Any] | None = None
    for event in events:
        if event.get("type") == "session" and event.get("id"):
            session_id = str(event["id"])
        if _message_text(event.get("message")):
            final_message = event["message"]
        if event.get("type") == "agent_end" and isinstance(event.get("messages"), list):
            for candidate in event["messages"]:
                if _message_text(candidate):
                    final_message = candidate

    output = _message_text(final_message)
    usage = final_message.get("usage", {}) if isinstance(final_message, dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = _safe_int(usage.get("input"))
    output_tokens = _safe_int(usage.get("output"))
    provider = str(final_message.get("provider") or "") if final_message else ""
    model = str(final_message.get("model") or "") if final_message else ""
    metadata: dict[str, Any] = {
        "backend": "omp",
        "session_id": session_id,
        "session_persisted": session_persisted,
        "agent_uri": None,
        "history_uri": None,
        "provider": provider or None,
        "model": model or None,
        "response_id": final_message.get("responseId") if final_message else None,
        "stop_reason": final_message.get("stopReason") if final_message else None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": _safe_int(usage.get("cacheRead")),
            "cache_write_tokens": _safe_int(usage.get("cacheWrite")),
            "total_tokens": (
                _safe_int(usage.get("totalTokens")) or input_tokens + output_tokens
            ),
            "cost": usage.get("cost") if isinstance(usage.get("cost"), dict) else {},
        },
        "task_progress": parse_omp_task_events(text),
    }
    return output, metadata, input_tokens, output_tokens


def run_omp_print(
    prompt: str,
    *,
    cwd: str | None = None,
    config: OmpRunnerConfig | None = None,
    model: str | None = None,
    timeout_sec: int | None = None,
) -> OmpExecutionResult:
    cfg = config or OmpRunnerConfig.from_env()
    effective_timeout = timeout_sec if timeout_sec is not None else cfg.timeout_sec
    cmd = build_omp_print_command(prompt, cfg, model=model)
    env = os.environ.copy()
    env.setdefault("PI_SKIP_VERSION_CHECK", "1")
    env.setdefault("PI_TELEMETRY", "0")

    started = time.monotonic()
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
            timeout=effective_timeout,
        )
    except FileNotFoundError:
        return OmpExecutionResult(
            returncode=127,
            stdout="",
            stderr=f"omp command not found: {cfg.command}",
            elapsed_sec=time.monotonic() - started,
            metadata={
                "backend": "omp",
                "command": cfg.command,
                "coordination_surface": "print",
            },
        )
    except subprocess.TimeoutExpired:
        return OmpExecutionResult(
            returncode=124,
            stdout="",
            stderr=f"runner_timeout: omp timed out after {effective_timeout}s",
            elapsed_sec=time.monotonic() - started,
            metadata={
                "backend": "omp",
                "command": cfg.command,
                "coordination_surface": "print",
            },
        )

    output, metadata, input_tokens, output_tokens = parse_omp_json_stream(
        completed.stdout,
        session_persisted=not cfg.no_session,
    )
    metadata["command"] = cfg.command
    metadata["requested_model"] = model or cfg.model
    metadata["coordination_surface"] = "print"
    if not output and completed.stdout.strip():
        output = completed.stdout.strip()
    return OmpExecutionResult(
        returncode=completed.returncode,
        stdout=output,
        stderr=completed.stderr.strip(),
        elapsed_sec=time.monotonic() - started,
        metadata=metadata,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _terminate_process_group(
    process: subprocess.Popen[str],
    grace_sec: float,
) -> tuple[str, str]:
    if process.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
    try:
        stdout, stderr = process.communicate(timeout=grace_sec)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
    return _as_text(stdout), _as_text(stderr)


def _run_omp_native_host(
    prompt: str,
    *,
    cwd: str | None,
    config: OmpRunnerConfig,
    model: str | None,
    timeout_sec: int,
    isolate_tasks: bool = False,
) -> OmpExecutionResult:
    started = time.monotonic()
    prompt_path: Path | None = None
    settings_path: Path | None = None
    process: subprocess.Popen[str] | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="awf-omp-coordinator-",
            suffix=".txt",
            mode="w",
            encoding="utf-8",
            delete=False,
        ) as handle:
            handle.write(prompt)
            handle.write("\n")
            prompt_path = Path(handle.name)
        os.chmod(prompt_path, 0o600)
        command = build_omp_native_command(prompt_path, config, model=model)
        if isolate_tasks:
            with tempfile.NamedTemporaryFile(
                prefix="awf-omp-settings-",
                suffix=".json",
                mode="w",
                encoding="utf-8",
                delete=False,
            ) as settings_handle:
                json.dump(
                    {
                        "task": {
                            "isolation": {
                                "mode": "auto",
                                "apply": False,
                                "merge": "patch",
                            }
                        }
                    },
                    settings_handle,
                )
                settings_handle.write("\n")
                settings_path = Path(settings_handle.name)
            os.chmod(settings_path, 0o600)
            command[1:1] = ["--config", str(settings_path)]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            cwd=cwd,
            env={
                **os.environ,
                "PI_SKIP_VERSION_CHECK": os.environ.get("PI_SKIP_VERSION_CHECK", "1"),
                "PI_TELEMETRY": os.environ.get("PI_TELEMETRY", "0"),
            },
            start_new_session=os.name == "posix",
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_sec)
            returncode = process.returncode
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = _terminate_process_group(
                process,
                config.termination_grace_sec,
            )
            stdout = stdout or _as_text(exc.stdout)
            stderr = stderr or _as_text(exc.stderr)
            returncode = 124
            timed_out = True
    except FileNotFoundError:
        return OmpExecutionResult(
            returncode=127,
            stdout="",
            stderr=f"omp command not found: {config.command}",
            elapsed_sec=time.monotonic() - started,
            metadata={
                "backend": "omp",
                "command": config.command,
                "coordination_surface": "native",
            },
        )
    except BaseException:
        if process is not None:
            _terminate_process_group(process, config.termination_grace_sec)
        raise
    finally:
        if prompt_path is not None:
            try:
                prompt_path.unlink()
            except FileNotFoundError:
                pass
        if settings_path is not None:
            try:
                settings_path.unlink()
            except FileNotFoundError:
                pass

    output, metadata, input_tokens, output_tokens = parse_omp_json_stream(
        stdout,
        session_persisted=not config.no_session,
    )
    metadata.update(
        command=config.command,
        requested_model=model or config.model,
        coordination_surface="native",
    )
    stderr = stderr.strip()
    if timed_out:
        message = (
            f"runner_timeout: omp native coordinator timed out after {timeout_sec}s"
        )
        stderr = f"{message}\n{stderr}" if stderr else message
    return OmpExecutionResult(
        returncode=returncode,
        stdout=output,
        stderr=stderr,
        elapsed_sec=time.monotonic() - started,
        timed_out=timed_out,
        metadata=metadata,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _parse_json_value(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            stripped = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return None


def parse_omp_native_envelope(
    text: str,
) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    """Validate and index the host envelope by stable input name."""
    value = _parse_json_value(text)
    if not isinstance(value, dict):
        return None, "host response is not a JSON object"
    if value.get("awf_omp_batch", 1) != 1:
        return None, "unsupported awf_omp_batch version"
    workers = value.get("workers")
    if not isinstance(workers, list):
        return None, "host envelope workers is not an array"
    indexed: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(workers):
        if not isinstance(item, dict):
            return None, f"host envelope workers[{index}] is not an object"
        name = item.get("name")
        if not isinstance(name, str) or not name:
            return None, f"host envelope workers[{index}].name is not a string"
        if name in indexed:
            return None, f"host envelope contains duplicate worker {name!r}"
        if "result" not in item and "error" not in item:
            return None, f"host envelope worker {name!r} has neither result nor error"
        indexed[name] = item
    return indexed, None


def _schema_value(
    schema: JsonSchema,
) -> tuple[dict[str, Any] | bool | None, str | None]:
    if isinstance(schema, (dict, bool)):
        return schema, None
    if isinstance(schema, str):
        parsed = _parse_json_value(schema)
        if isinstance(parsed, (dict, bool)):
            return parsed, None
        return None, "schema string is not a JSON object or boolean"
    return None, None


def validate_json_schema(value: Any, schema: JsonSchema) -> list[str]:
    """Validate a value against the complete JSON Schema Draft 2020-12 contract."""
    normalized, schema_error = _schema_value(schema)
    if schema_error:
        return [schema_error]
    if normalized is None or normalized is True:
        return []
    try:
        Draft202012Validator.check_schema(normalized)
    except SchemaError as exc:
        return [f"invalid schema: {exc.message}"]

    validator = Draft202012Validator(normalized)
    validation_errors = sorted(
        validator.iter_errors(value),
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            tuple(str(part) for part in error.absolute_schema_path),
        ),
    )
    errors: list[str] = []
    for error in validation_errors:
        path = "$"
        for part in error.absolute_path:
            path += f"[{part}]" if isinstance(part, int) else f".{part}"
        errors.append(f"{path}: {error.message}")
    return errors


def _task_record(
    progress: list[dict[str, Any]],
    index: int,
    worker_name: str,
) -> dict[str, Any] | None:
    for record in progress:
        if record.get("index") == index:
            return record
    for record in progress:
        if worker_name in {
            record.get("task_id"),
            record.get("task_name"),
            record.get("agent_name"),
        }:
            return record
    if index < len(progress) and progress[index].get("index") is None:
        return progress[index]
    return None


def _worker_metadata(
    execution: OmpExecutionResult,
    record: dict[str, Any] | None,
) -> dict[str, Any]:
    metadata = dict(execution.metadata)
    task_id = record.get("task_id") if record else None
    coordinator_model = metadata.get("model")
    resolved_model = record.get("resolved_model") if record else None
    worker_usage = {
        "tokens": record.get("tokens") if record else None,
        "cost": record.get("cost") if record else None,
        "duration_ms": record.get("duration_ms") if record else None,
        **(record.get("usage", {}) if record else {}),
    }
    metadata.update(
        coordination_surface="native",
        coordinator_session_id=metadata.get("session_id"),
        coordinator_model=coordinator_model,
        task_id=task_id,
        agent_name=record.get("agent_name") if record else None,
        task_status=record.get("status") if record else None,
        patch_path=record.get("patch_path") if record else None,
        status=record.get("status") if record else None,
        agent_uri=f"agent://{task_id}" if task_id else None,
        history_uri=f"history://{task_id}" if task_id else None,
        resolved_model=resolved_model,
        resolved_model_is_fallback=(
            record.get("resolved_model_is_fallback") if record else None
        ),
        model=resolved_model or coordinator_model,
        worker_usage=worker_usage,
        cost=worker_usage.get("cost"),
    )
    return metadata


def _capacity_failures(
    workers: Sequence[OmpWorkerTask],
    capacity: int,
) -> list[AgentResult]:
    message = (
        f"omp_capacity_exceeded: batch has {len(workers)} workers; "
        f"capacity is {capacity}"
    )
    return [
        AgentResult(
            provider_name="omp",
            role=worker.role,
            stdout="",
            stderr=message,
            returncode=1,
            elapsed_sec=0.0,
            metadata={
                "backend": "omp",
                "coordination_surface": "native",
                "task_id": None,
                "agent_uri": None,
                "history_uri": None,
                "schema_validation": {
                    "mode": worker.schema_mode,
                    "valid": None,
                    "errors": [],
                },
            },
        )
        for worker in workers
    ]


def run_omp_native_batch(
    workers: Sequence[OmpWorkerTask],
    *,
    cwd: str | None = None,
    config: OmpRunnerConfig | None = None,
    model: str | None = None,
    timeout_sec: int | None = None,
) -> list[AgentResult]:
    """Run a native OMP task batch in one host process and preserve input order."""
    if not workers:
        return []
    cfg = config or OmpRunnerConfig.from_env()
    if len(workers) > cfg.capacity:
        return _capacity_failures(workers, cfg.capacity)
    execution = _run_omp_native_host(
        build_omp_coordinator_prompt(workers, capacity=cfg.capacity),
        cwd=cwd,
        config=cfg,
        model=model,
        timeout_sec=timeout_sec if timeout_sec is not None else cfg.timeout_sec,
        isolate_tasks=any(worker.isolated is True for worker in workers),
    )
    indexed, envelope_error = parse_omp_native_envelope(execution.stdout)
    progress = execution.metadata.get("task_progress", [])
    if not isinstance(progress, list):
        progress = []
    provider = str(execution.metadata.get("provider") or "")
    provider_name = f"omp:{provider}" if provider else "omp"
    completed: list[AgentResult] = []
    for index, worker in enumerate(workers):
        record = _task_record(progress, index, worker.name)
        metadata = _worker_metadata(execution, record)
        item = indexed.get(worker.name) if indexed is not None else None
        if item is None:
            reason = envelope_error or (
                f"native host envelope missing worker {worker.name!r}"
            )
            timed_out = execution.timed_out
            metadata["schema_validation"] = {
                "mode": worker.schema_mode,
                "valid": False,
                "errors": [reason],
            }
            completed.append(
                AgentResult(
                    provider_name=provider_name,
                    role=worker.role,
                    stdout="",
                    stderr=f"{reason}\n{execution.stderr}".strip(),
                    returncode=124 if timed_out else (execution.returncode or 2),
                    elapsed_sec=execution.elapsed_sec,
                    timed_out=timed_out,
                    parse_error=True,
                    metadata=metadata,
                )
            )
            continue

        lifecycle_status = (
            str(record.get("status") or "").lower() if record is not None else ""
        )
        lifecycle_failure = (
            "native task lifecycle evidence missing"
            if record is None
            else (
                f"native task lifecycle status: {lifecycle_status or 'missing'}"
                if lifecycle_status not in {"completed", "success", "ok"}
                else ""
            )
        )
        status = str(item.get("status") or "completed").lower()
        has_result = "result" in item
        result_value = item.get("result")
        if isinstance(result_value, str):
            stdout = result_value
            embedded = _parse_json_value(result_value)
            parsed = result_value if embedded is None else embedded
        else:
            stdout = (
                json.dumps(
                    result_value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if has_result
                else ""
            )
            parsed = result_value
        validation_errors = validate_json_schema(
            parsed,
            worker.output_schema,
        )
        metadata["schema_validation"] = {
            "mode": worker.schema_mode,
            "valid": (
                not validation_errors if worker.output_schema is not None else None
            ),
            "errors": validation_errors,
        }
        completed_before_timeout = (
            execution.timed_out
            and lifecycle_status in {"completed", "success", "ok"}
        )
        host_failure = (
            f"native coordinator exited {execution.returncode}"
            if execution.returncode != 0 and not completed_before_timeout
            else ""
        )
        failed_status = (
            status not in {"completed", "success", "ok"}
            or not has_result
            or bool(lifecycle_failure)
            or bool(host_failure)
        )
        raw_returncode = item.get("returncode")
        if failed_status:
            if (
                isinstance(raw_returncode, int)
                and not isinstance(raw_returncode, bool)
                and raw_returncode != 0
            ):
                returncode = raw_returncode
            elif execution.returncode != 0:
                returncode = execution.returncode
            else:
                returncode = 1
        else:
            returncode = (
                raw_returncode
                if isinstance(raw_returncode, int)
                and not isinstance(raw_returncode, bool)
                else 0
            )
        parse_error = bool(worker.require_json and parsed is None)
        error_text = str(item.get("error") or "")
        if validation_errors and worker.schema_mode == "strict":
            parse_error = True
            returncode = returncode or 2
            validation_message = (
                "schema_validation_failed: " + "; ".join(validation_errors)
            )
            error_text = f"{error_text}\n{validation_message}".strip()
        if lifecycle_failure:
            error_text = f"{error_text}\n{lifecycle_failure}".strip()
        if host_failure:
            error_text = f"{error_text}\n{host_failure}".strip()
        elif failed_status and not error_text:
            error_text = f"native worker status: {status}"
        if execution.stderr and returncode != 0:
            error_text = f"{error_text}\n{execution.stderr}".strip()
        duration_ms = record.get("duration_ms") if record else None
        elapsed_sec = (
            float(duration_ms) / 1000.0
            if isinstance(duration_ms, (int, float))
            else execution.elapsed_sec
        )
        completed.append(
            AgentResult(
                provider_name=provider_name,
                role=worker.role,
                stdout=stdout,
                stderr=error_text,
                returncode=returncode,
                elapsed_sec=elapsed_sec,
                timed_out=execution.timed_out and not completed_before_timeout,
                parse_error=parse_error,
                parsed=parsed,
                metadata=metadata,
            )
        )
    return completed


def run_omp_agent(
    prompt: str,
    *,
    role: str,
    cwd: str | None = None,
    require_json: bool = False,
    config: OmpRunnerConfig | None = None,
    model: str | None = None,
    timeout_sec: int | None = None,
) -> AgentResult:
    result = run_omp_print(
        prompt,
        cwd=cwd,
        config=config,
        model=model,
        timeout_sec=timeout_sec,
    )
    parsed = _try_parse_json(result.stdout) if result.stdout else None
    provider = str(result.metadata.get("provider") or "")
    return AgentResult(
        provider_name=f"omp:{provider}" if provider else "omp",
        role=role,
        stdout=result.stdout,
        stderr=result.stderr,
        returncode=result.returncode,
        elapsed_sec=result.elapsed_sec,
        timed_out=result.returncode == 124 and "timeout" in result.stderr.lower(),
        parse_error=bool(result.stdout and require_json and parsed is None),
        parsed=parsed,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        metadata=result.metadata,
    )
