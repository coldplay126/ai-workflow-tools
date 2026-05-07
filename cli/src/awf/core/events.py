from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator, Protocol


class EventType(str, Enum):
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    ESCAPE_TRIGGERED = "escape_triggered"
    ORCHESTRATOR_DECIDED = "orchestrator_decided"
    STAGE_STARTED = "stage_started"
    STAGE_COMPLETED = "stage_completed"
    PHASE_STARTED = "phase_started"
    PHASE_COMPLETED = "phase_completed"
    WORKER_SPAWNED = "worker_spawned"
    WORKER_PROGRESS = "worker_progress"
    WORKER_COMPLETED = "worker_completed"
    ARTIFACT_CREATED = "artifact_created"
    ARTIFACT_UPDATED = "artifact_updated"
    PROVIDER_OUTPUT = "provider_output"
    PROVIDER_TOOL_CALL = "provider_tool_call"
    GATE_EVALUATED = "gate_evaluated"
    HEARTBEAT = "heartbeat"
    PROGRESS_UPDATE = "progress_update"
    MULTI_AGENT_STARTED = "multi_agent_started"
    AGENT_COMPLETED = "agent_completed"
    JUDGE_VERDICT = "judge_verdict"
    TEAM_TURN_STARTED = "team_turn_started"
    TEAM_TURN_COMPLETED = "team_turn_completed"


@dataclass
class ExecutionEvent:
    type: EventType
    timestamp: str
    run_id: str
    task_id: str
    source: str
    sequence: int
    data: dict[str, Any] = field(default_factory=dict)


class EventStream(Protocol):
    def __iter__(self) -> Iterator[ExecutionEvent]:
        ...

    def cancel(self) -> None:
        ...
