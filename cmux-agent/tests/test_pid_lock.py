"""PID lock tests (§2.8)."""

from __future__ import annotations

import os
from pathlib import Path

from cmux_agent.infrastructure import pid_lock


def test_acquire_on_clean_dir(tmp_path: Path) -> None:
    state = pid_lock.acquire(tmp_path)
    assert state.acquired is True
    assert state.previous_pid is None
    assert state.previous_alive is False
    assert state.path.is_file()
    assert int(state.path.read_text().strip().splitlines()[0]) == os.getpid()


def test_release_removes_lock(tmp_path: Path) -> None:
    state = pid_lock.acquire(tmp_path)
    pid_lock.release(state)
    assert not state.path.exists()


def test_release_is_noop_for_unacquired(tmp_path: Path) -> None:
    # Existing lock from someone else
    lock_path = tmp_path / pid_lock.LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("99999999\n", encoding="utf-8")

    fake_state = pid_lock.LockState(
        acquired=False, pid=os.getpid(), path=lock_path, previous_pid=99999999, previous_alive=True
    )
    pid_lock.release(fake_state)
    # File untouched because we never acquired
    assert lock_path.exists()


def test_stale_lock_is_reclaimed(tmp_path: Path) -> None:
    """A pid file pointing at a dead PID should be silently taken over."""
    lock_path = tmp_path / pid_lock.LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Use an obviously dead pid (very large numbers are typically unused)
    dead_pid = 99999999
    lock_path.write_text(f"{dead_pid}\n", encoding="utf-8")

    state = pid_lock.acquire(tmp_path)
    assert state.acquired is True
    assert state.previous_pid == dead_pid
    assert state.previous_alive is False
    assert int(lock_path.read_text().strip().splitlines()[0]) == os.getpid()


def test_live_lock_blocks_acquisition(tmp_path: Path) -> None:
    """A lock file pointing at our own PID counts as a live lock."""
    lock_path = tmp_path / pid_lock.LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    own_pid = os.getpid()
    lock_path.write_text(f"{own_pid}\n", encoding="utf-8")

    state = pid_lock.acquire(tmp_path)
    assert state.acquired is False
    assert state.previous_pid == own_pid
    assert state.previous_alive is True
    # File unchanged
    assert lock_path.read_text().strip() == str(own_pid)


def test_malformed_lock_file_is_treated_as_stale(tmp_path: Path) -> None:
    lock_path = tmp_path / pid_lock.LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("not-a-pid\n", encoding="utf-8")

    state = pid_lock.acquire(tmp_path)
    assert state.acquired is True
    assert state.previous_pid is None
