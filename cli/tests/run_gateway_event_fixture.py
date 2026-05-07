from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from awf.core.event_processor import EventProcessor
from awf.core.events import EventType
from awf.core.progress import ProgressDisplay


def main() -> int:
    seen: list[str] = []

    def _capture(event) -> None:
        seen.append(event.type.value)

    processor = EventProcessor(handlers=[_capture, ProgressDisplay(stream=open("/dev/null", "w")).handle])
    processor.emit(
        event_type=EventType.TASK_STARTED,
        task_id="task-1",
        source="fixture",
        data={"task_type": "analyze"},
    )
    processor.emit(
        event_type=EventType.HEARTBEAT,
        task_id="task-1",
        source="fixture",
        data={"elapsed_sec": 15, "label": "fixture stage2"},
    )
    processor.emit(
        event_type=EventType.TASK_COMPLETED,
        task_id="task-1",
        source="fixture",
        data={"task_type": "analyze", "duration_sec": 15.0, "returncode": 0},
    )
    assert seen == ["task_started", "heartbeat", "task_completed"]
    print("gateway_event_ok=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
