from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from typing import Optional

from awf.providers.base import ProviderCapability, ProviderResult
from awf.providers.subprocess_provider import SpawnSpec, SubprocessProvider


class CodexProvider(SubprocessProvider):
    name = "codex"
    capabilities = {ProviderCapability.COMPLETE, ProviderCapability.ADD_DIR}

    def __init__(
        self,
        command: Optional[str] = None,
        flags: Optional[list[str]] = None,
        reasoning_effort: Optional[str] = None,
        output_schema_path: Optional[str] = None,
    ) -> None:
        resolved_command = command or os.environ.get("AWF_CODEX_COMMAND", "codex")
        if flags is not None:
            resolved_flags = flags
        else:
            env_flags = os.environ.get("AWF_CODEX_FLAGS")
            resolved_flags = shlex.split(env_flags) if env_flags else ["exec", "--sandbox", "workspace-write"]
        super().__init__(command=resolved_command, flags=resolved_flags)
        self.timeout_sec = int(os.environ.get("AWF_CODEX_TIMEOUT_SEC", "300"))
        self.reasoning_effort = reasoning_effort
        self.output_schema_path = output_schema_path or os.environ.get("AWF_CODEX_OUTPUT_SCHEMA")

    def set_sandbox(self, sandbox: str) -> None:
        """Set or replace the codex exec sandbox flag."""
        for index, flag in enumerate(self.flags):
            if flag in {"--sandbox", "-s"} and index + 1 < len(self.flags):
                self.flags[index + 1] = sandbox
                return
            if flag.startswith("--sandbox="):
                self.flags[index] = f"--sandbox={sandbox}"
                return
        self.flags.extend(["--sandbox", sandbox])

    def build_spawn_spec(
        self,
        prompt: str,
        *,
        add_dirs: list[str] | None = None,
        stream_json: bool = False,
    ) -> SpawnSpec:
        """Build the shared Codex invocation used by complete and streaming runs."""
        with tempfile.NamedTemporaryFile(prefix="awf-codex-last-", suffix=".txt", delete=False) as handle:
            output_path = handle.name
        cmd = [self.command, *self.flags]
        if stream_json and "--json" not in cmd:
            cmd.append("--json")
        if self.reasoning_effort:
            cmd.extend(["-c", f"model_reasoning_effort={self.reasoning_effort}"])
        for directory in add_dirs or []:
            cmd.extend(["--add-dir", directory])
        if self.output_schema_path:
            cmd.extend(["--output-schema", self.output_schema_path])
        cmd.extend(["--output-last-message", output_path, "-"])
        return SpawnSpec(argv=cmd, stdin=prompt, output_path=output_path)

    def complete(
        self,
        prompt: str,
        *,
        cwd: str | None = None,
        add_dirs: list[str] | None = None,
        timeout_sec: int | None = None,
    ) -> ProviderResult:
        effective_timeout = timeout_sec if timeout_sec is not None else self.timeout_sec
        spawn_spec = self.build_spawn_spec(prompt, add_dirs=add_dirs)
        try:
            try:
                completed = subprocess.run(
                    spawn_spec.argv,
                    check=False,
                    capture_output=True,
                    text=True,
                    input=spawn_spec.stdin,
                    cwd=cwd,
                    timeout=effective_timeout,
                )
            except FileNotFoundError:
                return ProviderResult(
                    returncode=127,
                    stdout="",
                    stderr=f"codex command not found: {self.command}",
                )
            except subprocess.TimeoutExpired:
                return ProviderResult(
                    returncode=124,
                    stdout="",
                    stderr=f"provider_timeout: codex timed out after {effective_timeout}s",
                )
            return ProviderResult(
                returncode=completed.returncode,
                stdout=spawn_spec.captured_output_or(completed.stdout),
                stderr=completed.stderr,
            )
        finally:
            spawn_spec.cleanup()
