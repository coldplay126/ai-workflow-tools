"""Fail-closed idle inspection for host shutdown and agent CLI callers."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from awf.supervisor.agent import IdleState, IdleStatus
from awf.supervisor.runtime_paths import RuntimePaths
from awf.supervisor.store import SupervisorStore


_ACTIVE_LEASE_FIELDS = frozenset(
    ("job_id", "generation", "agent_id", "acquired_at", "lease_expires_at")
)
_REQUIRED_TABLES = frozenset(("event_outbox", "command_ledger", "job_sequence"))


def inspect_idle_status(
    paths: RuntimePaths,
    store_factory: Callable[[Path], SupervisorStore] = SupervisorStore,
) -> IdleStatus:
    """Return safe only for an absent marker and a verified empty production outbox.

    This deliberately does not create, repair, or migrate state.  Any malformed
    marker, missing database/schema, lock, or query failure is unknown, so callers
    such as the AWS idle-stop timer retain the host rather than risk interrupting
    an active or recoverable lease.
    """

    marker_state = _marker_state(paths.active_lease_path)
    if marker_state is IdleState.UNKNOWN:
        return _status(IdleState.UNKNOWN, paths)

    try:
        _validate_existing_store(paths.store_path)
        store = store_factory(paths.store_path)
        pending = store.pending_events(limit=1)
    except Exception:
        return _status(IdleState.UNKNOWN, paths)

    if marker_state is IdleState.BUSY or pending:
        return _status(IdleState.BUSY, paths)
    return _status(IdleState.SAFE, paths)


def _status(state: IdleState, paths: RuntimePaths) -> IdleStatus:
    return IdleStatus(
        state=state,
        active_lease_path=paths.active_lease_path,
        store_path=paths.store_path,
    )


def _marker_state(path: Path) -> IdleState:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return IdleState.SAFE
    except (OSError, TypeError, ValueError):
        return IdleState.UNKNOWN
    return IdleState.BUSY if _valid_marker(value) else IdleState.UNKNOWN


def _valid_marker(value: object) -> bool:
    if not isinstance(value, dict) or frozenset(value) != _ACTIVE_LEASE_FIELDS:
        return False
    if type(value["generation"]) is not int or value["generation"] < 0:
        return False
    return all(
        isinstance(value[field], str) and bool(value[field])
        for field in ("job_id", "agent_id", "acquired_at", "lease_expires_at")
    )


def _validate_existing_store(path: Path) -> None:
    """Verify the existing database in readonly mode before constructing a store."""

    if not path.is_file():
        raise OSError("supervisor database does not exist")
    uri = "file:{}?mode=ro".format(quote(path.as_posix(), safe="/"))
    connection = sqlite3.connect(uri, uri=True, timeout=0.0, isolation_level=None)
    try:
        schema_version = connection.execute("PRAGMA schema_version").fetchone()
        if schema_version is None or type(schema_version[0]) is not int or schema_version[0] < 1:
            raise ValueError("supervisor database schema is missing")
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        names = {row[0] for row in rows}
        if not _REQUIRED_TABLES.issubset(names):
            raise ValueError("supervisor database schema is missing")
        connection.execute("SELECT 1 FROM event_outbox LIMIT 1").fetchone()
    finally:
        connection.close()


__all__ = ["inspect_idle_status"]
