"""Tests for awf.core.operational_metrics — JSONL event persistence."""
from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.operational_metrics import (
    events_dir,
    iter_events,
    operations_root,
    record_event,
    record_scope_check,
    record_stage1_invalidation,
)


def test_record_event_creates_jsonl_under_operations_root(tmp_path):
    target = record_event(tmp_path, "test_event", {"k": "v"})
    assert target.parent == events_dir(tmp_path)
    assert target.suffix == ".jsonl"
    payload = json.loads(target.read_text(encoding="utf-8").strip())
    assert payload["type"] == "test_event"
    assert payload["payload"] == {"k": "v"}
    assert "ts" in payload


def test_record_event_appends_to_same_daily_file(tmp_path):
    record_event(tmp_path, "a", {"i": 1})
    record_event(tmp_path, "b", {"i": 2})
    files = sorted(events_dir(tmp_path).glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    types = [json.loads(line)["type"] for line in lines]
    assert types == ["a", "b"]


def test_record_event_is_concurrency_safe(tmp_path):
    """O_APPEND atomicity must keep concurrent writers from interleaving."""
    workers = 8
    per_worker = 25

    def worker(idx: int) -> None:
        for i in range(per_worker):
            record_event(tmp_path, "concurrent", {"worker": idx, "seq": i})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    files = sorted(events_dir(tmp_path).glob("*.jsonl"))
    total = 0
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            # Each line must round-trip — no torn writes, no interleaving.
            payload = json.loads(line)
            assert payload["type"] == "concurrent"
            total += 1
    assert total == workers * per_worker


def test_record_event_rejects_non_serializable_payload(tmp_path):
    import pytest

    class _NotJson:
        pass

    with pytest.raises(TypeError):
        record_event(tmp_path, "bad", {"obj": _NotJson()})


def test_iter_events_yields_chronological(tmp_path):
    record_event(tmp_path, "first", {"i": 1})
    record_event(tmp_path, "second", {"i": 2})
    events = list(iter_events(tmp_path))
    assert [e["type"] for e in events] == ["first", "second"]


def test_iter_events_skips_malformed_lines(tmp_path):
    target = events_dir(tmp_path)
    target.mkdir(parents=True, exist_ok=True)
    bad = target / "2026-05-01.jsonl"
    bad.write_text(
        '{"ts":"x","type":"ok","payload":{}}\n'
        "{ this is not json\n"
        '{"ts":"y","type":"also_ok","payload":{}}\n',
        encoding="utf-8",
    )
    events = list(iter_events(tmp_path))
    assert [e["type"] for e in events] == ["ok", "also_ok"]


# --------------------------------------------------------------------------
# Typed helpers — just check shape conversion.
# --------------------------------------------------------------------------


@dataclass
class _Stage1Stub:
    target_files: list
    unchanged_files: list
    indirect_paths: tuple
    invalidating_paths: tuple
    deleted_paths: tuple


def test_record_stage1_invalidation_extracts_counts(tmp_path):
    stub = _Stage1Stub(
        target_files=[{"path": "a"}, {"path": "b"}, {"path": "c"}],
        unchanged_files=[{"path": "x"}, {"path": "y"}],
        indirect_paths=("c",),
        invalidating_paths=("a",),
        deleted_paths=("z",),
    )
    record_stage1_invalidation(tmp_path, stub, service="payments", transitive_enabled=True)
    [event] = list(iter_events(tmp_path))
    assert event["type"] == "stage1_invalidation"
    p = event["payload"]
    assert p["service"] == "payments"
    assert p["transitive_enabled"] is True
    assert p["direct_count"] == 2  # 3 targets - 1 indirect
    assert p["indirect_count"] == 1
    assert p["invalidating_count"] == 1
    assert p["unchanged_count"] == 2
    assert p["deleted_count"] == 1


@dataclass
class _ClassificationStub:
    path: str
    status: str = "violation"
    reason: str = "out of scope"


@dataclass
class _ScopeStub:
    base_branch: str
    planned_set: tuple
    expanded_set: tuple
    changed_files: tuple
    violations: tuple
    planned_not_changed: tuple
    classifications: tuple = ()

    @property
    def violation_count(self) -> int:
        return len(self.violations)


def test_record_scope_check_extracts_violation_paths(tmp_path):
    stub = _ScopeStub(
        base_branch="main",
        planned_set=("a", "b"),
        expanded_set=("a", "b", "c"),
        changed_files=("a", "b", "c", "d"),
        violations=(_ClassificationStub("d"),),
        planned_not_changed=(),
    )
    record_scope_check(tmp_path, stub)
    [event] = list(iter_events(tmp_path))
    assert event["type"] == "scope_check"
    p = event["payload"]
    assert p["base_branch"] == "main"
    assert p["planned_count"] == 2
    assert p["violation_count"] == 1
    assert p["violation_paths"] == ["d"]


def test_operations_root_resolves_to_dot_awf_operations(tmp_path):
    assert operations_root(tmp_path) == tmp_path / ".awf-operations"
