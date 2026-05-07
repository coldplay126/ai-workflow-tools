from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Optional

from awf.core.analysis_state import analysis_state_path, load_analysis_state, save_analysis_state
from awf.core.config import AnalysisContext
from awf.core.events import EventType, ExecutionEvent
from awf.core.state import load_workflow_state, resolve_repo_root, save_workflow_state_snapshot


def _sync_bucket(state: dict) -> dict:
    return state.setdefault("eventSync", {})


_STATE_LOCKS: dict[str, threading.Lock] = {}
_STATE_LOCKS_GUARD = threading.Lock()


def _state_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _STATE_LOCKS_GUARD:
        lock = _STATE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _STATE_LOCKS[key] = lock
        return lock


@dataclass
class AnalysisStateUpdater:
    context: AnalysisContext

    def handle(self, event: ExecutionEvent) -> None:
        if event.type not in {
            EventType.TASK_STARTED,
            EventType.TASK_COMPLETED,
            EventType.STAGE_STARTED,
            EventType.STAGE_COMPLETED,
            EventType.WORKER_SPAWNED,
            EventType.WORKER_COMPLETED,
            EventType.ARTIFACT_CREATED,
            EventType.ARTIFACT_UPDATED,
            EventType.TASK_FAILED,
        }:
            return
        with _state_lock(analysis_state_path(self.context)):
            state = load_analysis_state(self.context)
            sync = _sync_bucket(state)
            sync["lastEventAt"] = event.timestamp
            if event.type in {EventType.ARTIFACT_CREATED, EventType.ARTIFACT_UPDATED}:
                artifacts = sync.setdefault("artifacts", {"created": 0, "updated": 0, "byKind": {}})
                if event.type == EventType.ARTIFACT_CREATED:
                    artifacts["created"] = int(artifacts.get("created", 0) or 0) + 1
                else:
                    artifacts["updated"] = int(artifacts.get("updated", 0) or 0) + 1
                kind = str(event.data.get("kind", "artifact"))
                by_kind = artifacts.setdefault("byKind", {})
                by_kind[kind] = int(by_kind.get(kind, 0) or 0) + 1
            elif event.type in {EventType.TASK_STARTED, EventType.TASK_COMPLETED}:
                tasks = sync.setdefault("tasks", {})
                task_state = tasks.setdefault(event.task_id, {})
                task_state["type"] = str(event.data.get("task_type", task_state.get("type", "task")))
                task_state["provider"] = str(event.data.get("provider", event.source))
                if event.type == EventType.TASK_STARTED:
                    task_state["status"] = "started"
                    task_state["startedAt"] = event.timestamp
                else:
                    task_state["status"] = "completed"
                    task_state["completedAt"] = event.timestamp
                    task_state["returncode"] = int(event.data.get("returncode", 0) or 0)
                    if "duration_sec" in event.data:
                        task_state["durationSec"] = event.data.get("duration_sec")
            elif event.type in {EventType.STAGE_STARTED, EventType.STAGE_COMPLETED}:
                stage = str(event.data.get("stage", "stage"))
                stages = sync.setdefault("stages", {})
                stage_state = stages.setdefault(stage, {})
                if event.type == EventType.STAGE_STARTED:
                    stage_state["status"] = "started"
                    stage_state["startedAt"] = event.timestamp
                else:
                    stage_state["status"] = "completed"
                    stage_state["completedAt"] = event.timestamp
                    if "duration_sec" in event.data:
                        stage_state["durationSec"] = event.data.get("duration_sec")
            elif event.type in {EventType.WORKER_SPAWNED, EventType.WORKER_COMPLETED}:
                worker_id = str(event.data.get("worker_id", "worker"))
                workers = sync.setdefault("workers", {})
                worker_state = workers.setdefault(worker_id, {})
                worker_state["role"] = str(event.data.get("role", worker_state.get("role", "")) or "")
                if event.type == EventType.WORKER_SPAWNED:
                    worker_state["status"] = "started"
                    worker_state["startedAt"] = event.timestamp
                else:
                    worker_state["status"] = "completed"
                    worker_state["completedAt"] = event.timestamp
                    if "passed" in event.data:
                        worker_state["passed"] = bool(event.data.get("passed"))
                    if "duration_sec" in event.data:
                        worker_state["durationSec"] = event.data.get("duration_sec")
            elif event.type == EventType.TASK_FAILED:
                sync["lastError"] = str(event.data.get("error", "") or "")
            save_analysis_state(self.context, state)


@dataclass
class WorkflowStateUpdater:
    repo_root: Optional[str]

    def handle(self, event: ExecutionEvent) -> None:
        if event.type not in {
            EventType.TASK_STARTED,
            EventType.TASK_COMPLETED,
            EventType.STAGE_STARTED,
            EventType.STAGE_COMPLETED,
            EventType.PHASE_STARTED,
            EventType.PHASE_COMPLETED,
            EventType.ESCAPE_TRIGGERED,
            EventType.ORCHESTRATOR_DECIDED,
            EventType.GATE_EVALUATED,
            EventType.ARTIFACT_CREATED,
            EventType.ARTIFACT_UPDATED,
            EventType.TASK_FAILED,
            EventType.AGENT_COMPLETED,
            EventType.JUDGE_VERDICT,
            EventType.TEAM_TURN_STARTED,
            EventType.TEAM_TURN_COMPLETED,
        }:
            return
        resolved_root = str(resolve_repo_root(self.repo_root))
        state_path = Path(resolved_root).resolve() / ".workflow" / "state.json"
        with _state_lock(state_path):
            state = load_workflow_state(resolved_root)
            sync = _sync_bucket(state)
            sync["lastEventAt"] = event.timestamp
            if event.type == EventType.AGENT_COMPLETED:
                agents = sync.setdefault("multiAgent", {}).setdefault("agents", [])
                agents.append({
                    "provider": str(event.data.get("provider", "")),
                    "role": str(event.data.get("role", "")),
                    "elapsed_sec": event.data.get("elapsed_sec"),
                    "ok": bool(event.data.get("ok", False)),
                    "input_tokens": int(event.data.get("input_tokens", 0) or 0),
                    "output_tokens": int(event.data.get("output_tokens", 0) or 0),
                    "at": event.timestamp,
                })
            elif event.type == EventType.JUDGE_VERDICT:
                ma = sync.setdefault("multiAgent", {})
                ma["mode"] = str(event.data.get("mode", ""))
                ma["verdict"] = str(event.data.get("verdict", ""))
                ma["reason"] = str(event.data.get("reason", ""))
                ma["selectedAgent"] = str(event.data.get("selected_agent", ""))
                ma["at"] = event.timestamp
            elif event.type in {EventType.ARTIFACT_CREATED, EventType.ARTIFACT_UPDATED}:
                artifacts = sync.setdefault("artifacts", {"created": 0, "updated": 0, "byKind": {}})
                if event.type == EventType.ARTIFACT_CREATED:
                    artifacts["created"] = int(artifacts.get("created", 0) or 0) + 1
                else:
                    artifacts["updated"] = int(artifacts.get("updated", 0) or 0) + 1
                kind = str(event.data.get("kind", "artifact"))
                by_kind = artifacts.setdefault("byKind", {})
                by_kind[kind] = int(by_kind.get(kind, 0) or 0) + 1
            elif event.type in {EventType.TASK_STARTED, EventType.TASK_COMPLETED}:
                tasks = sync.setdefault("tasks", {})
                task_state = tasks.setdefault(event.task_id, {})
                task_state["type"] = str(event.data.get("task_type", task_state.get("type", "task")))
                task_state["provider"] = str(event.data.get("provider", event.source))
                if event.type == EventType.TASK_STARTED:
                    task_state["status"] = "started"
                    task_state["startedAt"] = event.timestamp
                else:
                    task_state["status"] = "completed"
                    task_state["completedAt"] = event.timestamp
                    task_state["returncode"] = int(event.data.get("returncode", 0) or 0)
                    if "duration_sec" in event.data:
                        task_state["durationSec"] = event.data.get("duration_sec")
            elif event.type in {EventType.PHASE_STARTED, EventType.PHASE_COMPLETED}:
                phase = str(event.data.get("phase", "phase"))
                phases = sync.setdefault("phases", {})
                phase_state = phases.setdefault(phase, {})
                if event.type == EventType.PHASE_STARTED:
                    phase_state["status"] = "started"
                    phase_state["startedAt"] = event.timestamp
                    escapes = sync.get("escapes", {})
                    if isinstance(escapes, dict):
                        escapes.pop(phase, None)
                    decisions = sync.get("decisions", {})
                    if isinstance(decisions, dict):
                        decisions.pop(phase, None)
                    team_turns = sync.get("teamTurns")
                    if isinstance(team_turns, dict):
                        team_turns.pop(phase, None)
                else:
                    phase_state["status"] = "completed"
                    phase_state["completedAt"] = event.timestamp
                    if "duration_sec" in event.data:
                        phase_state["durationSec"] = event.data.get("duration_sec")
            elif event.type in {EventType.STAGE_STARTED, EventType.STAGE_COMPLETED}:
                stage = str(event.data.get("stage", "stage"))
                stages = sync.setdefault("stages", {})
                stage_state = stages.setdefault(stage, {})
                if event.type == EventType.STAGE_STARTED:
                    stage_state["status"] = "started"
                    stage_state["startedAt"] = event.timestamp
                else:
                    stage_state["status"] = "completed"
                    stage_state["completedAt"] = event.timestamp
                    if "duration_sec" in event.data:
                        stage_state["durationSec"] = event.data.get("duration_sec")
            elif event.type in {EventType.TEAM_TURN_STARTED, EventType.TEAM_TURN_COMPLETED}:
                phase = str(event.data.get("phase", "unknown"))
                turn = str(event.data.get("turn", "0"))
                turns = sync.setdefault("teamTurns", {})
                phase_turns = turns.setdefault(phase, {})
                turn_state = phase_turns.setdefault(turn, {})
                if event.type == EventType.TEAM_TURN_STARTED:
                    turn_state["status"] = "started"
                    turn_state["startedAt"] = event.timestamp
                else:
                    turn_state["status"] = "completed"
                    turn_state["completedAt"] = event.timestamp
                    if "duration_sec" in event.data:
                        turn_state["durationSec"] = event.data.get("duration_sec")
                    if "worker_count" in event.data:
                        turn_state["workerCount"] = event.data.get("worker_count")
            elif event.type == EventType.ESCAPE_TRIGGERED:
                escapes = sync.setdefault("escapes", {})
                phase = str(event.data.get("phase", "phase"))
                escapes[phase] = {
                    "reason": str(event.data.get("reason", "") or ""),
                    "summary": str(event.data.get("summary", "") or ""),
                    "recommendedAction": str(event.data.get("recommended_action", "") or ""),
                    "provider": str(event.data.get("provider", event.source)),
                    "recordedAt": event.timestamp,
                }
            elif event.type == EventType.ORCHESTRATOR_DECIDED:
                decisions = sync.setdefault("decisions", {})
                phase = str(event.data.get("phase", "phase"))
                decisions[phase] = {
                    "decision": str(event.data.get("decision", "") or ""),
                    "reason": str(event.data.get("reason", "") or ""),
                    "replanTarget": str(event.data.get("replan_target", "") or ""),
                    "recordedAt": event.timestamp,
                }
            elif event.type == EventType.GATE_EVALUATED:
                gates = sync.setdefault("gates", {})
                gate = str(event.data.get("gate", "gate"))
                gates[gate] = {
                    "passed": bool(event.data.get("passed", False)),
                    "recordedAt": event.timestamp,
                }
            elif event.type == EventType.TASK_FAILED:
                sync["lastError"] = str(event.data.get("error", "") or "")
            save_workflow_state_snapshot(resolved_root, state)
