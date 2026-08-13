"""Focused contracts for individual agent execution."""
from __future__ import annotations

import sys

import pytest

from awf.core.agent_runner import AgentResult, run_agent
from awf.providers.base import ProviderResult
from awf.providers.claude_code import ClaudeCodeProvider
from awf.providers.codex import CodexProvider


class _TimeoutProvider:
    name = "timeout-provider"

    def __init__(self) -> None:
        self.timeout_sec: int | None = None

    def complete(
        self,
        prompt: str,
        *,
        cwd: str | None = None,
        add_dirs: list[str] | None = None,
        timeout_sec: int | None = None,
    ) -> ProviderResult:
        self.timeout_sec = timeout_sec
        return ProviderResult(returncode=124, stdout="", stderr="provider timeout")


def test_run_agent_forwards_timeout_and_marks_provider_timeout() -> None:
    provider = _TimeoutProvider()

    result = run_agent(
        provider,
        "review",
        "reviewer",
        ".",
        timeout_sec=42,
    )

    assert provider.timeout_sec == 42
    assert result.timed_out is True
    assert result.returncode == 124


@pytest.mark.parametrize("parsed", ["not an object", [], 7])
def test_agent_result_non_object_parse_values_fail_closed(parsed: object) -> None:
    result = AgentResult(
        provider_name="provider",
        role="reviewer",
        stdout="raw output",
        stderr="",
        returncode=0,
        elapsed_sec=0.1,
        parsed=parsed,  # type: ignore[arg-type]
    )

    assert result.conclusion == ""
    assert result.findings == []
    assert result.has_critical is False
    assert result.major_count == 0


def test_run_agent_streaming_preserves_deadline_timeout() -> None:
    class _StreamingProvider:
        name = "streaming-provider"
        command = sys.executable
        flags = ["-c", "import time; time.sleep(60)"]

    result = run_agent(
        _StreamingProvider(),
        "review",
        "reviewer",
        ".",
        timeout_sec=1,
        on_progress=lambda _elapsed, _message: None,
    )

    assert result.returncode == 124
    assert result.timed_out is True


def test_run_agent_streaming_uses_claude_spawn_spec_execution_options() -> None:
    capture_program = (
        "import json, sys; "
        "print(json.dumps({'argv': sys.argv[1:]}))"
    )
    provider = ClaudeCodeProvider(
        command=sys.executable,
        flags=["-c", capture_program],
        verbose=False,
        effort="max",
        json_schema="/tmp/claude-schema.json",
    )

    result = run_agent(
        provider,
        "review this change",
        "reviewer",
        ".",
        add_dirs=["/tmp/claude-docs"],
        on_progress=lambda _elapsed, _message: None,
    )

    assert result.returncode == 0
    assert result.parsed == {
        "argv": [
            "--effort",
            "max",
            "--add-dir",
            "/tmp/claude-docs",
            "--json-schema",
            "/tmp/claude-schema.json",
            "review this change",
        ]
    }


def test_run_agent_streaming_uses_codex_spawn_spec_stdin_and_output_options() -> None:
    capture_program = (
        "import json, sys; "
        "args = sys.argv[1:]; "
        "output_path = args[args.index('--output-last-message') + 1]; "
        "open(output_path, 'w').write(json.dumps({'argv': args, 'stdin': sys.stdin.read()})); "
        "print(json.dumps({'type': 'item.completed'}))"
    )
    provider = CodexProvider(
        command=sys.executable,
        flags=["-c", capture_program],
        reasoning_effort="xhigh",
        output_schema_path="/tmp/codex-schema.json",
    )
    progress: list[str | None] = []

    result = run_agent(
        provider,
        "review this change",
        "reviewer",
        ".",
        add_dirs=["/tmp/codex-docs"],
        on_progress=lambda _elapsed, message: progress.append(message),
    )

    assert result.returncode == 0
    assert result.parsed is not None
    captured_argv = result.parsed["argv"]
    output_path = captured_argv[captured_argv.index("--output-last-message") + 1]
    assert captured_argv == [
        "--json",
        "-c",
        "model_reasoning_effort=xhigh",
        "--add-dir",
        "/tmp/codex-docs",
        "--output-schema",
        "/tmp/codex-schema.json",
        "--output-last-message",
        output_path,
        "-",
    ]
    assert result.parsed["stdin"] == "review this change"
    assert "review this change" not in result.parsed["argv"]
    assert progress == ["codex_event:item.completed"]
