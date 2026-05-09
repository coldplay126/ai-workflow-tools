"""Tests for ``awf wf next`` Phase 4 dual_strategy auto-promotion.

The synthesis policy itself (``synthesize_workflow_multi_provider_results``)
is exercised elsewhere; this file covers the auto-promotion decision —
when ``solo`` becomes ``cross`` for review/verify, when explicit modes
are respected, and when the project disables the behavior via config.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.commands.wf import (
    _DEFAULT_DUAL_STRATEGY_PHASES,
    _maybe_auto_promote_dual_strategy,
    _resolve_dual_strategy_phases,
)


# --------------------------------------------------------------------------
# Config resolution
# --------------------------------------------------------------------------


def test_resolve_dual_strategy_phases_default():
    assert _resolve_dual_strategy_phases({}) == list(_DEFAULT_DUAL_STRATEGY_PHASES)


def test_resolve_dual_strategy_phases_reads_explicit_list():
    cfg = {"wf": {"dual_strategy_phases": ["review"]}}
    assert _resolve_dual_strategy_phases(cfg) == ["review"]


def test_resolve_dual_strategy_phases_explicit_empty_disables():
    cfg = {"wf": {"dual_strategy_phases": []}}
    assert _resolve_dual_strategy_phases(cfg) == []


def test_resolve_dual_strategy_phases_falls_back_on_garbage():
    for bad in ([], {"wf": "off"}, {"wf": {"dual_strategy_phases": "review"}}):
        cfg = bad if isinstance(bad, dict) else {}
        # Either malformed or with wrong type; both must fall back safely.
        result = _resolve_dual_strategy_phases(cfg)
        assert isinstance(result, list)


def test_resolve_dual_strategy_phases_strips_whitespace_and_skips_blanks():
    cfg = {"wf": {"dual_strategy_phases": ["  review  ", "", "verify"]}}
    assert _resolve_dual_strategy_phases(cfg) == ["review", "verify"]


# --------------------------------------------------------------------------
# Auto-promotion decision
# --------------------------------------------------------------------------


def test_auto_promote_solo_to_cross_for_review_when_no_user_mode(tmp_path, capsys):
    promoted, was_promoted = _maybe_auto_promote_dual_strategy(
        user_mode=None,
        phase="review",
        provider_config={},
        repo_root=str(tmp_path),
    )
    assert was_promoted is True
    assert promoted == "cross"
    err = capsys.readouterr().err
    assert "dual_strategy_auto_promote" in err
    assert "review" in err


def test_auto_promote_engages_for_verify(tmp_path):
    promoted, was_promoted = _maybe_auto_promote_dual_strategy(
        user_mode=None,
        phase="verify",
        provider_config={},
        repo_root=str(tmp_path),
    )
    assert was_promoted is True
    assert promoted == "cross"


def test_auto_promote_skips_unrelated_phases(tmp_path):
    for phase in ("plan", "impl", "test", "approve", "done"):
        promoted, was_promoted = _maybe_auto_promote_dual_strategy(
            user_mode=None,
            phase=phase,
            provider_config={},
            repo_root=str(tmp_path),
        )
        assert was_promoted is False, f"unexpected promotion for {phase!r}"
        assert promoted is None


def test_auto_promote_respects_explicit_solo(tmp_path, capsys):
    promoted, was_promoted = _maybe_auto_promote_dual_strategy(
        user_mode="solo",
        phase="review",
        provider_config={},
        repo_root=str(tmp_path),
    )
    assert was_promoted is False
    assert promoted == "solo"
    # No promotion banner should appear.
    err = capsys.readouterr().err
    assert "dual_strategy_auto_promote" not in err


def test_auto_promote_respects_explicit_other_mode(tmp_path):
    for explicit in ("cross", "critical", "precise", "quick"):
        promoted, was_promoted = _maybe_auto_promote_dual_strategy(
            user_mode=explicit,
            phase="review",
            provider_config={},
            repo_root=str(tmp_path),
        )
        assert was_promoted is False
        assert promoted == explicit


def test_auto_promote_off_when_project_disables_all_phases(tmp_path):
    cfg = {"wf": {"dual_strategy_phases": []}}
    promoted, was_promoted = _maybe_auto_promote_dual_strategy(
        user_mode=None,
        phase="review",
        provider_config=cfg,
        repo_root=str(tmp_path),
    )
    assert was_promoted is False
    assert promoted is None


def test_auto_promote_records_telemetry_event(tmp_path):
    _maybe_auto_promote_dual_strategy(
        user_mode=None,
        phase="review",
        provider_config={},
        repo_root=str(tmp_path),
    )
    # operations_metrics should have written a JSONL line for the engagement.
    events_dir = tmp_path / ".awf-operations" / "events"
    files = list(events_dir.glob("*.jsonl"))
    assert files, "expected at least one event file"
    import json
    payloads = [
        json.loads(line)
        for path in files
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    types = [p["type"] for p in payloads]
    assert "dual_strategy_engaged" in types
    engagement = next(p for p in payloads if p["type"] == "dual_strategy_engaged")
    assert engagement["payload"]["phase"] == "review"
    assert engagement["payload"]["promoted_from"] == "solo"
    assert engagement["payload"]["promoted_to"] == "cross"


def test_auto_promote_does_not_record_event_when_explicit_mode(tmp_path):
    _maybe_auto_promote_dual_strategy(
        user_mode="solo",
        phase="review",
        provider_config={},
        repo_root=str(tmp_path),
    )
    events_dir = tmp_path / ".awf-operations" / "events"
    assert not events_dir.exists()


def test_auto_promote_swallows_telemetry_failure(tmp_path, monkeypatch):
    """Telemetry write errors must never block promotion."""
    from awf.core import operational_metrics

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(operational_metrics, "record_event", boom)
    promoted, was_promoted = _maybe_auto_promote_dual_strategy(
        user_mode=None,
        phase="review",
        provider_config={},
        repo_root=str(tmp_path),
    )
    # Promotion still succeeds.
    assert was_promoted is True
    assert promoted == "cross"
