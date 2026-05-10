from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field

from awf.core.agent_runner import AgentResult, _try_parse_json
from awf.providers.base import ProviderResult


@dataclass(frozen=True)
class PiRunnerConfig:
    command: str = "pi"
    extra_args: tuple[str, ...] = field(default_factory=tuple)
    timeout_sec: int = 300
    no_session: bool = True
    skip_version_check: bool = True

    @classmethod
    def from_env(cls) -> "PiRunnerConfig":
        command = os.environ.get("AWF_PI_COMMAND", "pi").strip() or "pi"
        timeout_raw = os.environ.get("AWF_PI_TIMEOUT_SEC", "300").strip()
        try:
            timeout_sec = int(timeout_raw)
        except ValueError:
            timeout_sec = 300
        return cls(command=command, timeout_sec=timeout_sec)


def build_pi_print_command(prompt: str, config: PiRunnerConfig | None = None) -> list[str]:
    cfg = config or PiRunnerConfig.from_env()
    cmd = [cfg.command, *cfg.extra_args]
    if cfg.no_session:
        cmd.append("--no-session")
    cmd.extend(["-p", prompt])
    return cmd


def run_pi_print(
    prompt: str,
    *,
    cwd: str | None = None,
    config: PiRunnerConfig | None = None,
    timeout_sec: int | None = None,
) -> ProviderResult:
    """Run Pi in print mode and normalize the result for awf callers."""
    cfg = config or PiRunnerConfig.from_env()
    effective_timeout = timeout_sec if timeout_sec is not None else cfg.timeout_sec
    cmd = build_pi_print_command(prompt, cfg)
    env = os.environ.copy()
    if cfg.skip_version_check:
        env.setdefault("PI_SKIP_VERSION_CHECK", "1")

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
        return ProviderResult(
            returncode=127,
            stdout="",
            stderr=f"pi command not found: {cfg.command}",
            provider_name="pi",
            elapsed_sec=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired:
        return ProviderResult(
            returncode=124,
            stdout="",
            stderr=f"runner_timeout: pi timed out after {effective_timeout}s",
            provider_name="pi",
            elapsed_sec=time.monotonic() - started,
        )

    return ProviderResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        provider_name="pi",
        elapsed_sec=time.monotonic() - started,
    )


def pi_result_to_agent_result(
    result: ProviderResult,
    *,
    role: str,
    require_json: bool = False,
) -> AgentResult:
    """Convert a Pi runner result into awf's multi-agent result shape."""
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    parsed = None
    parse_error = False

    if stdout:
        parsed = _try_parse_json(stdout)
        if parsed is None and require_json:
            parse_error = True

    return AgentResult(
        provider_name=result.provider_name or "pi",
        role=role,
        stdout=stdout,
        stderr=stderr,
        returncode=result.returncode,
        elapsed_sec=result.elapsed_sec,
        timed_out=_is_timeout_result(result.returncode, stderr),
        parse_error=parse_error,
        parsed=parsed,
    )


def run_pi_agent(
    prompt: str,
    *,
    role: str,
    cwd: str | None = None,
    require_json: bool = False,
    config: PiRunnerConfig | None = None,
    timeout_sec: int | None = None,
) -> AgentResult:
    """Run Pi print mode for one worker and return an AgentResult."""
    result = run_pi_print(
        prompt,
        cwd=cwd,
        config=config,
        timeout_sec=timeout_sec,
    )
    return pi_result_to_agent_result(
        result,
        role=role,
        require_json=require_json,
    )


def _is_timeout_result(returncode: int, stderr: str) -> bool:
    return returncode == 124 and "timeout" in stderr.lower()
