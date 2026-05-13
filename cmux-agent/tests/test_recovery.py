"""§2.10 stale-run recovery tests."""

from __future__ import annotations

from pathlib import Path

from cmux_agent.application.recovery import (
    RecoveryResult,
    probe_workspace,
    recover_stale_run,
)
from cmux_agent.domain.models import Agent, AgentRole, Run, RunStatus
from cmux_agent.infrastructure.cmux import CmuxResult
from cmux_agent.infrastructure.event_log import EventLog
from cmux_agent.infrastructure.filesystem import AgentFileSystem
from cmux_agent.infrastructure import pid_lock
from cmux_agent.infrastructure.storage import StateStore


class _AliveCmux:
    def tree(self, workspace_id: str | None = None) -> CmuxResult:
        return CmuxResult(ok=True, stdout="OK", stderr="")


class _DeadCmux:
    def tree(self, workspace_id: str | None = None) -> CmuxResult:
        return CmuxResult(ok=False, stdout="", stderr="Workspace not found: workspace:1")


def _make_runtime(tmp_path: Path, *, status: RunStatus = RunStatus.RUNNING) -> tuple[AgentFileSystem, StateStore, EventLog]:
    fs = AgentFileSystem(tmp_path / ".agent")
    fs.init()
    store = StateStore(fs.db_path)
    event_log = EventLog(fs.event_log_path)
    run = Run(run_id="run-1", status=status, workspace_id="workspace:1")
    store.save_run(run)
    store.save_agent(Agent(run_id="run-1", role=AgentRole.ORCHESTRATOR, name="orchestrator", surface_id="surface:1"))
    return fs, store, event_log


def test_probe_workspace_alive() -> None:
    result = probe_workspace(_AliveCmux(), "workspace:1")
    assert result.alive is True
    assert result.detail == "reachable"


def test_probe_workspace_dead() -> None:
    result = probe_workspace(_DeadCmux(), "workspace:1")
    assert result.alive is False
    assert "Workspace not found" in result.detail


def test_probe_workspace_missing_id() -> None:
    result = probe_workspace(_AliveCmux(), None)
    assert result.alive is False
    assert result.detail == "no_workspace_id"


def test_recover_no_active_run(tmp_path: Path) -> None:
    fs = AgentFileSystem(tmp_path / ".agent")
    fs.init()
    store = StateStore(fs.db_path)
    event_log = EventLog(fs.event_log_path)
    result = recover_stale_run(store=store, event_log=event_log, fs=fs, cmux=_AliveCmux())
    assert result.ran is False
    assert result.run_id is None
    assert "활성 run이 없습니다" in result.message


def test_recover_alive_workspace_is_no_op(tmp_path: Path) -> None:
    fs, store, event_log = _make_runtime(tmp_path)
    result = recover_stale_run(store=store, event_log=event_log, fs=fs, cmux=_AliveCmux())
    assert result.ran is False
    assert result.workspace_alive is True
    assert "alive" in result.message
    # Run status unchanged
    run = store.get_active_run()
    assert run is not None and run.status == RunStatus.RUNNING


def test_recover_dead_workspace_marks_run_failed(tmp_path: Path) -> None:
    fs, store, event_log = _make_runtime(tmp_path)
    # Plant a watcher lock so we can confirm it's cleared
    lock_state = pid_lock.acquire(fs.base)
    assert lock_state.acquired
    result = recover_stale_run(store=store, event_log=event_log, fs=fs, cmux=_DeadCmux())
    assert result.ran is True
    assert result.workspace_alive is False
    assert result.cleared_pid_lock is True
    assert "marked FAILED" in result.message
    assert "cmux-agent start" in result.message
    # Verify by run_id (get_active_run filters on CREATED/RUNNING only)
    run = store.get_run("run-1")
    assert run is not None and run.status == RunStatus.FAILED
    # And get_active_run now returns None — no active run
    assert store.get_active_run() is None


def test_recover_force_cleans_alive_workspace_too(tmp_path: Path) -> None:
    fs, store, event_log = _make_runtime(tmp_path)
    result = recover_stale_run(
        store=store, event_log=event_log, fs=fs, cmux=_AliveCmux(), force=True,
    )
    assert result.ran is True
    assert result.workspace_alive is True
    assert "forced recovery" in result.message
    run = store.get_run("run-1")
    assert run is not None and run.status == RunStatus.FAILED


def test_recover_when_run_already_failed_reports_no_active(tmp_path: Path) -> None:
    """A FAILED run is no longer 'active' — recover treats it as nothing to do."""
    fs, store, event_log = _make_runtime(tmp_path, status=RunStatus.FAILED)
    result = recover_stale_run(store=store, event_log=event_log, fs=fs, cmux=_DeadCmux())
    assert result.ran is False
    assert result.run_id is None
    assert "활성 run이 없습니다" in result.message
