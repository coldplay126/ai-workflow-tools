from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from types import MappingProxyType
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from awf.core.agent_runner import AgentResult, _try_parse_json
SchemaMode = Literal["permissive", "strict"]
CoordinationSurface = Literal["native", "print"]
OmpExecutionMode = Literal["external_host", "current_host"]
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
    execution_mode: OmpExecutionMode = "external_host"

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
        execution_mode_value = os.environ.get(
            "AWF_OMP_EXECUTION_MODE", "external_host"
        ).strip().lower()
        execution_mode: OmpExecutionMode = (
            "current_host"
            if execution_mode_value in {"current_host", "same_host"}
            else "external_host"
        )
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
            execution_mode=execution_mode,
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

class OmpCurrentHostBridge(Protocol):
    """Injected access to the task/hub tools of the already-running OMP host."""

    def __call__(
        self,
        *,
        prompt: str,
        workers: Sequence["OmpWorkerTask"],
        cwd: str | None,
        config: OmpRunnerConfig,
        model: str | None,
        timeout_sec: int,
        agent_model_overrides: Mapping[str, str],
    ) -> OmpExecutionResult: ...


@dataclass(frozen=True)
class OmpWorkerTask:
    """One exact worker descriptor passed to the native OMP coordinator."""

    name: str
    role: str
    prompt: str
    agent_type: str
    model: str | None = None
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
    """Serialize one exact native ``task`` batch and bounded steering contract."""
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
    wait_limit = max(1, min(capacity, 4))
    return f"""You are the OMP host coordinator for one AWF worker batch.
Call `task` exactly once using the native tool. Its `tasks` array MUST preserve the order and exact `name`, `agent`, `task`, `outputSchema`, and `schemaMode` values in COORDINATOR_INPUT. Include `isolated` only when it is not null. Do not rename tasks, invent task IDs, replace agent types, split the batch, or use another execution backend.
The configured capacity is {capacity}; this input has {len(workers)} workers and has already been capacity-checked.
Run a bounded, event-driven coordination loop after that one task call:
1. Track unsettled workers from native async task delivery. Only when delivery has not supplied the next event may you call `hub wait`; call it at most {wait_limit} times total. Never busy-poll `hub jobs`, sleep, or repeatedly snapshot status.
2. After each completed delivery, inspect that worker's exact output before waiting again.
3. If a concrete finding from a completed worker corrects or unblocks a still-running sibling, you may send at most one `hub send` message in the entire batch, only to that relevant sibling. Never message a completed worker.
4. Never call `task` again, start a successor, spawn a replacement, or invent identity. If the wait bound ends with unsettled workers, report them failed explicitly.
Respond with JSON only: {{"awf_omp_batch":1,"workers":[{{"name":"<stable input name>","status":"completed","result":<exact yielded data>}},{{"name":"<stable input name>","status":"failed","error":"<explicit error>"}}],"steering":{{"wait_calls":<0..{wait_limit}>,"inspected_completed":["<stable input name>"],"message":null|{{"target":"<stable input name>","kind":"corrective"|"blocker"}}}}}}
Return exactly one entry for each name, in any completion order: {names}. Never put task IDs, prompts, result excerpts, or message content in `steering`; AWF obtains authentic IDs from native task events.
COORDINATOR_INPUT={payload}
"""


def _build_omp_recovery_prompt(
    workers: Sequence[OmpWorkerTask],
    persisted_workers: Sequence[dict[str, Any]],
    *,
    capacity: int,
) -> str:
    """Resume existing native tasks without replaying their assignments."""
    identities = [
        {
            "name": worker.name,
            "task_id": persisted["task_id"],
            "agent_uri": persisted["agent_uri"],
            "history_uri": persisted["history_uri"],
        }
        for worker, persisted in zip(workers, persisted_workers, strict=True)
    ]
    payload = json.dumps(identities, ensure_ascii=False, separators=(",", ":"))
    names = json.dumps([worker.name for worker in workers], separators=(",", ":"))
    wait_limit = max(1, min(capacity, 4))
    return f"""Resume the exact interrupted AWF OMP batch in this persisted coordinator session.
The original native `task` batch already ran. Calling `task` is prohibited. Do not spawn replacements, successors, or any new agent, even when a handle is missing or unavailable.
Use only native async delivery, `hub wait`, `hub send`, and the exact persisted agent/history handles in RECOVERY_IDENTITIES. Wait is event-driven and bounded: call `hub wait` at most {wait_limit} times total; never busy-poll `hub jobs`, sleep, or repeatedly snapshot status.
Inspect each newly completed output. You may send at most one corrective or blocker `hub send` message in the entire resumed batch, and only to a relevant still-running persisted sibling. Never message a completed worker.
Respond with JSON only: {{"awf_omp_batch":1,"workers":[{{"name":"<stable input name>","status":"completed","result":<exact yielded data>}},{{"name":"<stable input name>","status":"failed","error":"<explicit error>"}}],"steering":{{"wait_calls":<0..{wait_limit}>,"inspected_completed":["<stable input name>"],"message":null|{{"target":"<stable input name>","kind":"corrective"|"blocker"}}}}}}
Return exactly one entry for each persisted name, in any completion order: {names}. Never include prompts, result excerpts, or message content in `steering`.
RECOVERY_IDENTITIES={payload}
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


def _authentic_uri(
    candidate: dict[str, Any],
    *,
    snake_name: str,
    camel_name: str,
    scheme: str,
) -> str | None:
    value = candidate.get(camel_name) or candidate.get(snake_name)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value.startswith(f"{scheme}://") else None


def _native_task_batch_calls(text: str) -> int:
    call_ids: set[str] = set()
    for index, event in enumerate(_json_events(text)):
        if (
            event.get("toolName") == "task"
            and event.get("type")
            in {"tool_execution_update", "tool_execution_end"}
        ):
            call_ids.add(str(event.get("toolCallId") or f"event-{index}"))
    return len(call_ids)


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
                "agent_uri": _authentic_uri(
                    candidate,
                    snake_name="agent_uri",
                    camel_name="agentUri",
                    scheme="agent",
                ),
                "history_uri": _authentic_uri(
                    candidate,
                    snake_name="history_uri",
                    camel_name="historyUri",
                    scheme="history",
                ),
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
        "session_persisted": bool(session_persisted and session_id),
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
        "task_batch_calls": _native_task_batch_calls(text),
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
    if cfg.execution_mode == "current_host":
        return OmpExecutionResult(
            returncode=2,
            stdout="",
            stderr=(
                "omp_same_host_capability_mismatch: print execution cannot use "
                "an external subprocess in execution_mode=current_host"
            ),
            elapsed_sec=0.0,
            metadata={
                "backend": "omp",
                "coordination_surface": "print",
                "execution_mode": "current_host",
            },
        )
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
                "execution_mode": "external_host",
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
                "execution_mode": "external_host",
            },
        )

    output, metadata, input_tokens, output_tokens = parse_omp_json_stream(
        completed.stdout,
        session_persisted=not cfg.no_session,
    )
    metadata["command"] = cfg.command
    metadata["requested_model"] = model or cfg.model
    metadata["coordination_surface"] = "print"
    metadata["execution_mode"] = "external_host"
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
    agent_model_overrides: dict[str, str] | None = None,
    resume_session_id: str | None = None,
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
        if resume_session_id is not None:
            command[-2:-2] = ["-r", resume_session_id]
        settings: dict[str, Any] = {}
        task_settings: dict[str, Any] = {}
        if agent_model_overrides:
            task_settings["agentModelOverrides"] = dict(
                sorted(agent_model_overrides.items())
            )
        if isolate_tasks:
            task_settings["isolation"] = {
                "mode": "auto",
                "apply": False,
                "merge": "patch",
            }
        if task_settings:
            settings["task"] = task_settings
            with tempfile.NamedTemporaryFile(
                prefix="awf-omp-settings-",
                suffix=".json",
                mode="w",
                encoding="utf-8",
                delete=False,
            ) as settings_handle:
                json.dump(settings, settings_handle)
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
                "execution_mode": "external_host",
                "resumed_session_id": resume_session_id,
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
        requested_worker_models=dict(sorted((agent_model_overrides or {}).items())),
        coordination_surface="native",
        execution_mode="external_host",
        resumed_session_id=resume_session_id,
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

def parse_omp_steering_evidence(
    text: str,
    *,
    worker_names: Sequence[str] = (),
) -> dict[str, Any]:
    """Return only bounded, non-content steering provenance from an envelope."""
    value = _parse_json_value(text)
    steering = value.get("steering") if isinstance(value, dict) else None
    if not isinstance(steering, dict):
        return {
            "reported": False,
            "wait_calls": 0,
            "inspected_completed": [],
            "message_sent": False,
            "message_target": None,
            "message_kind": None,
        }
    allowed = set(worker_names)
    raw_inspected = steering.get("inspected_completed")
    inspected: list[str] = []
    if isinstance(raw_inspected, list):
        for name in raw_inspected:
            if (
                isinstance(name, str)
                and name in allowed
                and name not in inspected
            ):
                inspected.append(name)
    raw_wait_calls = steering.get("wait_calls")
    wait_calls = (
        raw_wait_calls
        if isinstance(raw_wait_calls, int)
        and not isinstance(raw_wait_calls, bool)
        and raw_wait_calls >= 0
        else 0
    )
    message = steering.get("message")
    message_target: str | None = None
    message_kind: str | None = None
    if isinstance(message, dict):
        target = message.get("target")
        kind = message.get("kind")
        if (
            isinstance(target, str)
            and target in allowed
            and kind in {"corrective", "blocker"}
        ):
            message_target = target
            message_kind = str(kind)
    return {
        "reported": True,
        "wait_calls": wait_calls,
        "inspected_completed": inspected,
        "message_sent": message_target is not None,
        "message_target": message_target,
        "message_kind": message_kind,
    }


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


_CHECKPOINT_VERSION = 1
_TERMINAL_TASK_STATUSES = {
    "completed",
    "success",
    "ok",
    "failed",
    "error",
    "cancelled",
    "canceled",
}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _worker_descriptor_hash(worker: OmpWorkerTask) -> str:
    return _sha256_json(
        {
            "name": worker.name,
            "role": worker.role,
            "agent_type": worker.agent_type,
            "model": worker.model,
            "prompt_sha256": _sha256_bytes(worker.prompt.encode("utf-8")),
            "output_schema_sha256": _sha256_json(worker.output_schema),
            "schema_mode": worker.schema_mode,
            "isolated": worker.isolated,
            "require_json": worker.require_json,
        }
    )


def _native_batch_identity(
    workers: Sequence[OmpWorkerTask],
    config: OmpRunnerConfig,
    model: str | None,
) -> tuple[str, list[str]]:
    descriptor_hashes = [_worker_descriptor_hash(worker) for worker in workers]
    fingerprint = _sha256_json(
        {
            "version": _CHECKPOINT_VERSION,
            "descriptor_hashes": descriptor_hashes,
            "command_sha256": _sha256_bytes(config.command.encode("utf-8")),
            "extra_args_sha256": _sha256_json(config.extra_args),
            "model_sha256": _sha256_bytes(
                (model or config.model or "").encode("utf-8")
            ),
            "session_persistence_requested": not config.no_session,
            "execution_mode": config.execution_mode,
        }
    )
    return fingerprint, descriptor_hashes


def _native_checkpoint_path(cwd: str | None, fingerprint: str) -> Path:
    root = Path(cwd).resolve() if cwd is not None else Path.cwd().resolve()
    return (
        root
        / ".workflow"
        / "artifacts"
        / "dispatch"
        / f"omp-native-{fingerprint}.json"
    )


def _atomic_write_checkpoint(path: Path, checkpoint: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.",
            suffix=".tmp",
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as handle:
            json.dump(
                checkpoint,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _load_checkpoint(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except (OSError, json.JSONDecodeError):
        return None, "omp_checkpoint_identity_ambiguous: unreadable checkpoint"
    if not isinstance(value, dict):
        return None, "omp_checkpoint_identity_ambiguous: checkpoint is not an object"
    return value, None


def _checkpoint_worker_rows(
    workers: Sequence[OmpWorkerTask],
    descriptor_hashes: Sequence[str],
    progress: Sequence[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    progress_list = list(progress)
    for index, (worker, descriptor_hash) in enumerate(
        zip(workers, descriptor_hashes, strict=True)
    ):
        record = _task_record(progress_list, index, worker.name)
        task_id = str(record.get("task_id") or "").strip() if record else ""
        agent_uri = (
            str(record.get("agent_uri") or "").strip() if record else ""
        )
        history_uri = (
            str(record.get("history_uri") or "").strip() if record else ""
        )
        if task_id:
            agent_uri = agent_uri or f"agent://{task_id}"
            history_uri = history_uri or f"history://{task_id}"
        rows.append(
            {
                "index": index,
                "name": worker.name,
                "descriptor_sha256": descriptor_hash,
                "task_id": task_id or None,
                "agent_uri": agent_uri or None,
                "history_uri": history_uri or None,
                "status": (
                    str(record.get("status") or "").lower() or None
                    if record
                    else None
                ),
            }
        )
    return rows


def _prepared_checkpoint(
    workers: Sequence[OmpWorkerTask],
    descriptor_hashes: Sequence[str],
    fingerprint: str,
    config: OmpRunnerConfig,
    previous: dict[str, Any] | None,
    recovery_rows: Sequence[dict[str, Any]] | None,
    recovery_session_id: str | None,
) -> dict[str, Any]:
    prior_attempt = previous.get("attempt") if isinstance(previous, dict) else 0
    attempt = (
        prior_attempt + 1
        if isinstance(prior_attempt, int) and not isinstance(prior_attempt, bool)
        else 1
    )
    rows = (
        [dict(row) for row in recovery_rows]
        if recovery_rows is not None
        else _checkpoint_worker_rows(workers, descriptor_hashes)
    )
    return {
        "version": _CHECKPOINT_VERSION,
        "kind": "omp_native_batch",
        "batch_fingerprint": fingerprint,
        "descriptor_hashes": list(descriptor_hashes),
        "worker_names": [worker.name for worker in workers],
        "state": "resuming" if recovery_rows is not None else "prepared",
        "attempt": attempt,
        "session_persistence_requested": not config.no_session,
        "session_persisted": recovery_session_id is not None,
        "resumable": recovery_session_id is not None,
        "coordinator_session_id": recovery_session_id,
        "workers": rows,
        "steering_evidence": None,
    }


def _recovery_identity(
    checkpoint: dict[str, Any],
    workers: Sequence[OmpWorkerTask],
    descriptor_hashes: Sequence[str],
    fingerprint: str,
) -> tuple[str | None, list[dict[str, Any]] | None, str | None]:
    if checkpoint.get("version") != _CHECKPOINT_VERSION:
        return None, None, "omp_checkpoint_identity_ambiguous: unsupported version"
    if checkpoint.get("batch_fingerprint") != fingerprint:
        return None, None, "omp_checkpoint_identity_ambiguous: fingerprint mismatch"
    if checkpoint.get("descriptor_hashes") != list(descriptor_hashes):
        return None, None, "omp_checkpoint_identity_ambiguous: descriptor mismatch"
    if checkpoint.get("state") not in {"interrupted", "resuming"}:
        return None, None, "omp_checkpoint_identity_ambiguous: interrupted state missing"
    if (
        checkpoint.get("session_persistence_requested") is not True
        or checkpoint.get("session_persisted") is not True
        or checkpoint.get("resumable") is not True
    ):
        return None, None, "omp_checkpoint_handles_missing: session is not resumable"
    session_id = checkpoint.get("coordinator_session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None, None, "omp_checkpoint_handles_missing: coordinator session"
    raw_rows = checkpoint.get("workers")
    if not isinstance(raw_rows, list) or len(raw_rows) != len(workers):
        return None, None, "omp_checkpoint_handles_missing: worker identities"
    rows: list[dict[str, Any]] = []
    task_ids: set[str] = set()
    for index, (worker, descriptor_hash, raw_row) in enumerate(
        zip(workers, descriptor_hashes, raw_rows, strict=True)
    ):
        if not isinstance(raw_row, dict):
            return None, None, "omp_checkpoint_handles_missing: worker identity"
        task_id = raw_row.get("task_id")
        agent_uri = raw_row.get("agent_uri")
        history_uri = raw_row.get("history_uri")
        if (
            raw_row.get("index") != index
            or raw_row.get("name") != worker.name
            or raw_row.get("descriptor_sha256") != descriptor_hash
        ):
            return None, None, "omp_checkpoint_identity_ambiguous: worker mapping"
        if (
            not isinstance(task_id, str)
            or not task_id
            or task_id in task_ids
            or not isinstance(agent_uri, str)
            or not agent_uri.startswith("agent://")
            or not isinstance(history_uri, str)
            or not history_uri.startswith("history://")
        ):
            return None, None, "omp_checkpoint_handles_missing: authentic worker handles"
        task_ids.add(task_id)
        rows.append(dict(raw_row))
    return session_id.strip(), rows, None


def _merge_recovery_progress(
    current: Sequence[dict[str, Any]],
    persisted_rows: Sequence[dict[str, Any]],
    workers: Sequence[OmpWorkerTask],
) -> tuple[list[dict[str, Any]], str | None]:
    current_list = list(current)
    merged: list[dict[str, Any]] = []
    for index, (worker, persisted) in enumerate(
        zip(workers, persisted_rows, strict=True)
    ):
        live = _task_record(current_list, index, worker.name)
        persisted_task_id = str(persisted["task_id"])
        if live is not None:
            live_task_id = str(live.get("task_id") or "")
            if live_task_id and live_task_id != persisted_task_id:
                return [], (
                    "omp_checkpoint_identity_ambiguous: resumed task ID changed "
                    f"for {worker.name}"
                )
        merged_record = {
            "index": index,
            "task_id": persisted_task_id,
            "task_name": worker.name,
            "agent_uri": persisted["agent_uri"],
            "history_uri": persisted["history_uri"],
            "status": persisted.get("status"),
        }
        if live is not None:
            merged_record.update(
                {
                    key: value
                    for key, value in live.items()
                    if value is not None and key not in {"task_id", "index"}
                }
            )
        merged.append(merged_record)
    return merged, None


def _with_execution_metadata(
    execution: OmpExecutionResult,
    **updates: Any,
) -> OmpExecutionResult:
    metadata = dict(execution.metadata)
    metadata.update(updates)
    return OmpExecutionResult(
        returncode=execution.returncode,
        stdout=execution.stdout,
        stderr=execution.stderr,
        elapsed_sec=execution.elapsed_sec,
        timed_out=execution.timed_out,
        metadata=metadata,
        input_tokens=execution.input_tokens,
        output_tokens=execution.output_tokens,
    )


def _finalize_checkpoint(
    checkpoint: dict[str, Any],
    workers: Sequence[OmpWorkerTask],
    descriptor_hashes: Sequence[str],
    execution: OmpExecutionResult,
    indexed: dict[str, dict[str, Any]] | None,
    envelope_error: str | None,
    steering_evidence: dict[str, Any],
    duplicate_task_error: str | None,
) -> dict[str, Any]:
    progress = execution.metadata.get("task_progress")
    progress_list = progress if isinstance(progress, list) else []
    rows = _checkpoint_worker_rows(workers, descriptor_hashes, progress_list)
    coordinator_session_id = (
        execution.metadata.get("session_id")
        or checkpoint.get("coordinator_session_id")
    )
    session_persisted = bool(
        checkpoint.get("session_persistence_requested")
        and isinstance(coordinator_session_id, str)
        and coordinator_session_id
    )
    all_terminal = all(
        row.get("task_id")
        and row.get("status") in _TERMINAL_TASK_STATUSES
        for row in rows
    )
    complete_envelope = (
        indexed is not None
        and envelope_error is None
        and all(worker.name in indexed for worker in workers)
    )
    handles_complete = all(
        row.get("task_id")
        and row.get("agent_uri")
        and row.get("history_uri")
        for row in rows
    )
    if duplicate_task_error:
        state = "ambiguous"
    elif all_terminal and complete_envelope:
        state = "completed"
    elif session_persisted and handles_complete:
        state = "interrupted"
    elif checkpoint.get("session_persistence_requested"):
        state = "ambiguous"
    else:
        state = "interrupted"
    finalized = dict(checkpoint)
    finalized.update(
        state=state,
        session_persisted=session_persisted,
        resumable=bool(
            state == "interrupted" and session_persisted and handles_complete
        ),
        coordinator_session_id=(
            coordinator_session_id if session_persisted else None
        ),
        workers=rows,
        steering_evidence=steering_evidence,
    )
    return finalized


def _native_failures(
    workers: Sequence[OmpWorkerTask],
    message: str,
    *,
    execution_mode: OmpExecutionMode,
    checkpoint_path: Path | None = None,
) -> list[AgentResult]:
    return [
        AgentResult(
            provider_name="omp",
            role=worker.role,
            stdout="",
            stderr=message,
            returncode=2,
            elapsed_sec=0.0,
            parse_error=True,
            metadata={
                "backend": "omp",
                "coordination_surface": "native",
                "execution_mode": execution_mode,
                "task_id": None,
                "agent_uri": None,
                "history_uri": None,
                "checkpoint_path": (
                    str(checkpoint_path) if checkpoint_path is not None else None
                ),
                "schema_validation": {
                    "mode": worker.schema_mode,
                    "valid": False,
                    "errors": [message],
                },
            },
        )
        for worker in workers
    ]

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
    worker: OmpWorkerTask,
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
        requested_worker_model=worker.model,
        task_id=task_id,
        agent_name=record.get("agent_name") if record else None,
        task_status=record.get("status") if record else None,
        patch_path=record.get("patch_path") if record else None,
        status=record.get("status") if record else None,
        agent_uri=(
            record.get("agent_uri") or f"agent://{task_id}"
            if task_id and record
            else None
        ),
        history_uri=(
            record.get("history_uri") or f"history://{task_id}"
            if task_id and record
            else None
        ),
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
    host_bridge: OmpCurrentHostBridge | None = None,
) -> list[AgentResult]:
    """Run one native OMP batch with fail-closed durable recovery."""
    if not workers:
        return []
    cfg = config or OmpRunnerConfig.from_env()
    if len(workers) > cfg.capacity:
        return _capacity_failures(workers, cfg.capacity)
    agent_model_overrides: dict[str, str] = {}
    for worker in workers:
        if worker.model is None:
            continue
        previous_model = agent_model_overrides.get(worker.agent_type)
        if previous_model is not None and previous_model != worker.model:
            return _native_failures(
                workers,
                (
                    "omp_worker_model_conflict: native task settings cannot route "
                    f"agent {worker.agent_type!r} to both {previous_model!r} "
                    f"and {worker.model!r}"
                ),
                execution_mode=cfg.execution_mode,
            )
        agent_model_overrides[worker.agent_type] = worker.model
    effective_timeout = timeout_sec if timeout_sec is not None else cfg.timeout_sec
    prompt = build_omp_coordinator_prompt(workers, capacity=cfg.capacity)
    checkpoint_path: Path | None = None
    checkpoint: dict[str, Any] | None = None
    descriptor_hashes: list[str] = []
    recovery_rows: list[dict[str, Any]] | None = None
    recovery_session_id: str | None = None

    if cfg.execution_mode == "current_host":
        if host_bridge is None:
            return _native_failures(
                workers,
                (
                    "omp_same_host_capability_mismatch: execution_mode=current_host "
                    "requires an injected current OMP task/hub bridge"
                ),
                execution_mode=cfg.execution_mode,
            )
        try:
            execution = host_bridge(
                prompt=prompt,
                workers=workers,
                cwd=cwd,
                config=cfg,
                model=model,
                timeout_sec=effective_timeout,
                agent_model_overrides=MappingProxyType(
                    dict(sorted(agent_model_overrides.items()))
                ),
            )
        except Exception as exc:
            return _native_failures(
                workers,
                f"omp_same_host_bridge_failed: {type(exc).__name__}: {exc}",
                execution_mode=cfg.execution_mode,
            )
        if not isinstance(execution, OmpExecutionResult):
            return _native_failures(
                workers,
                "omp_same_host_bridge_invalid: bridge returned no OmpExecutionResult",
                execution_mode=cfg.execution_mode,
            )
        execution = _with_execution_metadata(
            execution,
            execution_mode="current_host",
            coordination_surface="native",
            requested_worker_models=dict(sorted(agent_model_overrides.items())),
        )
    else:
        fingerprint, descriptor_hashes = _native_batch_identity(
            workers,
            cfg,
            model,
        )
        checkpoint_path = _native_checkpoint_path(cwd, fingerprint)
        previous, checkpoint_error = _load_checkpoint(checkpoint_path)
        if checkpoint_error is not None and not cfg.no_session:
            return _native_failures(
                workers,
                checkpoint_error,
                execution_mode=cfg.execution_mode,
                checkpoint_path=checkpoint_path,
            )
        if (
            previous is not None
            and not cfg.no_session
            and previous.get("state") != "completed"
        ):
            (
                recovery_session_id,
                recovery_rows,
                checkpoint_error,
            ) = _recovery_identity(
                previous,
                workers,
                descriptor_hashes,
                fingerprint,
            )
            if checkpoint_error is not None:
                return _native_failures(
                    workers,
                    checkpoint_error,
                    execution_mode=cfg.execution_mode,
                    checkpoint_path=checkpoint_path,
                )
        checkpoint = _prepared_checkpoint(
            workers,
            descriptor_hashes,
            fingerprint,
            cfg,
            previous,
            recovery_rows,
            recovery_session_id,
        )
        try:
            _atomic_write_checkpoint(checkpoint_path, checkpoint)
        except OSError as exc:
            return _native_failures(
                workers,
                f"omp_checkpoint_write_failed: {type(exc).__name__}: {exc}",
                execution_mode=cfg.execution_mode,
                checkpoint_path=checkpoint_path,
            )
        if recovery_rows is not None:
            prompt = _build_omp_recovery_prompt(
                workers,
                recovery_rows,
                capacity=cfg.capacity,
            )
        agent_model_overrides = dict(sorted(agent_model_overrides.items()))
        execution = _run_omp_native_host(
            prompt,
            cwd=cwd,
            config=cfg,
            model=model,
            timeout_sec=effective_timeout,
            isolate_tasks=any(worker.isolated is True for worker in workers),
            agent_model_overrides=agent_model_overrides,
            resume_session_id=recovery_session_id,
        )

    progress = execution.metadata.get("task_progress", [])
    if not isinstance(progress, list):
        progress = []
    batch_error: str | None = None
    if recovery_rows is not None:
        progress, batch_error = _merge_recovery_progress(
            progress,
            recovery_rows,
            workers,
        )
        execution = _with_execution_metadata(
            execution,
            task_progress=progress,
            session_id=execution.metadata.get("session_id")
            or recovery_session_id,
            session_persisted=True,
            recovery_resumed=True,
        )
    else:
        execution = _with_execution_metadata(execution, recovery_resumed=False)

    raw_task_batch_calls = execution.metadata.get("task_batch_calls")
    task_batch_calls = (
        raw_task_batch_calls
        if isinstance(raw_task_batch_calls, int)
        and not isinstance(raw_task_batch_calls, bool)
        and raw_task_batch_calls >= 0
        else 0
    )
    duplicate_task_error: str | None = None
    if recovery_rows is not None and task_batch_calls:
        duplicate_task_error = (
            "omp_recovery_duplicate_task_batch: resumed coordinator called task"
        )
    elif recovery_rows is None and task_batch_calls > 1:
        duplicate_task_error = (
            "omp_native_duplicate_task_batch: coordinator called task more than once"
        )
    batch_error = batch_error or duplicate_task_error

    indexed, envelope_error = parse_omp_native_envelope(execution.stdout)
    steering_evidence = parse_omp_steering_evidence(
        execution.stdout,
        worker_names=[worker.name for worker in workers],
    )
    if checkpoint is not None and checkpoint_path is not None:
        finalized = _finalize_checkpoint(
            checkpoint,
            workers,
            descriptor_hashes,
            execution,
            indexed,
            envelope_error,
            steering_evidence,
            duplicate_task_error,
        )
        try:
            _atomic_write_checkpoint(checkpoint_path, finalized)
        except OSError as exc:
            batch_error = batch_error or (
                f"omp_checkpoint_finalize_failed: {type(exc).__name__}: {exc}"
            )
        execution = _with_execution_metadata(
            execution,
            checkpoint_path=str(checkpoint_path),
            checkpoint_state=finalized["state"],
            batch_fingerprint=finalized["batch_fingerprint"],
            steering_evidence=steering_evidence,
            native_batch_error=batch_error,
        )
    else:
        execution = _with_execution_metadata(
            execution,
            steering_evidence=steering_evidence,
            native_batch_error=batch_error,
        )

    progress = execution.metadata.get("task_progress", [])
    if not isinstance(progress, list):
        progress = []
    provider = str(execution.metadata.get("provider") or "")
    provider_name = f"omp:{provider}" if provider else "omp"
    completed: list[AgentResult] = []
    for index, worker in enumerate(workers):
        record = _task_record(progress, index, worker.name)
        metadata = _worker_metadata(execution, record, worker)
        item = indexed.get(worker.name) if indexed is not None else None
        if item is None:
            reason = batch_error or envelope_error or (
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
        host_failure = batch_error or (
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
