from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from awf.core.agent_runner import AgentResult, _try_parse_json


@dataclass(frozen=True)
class OmpRunnerConfig:
    command: str = "omp"
    model: str | None = None
    extra_args: tuple[str, ...] = field(default_factory=tuple)
    timeout_sec: int = 300
    no_session: bool = True

    @classmethod
    def from_env(cls) -> "OmpRunnerConfig":
        command = os.environ.get("AWF_OMP_COMMAND", "omp").strip() or "omp"
        model = os.environ.get("AWF_OMP_MODEL", "").strip() or None
        extra_args = tuple(shlex.split(os.environ.get("AWF_OMP_EXTRA_ARGS", "")))
        timeout_raw = os.environ.get("AWF_OMP_TIMEOUT_SEC", "300").strip()
        try:
            timeout_sec = int(timeout_raw)
        except ValueError:
            timeout_sec = 300
        no_session = os.environ.get("AWF_OMP_NO_SESSION", "1").strip().lower() not in {
            "0",
            "false",
            "no",
        }
        return cls(
            command=command,
            model=model,
            extra_args=extra_args,
            timeout_sec=timeout_sec,
            no_session=no_session,
        )


@dataclass(frozen=True)
class OmpExecutionResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed_sec: float
    metadata: dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0


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


def parse_omp_json_stream(text: str, *, session_persisted: bool) -> tuple[str, dict[str, Any], int, int]:
    """Extract the final assistant response and provenance from OMP NDJSON."""
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)

    session_id: str | None = None
    final_message: dict[str, Any] | None = None
    for event in events:
        if event.get("type") == "session" and event.get("id"):
            session_id = str(event["id"])
        message = event.get("message")
        if _message_text(message):
            final_message = message
        if event.get("type") == "agent_end":
            messages = event.get("messages", [])
            if isinstance(messages, list):
                for candidate in messages:
                    if _message_text(candidate):
                        final_message = candidate

    output = _message_text(final_message)
    usage = final_message.get("usage", {}) if isinstance(final_message, dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = int(usage.get("input") or 0)
    output_tokens = int(usage.get("output") or 0)
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
            "cache_read_tokens": int(usage.get("cacheRead") or 0),
            "cache_write_tokens": int(usage.get("cacheWrite") or 0),
            "total_tokens": int(usage.get("totalTokens") or input_tokens + output_tokens),
            "cost": usage.get("cost") if isinstance(usage.get("cost"), dict) else {},
        },
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
            metadata={"backend": "omp", "command": cfg.command},
        )
    except subprocess.TimeoutExpired:
        return OmpExecutionResult(
            returncode=124,
            stdout="",
            stderr=f"runner_timeout: omp timed out after {effective_timeout}s",
            elapsed_sec=time.monotonic() - started,
            metadata={"backend": "omp", "command": cfg.command},
        )

    output, metadata, input_tokens, output_tokens = parse_omp_json_stream(
        completed.stdout,
        session_persisted=not cfg.no_session,
    )
    metadata["command"] = cfg.command
    metadata["requested_model"] = model or cfg.model
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
