from __future__ import annotations

import os
import shlex
import subprocess
import time
from typing import Optional

from awf.providers.base import ProviderCapability, ProviderResult
from awf.providers.subprocess_provider import SubprocessProvider


_AUTO_MODEL_VALUES = {"", "auto", "gemini-auto", "gemini"}


class GeminiProvider(SubprocessProvider):
    name = "gemini"
    capabilities = {
        ProviderCapability.COMPLETE,
        ProviderCapability.TOOL_LOOP,
        ProviderCapability.ADD_DIR,
    }

    def __init__(
        self,
        command: Optional[str] = None,
        flags: Optional[list[str]] = None,
        model: Optional[str] = None,
    ) -> None:
        resolved_command = command or os.environ.get("AWF_GEMINI_COMMAND", "gemini")
        if flags is not None:
            resolved_flags = flags
        else:
            env_flags = os.environ.get("AWF_GEMINI_FLAGS")
            resolved_flags = shlex.split(env_flags) if env_flags else ["--output-format", "text"]
        super().__init__(command=resolved_command, flags=resolved_flags)
        self.timeout_sec = int(os.environ.get("AWF_GEMINI_TIMEOUT_SEC", "900"))
        self.model = model if model is not None else os.environ.get("AWF_GEMINI_MODEL")

    def set_permission_mode(self, mode: str) -> None:
        """Set or replace Gemini CLI approval mode.

        awf passes Claude-style "bypassPermissions" for --yolo. Gemini CLI
        calls the equivalent approval mode "yolo".
        """
        normalized = "yolo" if mode == "bypassPermissions" else mode
        for index, flag in enumerate(self.flags):
            if flag == "--approval-mode" and index + 1 < len(self.flags):
                self.flags[index + 1] = normalized
                return
            if flag.startswith("--approval-mode="):
                self.flags[index] = f"--approval-mode={normalized}"
                return
        self.flags.extend(["--approval-mode", normalized])

    def complete(
        self,
        prompt: str,
        *,
        cwd: str | None = None,
        add_dirs: list[str] | None = None,
        timeout_sec: int | None = None,
    ) -> ProviderResult:
        effective_timeout = timeout_sec if timeout_sec is not None else self.timeout_sec
        cmd = [self.command, *self.flags]
        model = (self.model or "").strip()
        if model.lower() not in _AUTO_MODEL_VALUES:
            cmd.extend(["--model", model])
        for directory in add_dirs or []:
            cmd.extend(["--include-directories", directory])
        # Keep the actual prompt on stdin to avoid OS argument length limits.
        # Gemini CLI appends stdin to the --prompt value in headless mode.
        cmd.extend(["--prompt", ""])

        started = time.monotonic()
        try:
            completed = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                input=prompt,
                cwd=cwd,
                timeout=effective_timeout,
            )
        except FileNotFoundError:
            return ProviderResult(
                returncode=127,
                stdout="",
                stderr=f"gemini command not found: {self.command}",
                provider_name=self.name,
                model=model or None,
                elapsed_sec=time.monotonic() - started,
            )
        except subprocess.TimeoutExpired:
            return ProviderResult(
                returncode=124,
                stdout="",
                stderr=f"provider_timeout: gemini timed out after {effective_timeout}s",
                provider_name=self.name,
                model=model or None,
                elapsed_sec=time.monotonic() - started,
            )
        return ProviderResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            provider_name=self.name,
            model=model or None,
            elapsed_sec=time.monotonic() - started,
        )
