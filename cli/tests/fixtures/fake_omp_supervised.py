#!/usr/bin/env python3
"""Native OMP JSON-stream fixture used only by process-level Supervisor tests.

CLI contract (the production runner invokes this exact surface):
    fake_omp_supervised.py --mode json [-p @PROMPT] [--no-session]

The fixture refuses non-JSON mode and prompt text rather than a ``@`` file path.
Its behavior is selected only by environment:

* ``AWF_FAKE_OMP_MODE=complete`` emits a single completed ``SupervisorJob``
  native task envelope and exits zero.
* ``AWF_FAKE_OMP_MODE=block`` records its own PID plus a sleeping child, emits
  durable native task handles, and waits for process-group SIGTERM.
* ``AWF_FAKE_OMP_SESSION_PATH`` is required and receives the session ID.
* ``AWF_FAKE_OMP_PARENT_PID_PATH`` and ``AWF_FAKE_OMP_CHILD_PID_PATH`` are
  required in ``block`` mode.
* ``AWF_FAKE_OMP_SECRET`` is intentionally echoed only in untrusted native
  output so the process test can prove metadata artifacts redact it.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SESSION_ID = "fixture-native-session"
TASK_ID = "01SUPERVISOR"


def _environment_path(name: str, *, required: bool = True) -> Path | None:
    value = os.environ.get(name, "")
    if not value:
        if required:
            raise SystemExit("{} is required".format(name))
        return None
    return Path(value)


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


def _task_record(status: str) -> dict[str, Any]:
    return {
        "index": 0,
        "id": TASK_ID,
        "name": "SupervisorJob",
        "agent": "task",
        "status": status,
        "resolvedModel": "fixture-native-model",
        "durationMs": 7,
        "tokens": 5,
    }


def _emit_native_handles(status: str) -> None:
    task = _task_record(status)
    _emit({"type": "session", "id": SESSION_ID})
    _emit(
        {
            "type": "tool_execution_update",
            "toolCallId": "native-task-call",
            "toolName": "task",
            "args": {
                "tasks": [
                    {"name": "SupervisorJob", "agent": "task", "task": "private fixture task"}
                ]
            },
            "partialResult": {"details": {"progress": [task]}},
        }
    )


def _emit_completed_envelope(secret: str) -> None:
    completed = _task_record("completed")
    _emit(
        {
            "type": "tool_execution_end",
            "toolCallId": "native-task-call",
            "toolName": "task",
            "result": {"details": {"results": [completed]}},
        }
    )
    envelope = {
        "awf_omp_batch": 1,
        "workers": [
            {
                "name": "SupervisorJob",
                "result": {"conclusion": "PASS", "fixture_omp_echo": secret},
            }
        ],
    }
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": json.dumps(envelope, separators=(",", ":"))}],
        "provider": "fixture-native-provider",
        "model": "fixture-native-model",
        "usage": {"input": 11, "output": 7, "totalTokens": 18},
        "responseId": "fixture-native-response",
        "stopReason": "stop",
    }
    _emit({"type": "message_end", "message": message})
    _emit({"type": "agent_end", "messages": [message]})


def _block_until_group_termination() -> None:
    parent_path = _environment_path("AWF_FAKE_OMP_PARENT_PID_PATH")
    child_path = _environment_path("AWF_FAKE_OMP_CHILD_PID_PATH")
    assert parent_path is not None and child_path is not None
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    parent_path.write_text(str(os.getpid()), encoding="utf-8")
    child_path.write_text(str(child.pid), encoding="utf-8")

    def _stop(_signum: int, _frame: object) -> None:
        # The runner, not this fixture, owns whole-process-group termination.
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, _stop)
    while True:
        time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--no-session", action="store_true")
    parser.add_argument("-p", required=True)
    arguments, unknown = parser.parse_known_args(argv)
    if unknown or arguments.mode != "json" or not arguments.p.startswith("@"):
        return 2
    if arguments.no_session:
        # The agent E2E requires a real persisted-session native execution.
        return 2
    prompt_path = Path(arguments.p[1:])
    if not prompt_path.is_file():
        return 2
    session_path = _environment_path("AWF_FAKE_OMP_SESSION_PATH")
    assert session_path is not None
    session_path.write_text(SESSION_ID, encoding="utf-8")
    mode = os.environ.get("AWF_FAKE_OMP_MODE", "")
    secret = os.environ.get("AWF_FAKE_OMP_SECRET", "")
    if mode == "complete":
        _emit_native_handles("completed")
        _emit_completed_envelope(secret)
        return 0
    if mode == "block":
        _emit_native_handles("running")
        _block_until_group_termination()
        return 143
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
