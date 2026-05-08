"""Tests for awf.core.dispatch — multi-agent dispatch abstraction."""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.dispatch import (
    CmuxDispatch,
    CmuxDispatchError,
    InlineDispatch,
    MultiAgentDispatch,
    SURFACE_CMUX,
    SURFACE_INLINE,
    WorkerSpec,
    cmux_dispatch_available,
    resolve_cmux_options_from_config,
    resolve_preference_from_config,
    select_dispatch,
)


# --------------------------------------------------------------------------
# Fake provider — minimal subset of what run_agent expects
# --------------------------------------------------------------------------


@dataclass
class _Result:
    stdout: str
    stderr: str = ""
    returncode: int = 0
    usage: object | None = None


class _FakeProvider:
    """Provider stub. ``complete`` returns ``stdout`` after ``delay`` seconds."""

    def __init__(self, name: str, stdout: str, *, delay: float = 0.0) -> None:
        self.name = name
        self._stdout = stdout
        self._delay = delay

    def complete(self, prompt, *, cwd=None, add_dirs=None):
        if self._delay:
            time.sleep(self._delay)
        return _Result(stdout=self._stdout)


def _spec(role: str, provider: _FakeProvider, **kw) -> WorkerSpec:
    defaults = {"prompt": "test", "timeout_sec": 90, "require_json": False}
    defaults.update(kw)
    return WorkerSpec(role=role, provider=provider, **defaults)


# --------------------------------------------------------------------------
# WorkerSpec
# --------------------------------------------------------------------------


def test_worker_spec_expected_seconds_matches_timeout():
    spec = _spec("r", _FakeProvider("p", "{}"), timeout_sec=42)
    assert spec.expected_seconds() == 42.0


def test_worker_spec_is_hashable_when_callbacks_omitted():
    spec = _spec("r", _FakeProvider("p", "{}"))
    # Frozen + no callable in compare fields → hashable.
    assert {spec}  # set() raises if not hashable


# --------------------------------------------------------------------------
# InlineDispatch
# --------------------------------------------------------------------------


def test_inline_dispatch_runs_single_worker_serially():
    dispatch = InlineDispatch()
    results = dispatch.run(
        [_spec("alpha", _FakeProvider("alpha-p", '{"ok": true}'))],
        cwd=".",
    )
    assert len(results) == 1
    assert results[0].provider_name == "alpha-p"
    assert results[0].role == "alpha"


def test_inline_dispatch_preserves_input_order_under_parallel():
    dispatch = InlineDispatch()
    # Stagger delays so completion order != input order.
    workers = [
        _spec("first", _FakeProvider("p1", "{}", delay=0.05)),
        _spec("second", _FakeProvider("p2", "{}", delay=0.0)),
        _spec("third", _FakeProvider("p3", "{}", delay=0.02)),
    ]
    results = dispatch.run(workers, cwd=".", strategy="parallel")
    assert [r.role for r in results] == ["first", "second", "third"]


def test_inline_dispatch_actually_parallelizes():
    dispatch = InlineDispatch()
    workers = [
        _spec(f"r{i}", _FakeProvider(f"p{i}", "{}", delay=0.1)) for i in range(4)
    ]
    started = time.monotonic()
    dispatch.run(workers, cwd=".", strategy="parallel")
    elapsed = time.monotonic() - started
    # Sequential would take 0.4s+; parallel must be substantially faster.
    assert elapsed < 0.3, f"parallel took too long: {elapsed:.2f}s"


def test_inline_dispatch_sequential_runs_serially():
    dispatch = InlineDispatch()
    order: list[str] = []
    lock = threading.Lock()

    class _OrderedProvider:
        def __init__(self, name, role):
            self.name = name
            self.role = role

        def complete(self, prompt, *, cwd=None, add_dirs=None):
            with lock:
                order.append(self.role)
            time.sleep(0.02)
            with lock:
                order.append(self.role + "-end")
            return _Result(stdout="{}")

    workers = [
        WorkerSpec(role="a", provider=_OrderedProvider("p1", "a"), prompt="t"),
        WorkerSpec(role="b", provider=_OrderedProvider("p2", "b"), prompt="t"),
    ]
    InlineDispatch().run(workers, cwd=".", strategy="sequential")

    # In sequential mode we should see the start/end of "a" before "b" begins.
    assert order == ["a", "a-end", "b", "b-end"]


def test_inline_dispatch_handles_empty_workers():
    assert InlineDispatch().run([], cwd=".") == []


# --------------------------------------------------------------------------
# CmuxDispatch — surface-level checks (full integration in test_dispatch_cmux)
# --------------------------------------------------------------------------


def test_cmux_dispatch_raises_clear_error_when_no_active_run(tmp_path):
    import pytest

    # No .agent/ directory exists in tmp_path → must surface a clear error.
    with pytest.raises(CmuxDispatchError) as excinfo:
        CmuxDispatch().run(
            [_spec("x", _FakeProvider("p", "{}"))], cwd=str(tmp_path)
        )
    msg = str(excinfo.value)
    assert "cmux-agent start" in msg
    assert str(tmp_path) in msg


# --------------------------------------------------------------------------
# Selection — heuristic + preference
# --------------------------------------------------------------------------


def test_cmux_dispatch_unavailable_when_no_active_run(tmp_path):
    # Without an active run in cwd, cmux is unavailable regardless of PATH.
    assert cmux_dispatch_available(str(tmp_path)) is False


def test_select_dispatch_inline_preference_always_inline(tmp_path):
    selected = select_dispatch(
        worker_count=3, estimated_seconds=300,
        preference="inline", cwd=str(tmp_path),
    )
    assert isinstance(selected, InlineDispatch)


def test_select_dispatch_cmux_preference_falls_back_when_unavailable(capsys, tmp_path):
    selected = select_dispatch(
        worker_count=3, estimated_seconds=300,
        preference="cmux", cwd=str(tmp_path),
    )
    assert isinstance(selected, InlineDispatch)
    err = capsys.readouterr().err
    assert "cmux" in err and "inline" in err


def test_select_dispatch_auto_picks_inline_when_cmux_unavailable(tmp_path):
    selected = select_dispatch(
        worker_count=3, estimated_seconds=120,
        preference="auto", cwd=str(tmp_path),
    )
    assert isinstance(selected, InlineDispatch)


def test_select_dispatch_auto_picks_inline_for_short_work(tmp_path):
    selected = select_dispatch(
        worker_count=3, estimated_seconds=30,
        preference="auto", cwd=str(tmp_path),
    )
    assert isinstance(selected, InlineDispatch)


def test_select_dispatch_auto_picks_inline_for_single_worker(tmp_path):
    selected = select_dispatch(
        worker_count=1, estimated_seconds=300,
        preference="auto", cwd=str(tmp_path),
    )
    assert isinstance(selected, InlineDispatch)


def test_select_dispatch_auto_picks_inline_for_large_worker_pool(tmp_path):
    selected = select_dispatch(
        worker_count=10, estimated_seconds=300,
        preference="auto", cwd=str(tmp_path),
    )
    assert isinstance(selected, InlineDispatch)


# --------------------------------------------------------------------------
# resolve_cmux_options_from_config
# --------------------------------------------------------------------------


def test_resolve_cmux_options_defaults_to_reusable():
    opts = resolve_cmux_options_from_config(None)
    assert opts.lifecycle == "reusable"
    assert opts.role_to_worker == {}


def test_resolve_cmux_options_reads_lifecycle_and_role_map():
    cfg = {
        "dispatch": {
            "worker_lifecycle": "ephemeral",
            "role_to_worker": {
                "plan_conformance": {"provider": "codex", "template": "review"},
            },
        }
    }
    opts = resolve_cmux_options_from_config(cfg)
    assert opts.lifecycle == "ephemeral"
    assert opts.role_to_worker == {
        "plan_conformance": {"provider": "codex", "template": "review"},
    }


def test_resolve_cmux_options_falls_back_on_unknown_lifecycle():
    cfg = {"dispatch": {"worker_lifecycle": "sideways"}}
    assert resolve_cmux_options_from_config(cfg).lifecycle == "reusable"


# --------------------------------------------------------------------------
# Config preference resolution
# --------------------------------------------------------------------------


def test_resolve_preference_defaults_to_auto_when_section_missing():
    assert resolve_preference_from_config({}) == "auto"
    assert resolve_preference_from_config(None) == "auto"


def test_resolve_preference_reads_explicit_inline():
    cfg = {"dispatch": {"surface_preference": "inline"}}
    assert resolve_preference_from_config(cfg) == "inline"


def test_resolve_preference_reads_explicit_cmux():
    cfg = {"dispatch": {"surface_preference": "cmux"}}
    assert resolve_preference_from_config(cfg) == "cmux"


def test_resolve_preference_normalizes_case_and_whitespace():
    cfg = {"dispatch": {"surface_preference": "  CMUX "}}
    assert resolve_preference_from_config(cfg) == "cmux"


def test_resolve_preference_falls_back_to_auto_on_garbage():
    for value in ("sideways", 42, None):
        cfg = {"dispatch": {"surface_preference": value}}
        assert resolve_preference_from_config(cfg) == "auto"


def test_resolve_preference_handles_malformed_section():
    cfg = {"dispatch": "off"}
    assert resolve_preference_from_config(cfg) == "auto"


# --------------------------------------------------------------------------
# Protocol compliance
# --------------------------------------------------------------------------


def test_inline_and_cmux_satisfy_protocol():
    # Smoke check that both backends declare the expected interface.
    inline: MultiAgentDispatch = InlineDispatch()
    cmux: MultiAgentDispatch = CmuxDispatch()
    assert inline.name == SURFACE_INLINE
    assert cmux.name == SURFACE_CMUX
    assert callable(inline.run) and callable(cmux.run)
