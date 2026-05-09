"""Deterministic event → operations wiki page compilation.

Reads ``.awf-operations/events/*.jsonl``, groups by event type, applies a
fixed aggregator per topic, and writes ``<topic>.md`` under
``wiki/operations/``. No external API calls — output is 100% reproducible
from the event log on disk.

Design notes:

- LLM consumption belongs to the *reader* (an agent reading the compiled
  page later), not the compiler. Keeping the compiler stdlib-only keeps
  awf-cli's dependency surface tiny and ADR-citable.
- Topics are 1:1 with event types except ``analysis_complete`` (single
  total_seconds metric carries low narrative value; covered by other
  observability tooling). Cross-event correlation is left to ad-hoc
  ``awf wiki events`` queries.
- Confidence is computed from sample size and time span — high if ≥50
  events ∧ ≥7d span, medium if ≥10 ∧ ≥3d, low otherwise. ``contested`` is
  reserved for human ADR use and never set by this module.
- ``metric_method`` frontmatter key pins schema/aggregator semantics so
  later changes can ship without silently overwriting old pages with new
  meaning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from awf.core.operational_metrics import iter_events
from awf.core.wiki import WikiPage, regenerate_index, wiki_root, write_page

METRIC_METHOD = "deterministic_v1"
DEFAULT_SINCE_DAYS = 90


@dataclass(frozen=True)
class TopicSpec:
    """One compile target — event types it consumes + aggregator function."""

    name: str  # filename slug (no .md)
    title: str  # frontmatter title
    event_types: tuple[str, ...]
    aggregate: Callable[[list[dict]], str]


@dataclass
class CompiledPage:
    """Result of compiling a single topic. ``written=False`` for dry-run or 0-event skip."""

    topic: str
    path: Path
    event_count: int
    event_window: tuple[str, str]  # (start_iso_date, end_iso_date); empty strings if no events
    confidence: str
    body: str
    written: bool = False
    skipped_reason: str = ""


# ---------------------------------------------------------------------------
# Helpers — small numeric utilities; we deliberately avoid statistics module
# for percentiles to keep behavior identical across Python versions.
# ---------------------------------------------------------------------------


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def _mean(values: list[float] | list[int]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _percentile(values: list[float], p: float) -> float:
    """Linear interpolation percentile. ``p`` in [0, 1]."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _confidence(event_count: int, span_days: float) -> str:
    if event_count >= 50 and span_days >= 7:
        return "high"
    if event_count >= 10 and span_days >= 3:
        return "medium"
    return "low"


def _parse_event_ts(ev: dict) -> datetime | None:
    raw = ev.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _event_payload(ev: dict) -> dict:
    payload = ev.get("payload")
    return payload if isinstance(payload, dict) else {}


def _row_distribution(label: str, values: list[int]) -> str:
    """Render a distribution row for the per-run table."""
    if not values:
        return f"| {label} | 0 | 0.00 | 0.00 | 0.00 |"
    floats = [float(v) for v in values]
    return (
        f"| {label} | {sum(values)} | {_mean(values):.2f} | "
        f"{_percentile(floats, 0.5):.2f} | "
        f"{_percentile(floats, 0.95):.2f} |"
    )


# ---------------------------------------------------------------------------
# Aggregators — one per topic. Each takes the matched event list (raw
# JSONL records with ``ts``/``type``/``payload``) and returns the markdown
# body without frontmatter.
# ---------------------------------------------------------------------------


def aggregate_stage1_invalidation(events: list[dict]) -> str:
    payloads = [_event_payload(ev) for ev in events]
    n = len(payloads)
    direct = [int(p.get("direct_count", 0)) for p in payloads]
    indirect = [int(p.get("indirect_count", 0)) for p in payloads]
    invalidating = [int(p.get("invalidating_count", 0)) for p in payloads]
    unchanged = [int(p.get("unchanged_count", 0)) for p in payloads]
    deleted = [int(p.get("deleted_count", 0)) for p in payloads]
    transitive_on = sum(1 for p in payloads if p.get("transitive_enabled"))

    transitive_share = [
        _safe_div(i, d + i) for d, i in zip(direct, indirect)
    ]
    change_density = [
        _safe_div(inv, inv + unc) for inv, unc in zip(invalidating, unchanged)
    ]

    lines = [
        "# Stage 1 import-graph invalidation",
        "",
        "Aggregate of `stage1_invalidation` events emitted by `awf analyze` "
        "runs. Compiled by `awf wiki compile` (`metric_method: "
        f"{METRIC_METHOD}`).",
        "",
        "## Run counts",
        "",
        f"- Total runs: **{n}**",
        f"- Transitive invalidation enabled: {transitive_on} "
        f"({_safe_div(transitive_on, n) * 100:.1f}%)",
        "",
        "## Per-run distributions",
        "",
        "| metric | sum | mean | p50 | p95 |",
        "|---|---:|---:|---:|---:|",
        _row_distribution("direct_count", direct),
        _row_distribution("indirect_count", indirect),
        _row_distribution("invalidating_count", invalidating),
        _row_distribution("unchanged_count", unchanged),
        _row_distribution("deleted_count", deleted),
        "",
        "## Derived ratios (per-run mean)",
        "",
        f"- **Transitive share** (`indirect / (direct+indirect)`): "
        f"**{_mean(transitive_share):.3f}** — fraction of work driven by "
        f"graph reach beyond directly changed files.",
        f"- **Change density** (`invalidating / (invalidating+unchanged)`): "
        f"**{_mean(change_density):.3f}** — fraction of re-checked files "
        f"that produced material changes. Low values (<0.2) are the "
        f"signal for AST-adapter ROI vs the regex import extractor.",
        "",
    ]
    return "\n".join(lines) + "\n"


def aggregate_scope_check(events: list[dict]) -> str:
    payloads = [_event_payload(ev) for ev in events]
    n = len(payloads)
    planned = [int(p.get("planned_count", 0)) for p in payloads]
    expanded = [int(p.get("expanded_count", 0)) for p in payloads]
    changed = [int(p.get("changed_count", 0)) for p in payloads]
    violations = [int(p.get("violation_count", 0)) for p in payloads]
    pnc = [int(p.get("planned_not_changed_count", 0)) for p in payloads]

    violating_runs = sum(1 for v in violations if v > 0)
    expansion_factor = [
        _safe_div(e - pl, pl) if pl else 0.0 for e, pl in zip(expanded, planned)
    ]
    pnc_share = [
        _safe_div(p_, pl) if pl else 0.0 for p_, pl in zip(pnc, planned)
    ]

    base_branch_counts: dict[str, int] = {}
    for p in payloads:
        bb = str(p.get("base_branch") or "?")
        base_branch_counts[bb] = base_branch_counts.get(bb, 0) + 1

    lines = [
        "# WF G5 scope-check",
        "",
        "Aggregate of `scope_check` events emitted by `awf wf scope-check` "
        "runs. Compiled by `awf wiki compile` (`metric_method: "
        f"{METRIC_METHOD}`).",
        "",
        "## Run counts",
        "",
        f"- Total scope-check runs: **{n}**",
        f"- Runs with at least one violation: **{violating_runs}** "
        f"({_safe_div(violating_runs, n) * 100:.1f}%)",
        "",
        "## Per-run distributions",
        "",
        "| metric | sum | mean | p50 | p95 |",
        "|---|---:|---:|---:|---:|",
        _row_distribution("planned_count", planned),
        _row_distribution("expanded_count", expanded),
        _row_distribution("changed_count", changed),
        _row_distribution("violation_count", violations),
        _row_distribution("planned_not_changed_count", pnc),
        "",
        "## Derived ratios (per-run mean)",
        "",
        f"- **Graph expansion factor** "
        f"(`(expanded - planned) / planned`): **{_mean(expansion_factor):.3f}** — "
        f"how much `awf wf expand-scope` adds beyond the user's planned set.",
        f"- **Planned-not-changed share** (`planned_not_changed / planned`): "
        f"**{_mean(pnc_share):.3f}** — fraction of planned files the run "
        f"never actually touched (signal for over-scoping).",
        "",
        "## Base branches",
        "",
    ]
    if base_branch_counts:
        lines.append("| base_branch | runs |")
        lines.append("|---|---:|")
        for bb in sorted(base_branch_counts):
            lines.append(f"| `{bb}` | {base_branch_counts[bb]} |")
    else:
        lines.append("_(no base_branch values recorded)_")
    lines.append("")
    return "\n".join(lines) + "\n"


def aggregate_dispatch_performance(events: list[dict]) -> str:
    payloads = [_event_payload(ev) for ev in events]
    n = len(payloads)

    # Group by (backend, strategy)
    @dataclass
    class _Bucket:
        runs: int = 0
        worker_total: int = 0
        success_total: int = 0
        timed_out_total: int = 0
        seconds: list[float] = field(default_factory=list)

    buckets: dict[tuple[str, str], _Bucket] = {}
    mode_counts: dict[str, int] = {}
    for p in payloads:
        backend = str(p.get("backend") or "?")
        strategy = str(p.get("strategy") or "?")
        mode = str(p.get("mode") or "?")
        key = (backend, strategy)
        b = buckets.setdefault(key, _Bucket())
        b.runs += 1
        b.worker_total += int(p.get("worker_count", 0))
        b.success_total += int(p.get("success_count", 0))
        b.timed_out_total += int(p.get("timed_out_count", 0))
        try:
            b.seconds.append(float(p.get("total_seconds", 0.0)))
        except (TypeError, ValueError):
            pass
        mode_counts[mode] = mode_counts.get(mode, 0) + 1

    lines = [
        "# Multi-agent dispatch performance",
        "",
        "Aggregate of `dispatch_complete` events emitted by `awf wf next` "
        "(cross/critical modes). Compiled by `awf wiki compile` "
        f"(`metric_method: {METRIC_METHOD}`).",
        "",
        "## Run counts",
        "",
        f"- Total dispatch runs: **{n}**",
        "",
        "## By backend × strategy",
        "",
    ]
    if buckets:
        lines.append(
            "| backend | strategy | runs | success_rate | timed_out_rate "
            "| mean_seconds | p95_seconds |"
        )
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        for (backend, strategy), b in sorted(buckets.items()):
            success_rate = _safe_div(b.success_total, b.worker_total)
            timed_out_rate = _safe_div(b.timed_out_total, b.worker_total)
            mean_s = _mean(b.seconds)
            p95_s = _percentile(b.seconds, 0.95)
            lines.append(
                f"| `{backend}` | `{strategy}` | {b.runs} | "
                f"{success_rate:.3f} | {timed_out_rate:.3f} | "
                f"{mean_s:.2f} | {p95_s:.2f} |"
            )
    else:
        lines.append("_(no dispatch_complete events)_")
    lines.append("")
    lines.append("## Mode breakdown")
    lines.append("")
    if mode_counts:
        lines.append("| mode | runs |")
        lines.append("|---|---:|")
        for mode in sorted(mode_counts):
            lines.append(f"| `{mode}` | {mode_counts[mode]} |")
    else:
        lines.append("_(none)_")
    lines.append("")
    return "\n".join(lines) + "\n"


def aggregate_dual_strategy_promotions(events: list[dict]) -> str:
    payloads = [_event_payload(ev) for ev in events]
    n = len(payloads)
    phase_counts: dict[str, int] = {}
    for p in payloads:
        phase = str(p.get("phase") or "?")
        phase_counts[phase] = phase_counts.get(phase, 0) + 1

    lines = [
        "# WF dual_strategy auto-promotions",
        "",
        "Aggregate of `dual_strategy_engaged` events emitted by `awf wf "
        "next` when a phase auto-promotes solo→cross. Compiled by "
        f"`awf wiki compile` (`metric_method: {METRIC_METHOD}`).",
        "",
        f"- Total auto-promotions: **{n}**",
        "",
        "## By phase",
        "",
    ]
    if phase_counts:
        lines.append("| phase | promotions | share |")
        lines.append("|---|---:|---:|")
        for phase in sorted(phase_counts):
            count = phase_counts[phase]
            lines.append(
                f"| `{phase}` | {count} | "
                f"{_safe_div(count, n) * 100:.1f}% |"
            )
    else:
        lines.append("_(no promotions recorded)_")
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Topic registry — 1:1 with event types except analysis_complete (excluded;
# single-metric events covered by general performance tooling).
# ---------------------------------------------------------------------------


TOPIC_REGISTRY: dict[str, TopicSpec] = {
    "stage1-invalidation": TopicSpec(
        name="stage1-invalidation",
        title="Stage 1 import-graph invalidation",
        event_types=("stage1_invalidation",),
        aggregate=aggregate_stage1_invalidation,
    ),
    "scope-check": TopicSpec(
        name="scope-check",
        title="WF G5 scope-check",
        event_types=("scope_check",),
        aggregate=aggregate_scope_check,
    ),
    "dispatch-performance": TopicSpec(
        name="dispatch-performance",
        title="Multi-agent dispatch performance",
        event_types=("dispatch_complete",),
        aggregate=aggregate_dispatch_performance,
    ),
    "dual-strategy-promotions": TopicSpec(
        name="dual-strategy-promotions",
        title="WF dual_strategy auto-promotions",
        event_types=("dual_strategy_engaged",),
        aggregate=aggregate_dual_strategy_promotions,
    ),
}


def known_topics() -> list[str]:
    return sorted(TOPIC_REGISTRY)


# ---------------------------------------------------------------------------
# Compilation orchestrator.
# ---------------------------------------------------------------------------


def _filter_events_for_topic(
    events: list[dict],
    spec: TopicSpec,
    cutoff: datetime | None,
) -> tuple[list[dict], tuple[str, str]]:
    """Return (matched_events, (window_start_date, window_end_date))."""
    wanted = set(spec.event_types)
    matched: list[dict] = []
    timestamps: list[datetime] = []
    for ev in events:
        if ev.get("type") not in wanted:
            continue
        ts = _parse_event_ts(ev)
        if cutoff is not None:
            if ts is None or ts < cutoff:
                continue
        matched.append(ev)
        if ts is not None:
            timestamps.append(ts)
    if not timestamps:
        return matched, ("", "")
    return matched, (
        min(timestamps).date().isoformat(),
        max(timestamps).date().isoformat(),
    )


def _operations_page_path(repo_root: str | Path, topic: str) -> Path:
    return wiki_root(repo_root) / "operations" / f"{topic}.md"


def compile_from_events(
    repo_root: str | Path,
    *,
    since_days: int | None = DEFAULT_SINCE_DAYS,
    topic: str | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    auto_regenerate_index: bool = True,
) -> list[CompiledPage]:
    """Compile per-topic operations wiki pages from the event stream.

    Args:
        repo_root: project root containing ``.awf-operations/``.
        since_days: drop events older than this many days. ``None`` = no
            time filter (use the full retained log).
        topic: if set, only compile that one topic (must be in
            ``TOPIC_REGISTRY``). Otherwise compile all known topics.
        dry_run: compute pages but do not write to disk; ``CompiledPage.written``
            stays False and ``regenerate_index`` is not called.
        now: override "current time" for testing.
        auto_regenerate_index: when False, callers must call
            ``regenerate_index`` themselves (used by tests that assert ordering).

    Returns:
        One ``CompiledPage`` per topic that had ≥1 event in window. Topics
        with no matching events are skipped silently except for the
        ``CompiledPage`` entry with ``skipped_reason='no_events_in_window'``.
    """
    now = now or datetime.now(timezone.utc)
    cutoff: datetime | None = (
        None if since_days is None else now - timedelta(days=since_days)
    )

    if topic is not None and topic not in TOPIC_REGISTRY:
        raise ValueError(
            f"unknown topic {topic!r}; expected one of {known_topics()}"
        )

    selected_topics = (
        [TOPIC_REGISTRY[topic]] if topic else list(TOPIC_REGISTRY.values())
    )

    all_events = list(iter_events(repo_root))

    pages: list[CompiledPage] = []
    wrote_any = False
    for spec in selected_topics:
        matched, window = _filter_events_for_topic(all_events, spec, cutoff)
        body = spec.aggregate(matched)
        if not matched:
            pages.append(
                CompiledPage(
                    topic=spec.name,
                    path=_operations_page_path(repo_root, spec.name),
                    event_count=0,
                    event_window=window,
                    confidence="low",
                    body=body,
                    written=False,
                    skipped_reason="no_events_in_window",
                )
            )
            continue
        span_days = _span_days(window)
        confidence = _confidence(len(matched), span_days)
        path = _operations_page_path(repo_root, spec.name)
        page = WikiPage(
            frontmatter={
                "title": spec.title,
                "last_compiled_at": now.isoformat(),
                "event_window": list(window),
                "event_count": len(matched),
                "event_types": list(spec.event_types),
                "metric_method": METRIC_METHOD,
                "confidence": confidence,
            },
            body=body,
        )
        if not dry_run:
            write_page(path, page)
            wrote_any = True
        pages.append(
            CompiledPage(
                topic=spec.name,
                path=path,
                event_count=len(matched),
                event_window=window,
                confidence=confidence,
                body=body,
                written=not dry_run,
            )
        )

    if wrote_any and auto_regenerate_index and not dry_run:
        regenerate_index(repo_root)

    return pages


def _span_days(window: tuple[str, str]) -> float:
    start, end = window
    if not start or not end:
        return 0.0
    try:
        s = datetime.fromisoformat(start)
        e = datetime.fromisoformat(end)
    except ValueError:
        return 0.0
    return max((e - s).total_seconds() / 86400.0, 0.0)
