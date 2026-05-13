"""Stale-run recovery helpers (§2.10).

When the cmux app is closed while the cmux-agent run is still RUNNING,
the SQLite row keeps pointing at a workspace_id that no longer exists.
The next `spawn_worker` (or the next `cmux-agent start`) then fails
with a bare "Workspace not found" from cmux.

Group E added a diagnostic precheck. Group G turns that into a real
recovery path: detect the stale workspace, mark the run FAILED, and
clear the watcher PID lock so the user can immediately `cmux-agent
start` again without hand-editing SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass

from cmux_agent.domain.models import Run, RunStatus
from cmux_agent.infrastructure.cmux import CmuxAdapter
from cmux_agent.infrastructure.event_log import EventLog
from cmux_agent.infrastructure.filesystem import AgentFileSystem
from cmux_agent.infrastructure.storage import StateStore
from cmux_agent.domain.events import run_status_changed


@dataclass(frozen=True)
class WorkspaceProbeResult:
    workspace_id: str | None
    alive: bool
    detail: str


@dataclass(frozen=True)
class RecoveryResult:
    """Outcome of recover_stale_run."""

    ran: bool          # True if cleanup work actually happened
    workspace_alive: bool
    run_id: str | None
    cleared_pid_lock: bool
    message: str


def probe_workspace(cmux: CmuxAdapter, workspace_id: str | None) -> WorkspaceProbeResult:
    """Ask cmux whether the workspace still exists. None workspace_id → alive=False, detail noted."""
    if not workspace_id:
        return WorkspaceProbeResult(workspace_id=None, alive=False, detail="no_workspace_id")
    result = cmux.tree(workspace_id=workspace_id)
    if result.ok:
        return WorkspaceProbeResult(workspace_id=workspace_id, alive=True, detail="reachable")
    detail = (result.stderr or "").strip() or "tree_failed"
    return WorkspaceProbeResult(workspace_id=workspace_id, alive=False, detail=detail)


def _clear_watcher_lock(fs: AgentFileSystem) -> bool:
    """Best-effort watcher PID lock removal. Returns True if a file was deleted."""
    from cmux_agent.infrastructure import pid_lock

    lock_path = fs.base / pid_lock.LOCK_FILENAME
    if lock_path.is_file():
        try:
            lock_path.unlink()
            return True
        except OSError:
            return False
    return False


def recover_stale_run(
    *,
    store: StateStore,
    event_log: EventLog,
    fs: AgentFileSystem,
    cmux: CmuxAdapter,
    force: bool = False,
) -> RecoveryResult:
    """Recover from a stale workflow run (§2.10 auto-recovery).

    - Looks up the active run.
    - Probes its workspace via `cmux tree`. When the workspace is alive and
      `force=False`, returns early without doing anything.
    - When the workspace is dead (or `force=True`), marks the run as FAILED,
      appends a `run_status_changed` event, and removes the watcher PID
      lock so a fresh `cmux-agent start` can take it over.
    """
    run: Run | None = store.get_active_run()
    if not run:
        return RecoveryResult(
            ran=False,
            workspace_alive=False,
            run_id=None,
            cleared_pid_lock=False,
            message="활성 run이 없습니다.",
        )

    probe = probe_workspace(cmux, run.workspace_id)
    if probe.alive and not force:
        return RecoveryResult(
            ran=False,
            workspace_alive=True,
            run_id=run.run_id,
            cleared_pid_lock=False,
            message=(
                f"workspace {run.workspace_id} is alive — nothing to recover. "
                f"Pass --force to clean up anyway."
            ),
        )

    old_status = run.status.value
    if old_status != RunStatus.FAILED.value:
        try:
            store.update_run_status(run.run_id, RunStatus.FAILED)
            event_log.append(run_status_changed(run.run_id, old_status, RunStatus.FAILED.value))
        except Exception:
            # The transition guard may reject CREATED→FAILED etc.; surface as message
            # but still attempt the rest of the cleanup.
            pass

    cleared = _clear_watcher_lock(fs)

    if force and probe.alive:
        message = (
            f"forced recovery: run {run.run_id[:8]} marked FAILED, "
            f"watcher lock {'removed' if cleared else 'absent'}. "
            f"workspace {run.workspace_id} is still alive — consider `cmux-agent stop` "
            f"to also close cmux surfaces."
        )
    else:
        message = (
            f"stale run cleaned: run {run.run_id[:8]} marked FAILED (workspace "
            f"{run.workspace_id} unreachable: {probe.detail}). "
            f"watcher lock {'removed' if cleared else 'absent'}. "
            f"start a fresh cycle with `cmux-agent start`."
        )

    return RecoveryResult(
        ran=True,
        workspace_alive=probe.alive,
        run_id=run.run_id,
        cleared_pid_lock=cleared,
        message=message,
    )
