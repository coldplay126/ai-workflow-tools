from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from awf.providers.base import ProviderCapability

if TYPE_CHECKING:
    from awf.core.config import AwfConfig
    from awf.providers.base import Provider


class TaskType(str, Enum):
    ANALYZE = "analyze"
    WF_PHASE = "wf_phase"
    CHAT = "chat"
    AI_CONTEXT = "ai_context"


@dataclass
class TaskConstraints:
    timeout_sec: Optional[int] = None
    max_retries: int = 2
    mode: Optional[str] = None
    deep: bool = False
    non_interactive: bool = False
    dry_run: bool = False


@dataclass
class TaskContext:
    cwd: str
    repo_root: str
    docs_root: Optional[str]
    github_root: Optional[str]
    config: "AwfConfig"
    provider_name: str


@dataclass
class TaskDefinition:
    task_id: str
    parent_task_id: Optional[str]
    correlation_id: str
    idempotency_key: str
    type: TaskType
    params: dict[str, Any]
    constraints: TaskConstraints
    context: TaskContext


def resolve_execution_mode(provider: "Provider", task: TaskDefinition) -> str:
    capabilities = getattr(provider, "capabilities", {ProviderCapability.COMPLETE})
    if task.type == TaskType.ANALYZE:
        return "native" if ProviderCapability.ANALYZE_NATIVE in capabilities else "fallback"
    if task.type == TaskType.WF_PHASE:
        return "native" if ProviderCapability.WF_NATIVE in capabilities else "fallback"
    return "fallback"
