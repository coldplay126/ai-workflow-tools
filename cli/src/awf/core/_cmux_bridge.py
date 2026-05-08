"""Filesystem + SQLite + subprocess bridge to a cmux-agent run.

CmuxDispatch uses these helpers to read/write artifacts in a project's
``.agent/`` directory and to spawn workers via the ``cmux-agent`` CLI
without importing the ``cmux_agent`` Python package. The integration
boundary is intentionally the ``.agent/`` filesystem layout (stable on
disk) plus the ``cmux-agent`` CLI (the public surface), so awf-cli can
keep its Python floor at 3.9 even while cmux-agent requires 3.11+.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ORCHESTRATOR_NAME = "awf-orchestrator"
AGENT_DIR_NAME = ".agent"
DB_FILENAME = "control-plane.sqlite3"
SPAWNED_MARKER_DIR = ".awf-spawned"


@dataclass(frozen=True)
class CmuxRunState:
    """Snapshot of an active cmux-agent run, sufficient to drive dispatch."""

    run_id: str
    db_path: Path
    base_dir: Path  # <cwd>/.agent

    @property
    def outbox(self) -> Path:
        return self.base_dir / "outbox"

    @property
    def inbox_dir(self) -> Path:
        return self.base_dir / "inbox"

    @property
    def inbox_orchestrator(self) -> Path:
        return self.inbox_dir / ORCHESTRATOR_NAME

    @property
    def processed(self) -> Path:
        return self.base_dir / "processed"

    @property
    def spawned_marker_dir(self) -> Path:
        return self.base_dir / SPAWNED_MARKER_DIR


@dataclass(frozen=True)
class WorkerInfo:
    name: str
    role_hint: str
    surface_id: str | None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_active_run(cwd: str | Path) -> CmuxRunState | None:
    """Return the active cmux-agent run for ``cwd``, or None.

    Treats every failure mode (missing dir, missing db, locked db, no row)
    as "no active run" — the caller decides whether to fall back or error.
    """
    base = Path(cwd) / AGENT_DIR_NAME
    db = base / DB_FILENAME
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT run_id FROM runs WHERE status IN ('CREATED','RUNNING') "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    return CmuxRunState(run_id=row["run_id"], db_path=db, base_dir=base)


def list_workers(state: CmuxRunState) -> list[WorkerInfo]:
    conn = sqlite3.connect(str(state.db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT name, surface_id FROM agents "
            "WHERE run_id = ? AND role = 'WORKER'",
            (state.run_id,),
        ).fetchall()
    finally:
        conn.close()
    out: list[WorkerInfo] = []
    for r in rows:
        name = r["name"]
        hint = name[len("worker-"):] if name.startswith("worker-") else name
        out.append(
            WorkerInfo(name=name, role_hint=hint, surface_id=r["surface_id"])
        )
    return out


def ensure_orchestrator_registered(state: CmuxRunState) -> None:
    """Insert the awf-orchestrator agent row + create its inbox.

    Idempotent. Uses a NULL surface_id so the cmux-agent broker skips
    terminal injection when delivering to us — we only consume inbox files.
    """
    conn = sqlite3.connect(str(state.db_path))
    try:
        existing = conn.execute(
            "SELECT 1 FROM agents WHERE run_id = ? AND name = ?",
            (state.run_id, ORCHESTRATOR_NAME),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO agents "
                "(agent_id, run_id, role, name, surface_id, created_at) "
                "VALUES (?, ?, 'ORCHESTRATOR', ?, NULL, ?)",
                (str(uuid.uuid4()), state.run_id, ORCHESTRATOR_NAME, _now_iso()),
            )
            conn.commit()
    finally:
        conn.close()
    state.inbox_orchestrator.mkdir(parents=True, exist_ok=True)


def write_dispatch_artifact(
    state: CmuxRunState,
    *,
    batch_id: str,
    worker_idx: int,
    recipient: str,
    role: str,
    prompt: str,
    require_json: bool,
) -> Path:
    """Write a dispatch artifact to ``outbox/`` atomically (.tmp → rename)."""
    state.outbox.mkdir(parents=True, exist_ok=True)
    payload = {
        "type": "dispatch",
        "sender": ORCHESTRATOR_NAME,
        "recipient": recipient,
        "message": prompt,
        "context": {
            "awf_dispatch_id": batch_id,
            "awf_worker_idx": worker_idx,
            "awf_role": role,
            "require_json": require_json,
        },
    }
    target = state.outbox / f"awf-{batch_id}-{worker_idx:03d}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(target)
    return target


def poll_results(
    state: CmuxRunState,
    *,
    batch_id: str,
    deadlines: dict[int, float],
    poll_interval: float = 0.5,
) -> dict[int, dict | None]:
    """Block until each worker_idx in ``deadlines`` has a response or its
    monotonic deadline passes.

    Returns ``{worker_idx: payload}`` where payload is None on timeout.
    Consumed inbox files are moved to ``inbox/<orchestrator>/_consumed/``
    to avoid double-processing on subsequent dispatches.
    """
    consumed_dir = state.inbox_orchestrator / "_consumed"
    consumed_dir.mkdir(parents=True, exist_ok=True)

    pending = dict(deadlines)
    results: dict[int, dict | None] = {}

    while pending:
        now = time.monotonic()
        timed_out = [idx for idx, deadline in pending.items() if now >= deadline]
        for idx in timed_out:
            results[idx] = None
            del pending[idx]
        if not pending:
            break

        if state.inbox_orchestrator.exists():
            for path in sorted(state.inbox_orchestrator.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                ctx = data.get("context") or {}
                if ctx.get("awf_dispatch_id") != batch_id:
                    continue
                idx = ctx.get("awf_worker_idx")
                if not isinstance(idx, int) or idx not in pending:
                    continue
                results[idx] = data
                del pending[idx]
                try:
                    path.replace(consumed_dir / path.name)
                except OSError:
                    pass

        if not pending:
            break
        time.sleep(poll_interval)

    return results


def remove_processed_for_batch(state: CmuxRunState, batch_id: str) -> int:
    """Best-effort delete of ``processed/`` files written by this batch."""
    if not state.processed.exists():
        return 0
    removed = 0
    for path in state.processed.glob(f"awf-{batch_id}-*"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def mark_spawned(state: CmuxRunState, worker_name: str) -> None:
    """Record that awf spawned ``worker_name`` so cleanup can find it later."""
    state.spawned_marker_dir.mkdir(parents=True, exist_ok=True)
    (state.spawned_marker_dir / worker_name).touch()


def list_spawned(state: CmuxRunState) -> list[str]:
    if not state.spawned_marker_dir.exists():
        return []
    return sorted(p.name for p in state.spawned_marker_dir.iterdir() if p.is_file())


def clear_spawned_marker(state: CmuxRunState, worker_name: str) -> None:
    marker = state.spawned_marker_dir / worker_name
    try:
        marker.unlink()
    except OSError:
        pass


def spawn_worker_subprocess(
    *,
    cwd: str,
    name: str,
    role: str | None,
    provider: str | None,
    template: str | None,
    flags: str | None,
    timeout_sec: float = 60.0,
) -> tuple[bool, str]:
    """Invoke ``cmux-agent spawn <name>`` as a subprocess.

    Returns ``(ok, message)``. cmux-agent's spawn is itself responsible for
    creating the cmux surface, registering the agent, and booting the AI CLI.
    """
    cmd = ["cmux-agent", "spawn", name]
    if role:
        cmd += ["--role", role]
    if template:
        cmd += ["--worker-template", template]
    if provider:
        cmd += ["--provider", provider]
    if flags:
        cmd += ["--flags", flags]
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout_sec
        )
    except FileNotFoundError:
        return False, "cmux-agent CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return False, f"cmux-agent spawn timed out after {timeout_sec:.0f}s"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return False, detail or f"cmux-agent spawn exited with {proc.returncode}"
    return True, (proc.stdout or "").strip()


def teardown_worker(
    state: CmuxRunState,
    worker: WorkerInfo,
    *,
    cmux_close_timeout: float = 10.0,
) -> None:
    """Best-effort teardown of a worker spawned by awf.

    Closes the cmux surface (ignoring errors), removes the SQLite agent row,
    deletes the inbox directory, and clears the awf-spawned marker. Designed
    to run in a finally block: any exception here is swallowed so cleanup
    failures never mask the original outcome.
    """
    if worker.surface_id:
        try:
            subprocess.run(
                ["cmux", "close-surface", "--surface", worker.surface_id],
                capture_output=True,
                text=True,
                timeout=cmux_close_timeout,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

    try:
        conn = sqlite3.connect(str(state.db_path))
        try:
            conn.execute(
                "DELETE FROM agents WHERE run_id = ? AND name = ?",
                (state.run_id, worker.name),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass

    inbox = state.inbox_dir / worker.name
    if inbox.exists():
        try:
            for child in inbox.iterdir():
                try:
                    child.unlink()
                except OSError:
                    pass
            inbox.rmdir()
        except OSError:
            pass

    clear_spawned_marker(state, worker.name)
