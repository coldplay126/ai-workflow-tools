from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from awf.core.events import EventType, ExecutionEvent


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class EventProcessor:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    _sequence: int = 0
    handlers: list[Callable[[ExecutionEvent], None]] = field(default_factory=list)
    events: list[ExecutionEvent] = field(default_factory=list)

    def record(self, event: ExecutionEvent) -> ExecutionEvent:
        self.events.append(event)
        for handler in self.handlers:
            handler(event)
        return event

    def emit(self, *, event_type: EventType, task_id: str, source: str, data: Optional[dict[str, Any]] = None) -> ExecutionEvent:
        event = ExecutionEvent(
            type=event_type,
            timestamp=_now_iso(),
            run_id=self.run_id,
            task_id=task_id,
            source=source,
            sequence=self._sequence,
            data=data or {},
        )
        self._sequence += 1
        return self.record(event)


def run_complete_with_events(
    *,
    provider,
    prompt: str,
    cwd: str,
    add_dirs: Optional[list[str]],
    label: str,
    task_type: str,
    task_id: str,
    source: str,
    processor: EventProcessor,
    display_task_events: bool = True,
):
    result_holder: dict[str, object] = {}
    error_holder: dict[str, BaseException] = {}
    done = threading.Event()

    def _target() -> None:
        try:
            result_holder["result"] = provider.complete(prompt, cwd=cwd, add_dirs=add_dirs)
        except BaseException as exc:  # pragma: no cover - defensive relay
            error_holder["error"] = exc
        finally:
            done.set()

    processor.emit(
        event_type=EventType.TASK_STARTED,
        task_id=task_id,
        source=source,
        data={"task_type": task_type, "provider": source, "mode": "fallback", "display": display_task_events},
    )

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    started_at = time.monotonic()
    heartbeat_sec = 15
    next_heartbeat = heartbeat_sec
    while not done.wait(timeout=1):
        elapsed = time.monotonic() - started_at
        if elapsed >= next_heartbeat:
            processor.emit(
                event_type=EventType.HEARTBEAT,
                task_id=task_id,
                source=source,
                data={"elapsed_sec": elapsed, "label": label},
            )
            next_heartbeat += heartbeat_sec
    elapsed = time.monotonic() - started_at
    if "error" in error_holder:
        processor.emit(
            event_type=EventType.TASK_FAILED,
            task_id=task_id,
            source=source,
            data={
                "task_type": task_type,
                "duration_sec": elapsed,
                "error": str(error_holder["error"]),
                "display": display_task_events,
            },
        )
        raise error_holder["error"]
    result = result_holder["result"]
    processor.emit(
        event_type=EventType.TASK_COMPLETED,
        task_id=task_id,
        source=source,
        data={
            "task_type": task_type,
            "duration_sec": elapsed,
            "returncode": int(getattr(result, "returncode", 0) or 0),
            "display": display_task_events,
        },
    )
    return result, elapsed
