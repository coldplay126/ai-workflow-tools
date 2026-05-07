from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from awf.core.events import EventType
from awf.providers.claude_code import _parse_stream_json_event


def main() -> int:
    run_id = "run-1"
    task_id = "task-1"
    source = "claude-code"

    delta_events = _parse_stream_json_event(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "pong"},
            },
        },
        run_id=run_id,
        task_id=task_id,
        task_type="analyze",
        source=source,
        sequence=0,
    )
    assert len(delta_events) == 1
    assert delta_events[0].type == EventType.PROVIDER_OUTPUT
    assert delta_events[0].data["text"] == "pong"

    result_events = _parse_stream_json_event(
        {
            "type": "result",
            "is_error": False,
            "duration_ms": 1234,
            "result": "pong",
            "stop_reason": "end_turn",
        },
        run_id=run_id,
        task_id=task_id,
        task_type="analyze",
        source=source,
        sequence=1,
    )
    assert len(result_events) == 1
    assert result_events[0].type == EventType.TASK_COMPLETED
    assert result_events[0].data["returncode"] == 0
    assert result_events[0].data["result"] == "pong"

    print("claude_stream_parser_ok=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
