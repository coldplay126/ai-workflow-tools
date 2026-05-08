"""cmux-agent failure diagnostics CLI tests."""

from __future__ import annotations

from pathlib import Path

from cmux_agent.cli import main
from cmux_agent.domain.events import (
    artifact_validation_failed,
    message_failed,
    run_created,
)
from cmux_agent.domain.models import Run, RunStatus
from cmux_agent.infrastructure.event_log import EventLog
from cmux_agent.infrastructure.filesystem import AgentFileSystem
from cmux_agent.infrastructure.storage import StateStore


def _make_run(tmp_path: Path) -> tuple[AgentFileSystem, EventLog]:
    fs = AgentFileSystem(tmp_path / ".agent")
    fs.init()
    store = StateStore(fs.db_path)
    store.save_run(Run(run_id="run-1", status=RunStatus.RUNNING))
    return fs, EventLog(fs.event_log_path)


def test_status_can_show_recent_failures(tmp_path, capsys):
    fs, event_log = _make_run(tmp_path)
    failed = fs.failed / "bad-dispatch.json"
    failed.write_text("{}", encoding="utf-8")
    event_log.append(
        artifact_validation_failed(
            "run-1",
            str(fs.outbox / "bad-dispatch.json"),
            "미등록 recipient: worker-missing",
        )
    )
    event_log.append(message_failed("run-1", "msg-1", "inbox 전달 실패"))

    main(["--cwd", str(tmp_path), "status", "--failures"])

    output = capsys.readouterr().out
    assert "Failures: 1 artifact validation, 1 message, 1 failed artifacts" in output
    assert "Recent failure events:" in output
    assert "bad-dispatch.json - 미등록 recipient: worker-missing" in output
    assert "msg-1 - inbox 전달 실패" in output
    assert "Failed artifacts:" in output


def test_events_can_filter_to_failures(tmp_path, capsys):
    fs, event_log = _make_run(tmp_path)
    event_log.append(run_created("run-1"))
    event_log.append(
        artifact_validation_failed(
            "run-1",
            str(fs.outbox / "invalid.json"),
            "필수 필드 누락: {'message'}",
        )
    )

    main(["--cwd", str(tmp_path), "events", "--failures"])

    output = capsys.readouterr().out
    assert "artifact.validation_failed" in output
    assert "invalid.json" in output
    assert "run.created" not in output


def test_failures_command_prints_empty_state(tmp_path, capsys):
    _make_run(tmp_path)

    main(["--cwd", str(tmp_path), "failures"])

    output = capsys.readouterr().out
    assert "최근 실패가 없습니다." in output
