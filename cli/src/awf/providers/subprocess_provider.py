from __future__ import annotations

import subprocess
from typing import Optional

from awf.providers.base import ProviderCapability, ProviderResult


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
