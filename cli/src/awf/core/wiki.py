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
PROFILE_MARKER = ".profile"
PROFILE_CONSUMER = "consumer"
PROFILE_SELF_IMPROVEMENT = "self_improvement"
KNOWN_PROFILES = (PROFILE_CONSUMER, PROFILE_SELF_IMPROVEMENT)


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

        provenance_keys = (
            "source_runs",
            "source_commits",
            "context_prs",
            "event_window",
        )
        if not any(page.frontmatter.get(k) for k in provenance_keys):
            issues.append(
                LintIssue(
                    page=rel,
                    code="missing_provenance",
                    detail=(
                        "frontmatter has none of: "
                        + ", ".join(provenance_keys)
                    ),
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


# ---------------------------------------------------------------------------
# Profile — local hint stored at .awf-operations/.profile.
# ---------------------------------------------------------------------------


def _profile_marker_path(repo_root: str | os.PathLike[str]) -> Path:
    return operations_root(repo_root) / PROFILE_MARKER


def read_profile(repo_root: str | os.PathLike[str]) -> str:
    """Return the profile recorded for this repo, or the default ``consumer``.

    The marker is a single-line file written by ``awf wiki init``. We don't
    fall back to ``.awf.toml`` because the storage layer otherwise keeps
    everything self-contained under ``.awf-operations/``; users who want a
    committed, machine-portable profile can wire one via ``--profile`` on
    each wiki command.
    """
    marker = _profile_marker_path(repo_root)
    if not marker.exists():
        return PROFILE_CONSUMER
    try:
        value = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return PROFILE_CONSUMER
    return value if value in KNOWN_PROFILES else PROFILE_CONSUMER


def write_profile(repo_root: str | os.PathLike[str], profile: str) -> Path:
    if profile not in KNOWN_PROFILES:
        raise ValueError(
            f"unknown wiki profile {profile!r}; expected one of {KNOWN_PROFILES}"
        )
    marker = _profile_marker_path(repo_root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(profile + "\n", encoding="utf-8")
    return marker


def slugify(text: str, *, max_len: int = 60) -> str:
    """Filesystem-safe slug: ASCII letters/digits/hyphen only.

    Korean and other multi-byte input is normalized by stripping rather
    than transliterating to keep the implementation dependency-free.
    Falls back to ``decision`` when the input has no usable characters.
    """
    cleaned = re.sub(r"[^A-Za-z0-9\s-]+", "", text)
    cleaned = re.sub(r"\s+", "-", cleaned).strip("-").lower()
    return (cleaned[:max_len].rstrip("-") or "decision")


def decision_template(
    *,
    profile: str,
    title: str,
    context_prs: list[str] | None = None,
    pr_body: str | None = None,
) -> WikiPage:
    """Build a WikiPage with an ADR-style template suited to ``profile``.

    Self-improvement variant frames the page around "Option A vs B"
    architectural choices in awf itself; the consumer variant frames it
    around configuration tuning + rollout decisions in a project that
    *uses* awf. Both share the same frontmatter schema so lint and
    downstream compile work identically.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    fm: dict[str, Any] = {
        "title": title,
        "date": today,
        "status": "proposed",
        "context_prs": context_prs or [],
        "last_compiled_at": datetime.now(timezone.utc).isoformat(),
    }
    if profile == PROFILE_SELF_IMPROVEMENT:
        body = _SELF_IMPROVEMENT_DECISION_BODY
    else:
        body = _CONSUMER_DECISION_BODY
    if pr_body:
        body = body + "\n\n## Source PR excerpt\n\n" + pr_body.strip() + "\n"
    return WikiPage(frontmatter=fm, body=body)


_SELF_IMPROVEMENT_DECISION_BODY = """\
# Decision

(One-sentence statement of what was decided.)

## Context

What invariant or pressure forced this decision? Cite code paths or
prior PRs.

## Options considered

- **Option A — …**: pros / cons.
- **Option B — …**: pros / cons.
- (Add more if relevant.)

## Decision rationale

Why the chosen option wins given the constraints. Reference benchmarks
or design notes that future readers will need.

## Consequences

- **Now**: behavior change observable in next release.
- **Locks-in**: future PRs that rely on this choice.
- **Revisit when**: signal that would invalidate this decision (e.g.,
  new metric exceeds threshold, user demand for the rejected option).
"""

_CONSUMER_DECISION_BODY = """\
# Decision

(One-sentence statement of what was decided for this project's use of awf.)

## Context

What problem in the project triggered this? What awf knob or workflow
is involved?

## Configuration / policy chosen

The concrete change — config snippet, command flag, or workflow rule.

## Rejected alternatives

- **Alternative A — …**: why not.
- **Alternative B — …**: why not.

## Rollout

How this lands (PR, deployment, comms).

## Revisit when

Conditions that would prompt reconsidering — e.g., service growth, new
awf release, change in team size or risk tolerance.
"""


def decision_path(
    repo_root: str | os.PathLike[str],
    *,
    title: str,
    date: str | None = None,
) -> Path:
    """Compute the on-disk path for a decision page.

    Filename pattern is ``<YYYY-MM-DD>-<slug>.md`` so chronological order
    matches lexical order — convenient for ``ls``, ``grep``, and the
    auto-generated index.
    """
    today = date or datetime.now(timezone.utc).date().isoformat()
    slug = slugify(title)
    return wiki_root(repo_root) / "decisions" / f"{today}-{slug}.md"


def starter_directories(profile: str) -> list[str]:
    """Return the wiki/ subdirectories to seed for a given profile.

    The starter dirs differ in their conceptual focus, not their schema —
    every directory still holds the same WikiPage shape with provenance
    frontmatter.
    """
    common = ["decisions", "operations"]
    if profile == PROFILE_SELF_IMPROVEMENT:
        return common + ["concepts"]
    return common + ["services"]


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
