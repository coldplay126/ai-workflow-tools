from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Optional

from awf.providers.base import ProviderCapability, ProviderResult


@dataclass
class SpawnSpec:
    """Provider-owned subprocess invocation and its input/output transport."""

    argv: list[str]
    stdin: str | None = None
    output_path: str | None = None

    def captured_output_or(self, stdout: str) -> str:
        if not self.output_path:
            return stdout
        try:
            with open(self.output_path, "r", encoding="utf-8") as saved:
                captured = saved.read()
        except FileNotFoundError:
            return stdout
        return captured if captured.strip() else stdout

    def cleanup(self) -> None:
        if not self.output_path:
            return
        try:
            os.unlink(self.output_path)
        except FileNotFoundError:
            pass


class SubprocessProvider:
    name = "subprocess"
    capabilities = {ProviderCapability.COMPLETE}

    def __init__(self, command: str, flags: Optional[list[str]] = None) -> None:
        self.command = command
        self.flags = flags or []

    def complete(
        self,
        prompt: str,
        *,
        cwd: str | None = None,
        add_dirs: list[str] | None = None,
        timeout_sec: int | None = None,
    ) -> ProviderResult:
        cmd = [self.command, *self.flags, prompt]
        try:
            completed = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired:
            return ProviderResult(
                returncode=124,
                stdout="",
                stderr=f"provider_timeout: subprocess provider timed out after {timeout_sec} seconds",
            )
        return ProviderResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
