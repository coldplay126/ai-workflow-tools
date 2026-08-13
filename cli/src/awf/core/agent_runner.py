"""Individual agent execution with timeout and result parsing."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class AgentResult:
    provider_name: str
    role: str  # plan_conformance, quality_validation, precision, speed, primary
    stdout: str
    stderr: str
    returncode: int
    elapsed_sec: float
    timed_out: bool = False
    parse_error: bool = False
    parsed: dict[str, Any] | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def conclusion(self) -> str:
        if not isinstance(self.parsed, dict):
            return ""
        return str(self.parsed.get("conclusion", ""))

    @property
    def findings(self) -> list[dict]:
        if not isinstance(self.parsed, dict):
            return []
        findings = self.parsed.get("findings", [])
        if not isinstance(findings, list):
            return []
        return [finding for finding in findings if isinstance(finding, dict)]

    @property
    def has_critical(self) -> bool:
        return any(
            str(f.get("severity", "")).upper() in {"CRITICAL", "HIGH"}
            for f in self.findings
        )

    @property
    def major_count(self) -> int:
        return sum(
            1 for f in self.findings
            if str(f.get("severity", "")).upper() in {"MAJOR", "MEDIUM"}
        )


@dataclass
class MultiAgentResult:
    mode: str
    agents: list[AgentResult] = field(default_factory=list)
    judge_verdict: str = ""  # PASS, FAIL, ESCALATE
    judge_reason: str = ""
    selected_agent: str = ""
    combined_output: str = ""
    fix_feedback: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.judge_verdict == "PASS"

    def usage_entries(self) -> list[dict[str, Any]]:
        """Return usage entries for summarize_usage()."""
        return [
            {
                "provider": a.provider_name,
                "role": a.role,
                "input_tokens": a.input_tokens,
                "output_tokens": a.output_tokens,
            }
            for a in self.agents
            if a.input_tokens or a.output_tokens
        ]


def run_agent(
    provider,
    prompt: str,
    role: str,
    cwd: str,
    *,
    timeout_sec: int = 90,
    require_json: bool = False,
    add_dirs: list[str] | None = None,
    on_progress=None,
) -> AgentResult:
    """Run a single agent with timeout.

    on_progress: optional callback(elapsed_sec, stderr_line) for real-time updates.
    """
    import sys

    started = time.monotonic()
    provider_name = getattr(provider, "name", "unknown")

    try:
        # Try streaming execution for providers that support it (SubprocessProvider with a command)
        provider_name = getattr(provider, "name", "unknown")
        supports_streaming = hasattr(provider, 'command') and hasattr(provider, 'flags')
        if on_progress and supports_streaming:
            result = _run_agent_streaming(provider, prompt, cwd, add_dirs, timeout_sec, on_progress)
        else:
            result = provider.complete(
                prompt,
                cwd=cwd,
                add_dirs=add_dirs,
                timeout_sec=timeout_sec,
            )

        elapsed = time.monotonic() - started
        timed_out = elapsed > timeout_sec or result.returncode == 124

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        # Extract token usage from provider result
        usage = getattr(result, "usage", None)
        input_tokens = getattr(usage, "input_tokens", 0) if usage else 0
        output_tokens = getattr(usage, "output_tokens", 0) if usage else 0

        agent_result = AgentResult(
            provider_name=provider_name,
            role=role,
            stdout=stdout,
            stderr=stderr,
            returncode=result.returncode,
            elapsed_sec=elapsed,
            timed_out=timed_out,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

        # Try to parse JSON from output
        if stdout:
            parsed = _try_parse_json(stdout)
            if parsed is not None:
                agent_result.parsed = parsed
            elif require_json:
                agent_result.parse_error = True

        return agent_result

    except Exception as exc:
        elapsed = time.monotonic() - started
        print(f"agent_error: {provider_name} ({role}): {exc}", file=sys.stderr)
        return AgentResult(
            provider_name=provider_name,
            role=role,
            stdout="",
            stderr=str(exc),
            returncode=2,
            elapsed_sec=elapsed,
            timed_out=False,
        )


def _run_agent_streaming(provider, prompt, cwd, add_dirs, timeout_sec, on_progress):
    """Run subprocess provider with real-time stderr streaming."""
    import os
    import selectors
    import subprocess

    from awf.providers.base import ProviderResult
    from awf.providers.subprocess_provider import SpawnSpec

    provider_name = getattr(provider, "name", "unknown")
    build_spawn_spec = getattr(provider, "build_spawn_spec", None)
    if callable(build_spawn_spec):
        spawn_spec = build_spawn_spec(prompt, add_dirs=add_dirs, stream_json=True)
    else:
        command = [provider.command, *provider.flags]
        for directory in add_dirs or []:
            command.extend(["--add-dir", directory])
        command.append(prompt)
        spawn_spec = SpawnSpec(argv=command)

    started = time.monotonic()
    try:
        process = subprocess.Popen(
            spawn_spec.argv,
            stdin=subprocess.PIPE if spawn_spec.stdin is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            bufsize=1,
        )
    except FileNotFoundError:
        spawn_spec.cleanup()
        return ProviderResult(returncode=127, stdout="", stderr=f"{provider_name} not found: {provider.command}")

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    selector = selectors.DefaultSelector()
    if process.stdout:
        selector.register(process.stdout, selectors.EVENT_READ, data="stdout")
    if process.stderr:
        selector.register(process.stderr, selectors.EVENT_READ, data="stderr")

    stdin_stream = process.stdin
    stdin_data: memoryview | None = None
    stdin_offset = 0
    if stdin_stream is not None:
        stdin_data = memoryview((spawn_spec.stdin or "").encode())
        if stdin_data:
            os.set_blocking(stdin_stream.fileno(), False)
            selector.register(stdin_stream, selectors.EVENT_WRITE, data="stdin")
        else:
            stdin_stream.close()

    returncode: int | None = None
    timeout_stderr: str | None = None
    try:
        while selector.get_map():
            elapsed = time.monotonic() - started
            if elapsed > timeout_sec:
                process.kill()
                returncode = 124
                timeout_stderr = f"timed out after {timeout_sec}s"
                break

            events = selector.select(timeout=1.0)
            if not events:
                if process.poll() is not None:
                    break
                on_progress(time.monotonic() - started, None)
                continue

            for key, _ in events:
                if key.data == "stdin":
                    try:
                        written = os.write(key.fileobj.fileno(), stdin_data[stdin_offset:])
                    except BrokenPipeError:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    stdin_offset += written
                    if stdin_offset == len(stdin_data):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue

                chunk = key.fileobj.readline()
                if chunk == "":
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    stdout_chunks.append(chunk)
                    if provider_name == "codex":
                        event_label = _codex_json_event_label(chunk)
                        if event_label:
                            on_progress(time.monotonic() - started, event_label)
                else:
                    stderr_chunks.append(chunk)
                    line = chunk.strip()
                    if line:
                        on_progress(time.monotonic() - started, line)

        if returncode is None:
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = 124
                timeout_stderr = f"timed out after {timeout_sec}s"
        else:
            process.wait(timeout=5)

        stdout = spawn_spec.captured_output_or("".join(stdout_chunks))
        return ProviderResult(
            returncode=returncode,
            stdout=stdout,
            stderr=timeout_stderr or "".join(stderr_chunks),
        )
    finally:
        if stdin_stream is not None and not stdin_stream.closed:
            stdin_stream.close()
        selector.close()
        spawn_spec.cleanup()


def _codex_json_event_label(line: str) -> str | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    event_type = str(payload.get("type") or payload.get("event") or "")
    if not event_type:
        return None
    return f"codex_event:{event_type}"


def _try_parse_json(text: str) -> dict | None:
    """Try to parse JSON from text, stripping markdown fences if needed."""
    stripped = text.strip()

    # Direct JSON
    if stripped.startswith("{"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    # Markdown fenced JSON
    if "```" in stripped:
        lines = stripped.splitlines()
        json_lines = []
        in_fence = False
        for line in lines:
            if line.strip().startswith("```"):
                if in_fence:
                    break
                in_fence = True
                continue
            if in_fence:
                json_lines.append(line)
        if json_lines:
            try:
                return json.loads("\n".join(json_lines))
            except json.JSONDecodeError:
                pass

    # Find first { to last } — but only if it looks like a real JSON object
    # Avoid matching random braces in error messages or code
    first_brace = stripped.find("{")
    last_brace = stripped.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        candidate = stripped[first_brace:last_brace + 1]
        # Sanity check: must contain a colon (key:value pair) and be > 10 chars
        if len(candidate) > 10 and ":" in candidate:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

    return None
