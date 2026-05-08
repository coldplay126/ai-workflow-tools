"""cmux-agent observability helpers.

Read-only consumer for `.agent/events.jsonl` files produced by the
cmux-agent package. This module does NOT import cmux_agent; it only
knows the 4-field JSONL schema (ts/event/run_id/data).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Optional

FAILURE_EVENTS = {"artifact.validation_failed", "message.failed"}


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _resolve_log_path(repo_root: Optional[str], explicit: Optional[str]) -> Path:
    """Resolve the cmux events JSONL path.

    Priority:
      1. $AWF_CMUX_LOG (if set and non-empty)
      2. explicit argument (file or directory)
      3. <root>/.agent/events.jsonl
      4. <root>/cmux-agent/.agent/events.jsonl
    """
    env_path = os.environ.get("AWF_CMUX_LOG", "").strip()
    if env_path:
        return Path(env_path).expanduser()

    if explicit:
        p = Path(explicit).expanduser()
        if p.is_dir():
            candidate = p / ".agent" / "events.jsonl"
            return candidate
        return p

    base = Path(repo_root).expanduser() if repo_root else Path.cwd()
    candidate_a = base / ".agent" / "events.jsonl"
    if candidate_a.exists():
        return candidate_a
    candidate_b = base / "cmux-agent" / ".agent" / "events.jsonl"
    if candidate_b.exists():
        return candidate_b
    # Default to the primary candidate so error messages point to the
    # most expected location.
    return candidate_a


# ---------------------------------------------------------------------------
# JSONL reader
# ---------------------------------------------------------------------------

def _iter_events(path: Path) -> Iterator[dict]:
    """Yield decoded event records from a JSONL file.

    Raises FileNotFoundError if the file is absent. Malformed lines are
    reported to stderr and skipped.
    """
    if not path.exists():
        raise FileNotFoundError(str(path))
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                print(
                    f"warning: malformed event at line {lineno}, skipped",
                    file=sys.stderr,
                )
                continue
            if isinstance(record, dict):
                yield record


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _format_summary(event_name: str, data: dict) -> str:
    data = data or {}
    if event_name == "run.created":
        workspace = data.get("workspace_id")
        return f"workspace={workspace}" if workspace is not None else ""
    if event_name == "run.status_changed":
        old = data.get("old")
        new = data.get("new")
        return f"{old} -> {new}"
    if event_name == "agent.registered":
        role = data.get("role", "")
        name = data.get("name", "")
        return f"{role} {name}".strip()
    if event_name.startswith("artifact."):
        path = data.get("path", "")
        if event_name == "artifact.validation_failed":
            reason = data.get("reason", "")
            return f"{path} ({reason})" if reason else path
        return path
    if event_name.startswith("message."):
        if event_name == "message.failed":
            reason = data.get("reason", "")
            recipient = data.get("recipient", "")
            if recipient and reason:
                return f"{recipient} ({reason})"
            return reason or recipient or ""
        recipient = data.get("recipient", "")
        return recipient
    try:
        rendered = json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(data)
    return rendered[:80]


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _ansi_for_event(event: str) -> str:
    if event.startswith("run."):
        return "\x1b[36m"  # cyan
    if event.startswith("agent."):
        return "\x1b[35m"  # magenta
    if event.startswith("artifact."):
        return "\x1b[32m"  # green
    if event == "message.failed":
        return "\x1b[31m"  # red
    if event.startswith("message."):
        return "\x1b[33m"  # yellow
    return ""


_ANSI_RESET = "\x1b[0m"


def _format_row(record: dict, *, color: bool = False) -> str:
    ts = str(record.get("ts", ""))
    run_id = str(record.get("run_id", "") or "")
    run_prefix = run_id[:8] if run_id else "-"
    event = str(record.get("event", ""))
    summary = _format_summary(event, record.get("data") or {})

    ts_col = f"{ts:<32}"
    run_col = f"{run_prefix:<10}"
    event_col = f"{event:<28}"
    line = f"{ts_col}  {run_col}  {event_col}  {summary}"
    if color:
        ansi = _ansi_for_event(event)
        if ansi:
            line = f"{ansi}{line}{_ANSI_RESET}"
    return line


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def _match_filters(record: dict, run_id: Optional[str], event: Optional[str]) -> bool:
    if run_id and record.get("run_id") != run_id:
        return False
    if event and record.get("event") != event:
        return False
    return True


def _is_failure_event(record: dict) -> bool:
    return record.get("event") in FAILURE_EVENTS


def _failure_entry(record: dict) -> dict:
    data = record.get("data") or {}
    event = str(record.get("event", ""))
    entry = {
        "ts": str(record.get("ts", "")),
        "run_id": str(record.get("run_id", "") or ""),
        "event": event,
        "reason": str(data.get("reason", "") or ""),
        "path": str(data.get("path", "") or ""),
        "message_id": str(data.get("message_id", "") or ""),
        "recipient": str(data.get("recipient", "") or ""),
    }
    if event == "artifact.validation_failed":
        entry["target"] = Path(entry["path"]).name if entry["path"] else "-"
    elif entry["message_id"]:
        entry["target"] = entry["message_id"]
    elif entry["recipient"]:
        entry["target"] = entry["recipient"]
    else:
        entry["target"] = "-"
    return entry


# ---------------------------------------------------------------------------
# tail
# ---------------------------------------------------------------------------

def _emit_record(record: dict, *, json_mode: bool, color: bool) -> None:
    if json_mode:
        print(json.dumps(record, ensure_ascii=False))
    else:
        print(_format_row(record, color=color))


def _follow_loop(
    path: Path,
    filter_fn: Callable[[dict], bool],
    emit_fn: Callable[[dict], None],
    poll: float = 0.2,
) -> int:
    """Poll-based follow. Returns 0 on KeyboardInterrupt or rotation EOF.

    Defensive rotation detection: if file inode changes or size shrinks
    below current position, reopen from the start.
    """
    try:
        fh = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        print(
            f"error: cmux events log not found at {path}. "
            "Set AWF_CMUX_LOG or pass a path.",
            file=sys.stderr,
        )
        return 2

    try:
        fh.seek(0, 2)  # EOF
        try:
            last_inode = os.fstat(fh.fileno()).st_ino
        except OSError:
            last_inode = None

        while True:
            line = fh.readline()
            if line:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    print(
                        "warning: malformed event during follow, skipped",
                        file=sys.stderr,
                    )
                    continue
                if not isinstance(record, dict):
                    continue
                if filter_fn(record):
                    emit_fn(record)
                continue

            # No new data; check for rotation then sleep.
            try:
                if path.exists():
                    st = path.stat()
                    pos = fh.tell()
                    if (last_inode is not None and st.st_ino != last_inode) or st.st_size < pos:
                        fh.close()
                        fh = path.open("r", encoding="utf-8")
                        try:
                            last_inode = os.fstat(fh.fileno()).st_ino
                        except OSError:
                            last_inode = None
            except OSError:
                pass
            try:
                time.sleep(poll)
            except KeyboardInterrupt:
                return 0
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            fh.close()
        except Exception:
            pass


def run_cmux_tail(args: argparse.Namespace) -> int:
    try:
        path = _resolve_log_path(
            getattr(args, "repo_root", None),
            getattr(args, "path", None),
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    run_id = getattr(args, "run_id", None)
    event = getattr(args, "event", None)
    json_mode = bool(getattr(args, "json", False))
    limit = getattr(args, "limit", None)
    follow = bool(getattr(args, "follow", False))
    color = (not json_mode) and _use_color()

    if not path.exists():
        print(
            f"error: cmux events log not found at {path}. "
            "Set AWF_CMUX_LOG or pass a path.",
            file=sys.stderr,
        )
        return 2

    def filter_fn(rec: dict) -> bool:
        return _match_filters(rec, run_id, event)

    try:
        iterator = _iter_events(path)
    except FileNotFoundError:
        print(
            f"error: cmux events log not found at {path}. "
            "Set AWF_CMUX_LOG or pass a path.",
            file=sys.stderr,
        )
        return 2

    filtered = (rec for rec in iterator if filter_fn(rec))
    if limit is not None and limit > 0:
        collected = list(deque(filtered, maxlen=limit))
    else:
        collected = list(filtered)

    for record in collected:
        _emit_record(record, json_mode=json_mode, color=color)

    if follow:
        def emit_fn(rec: dict) -> None:
            _emit_record(rec, json_mode=json_mode, color=color)

        poll = float(os.environ.get("AWF_CMUX_POLL", "0.2") or 0.2)
        return _follow_loop(path, filter_fn, emit_fn, poll=poll)

    return 0


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES = {"completed", "failed", "aborted"}


def _parse_ts(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _format_duration(delta) -> str:
    if delta is None:
        return "-"
    total = int(delta.total_seconds())
    if total < 0:
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours:d}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes:d}m{seconds:02d}s"
    return f"{seconds:d}s"


def _aggregate_runs(records) -> list[dict]:
    """Aggregate per-run summary. Preserves insertion order of first sighting."""
    runs: dict[str, dict] = {}
    order: list[str] = []
    for rec in records:
        run_id = rec.get("run_id")
        if not run_id:
            continue
        event = rec.get("event", "")
        ts_raw = rec.get("ts", "")
        ts_parsed = _parse_ts(ts_raw) if isinstance(ts_raw, str) else None
        entry = runs.get(run_id)
        if entry is None:
            entry = {
                "run_id": run_id,
                "started": ts_raw,
                "started_ts": ts_parsed,
                "last_ts_raw": ts_raw,
                "last_ts": ts_parsed,
                "events_count": 0,
                "last_status": None,
            }
            runs[run_id] = entry
            order.append(run_id)
        entry["events_count"] += 1
        if ts_parsed is not None:
            if entry["last_ts"] is None or ts_parsed >= entry["last_ts"]:
                entry["last_ts"] = ts_parsed
                entry["last_ts_raw"] = ts_raw
            if entry["started_ts"] is None or ts_parsed < entry["started_ts"]:
                entry["started_ts"] = ts_parsed
                entry["started"] = ts_raw
        if event == "run.status_changed":
            data = rec.get("data") or {}
            new = data.get("new")
            if new:
                entry["last_status"] = new

    summaries: list[dict] = []
    for run_id in order:
        entry = runs[run_id]
        last_status = entry["last_status"]
        if last_status in _TERMINAL_STATUSES:
            status = last_status
        elif last_status:
            status = "running"
        else:
            status = "running"
        if entry["started_ts"] and entry["last_ts"]:
            duration = entry["last_ts"] - entry["started_ts"]
        else:
            duration = None
        summaries.append(
            {
                "run_id": run_id,
                "started": entry["started"],
                "status": status,
                "events": entry["events_count"],
                "duration": _format_duration(duration),
            }
        )
    return summaries


def run_cmux_runs(args: argparse.Namespace) -> int:
    try:
        path = _resolve_log_path(
            getattr(args, "repo_root", None),
            getattr(args, "path", None),
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not path.exists():
        print(
            f"error: cmux events log not found at {path}. "
            "Set AWF_CMUX_LOG or pass a path.",
            file=sys.stderr,
        )
        return 2

    if getattr(args, "event", None):
        print(
            "warning: --event does not filter runs aggregation; ignored",
            file=sys.stderr,
        )

    try:
        records = list(_iter_events(path))
    except FileNotFoundError:
        print(
            f"error: cmux events log not found at {path}. "
            "Set AWF_CMUX_LOG or pass a path.",
            file=sys.stderr,
        )
        return 2

    summaries = _aggregate_runs(records)
    limit = getattr(args, "limit", None)
    if limit is not None and limit > 0:
        summaries = summaries[-limit:]

    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        print(json.dumps(summaries, ensure_ascii=False, indent=2))
        return 0

    header = f"{'RUN-ID':<14}  {'STARTED':<32}  {'STATUS':<10}  {'EVENTS':>6}  DURATION"
    print(header)
    for entry in summaries:
        run_id = str(entry["run_id"])
        run_col = run_id[:12]
        started = str(entry.get("started") or "")
        status = str(entry.get("status") or "")
        events = entry.get("events", 0)
        duration = str(entry.get("duration") or "-")
        print(f"{run_col:<14}  {started:<32}  {status:<10}  {events:>6}  {duration}")
    return 0


# ---------------------------------------------------------------------------
# failures
# ---------------------------------------------------------------------------

def run_cmux_failures(args: argparse.Namespace) -> int:
    try:
        path = _resolve_log_path(
            getattr(args, "repo_root", None),
            getattr(args, "path", None),
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not path.exists():
        print(
            f"error: cmux events log not found at {path}. "
            "Set AWF_CMUX_LOG or pass a path.",
            file=sys.stderr,
        )
        return 2

    run_id = getattr(args, "run_id", None)
    try:
        records = [
            record
            for record in _iter_events(path)
            if _match_filters(record, run_id, None) and _is_failure_event(record)
        ]
    except FileNotFoundError:
        print(
            f"error: cmux events log not found at {path}. "
            "Set AWF_CMUX_LOG or pass a path.",
            file=sys.stderr,
        )
        return 2

    limit = getattr(args, "limit", None)
    if limit is not None and limit > 0:
        records = records[-limit:]

    entries = [_failure_entry(record) for record in records]
    if bool(getattr(args, "json", False)):
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        return 0

    if not entries:
        print("No cmux failure events found.")
        return 0

    header = f"{'TS':<32}  {'RUN-ID':<10}  {'EVENT':<28}  {'TARGET':<24}  REASON"
    print(header)
    for entry in entries:
        ts = entry["ts"]
        run_col = entry["run_id"][:8] if entry["run_id"] else "-"
        event = entry["event"]
        target = entry["target"]
        reason = entry["reason"]
        print(f"{ts:<32}  {run_col:<10}  {event:<28}  {target:<24}  {reason}")
    return 0
