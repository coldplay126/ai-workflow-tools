"""Tests for awf.core.wiki_compile — deterministic event → page synthesis.

Synthetic event fixtures are used because real events accumulate at runtime
(events files were empty when this module landed, by design — compile_from_events
must work the day it ships and stay correct as data flows in).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from awf.core.operational_metrics import events_dir, record_event
from awf.core.wiki import lint, read_page, regenerate_index, wiki_root
from awf.core.wiki_compile import (
    DEFAULT_SINCE_DAYS,
    METRIC_METHOD,
    TOPIC_REGISTRY,
    aggregate_dispatch_performance,
    aggregate_dual_strategy_promotions,
    aggregate_scope_check,
    aggregate_stage1_invalidation,
    compile_from_events,
    known_topics,
)


# ---------------------------------------------------------------------------
# Fixture helpers — synthesize JSONL events directly so tests don't depend
# on the higher-level recording paths.
# ---------------------------------------------------------------------------


def _record(tmp_path: Path, *, event_type: str, payload: dict, ts: datetime) -> None:
    record_event(
        tmp_path,
        event_type,
        payload,
        ts=ts.isoformat(),
    )


def _stage1_payload(**overrides) -> dict:
    base = {
        "service": "sample-api",
        "transitive_enabled": True,
        "direct_count": 3,
        "indirect_count": 2,
        "invalidating_count": 4,
        "unchanged_count": 1,
        "deleted_count": 0,
    }
    base.update(overrides)
    return base


def _scope_check_payload(**overrides) -> dict:
    base = {
        "base_branch": "main",
        "planned_count": 8,
        "expanded_count": 12,
        "changed_count": 7,
        "violation_count": 0,
        "violation_paths": [],
        "planned_not_changed_count": 1,
    }
    base.update(overrides)
    return base


def _dispatch_payload(**overrides) -> dict:
    base = {
        "backend": "cmux",
        "strategy": "parallel_evaluate",
        "mode": "cross",
        "worker_count": 2,
        "success_count": 2,
        "timed_out_count": 0,
        "total_seconds": 18.4,
    }
    base.update(overrides)
    return base


def _dual_strategy_payload(phase: str = "review") -> dict:
    return {"phase": phase, "promoted_from": "solo", "promoted_to": "cross"}


# ---------------------------------------------------------------------------
# Registry / known_topics
# ---------------------------------------------------------------------------


def test_known_topics_excludes_analysis_complete():
    topics = known_topics()
    # 4-page selective mapping per design proposal
    assert topics == [
        "dispatch-performance",
        "dual-strategy-promotions",
        "scope-check",
        "stage1-invalidation",
    ]
    # No topic should consume analysis_complete events.
    for spec in TOPIC_REGISTRY.values():
        assert "analysis_complete" not in spec.event_types


# ---------------------------------------------------------------------------
# 0-event behaviour — must be graceful
# ---------------------------------------------------------------------------


def test_compile_with_no_events_writes_no_pages(tmp_path):
    pages = compile_from_events(tmp_path)
    # Every topic reported with skipped_reason; no files on disk.
    assert len(pages) == len(TOPIC_REGISTRY)
    for p in pages:
        assert p.event_count == 0
        assert p.written is False
        assert p.skipped_reason == "no_events_in_window"
        assert not p.path.exists()
    # No operations dir created either.
    assert not (wiki_root(tmp_path) / "operations").exists() or not list(
        (wiki_root(tmp_path) / "operations").glob("*.md")
    )


def test_compile_unknown_topic_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown topic"):
        compile_from_events(tmp_path, topic="not-a-topic")


# ---------------------------------------------------------------------------
# stage1_invalidation aggregator
# ---------------------------------------------------------------------------


def test_compile_stage1_invalidation_writes_page_with_metrics(tmp_path):
    now = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    # 3 runs in the last day — small sample → confidence "low"
    for offset in (3, 2, 1):
        _record(
            tmp_path,
            event_type="stage1_invalidation",
            payload=_stage1_payload(direct_count=4, indirect_count=6, invalidating_count=2, unchanged_count=8),
            ts=now - timedelta(days=offset),
        )

    pages = compile_from_events(tmp_path, now=now, topic="stage1-invalidation")
    assert len(pages) == 1
    page = pages[0]
    assert page.written is True
    assert page.event_count == 3
    assert page.confidence == "low"  # 3 events < 10 → low

    on_disk = read_page(page.path)
    assert on_disk.frontmatter["title"] == "Stage 1 import-graph invalidation"
    assert on_disk.frontmatter["metric_method"] == METRIC_METHOD
    assert on_disk.frontmatter["event_count"] == "3"  # YAML mini-parser stores as str
    assert on_disk.frontmatter["event_types"] == ["stage1_invalidation"]
    # Body must include the derived ratios with concrete values.
    assert "Transitive share" in on_disk.body
    assert "Change density" in on_disk.body
    # 2 invalidating / 10 total = 0.200 — over-invalidation signal
    assert "0.200" in on_disk.body


def test_compile_confidence_promotes_with_volume_and_span(tmp_path):
    now = datetime(2026, 5, 15, tzinfo=timezone.utc)
    # 60 events across 10 days → high confidence
    for i in range(60):
        _record(
            tmp_path,
            event_type="stage1_invalidation",
            payload=_stage1_payload(),
            ts=now - timedelta(days=10 - (i / 60.0) * 10),
        )
    pages = compile_from_events(tmp_path, now=now, topic="stage1-invalidation")
    assert pages[0].event_count == 60
    assert pages[0].confidence == "high"


def test_compile_confidence_medium_band(tmp_path):
    now = datetime(2026, 5, 15, tzinfo=timezone.utc)
    # 15 events across 5 days → medium
    for i in range(15):
        _record(
            tmp_path,
            event_type="stage1_invalidation",
            payload=_stage1_payload(),
            ts=now - timedelta(days=5 - (i / 15.0) * 5),
        )
    pages = compile_from_events(tmp_path, now=now, topic="stage1-invalidation")
    assert pages[0].confidence == "medium"


# ---------------------------------------------------------------------------
# scope_check aggregator
# ---------------------------------------------------------------------------


def test_compile_scope_check_groups_base_branches(tmp_path):
    now = datetime(2026, 5, 15, tzinfo=timezone.utc)
    _record(tmp_path, event_type="scope_check", payload=_scope_check_payload(base_branch="main"), ts=now)
    _record(tmp_path, event_type="scope_check", payload=_scope_check_payload(base_branch="main"), ts=now)
    _record(tmp_path, event_type="scope_check", payload=_scope_check_payload(base_branch="feat/x", violation_count=2), ts=now)

    pages = compile_from_events(tmp_path, now=now, topic="scope-check")
    body = pages[0].body
    assert "| `main` | 2 |" in body
    assert "| `feat/x` | 1 |" in body
    # 1 of 3 had violations → 33.3%
    assert "33.3%" in body


# ---------------------------------------------------------------------------
# dispatch_complete aggregator
# ---------------------------------------------------------------------------


def test_compile_dispatch_groups_by_backend_strategy(tmp_path):
    now = datetime(2026, 5, 15, tzinfo=timezone.utc)
    # cmux × parallel_evaluate: 2 successes, 1 failure across 4 workers total
    _record(tmp_path, event_type="dispatch_complete",
            payload=_dispatch_payload(worker_count=2, success_count=2, timed_out_count=0, total_seconds=10.0),
            ts=now)
    _record(tmp_path, event_type="dispatch_complete",
            payload=_dispatch_payload(worker_count=2, success_count=1, timed_out_count=1, total_seconds=20.0),
            ts=now)
    # inline backend, different strategy
    _record(tmp_path, event_type="dispatch_complete",
            payload=_dispatch_payload(backend="inline", strategy="single", worker_count=1, success_count=1, total_seconds=5.0),
            ts=now)

    pages = compile_from_events(tmp_path, now=now, topic="dispatch-performance")
    body = pages[0].body
    # cmux × parallel_evaluate: success_rate = 3/4 = 0.750, timed_out = 1/4 = 0.250
    assert "| `cmux` | `parallel_evaluate` | 2 | 0.750 | 0.250 |" in body
    # inline × single: success_rate 1.000, mean_seconds 5.00
    assert "| `inline` | `single` | 1 | 1.000 | 0.000 | 5.00" in body


# ---------------------------------------------------------------------------
# dual_strategy_engaged aggregator
# ---------------------------------------------------------------------------


def test_compile_dual_strategy_breaks_down_by_phase(tmp_path):
    now = datetime(2026, 5, 15, tzinfo=timezone.utc)
    for phase in ("review", "review", "review", "verify"):
        _record(tmp_path, event_type="dual_strategy_engaged",
                payload=_dual_strategy_payload(phase), ts=now)
    pages = compile_from_events(tmp_path, now=now, topic="dual-strategy-promotions")
    body = pages[0].body
    assert "Total auto-promotions: **4**" in body
    assert "| `review` | 3 | 75.0% |" in body
    assert "| `verify` | 1 | 25.0% |" in body


# ---------------------------------------------------------------------------
# --since filter
# ---------------------------------------------------------------------------


def test_compile_since_filter_drops_old_events(tmp_path):
    now = datetime(2026, 5, 15, tzinfo=timezone.utc)
    # 2 recent events, 3 old events (60 days ago)
    for offset in (1, 2):
        _record(tmp_path, event_type="stage1_invalidation",
                payload=_stage1_payload(), ts=now - timedelta(days=offset))
    for offset in (60, 65, 70):
        _record(tmp_path, event_type="stage1_invalidation",
                payload=_stage1_payload(), ts=now - timedelta(days=offset))

    pages = compile_from_events(tmp_path, since_days=7, now=now, topic="stage1-invalidation")
    assert pages[0].event_count == 2
    # And no since filter at all → all 5
    pages = compile_from_events(tmp_path, since_days=None, now=now, topic="stage1-invalidation")
    assert pages[0].event_count == 5


# ---------------------------------------------------------------------------
# Multi-topic compile
# ---------------------------------------------------------------------------


def test_compile_all_topics_writes_one_page_per_event_type(tmp_path):
    now = datetime(2026, 5, 15, tzinfo=timezone.utc)
    _record(tmp_path, event_type="stage1_invalidation", payload=_stage1_payload(), ts=now)
    _record(tmp_path, event_type="scope_check", payload=_scope_check_payload(), ts=now)
    _record(tmp_path, event_type="dispatch_complete", payload=_dispatch_payload(), ts=now)
    _record(tmp_path, event_type="dual_strategy_engaged", payload=_dual_strategy_payload(), ts=now)
    # analysis_complete must NOT produce a page (excluded per Option B).
    _record(tmp_path, event_type="analysis_complete",
            payload={"service": "x", "domain": "y", "mode": "precise", "total_seconds": 1.0},
            ts=now)

    pages = compile_from_events(tmp_path, now=now)
    written = [p for p in pages if p.written]
    assert {p.topic for p in written} == {
        "stage1-invalidation",
        "scope-check",
        "dispatch-performance",
        "dual-strategy-promotions",
    }
    # operations/ now contains 4 .md files
    op_dir = wiki_root(tmp_path) / "operations"
    assert sorted(p.name for p in op_dir.glob("*.md")) == [
        "dispatch-performance.md",
        "dual-strategy-promotions.md",
        "scope-check.md",
        "stage1-invalidation.md",
    ]


# ---------------------------------------------------------------------------
# Idempotency + index integration
# ---------------------------------------------------------------------------


def test_compile_is_idempotent_overwrites_in_place(tmp_path):
    now = datetime(2026, 5, 15, tzinfo=timezone.utc)
    _record(tmp_path, event_type="stage1_invalidation", payload=_stage1_payload(), ts=now)

    p1 = compile_from_events(tmp_path, now=now, topic="stage1-invalidation")[0]
    first_text = p1.path.read_text(encoding="utf-8")
    # Second run with same data → byte-identical (apart from last_compiled_at
    # which we override via now).
    p2 = compile_from_events(tmp_path, now=now, topic="stage1-invalidation")[0]
    second_text = p2.path.read_text(encoding="utf-8")
    assert first_text == second_text


def test_compile_regenerates_index_so_lint_stays_clean(tmp_path):
    now = datetime(2026, 5, 15, tzinfo=timezone.utc)
    _record(tmp_path, event_type="stage1_invalidation", payload=_stage1_payload(), ts=now)
    compile_from_events(tmp_path, now=now)
    # Operations page should be in the index → no orphan lint issue.
    issues = lint(tmp_path)
    codes = {i.code for i in issues}
    assert "orphan" not in codes
    assert "missing_provenance" not in codes  # event_window satisfies provenance


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


def test_compile_dry_run_writes_nothing(tmp_path):
    now = datetime(2026, 5, 15, tzinfo=timezone.utc)
    _record(tmp_path, event_type="stage1_invalidation", payload=_stage1_payload(), ts=now)

    pages = compile_from_events(tmp_path, now=now, dry_run=True, topic="stage1-invalidation")
    assert pages[0].event_count == 1
    assert pages[0].written is False
    assert not pages[0].path.exists()
    # Body must still be populated so callers can show it.
    assert "Stage 1 import-graph invalidation" in pages[0].body


# ---------------------------------------------------------------------------
# Malformed event handling
# ---------------------------------------------------------------------------


def test_compile_skips_events_with_unparseable_ts_when_filtering(tmp_path):
    now = datetime(2026, 5, 15, tzinfo=timezone.utc)
    # Append one event with a broken ts via direct file write.
    events_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    f = events_dir(tmp_path) / "2026-05-15.jsonl"
    good = json.dumps({
        "ts": now.isoformat(),
        "type": "stage1_invalidation",
        "payload": _stage1_payload(),
    })
    bad = json.dumps({
        "ts": "not-a-timestamp",
        "type": "stage1_invalidation",
        "payload": _stage1_payload(),
    })
    f.write_text(good + "\n" + bad + "\n", encoding="utf-8")
    # With since_days set, the unparseable-ts event must be dropped, not
    # crash. Only the good event survives.
    pages = compile_from_events(tmp_path, since_days=7, now=now, topic="stage1-invalidation")
    assert pages[0].event_count == 1


# ---------------------------------------------------------------------------
# Aggregator-level unit tests (no disk I/O)
# ---------------------------------------------------------------------------


def test_aggregate_stage1_invalidation_handles_zero_invalidating():
    body = aggregate_stage1_invalidation([
        {"ts": "2026-05-15T00:00:00+00:00", "type": "stage1_invalidation",
         "payload": _stage1_payload(invalidating_count=0, unchanged_count=0)}
    ])
    # Must not divide by zero — change density should render as 0.000.
    assert "0.000" in body


def test_aggregate_scope_check_handles_zero_planned():
    body = aggregate_scope_check([
        {"ts": "2026-05-15T00:00:00+00:00", "type": "scope_check",
         "payload": _scope_check_payload(planned_count=0, expanded_count=0,
                                          planned_not_changed_count=0)}
    ])
    assert "Graph expansion factor" in body


def test_aggregate_dispatch_performance_zero_workers():
    body = aggregate_dispatch_performance([
        {"ts": "2026-05-15T00:00:00+00:00", "type": "dispatch_complete",
         "payload": _dispatch_payload(worker_count=0, success_count=0)}
    ])
    # success_rate computed from 0/0 → 0.000 (no crash)
    assert "0.000" in body


def test_aggregate_dual_strategy_no_payloads_renders_empty_message():
    body = aggregate_dual_strategy_promotions([])
    assert "Total auto-promotions: **0**" in body
    assert "(no promotions recorded)" in body
