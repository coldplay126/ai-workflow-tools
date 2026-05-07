from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from awf.core.events import EventStream
    from awf.core.task import TaskDefinition


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ProviderResult:
    returncode: int
    stdout: str
    stderr: str
    usage: TokenUsage | None = None
    provider_name: str = ""
    model: str | None = None
    elapsed_sec: float = 0.0


class ProviderCapability(str, Enum):
    COMPLETE = "complete"
    ANALYZE_NATIVE = "analyze_native"  # DEPRECATED v0.1: host responsibility, not provider
    WF_NATIVE = "wf_native"  # DEPRECATED v0.1: host responsibility, not provider
    TOOL_LOOP = "tool_loop"
    EVENT_STREAM = "event_stream"
    THINKING = "thinking"
    CITATIONS = "citations"
    SESSION = "session"
    ADD_DIR = "add_dir"


class Provider(Protocol):
    name: str
    capabilities: set[ProviderCapability]

    def complete(
        self,
        prompt: str,
        *,
        cwd: str | None = None,
        add_dirs: list[str] | None = None,
        timeout_sec: int | None = None,
    ) -> ProviderResult:
        ...


class GatewayProvider(Provider, Protocol):
    def execute(self, task: TaskDefinition) -> EventStream:
        ...
