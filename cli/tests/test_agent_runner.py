"""Focused contracts for individual agent execution."""
from __future__ import annotations


import sys

import pytest

from awf.core.agent_runner import AgentResult, run_agent
from awf.providers.base import ProviderResult


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
