"""cmd_stop tests covering §2.9 workspace auto-close."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from cmux_agent.cli import commands as command_module
from cmux_agent.domain.models import Agent, AgentRole, Run, RunStatus
from cmux_agent.infrastructure.cmux import CmuxResult
from cmux_agent.infrastructure.filesystem import AgentFileSystem
from cmux_agent.infrastructure.storage import StateStore


class StopFakeCmux:
    """Minimal cmux fake for cmd_stop: records close calls, no surface tracking."""

    def __init__(self, *, close_surface_ok: bool = True, close_workspace_ok: bool = True) -> None:
        self.close_surface_calls: list[str] = []
        self.close_workspace_calls: list[str] = []
        self._close_surface_ok = close_surface_ok
        self._close_workspace_ok = close_workspace_ok

    def close_surface(self, surface_id: str) -> CmuxResult:
        self.close_surface_calls.append(surface_id)
        return CmuxResult(
            ok=self._close_surface_ok,
            stdout="" if self._close_surface_ok else "",
            stderr="" if self._close_surface_ok else "boom",
        )

    def close_workspace(self, workspace_id: str) -> CmuxResult:
        self.close_workspace_calls.append(workspace_id)
        return CmuxResult(
            ok=self._close_workspace_ok,
            stdout="",
            stderr="" if self._close_workspace_ok else "boom",
        )


def _make_run_with_surfaces(tmp_path: Path) -> tuple[AgentFileSystem, StateStore]:
    fs = AgentFileSystem(tmp_path / ".agent")
    fs.init()
    store = StateStore(fs.db_path)
    run = Run(run_id="run-1", status=RunStatus.RUNNING, workspace_id="workspace:1")
    store.save_run(run)
    store.save_agent(Agent(run_id="run-1", role=AgentRole.CONTROLLER, name="controller", surface_id="surface:1"))
    store.save_agent(Agent(run_id="run-1", role=AgentRole.ORCHESTRATOR, name="orchestrator", surface_id="surface:2"))
    store.save_agent(Agent(run_id="run-1", role=AgentRole.WORKER, name="worker-1", surface_id="surface:3"))
    return fs, store


def test_cmd_stop_closes_surfaces_and_workspace_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(command_module, "_active_cwd", str(tmp_path))
    _make_run_with_surfaces(tmp_path)
    fake = StopFakeCmux()
    monkeypatch.setattr(command_module, "CmuxAdapter", lambda: fake)

    command_module.cmd_stop(Namespace(run_id=None, clean=False, keep_workspace=False))

    assert sorted(fake.close_surface_calls) == ["surface:1", "surface:2", "surface:3"]
    assert fake.close_workspace_calls == ["workspace:1"]


def test_cmd_stop_keep_workspace_skips_close(tmp_path, monkeypatch):
    monkeypatch.setattr(command_module, "_active_cwd", str(tmp_path))
    _make_run_with_surfaces(tmp_path)
    fake = StopFakeCmux()
    monkeypatch.setattr(command_module, "CmuxAdapter", lambda: fake)

    command_module.cmd_stop(Namespace(run_id=None, clean=False, keep_workspace=True))

    assert fake.close_surface_calls == []
    assert fake.close_workspace_calls == []


def test_cmd_stop_continues_after_close_surface_failure(tmp_path, monkeypatch):
    """surface close 실패는 logger.warning에 그치고 cmd_stop은 계속 진행."""
    monkeypatch.setattr(command_module, "_active_cwd", str(tmp_path))
    _make_run_with_surfaces(tmp_path)
    fake = StopFakeCmux(close_surface_ok=False)
    monkeypatch.setattr(command_module, "CmuxAdapter", lambda: fake)

    command_module.cmd_stop(Namespace(run_id=None, clean=False, keep_workspace=False))

    # Surfaces are still attempted
    assert len(fake.close_surface_calls) == 3
    # Workspace close still attempted even after surface failures
    assert fake.close_workspace_calls == ["workspace:1"]


def test_cmd_stop_without_workspace_skips_workspace_close(tmp_path, monkeypatch):
    monkeypatch.setattr(command_module, "_active_cwd", str(tmp_path))
    fs = AgentFileSystem(tmp_path / ".agent")
    fs.init()
    store = StateStore(fs.db_path)
    # Run without workspace_id
    run = Run(run_id="run-1", status=RunStatus.RUNNING, workspace_id=None)
    store.save_run(run)
    store.save_agent(Agent(run_id="run-1", role=AgentRole.CONTROLLER, name="controller", surface_id="surface:1"))

    fake = StopFakeCmux()
    monkeypatch.setattr(command_module, "CmuxAdapter", lambda: fake)

    command_module.cmd_stop(Namespace(run_id=None, clean=False, keep_workspace=False))

    assert fake.close_surface_calls == ["surface:1"]
    assert fake.close_workspace_calls == []
