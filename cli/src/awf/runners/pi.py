from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field

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
