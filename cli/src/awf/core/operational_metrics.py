"""Project-scoped operational metrics persistence.

awf emits a stream of operational events (transitive invalidation summaries,
scope-check verdicts, multi-agent dispatch outcomes) that today are only
logged to stderr or returned as command output. This module persists them
to ``<repo_root>/.awf-operations/events/YYYY-MM-DD.jsonl`` so a follow-up
analysis pass (or the wiki module's ``compile_from_events``) can compute
ratios like "what fraction of stage1 indirect invalidations actually
produced different findings?" — the ground truth required to decide
whether the regex-based import extractor needs an AST upgrade.

The on-disk format is intentionally flat JSONL: one self-describing event
per line, append-only. Single-line writes <= PIPE_BUF (4 KiB on macOS,
typically larger on Linux) are atomic on POSIX, so concurrent writers do
not interleave. Events larger than PIPE_BUF are still safe in practice
because ``awf`` is a CLI invoked one-at-a-time per project, but we still
fsync each append to keep the on-disk state consistent if a process is
killed mid-run.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OPERATIONS_DIR_NAME = ".awf-operations"
EVENTS_DIR_NAME = "events"
WIKI_DIR_NAME = "wiki"
LOG_FILE_NAME = "log.md"
INDEX_FILE_NAME = "index.md"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def operations_root(repo_root: str | os.PathLike[str]) -> Path:
    return Path(repo_root) / OPERATIONS_DIR_NAME


def events_dir(repo_root: str | os.PathLike[str]) -> Path:
    return operations_root(repo_root) / EVENTS_DIR_NAME


def _events_file_for_today(repo_root: str | os.PathLike[str]) -> Path:
    base = events_dir(repo_root)
    base.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return base / f"{today}.jsonl"


def record_event(
    repo_root: str | os.PathLike[str],
    event_type: str,
    payload: dict[str, Any],
    *,
    ts: str | None = None,
) -> Path:
    """Append a single event to today's JSONL file. Returns the file path.

    The event envelope is ``{"ts", "type", "payload"}``. Callers must keep
    payloads JSON-serializable; non-serializable values raise TypeError so
    the caller sees the bug instead of a silent miss.
    """
    target = _events_file_for_today(repo_root)
    record = {
        "ts": ts or _now_iso(),
        "type": event_type,
        "payload": payload,
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    # Open with O_APPEND so concurrent writers don't truncate each other.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return target


# ---------------------------------------------------------------------------
# Typed helpers — keep call sites short while documenting payload shape.
# ---------------------------------------------------------------------------


def record_stage1_invalidation(
    repo_root: str | os.PathLike[str],
    invalidation,  # Stage1GraphInvalidation; not imported to avoid cycles
    *,
    service: str | None = None,
    transitive_enabled: bool,
) -> Path:
    """Persist a Stage1GraphInvalidation summary.

    Counts only — paths themselves are kept out of the event so a long-
    running project doesn't bloat events.jsonl. Path lists remain
    available in stderr logs and ``.ai-context/`` artifacts for the same
    run when deeper introspection is needed.
    """
    payload = {
        "service": service,
        "transitive_enabled": transitive_enabled,
        "direct_count": (
            len(invalidation.target_files)
            - len(invalidation.indirect_paths)
        ),
        "indirect_count": len(invalidation.indirect_paths),
        "invalidating_count": len(invalidation.invalidating_paths),
        "unchanged_count": len(invalidation.unchanged_files),
        "deleted_count": len(invalidation.deleted_paths),
    }
    return record_event(repo_root, "stage1_invalidation", payload)


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def record_analysis_complete(
    repo_root: str | os.PathLike[str],
    *,
    service: str | None = None,
    domain: str | None = None,
    mode: str | None = None,
    total_seconds: float | None = None,
    source_file_count: int | None = None,
    bundle_line_count: int | None = None,
    bundle_token_estimate: int | None = None,
    output_file_count: int | None = None,
) -> Path:
    """Persist an ``analysis_complete`` summary with bundle/output counts."""
    payload: dict[str, Any] = {
        "service": service,
        "domain": domain,
        "mode": mode,
    }
    if total_seconds is not None:
        payload["total_seconds"] = round(float(total_seconds), 2)
    for key, value in {
        "source_file_count": source_file_count,
        "bundle_line_count": bundle_line_count,
        "bundle_token_estimate": bundle_token_estimate,
        "output_file_count": output_file_count,
    }.items():
        int_value = _maybe_int(value)
        if int_value is not None:
            payload[key] = int_value
    return record_event(repo_root, "analysis_complete", payload)


def record_scope_check(
    repo_root: str | os.PathLike[str],
    result,  # ScopeCheckResult; not imported to avoid cycles
) -> Path:
    """Persist a ScopeCheckResult summary.

    Counts plus violation paths (typically a small set; useful for
    later-stage triage without re-running the check).
    """
    payload = {
        "base_branch": result.base_branch,
        "planned_count": len(result.planned_set),
        "expanded_count": len(result.expanded_set),
        "changed_count": len(result.changed_files),
        "violation_count": result.violation_count,
        "violation_paths": [v.path for v in result.violations],
        "planned_not_changed_count": len(result.planned_not_changed),
    }
    return record_event(repo_root, "scope_check", payload)


def iter_events(repo_root: str | os.PathLike[str]):
    """Yield every recorded event in chronological order.

    Skips malformed lines silently — JSONL is a debugging stream, not a
    transactional log; one bad line should not block analysis of the rest.
    """
    base = events_dir(repo_root)
    if not base.exists():
        return
    for path in sorted(base.glob("*.jsonl")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
