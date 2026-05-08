"""Unit tests for `awf cmux` read-only observability commands.

These tests must NOT import cmux_agent. The cmux commands are a
stand-alone consumer of the JSONL event schema (ts/event/run_id/data).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.commands import cmux as cmux_module
from awf.commands.cmux import (
    _aggregate_runs,
    _follow_loop,
    _iter_events,
    _resolve_log_path,
    run_cmux_runs,
    run_cmux_tail,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SAMPLE_EVENTS = [
    {"ts": "2026-04-17T10:00:00+00:00", "event": "run.created", "run_id": "run-aaaa-1", "data": {"workspace_id": "ws-1"}},
    {"ts": "2026-04-17T10:00:05+00:00", "event": "agent.registered", "run_id": "run-aaaa-1", "data": {"name": "alice", "role": "planner"}},
    {"ts": "2026-04-17T10:00:10+00:00", "event": "run.status_changed", "run_id": "run-aaaa-1", "data": {"old": "pending", "new": "running"}},
    {"ts": "2026-04-17T10:01:00+00:00", "event": "artifact.detected", "run_id": "run-aaaa-1", "data": {"path": "out/plan.md", "sender": "alice"}},
    {"ts": "2026-04-17T10:02:30+00:00", "event": "run.status_changed", "run_id": "run-aaaa-1", "data": {"old": "running", "new": "completed"}},
    {"ts": "2026-04-17T11:00:00+00:00", "event": "run.created", "run_id": "run-bbbb-2", "data": {"workspace_id": "ws-2"}},
    {"ts": "2026-04-17T11:00:05+00:00", "event": "run.status_changed", "run_id": "run-bbbb-2", "data": {"old": "pending", "new": "running"}},
    {"ts": "2026-04-17T11:00:30+00:00", "event": "message.failed", "run_id": "run-bbbb-2", "data": {"message_id": "m1", "reason": "timeout", "recipient": "bob"}},
]


def _write_jsonl(path: Path, events) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def _make_args(**kwargs) -> argparse.Namespace:
    defaults = dict(
        path=None,
        repo_root=None,
        run_id=None,
        event=None,
        limit=None,
        json=False,
        follow=False,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# T8 required tests
# ---------------------------------------------------------------------------

def test_no_cmux_agent_import():
    """cmux commands must be stand-alone; cmux_agent must not be imported."""
    assert "cmux_agent" not in sys.modules


def test_resolve_log_path_env_wins(tmp_path, monkeypatch):
    """AWF_CMUX_LOG takes priority over all other sources."""
    explicit = tmp_path / "other.jsonl"
    explicit.write_text("", encoding="utf-8")
    env_target = tmp_path / "env.jsonl"

    monkeypatch.setenv("AWF_CMUX_LOG", str(env_target))
    resolved = _resolve_log_path(str(tmp_path), str(explicit))
    assert resolved == env_target

    # Empty string should be ignored.
    monkeypatch.setenv("AWF_CMUX_LOG", "")
    resolved = _resolve_log_path(str(tmp_path), str(explicit))
    assert resolved == explicit


def test_resolve_log_path_auto_discovery(tmp_path, monkeypatch):
    """Fallback chain: explicit -> <root>/.agent/events.jsonl -> cmux-agent/."""
    monkeypatch.delenv("AWF_CMUX_LOG", raising=False)

    # No files yet: returns the primary candidate for error messaging.
    resolved = _resolve_log_path(str(tmp_path), None)
    assert resolved == tmp_path / ".agent" / "events.jsonl"

    # Create the cmux-agent fallback only.
    nested = tmp_path / "cmux-agent" / ".agent" / "events.jsonl"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text("", encoding="utf-8")
    resolved = _resolve_log_path(str(tmp_path), None)
    assert resolved == nested

    # Create the primary; it must now win over the nested one.
    primary = tmp_path / ".agent" / "events.jsonl"
    primary.parent.mkdir(parents=True, exist_ok=True)
    primary.write_text("", encoding="utf-8")
    resolved = _resolve_log_path(str(tmp_path), None)
    assert resolved == primary

    # Explicit directory argument resolves to its .agent/events.jsonl.
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    resolved = _resolve_log_path(None, str(other_dir))
    assert resolved == other_dir / ".agent" / "events.jsonl"


def test_tail_and_runs_read_cmux_agent_event_schema_from_repo_root(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AWF_CMUX_LOG", raising=False)
    log = tmp_path / "cmux-agent" / ".agent" / "events.jsonl"
    events = [
        {
            "ts": "2026-05-08T00:00:00+00:00",
            "event": "run.created",
            "run_id": "run-operational-1",
            "data": {"workspace_id": "workspace:1"},
        },
        {
            "ts": "2026-05-08T00:00:01+00:00",
            "event": "agent.registered",
            "run_id": "run-operational-1",
            "data": {"name": "orchestrator", "role": "orchestrator"},
        },
        {
            "ts": "2026-05-08T00:00:05+00:00",
            "event": "run.status_changed",
            "run_id": "run-operational-1",
            "data": {"old": "RUNNING", "new": "completed"},
        },
    ]
    _write_jsonl(log, events)

    tail_args = _make_args(repo_root=str(tmp_path), json=True, limit=2)
    assert run_cmux_tail(tail_args) == 0
    tail_lines = capsys.readouterr().out.strip().splitlines()
    assert [json.loads(line)["event"] for line in tail_lines] == [
        "agent.registered",
        "run.status_changed",
    ]

    runs_args = _make_args(repo_root=str(tmp_path), json=True)
    assert run_cmux_runs(runs_args) == 0
    runs = json.loads(capsys.readouterr().out)
    assert runs == [
        {
            "run_id": "run-operational-1",
            "started": "2026-05-08T00:00:00+00:00",
            "status": "completed",
            "events": 3,
            "duration": "5s",
        }
    ]


def test_tail_filters_by_run_id_and_event(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AWF_CMUX_LOG", raising=False)
    log = tmp_path / ".agent" / "events.jsonl"
    _write_jsonl(log, _SAMPLE_EVENTS)

    args = _make_args(
        path=str(log),
        run_id="run-aaaa-1",
        event="run.status_changed",
        json=True,
    )
    assert run_cmux_tail(args) == 0
    captured = capsys.readouterr().out.strip().splitlines()
    assert len(captured) == 2
    for line in captured:
        record = json.loads(line)
        assert record["run_id"] == "run-aaaa-1"
        assert record["event"] == "run.status_changed"


def test_tail_limit_returns_last_n(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AWF_CMUX_LOG", raising=False)
    log = tmp_path / ".agent" / "events.jsonl"
    _write_jsonl(log, _SAMPLE_EVENTS)

    args = _make_args(path=str(log), limit=3, json=True)
    assert run_cmux_tail(args) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 3
    tail_events = [json.loads(line)["event"] for line in lines]
    assert tail_events == [ev["event"] for ev in _SAMPLE_EVENTS[-3:]]


def test_tail_json_mode_preserves_raw(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AWF_CMUX_LOG", raising=False)
    log = tmp_path / ".agent" / "events.jsonl"
    _write_jsonl(log, _SAMPLE_EVENTS[:2])

    args = _make_args(path=str(log), json=True)
    assert run_cmux_tail(args) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    decoded = [json.loads(line) for line in lines]
    assert decoded == _SAMPLE_EVENTS[:2]


def test_runs_summary_aggregates_status_and_duration(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AWF_CMUX_LOG", raising=False)
    log = tmp_path / ".agent" / "events.jsonl"
    _write_jsonl(log, _SAMPLE_EVENTS)

    args = _make_args(path=str(log), json=True)
    assert run_cmux_runs(args) == 0
    output = capsys.readouterr().out
    data = json.loads(output)
    by_id = {entry["run_id"]: entry for entry in data}

    assert by_id["run-aaaa-1"]["status"] == "completed"
    assert by_id["run-aaaa-1"]["events"] == 5
    assert by_id["run-aaaa-1"]["duration"] == "2m30s"
    assert by_id["run-bbbb-2"]["status"] == "running"
    assert by_id["run-bbbb-2"]["events"] == 3


def test_follow_terminates_on_keyboard_interrupt(tmp_path, monkeypatch):
    monkeypatch.delenv("AWF_CMUX_LOG", raising=False)
    log = tmp_path / ".agent" / "events.jsonl"
    _write_jsonl(log, _SAMPLE_EVENTS[:1])

    def _raise(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(cmux_module.time, "sleep", _raise)

    result = _follow_loop(
        log,
        filter_fn=lambda rec: True,
        emit_fn=lambda rec: None,
        poll=0.0,
    )
    assert result == 0


# ---------------------------------------------------------------------------
# Additional defensive tests
# ---------------------------------------------------------------------------

def test_tail_missing_log_returns_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AWF_CMUX_LOG", raising=False)
    missing = tmp_path / "nope" / "events.jsonl"
    args = _make_args(path=str(missing))
    rc = run_cmux_tail(args)
    captured = capsys.readouterr()
    assert rc == 2
    assert "cmux events log not found" in captured.err


def test_iter_events_skips_malformed_lines(tmp_path, capsys):
    log = tmp_path / "events.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps(_SAMPLE_EVENTS[0]) + "\n"
        + "{not-json\n"
        + json.dumps(_SAMPLE_EVENTS[1]) + "\n",
        encoding="utf-8",
    )
    records = list(_iter_events(log))
    err = capsys.readouterr().err
    assert len(records) == 2
    assert "malformed event" in err


def test_use_color_respects_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")

    class _FakeStdout:
        def isatty(self):
            return True

    monkeypatch.setattr(cmux_module.sys, "stdout", _FakeStdout())
    assert cmux_module._use_color() is False

    monkeypatch.delenv("NO_COLOR", raising=False)
    assert cmux_module._use_color() is True
