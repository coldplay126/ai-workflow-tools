"""Project-scoped LLM-friendly knowledge wiki.

Layout under ``<repo_root>/.awf-operations/``::

    events/<YYYY-MM-DD>.jsonl   ← raw operational event stream (immutable)
    log.md                       ← append-only "## [ts] type | summary"
    index.md                     ← regenerated catalog of wiki pages
    wiki/
      operations/<topic>.md      ← LLM-maintained synthesis pages
      concepts/<topic>.md        ← future: concept pages

This module owns the file conventions:

- **YAML frontmatter** — every wiki page starts with ``---\\n…\\n---`` block.
  Required keys: ``title``, ``last_compiled_at``. Recommended: ``source_runs``
  (list of run/event ids), ``source_commits`` (list of git SHAs), ``confidence``
  (one of ``high``/``medium``/``low``/``contested``), ``related`` (relative
  paths to peer pages).
- **log.md** — append-only history line: ``## [YYYY-MM-DDTHH:MM:SSZ] <type> | <summary>``.
  No mutation of prior entries; new lines append at end.
- **index.md** — regenerated wholesale on each ``regenerate_index`` call.
  Sorted by category (subdir under wiki/) then page title.

Karpathy's LLM Wiki pattern (2026-04 gist) inspired these conventions:
single source of truth on disk + LLM-maintained summaries + cross-links.
We deliberately keep the schema thin so a future ``awf wiki compile``
command (not in this PR) can synthesize event streams into pages without
needing to migrate the format.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from awf.core.operational_metrics import (
    INDEX_FILE_NAME,
    LOG_FILE_NAME,
    WIKI_DIR_NAME,
    operations_root,
)

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
STALE_DAYS = 30


def wiki_root(repo_root: str | os.PathLike[str]) -> Path:
    return operations_root(repo_root) / WIKI_DIR_NAME


def log_path(repo_root: str | os.PathLike[str]) -> Path:
    return operations_root(repo_root) / LOG_FILE_NAME


def index_path(repo_root: str | os.PathLike[str]) -> Path:
    return operations_root(repo_root) / INDEX_FILE_NAME


# ---------------------------------------------------------------------------
# Frontmatter — minimal YAML subset (string/list-of-strings only).
# ---------------------------------------------------------------------------


@dataclass
class WikiPage:
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""


def _parse_frontmatter(raw: str) -> dict[str, Any]:
    """Parse a constrained YAML subset: ``key: value`` and ``key: [a, b, c]``.

    We avoid pulling in PyYAML to keep awf-cli's dependency surface tiny.
    Anything more elaborate fails fast with ValueError so the lint pass
    catches it.
    """
    fm: dict[str, Any] = {}
    for raw_line in raw.splitlines():
        line = raw_line.rstrip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"frontmatter line missing colon: {raw_line!r}")
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                fm[key] = []
            else:
                fm[key] = [item.strip().strip('"').strip("'") for item in inner.split(",")]
        else:
            fm[key] = value.strip('"').strip("'")
    return fm


def _serialize_frontmatter(fm: dict[str, Any]) -> str:
    lines: list[str] = []
    for key in sorted(fm):
        value = fm[key]
        if isinstance(value, list):
            inner = ", ".join(str(v) for v in value)
            lines.append(f"{key}: [{inner}]")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def read_page(path: str | os.PathLike[str]) -> WikiPage:
    raw = Path(path).read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(raw)
    if not match:
        return WikiPage(frontmatter={}, body=raw)
    fm = _parse_frontmatter(match.group(1))
    return WikiPage(frontmatter=fm, body=match.group(2))


def write_page(path: str | os.PathLike[str], page: WikiPage) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fm_block = _serialize_frontmatter(page.frontmatter)
    body = page.body if page.body.endswith("\n") else page.body + "\n"
    target.write_text(f"---\n{fm_block}\n---\n{body}", encoding="utf-8")


# ---------------------------------------------------------------------------
# log.md — append-only history.
# ---------------------------------------------------------------------------


def append_log_entry(
    repo_root: str | os.PathLike[str],
    *,
    event_type: str,
    summary: str,
    ts: str | None = None,
) -> Path:
    """Append one ``## [ts] type | summary`` line to log.md.

    The log file is created lazily with a one-line header so it stays
    parseable by ``grep '^## '`` even when no events have been recorded.
    """
    target = log_path(repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    header = ""
    if not target.exists():
        header = "# Operations log\n\nAppend-only chronological record of awf operational events. Generated by `awf.core.wiki`.\n\n"
    timestamp = ts or datetime.now(timezone.utc).isoformat()
    entry = f"## [{timestamp}] {event_type} | {summary}\n"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        if header:
            os.write(fd, header.encode("utf-8"))
        os.write(fd, entry.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return target


# ---------------------------------------------------------------------------
# index.md — regenerated catalog.
# ---------------------------------------------------------------------------


def regenerate_index(repo_root: str | os.PathLike[str]) -> Path:
    """Rebuild ``index.md`` from current wiki/ contents.

    Pages are grouped by their immediate subdirectory under wiki/, sorted
    alphabetically. Each entry shows the title from frontmatter (falling
    back to filename) and the last_compiled_at if present.
    """
    target = index_path(repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    root = wiki_root(repo_root)
    grouped: dict[str, list[tuple[str, str, str]]] = {}
    if root.exists():
        for path in sorted(root.rglob("*.md")):
            relative = path.relative_to(root)
            category = relative.parts[0] if len(relative.parts) > 1 else "_root"
            try:
                page = read_page(path)
            except (OSError, ValueError):
                page = WikiPage()
            title = str(page.frontmatter.get("title") or relative.stem)
            stamp = str(page.frontmatter.get("last_compiled_at") or "")
            grouped.setdefault(category, []).append(
                (title, str(relative), stamp)
            )

    lines = [
        "# Wiki index",
        "",
        "Auto-regenerated by `awf wiki regenerate-index`. Do not edit by hand.",
        "",
    ]
    if not grouped:
        lines.append("_No pages yet._")
    else:
        for category in sorted(grouped):
            display = category.replace("_root", "(root)")
            lines.append(f"## {display}")
            lines.append("")
            for title, relpath, stamp in sorted(grouped[category]):
                stamp_part = f" — _last compiled {stamp}_" if stamp else ""
                lines.append(f"- [{title}](wiki/{relpath}){stamp_part}")
            lines.append("")
    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# lint — orphan/stale/missing-provenance detection.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LintIssue:
    page: str
    code: str  # missing_provenance | stale | orphan | malformed_frontmatter
    detail: str


def lint(
    repo_root: str | os.PathLike[str],
    *,
    stale_days: int = STALE_DAYS,
    now: datetime | None = None,
) -> list[LintIssue]:
    """Return every issue found in the wiki/ tree.

    Issues:
    - **missing_provenance**: page has neither ``source_runs`` nor
      ``source_commits`` in frontmatter.
    - **stale**: ``last_compiled_at`` is older than ``stale_days``.
    - **orphan**: page exists on disk but is not linked from index.md
      (run ``regenerate_index`` to clear).
    - **malformed_frontmatter**: page frontmatter cannot be parsed.
    """
    issues: list[LintIssue] = []
    root = wiki_root(repo_root)
    if not root.exists():
        return issues

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=stale_days)
    indexed: set[str] = set()
    idx = index_path(repo_root)
    if idx.exists():
        text = idx.read_text(encoding="utf-8")
        for match in re.finditer(r"\(wiki/([^)]+)\)", text):
            indexed.add(match.group(1))

    for path in sorted(root.rglob("*.md")):
        rel = str(path.relative_to(root))
        try:
            page = read_page(path)
        except ValueError as exc:
            issues.append(LintIssue(page=rel, code="malformed_frontmatter", detail=str(exc)))
            continue

        if not page.frontmatter.get("source_runs") and not page.frontmatter.get("source_commits"):
            issues.append(
                LintIssue(
                    page=rel,
                    code="missing_provenance",
                    detail="frontmatter has neither source_runs nor source_commits",
                )
            )

        stamp_str = str(page.frontmatter.get("last_compiled_at") or "")
        if stamp_str:
            try:
                stamp = datetime.fromisoformat(stamp_str.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                if stamp < cutoff:
                    issues.append(
                        LintIssue(
                            page=rel,
                            code="stale",
                            detail=f"last_compiled_at {stamp_str} older than {stale_days}d",
                        )
                    )
            except ValueError:
                issues.append(
                    LintIssue(
                        page=rel,
                        code="malformed_frontmatter",
                        detail=f"last_compiled_at not ISO-8601: {stamp_str!r}",
                    )
                )

        if rel not in indexed:
            issues.append(
                LintIssue(
                    page=rel,
                    code="orphan",
                    detail="page not listed in index.md (run `awf wiki regenerate-index`)",
                )
            )

    return issues


# ---------------------------------------------------------------------------
# Convenience used by hooks.
# ---------------------------------------------------------------------------


def log_event(
    repo_root: str | os.PathLike[str],
    event_type: str,
    summary: str,
) -> None:
    """Shortcut for hook sites: append one log entry, swallowing IOError.

    Hooks must never break the user-facing command if disk is full or
    permissions are odd; the wiki is operational telemetry, not a gate.
    """
    try:
        append_log_entry(repo_root, event_type=event_type, summary=summary)
    except OSError:
        pass


def event_summary_lines(events: Iterable[dict[str, Any]]) -> list[str]:
    """Render an event stream as bullet lines for ad-hoc CLI display."""
    lines: list[str] = []
    for ev in events:
        ts = ev.get("ts", "?")
        kind = ev.get("type", "?")
        payload = ev.get("payload", {})
        if isinstance(payload, dict):
            preview = ", ".join(
                f"{k}={v}" for k, v in payload.items() if k != "violation_paths"
            )
        else:
            preview = repr(payload)
        lines.append(f"- {ts} {kind} :: {preview}")
    return lines
