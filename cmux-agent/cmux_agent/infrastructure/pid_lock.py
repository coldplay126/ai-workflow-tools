"""PID-based singleton lock for long-running watcher processes (§2.8).

`cmd_watch` and `cmux-agent start` both spawn a watcher; without a lock,
a second `start` run leaves the previous watcher orphaned and two
watchers race on the same outbox, causing the `[Errno 2] No such file`
log noise documented in 2026-05-13 cycle §2.8.

The lock writes the current pid to a file inside `.agent/`. A stale lock
(pid no longer alive) is reclaimed silently; a live lock is reported to
the caller so they can choose to stop the existing watcher first.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

LOCK_FILENAME = ".watcher.pid"


@dataclass(frozen=True)
class LockState:
    """Outcome of a lock acquisition attempt."""

    acquired: bool
    pid: int
    path: Path
    previous_pid: int | None = None
    previous_alive: bool = False


def _pid_alive(pid: int) -> bool:
    """Return True if a process with this pid currently exists.

    Uses signal 0 (no-op) which raises ProcessLookupError when the pid
    is gone, PermissionError when the pid exists but is owned by another
    user (still counts as alive for our purposes).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire(base_dir: Path) -> LockState:
    """Try to take the watcher lock under `base_dir`.

    - If no lock exists, write one and return acquired=True.
    - If a stale lock exists (pid dead), reclaim it and return acquired=True
      with previous_pid set so the caller can log the takeover.
    - If a live lock exists, return acquired=False with previous_pid/alive.
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    path = base_dir / LOCK_FILENAME
    previous_pid: int | None = None
    previous_alive = False
    if path.exists():
        raw = path.read_text(encoding="utf-8").strip()
        try:
            previous_pid = int(raw.splitlines()[0]) if raw else None
        except (ValueError, IndexError):
            previous_pid = None
        if previous_pid is not None and _pid_alive(previous_pid):
            return LockState(
                acquired=False,
                pid=os.getpid(),
                path=path,
                previous_pid=previous_pid,
                previous_alive=True,
            )
        # stale → fall through to overwrite
        previous_alive = False
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    return LockState(
        acquired=True,
        pid=os.getpid(),
        path=path,
        previous_pid=previous_pid,
        previous_alive=previous_alive,
    )


def release(state: LockState) -> None:
    """Remove the lock file iff we still own it (best-effort)."""
    if not state.acquired:
        return
    try:
        if state.path.is_file():
            raw = state.path.read_text(encoding="utf-8").strip()
            current = int(raw.splitlines()[0]) if raw else -1
            if current == state.pid:
                state.path.unlink()
    except (OSError, ValueError):
        # Best-effort cleanup; never raise from release.
        pass
