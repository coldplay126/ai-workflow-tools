from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

import pytest

from awf.runners.omp import OmpRunnerConfig, OmpWorkerTask, run_omp_native_batch


class CancelAfterOneTick:
    poll_interval_sec = 0.01

    def __init__(self, child_pid_path: Path) -> None:
        self._child_pid_path = child_pid_path
        self.ticks = 0

    def on_tick(self) -> Optional[str]:
        self.ticks += 1
        return "lease_lost" if self._child_pid_path.is_file() else None


def _write_sleeping_omp(path: Path, *, child_pid_path: Path) -> Path:
    script = f'''#!/usr/bin/env python3
import signal
import subprocess
import sys
import time
from pathlib import Path

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path({str(child_pid_path)!r}).write_text(str(child.pid), encoding="utf-8")

def stop_child(_signum, _frame):
    try:
        child.terminate()
    except ProcessLookupError:
        pass
    child.wait()
    raise SystemExit(143)

signal.signal(signal.SIGTERM, stop_child)
time.sleep(60)
'''
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def _write_successful_omp(path: Path) -> Path:
    envelope = json.dumps(
        {
            "awf_omp_batch": 1,
            "workers": [
                {
                    "name": "SupervisorJob",
                    "result": {"conclusion": "PASS"},
                }
            ],
        },
        separators=(",", ":"),
    )
    agents = [
        {
            "index": 0,
            "id": "01SUPERVISOR",
            "agent": "task",
            "status": "completed",
        }
    ]
    script = f'''#!/usr/bin/env python3
import json

agents = {agents!r}
message = {{
    "role": "assistant",
    "content": [{{"type": "text", "text": {envelope!r}}}],
    "provider": "fixture-provider",
    "model": "coordinator-model",
    "usage": {{"input": 3, "output": 2, "totalTokens": 5}},
}}
print(json.dumps({{"type": "session", "id": "coordinator-session"}}))
print(json.dumps({{
    "type": "tool_execution_update",
    "toolCallId": "call-task-fixture",
    "toolName": "task",
    "args": {{"tasks": agents}},
    "partialResult": {{"details": {{"progress": agents}}}},
}}))
print(json.dumps({{
    "type": "tool_execution_end",
    "toolCallId": "call-task-fixture",
    "toolName": "task",
    "result": {{"details": {{"results": agents}}}},
}}))
print(json.dumps({{"type": "message_end", "message": message}}))
print(json.dumps({{"type": "agent_end", "messages": [message]}}))
'''
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def _supervisor_worker() -> OmpWorkerTask:
    return OmpWorkerTask(
        name="SupervisorJob",
        role="supervisor-job",
        prompt="Execute the exact task.",
        agent_type="task",
        require_json=True,
    )


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup is POSIX-specific")
def test_native_batch_terminates_process_group_on_lease_loss(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    fake_omp = _write_sleeping_omp(tmp_path / "omp", child_pid_path=child_pid_path)
    control = CancelAfterOneTick(child_pid_path)

    [result] = run_omp_native_batch(
        [_supervisor_worker()],
        cwd=str(tmp_path),
        config=OmpRunnerConfig(
            command=str(fake_omp),
            no_session=False,
            timeout_sec=30,
            termination_grace_sec=0.05,
        ),
        control=control,
    )

    assert control.ticks >= 1
    assert result.returncode == 130
    assert result.metadata["termination_reason"] == "lease_lost"
    checkpoint_path = Path(result.metadata["checkpoint_path"])
    assert checkpoint_path.is_file()
    assert json.loads(checkpoint_path.read_text(encoding="utf-8"))["state"] == "ambiguous"
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    for _ in range(20):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_native_batch_without_control_preserves_checkpoint_and_provenance(
    tmp_path: Path,
) -> None:
    fake_omp = _write_successful_omp(tmp_path / "omp")

    [result] = run_omp_native_batch(
        [_supervisor_worker()],
        cwd=str(tmp_path),
        config=OmpRunnerConfig(command=str(fake_omp), no_session=False),
    )

    assert result.returncode == 0
    assert result.parsed == {"conclusion": "PASS"}
    assert Path(result.metadata["checkpoint_path"]).is_file()
    assert result.metadata["checkpoint_state"] == "completed"
    assert result.metadata["batch_fingerprint"]
    assert result.metadata["coordinator_session_id"] == "coordinator-session"
    assert result.metadata["task_id"] == "01SUPERVISOR"
    assert result.metadata["agent_uri"] == "agent://01SUPERVISOR"
    assert result.metadata["history_uri"] == "history://01SUPERVISOR"


def test_native_batch_rejects_control_for_current_host(tmp_path: Path) -> None:
    class UnexpectedBridge:
        def __call__(self, **_kwargs: object) -> object:
            raise AssertionError("current-host bridge must not run with control")

    [result] = run_omp_native_batch(
        [_supervisor_worker()],
        cwd=str(tmp_path),
        config=OmpRunnerConfig(
            command=str(tmp_path / "must-not-run"),
            execution_mode="current_host",
        ),
        host_bridge=UnexpectedBridge(),
        control=CancelAfterOneTick(tmp_path / "never-created.pid"),
    )

    assert result.returncode == 2
    assert result.metadata["execution_mode"] == "current_host"
    assert "omp_same_host_control_unsupported" in result.stderr
