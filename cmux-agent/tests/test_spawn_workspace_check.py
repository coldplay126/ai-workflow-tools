"""§2.10 spawn_worker workspace-alive precheck."""

from __future__ import annotations

from pathlib import Path

from cmux_agent.application.prompting import PromptBuilder
from cmux_agent.application.runtime import AgentRuntime
from cmux_agent.domain.models import Run, RunStatus
from cmux_agent.infrastructure.cmux import CmuxResult
from cmux_agent.infrastructure.event_log import EventLog
from cmux_agent.infrastructure.filesystem import AgentFileSystem
from cmux_agent.infrastructure.storage import StateStore


class _DeadWorkspaceCmux:
    """Stub cmux where the configured workspace was closed out-of-band."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def tree(self, workspace_id: str | None = None) -> CmuxResult:
        self.calls.append(("tree", {"workspace_id": workspace_id}))
        # Simulate cmux saying the workspace is gone.
        return CmuxResult(ok=False, stdout="", stderr="Workspace not found: workspace:1")

    def new_surface(self, *, pane_id=None, workspace_id=None) -> CmuxResult:
        # Should not be reached when tree() pre-check fails.
        self.calls.append(("new_surface", {"workspace_id": workspace_id}))
        return CmuxResult(ok=False, stdout="", stderr="should not be called")


class _LiveWorkspaceButSurfaceFailsCmux:
    """Tree succeeds, but new_surface still fails (e.g. cmux race)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def tree(self, workspace_id: str | None = None) -> CmuxResult:
        self.calls.append(("tree", {"workspace_id": workspace_id}))
        return CmuxResult(ok=True, stdout="OK", stderr="")

    def new_surface(self, *, pane_id=None, workspace_id=None) -> CmuxResult:
        self.calls.append(("new_surface", {"workspace_id": workspace_id}))
        return CmuxResult(ok=False, stdout="", stderr="Workspace not found suddenly")


def _make_runtime(tmp_path: Path, cmux) -> AgentRuntime:
    fs = AgentFileSystem(tmp_path / ".agent")
    fs.init()
    store = StateStore(fs.db_path)
    event_log = EventLog(fs.event_log_path)
    run = Run(run_id="run-1", status=RunStatus.RUNNING, workspace_id="workspace:1")
    store.save_run(run)
    prompt_builder = PromptBuilder(str(fs.outbox), str(fs.inbox))
    return AgentRuntime(
        store=store,
        event_log=event_log,
        fs=fs,
        cmux=cmux,
        prompt_builder=prompt_builder,
        run_id=run.run_id,
        workspace_id=run.workspace_id,
    )


def test_dead_workspace_short_circuits_with_actionable_error(tmp_path: Path) -> None:
    cmux = _DeadWorkspaceCmux()
    runtime = _make_runtime(tmp_path, cmux)

    result = runtime.spawn_worker(name="worker-review", role="review")

    assert result.ok is False
    assert "workspace workspace:1" in result.error
    # Should mention recovery path
    assert "cmux-agent stop" in result.error and "cmux-agent start" in result.error
    # new_surface must NOT have been attempted (precheck short-circuits)
    names = [c[0] for c in cmux.calls]
    assert "tree" in names
    assert "new_surface" not in names


def test_workspace_not_found_at_new_surface_appends_hint(tmp_path: Path) -> None:
    """If tree() succeeds but new_surface races, the same hint is appended."""
    cmux = _LiveWorkspaceButSurfaceFailsCmux()
    runtime = _make_runtime(tmp_path, cmux)

    result = runtime.spawn_worker(name="worker-impl", role="impl")

    assert result.ok is False
    assert "Workspace not found suddenly" in result.error
    assert "cmux-agent stop" in result.error
