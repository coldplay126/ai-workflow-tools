"""Tests for awf.core.dispatch — multi-agent dispatch abstraction."""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.dispatch import (
    BACKEND_CAPABILITIES,
    ChainedStep,
    CmuxDispatch,
    CmuxDispatchError,
    DispatchRoutingConfig,
    DispatchSelectionError,
    InlineDispatch,
    MultiAgentDispatch,
    OmpDispatch,
    OmpDispatchOptions,
    PiDispatch,
    PiDispatchOptions,
    SURFACE_CMUX,
    SURFACE_INLINE,
    SURFACE_PI,
    SURFACE_OMP,
    RoutingRequirements,
    WorkerSpec,
    backend_capabilities,
    cmux_dispatch_available,
    infer_routing_requirements,
    pi_dispatch_available,
    resolve_cmux_options_from_config,
    resolve_preference_from_config,
    resolve_routing_config_from_config,
    select_dispatch,
)
from awf.runners.pi import PiRunnerConfig
from awf.runners.omp import OmpRunnerConfig


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


def _write_fake_pi(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


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


def test_routing_requirements_are_inferred_from_worker_contract_fields():
    spec = _spec(
        "reviewer",
        _FakeProvider("p", "{}"),
        require_json=True,
        agent_type="reviewer",
        output_schema={"type": "object"},
        schema_mode="strict",
        isolated=True,
    )

    required = infer_routing_requirements([spec]).required_capabilities

    assert {
        "structured_output",
        "agent_type",
        "output_schema",
        "schema_mode",
        "strict_schema",
        "isolated",
    }.issubset(required)


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
# InlineDispatch.run_chained
# --------------------------------------------------------------------------


def test_inline_chained_runs_steps_sequentially_with_prior_results():
    captured_priors: list[list[str]] = []

    def factory_for(label: str):
        def _f(prior):
            captured_priors.append([r.role for r in prior])
            return _spec(label, _FakeProvider(f"p-{label}", f'{{"label":"{label}"}}'))
        return _f

    steps = [
        ChainedStep(role="a", factory=factory_for("a")),
        ChainedStep(role="b", factory=factory_for("b")),
        ChainedStep(role="c", factory=factory_for("c")),
    ]
    results = InlineDispatch().run_chained(steps, cwd=".")

    assert [r.role for r in results] == ["a", "b", "c"]
    assert captured_priors == [[], ["a"], ["a", "b"]]


def test_inline_chained_skips_step_when_factory_returns_none():
    def step1(prior):
        return _spec("a", _FakeProvider("p1", "{}"))

    def step2(prior):
        return None  # provider unavailable → skip

    def step3(prior):
        # Step 2 is missing from prior, so the chain shows only step 1's result.
        assert [r.role for r in prior] == ["a"]
        return _spec("c", _FakeProvider("p3", "{}"))

    results = InlineDispatch().run_chained(
        [
            ChainedStep(role="a", factory=step1),
            ChainedStep(role="b", factory=step2),
            ChainedStep(role="c", factory=step3),
        ],
        cwd=".",
    )
    assert [r.role for r in results] == ["a", "c"]


def test_inline_chained_empty_steps_returns_empty_list():
    assert InlineDispatch().run_chained([], cwd=".") == []


# --------------------------------------------------------------------------
# PiDispatch
# --------------------------------------------------------------------------


def test_pi_dispatch_runs_worker_through_pi_print_mode(tmp_path):
    fake_pi = _write_fake_pi(
        tmp_path / "pi",
        "#!/bin/sh\nprintf '%s\\n' '{\"conclusion\":\"PASS\",\"findings\":[]}'\n",
    )
    dispatch = PiDispatch(
        PiDispatchOptions(config=PiRunnerConfig(command=str(fake_pi)))
    )

    results = dispatch.run(
        [
            _spec(
                "plan_conformance",
                _FakeProvider("ignored", "{}"),
                prompt="review",
                require_json=True,
            )
        ],
        cwd=str(tmp_path),
    )

    assert len(results) == 1
    assert results[0].provider_name == SURFACE_PI
    assert results[0].role == "plan_conformance"
    assert results[0].parse_error is False
    assert results[0].conclusion == "PASS"


def test_pi_dispatch_preserves_input_order_under_parallel(tmp_path):
    fake_pi = _write_fake_pi(
        tmp_path / "pi",
        "\n".join([
            "#!/bin/sh",
            'prompt=""',
            'while [ "$#" -gt 0 ]; do',
            '  if [ "$1" = "-p" ]; then shift; prompt="$1"; fi',
            "  shift",
            "done",
            'case "$prompt" in *slow*) sleep 0.05 ;; esac',
            'printf "reply:%s\\n" "$prompt"',
            "",
        ]),
    )
    dispatch = PiDispatch(
        PiDispatchOptions(config=PiRunnerConfig(command=str(fake_pi)))
    )

    results = dispatch.run(
        [
            _spec("first", _FakeProvider("ignored", "{}"), prompt="slow"),
            _spec("second", _FakeProvider("ignored", "{}"), prompt="fast"),
        ],
        cwd=str(tmp_path),
        strategy="parallel",
    )

    assert [r.role for r in results] == ["first", "second"]
    assert [r.stdout for r in results] == ["reply:slow", "reply:fast"]


def test_pi_chained_runs_steps_with_prior_results(tmp_path):
    fake_pi = _write_fake_pi(
        tmp_path / "pi",
        "#!/bin/sh\nprintf chain\n",
    )
    dispatch = PiDispatch(
        PiDispatchOptions(config=PiRunnerConfig(command=str(fake_pi)))
    )
    captured_priors: list[list[str]] = []

    def factory_for(label: str):
        def _f(prior):
            captured_priors.append([r.role for r in prior])
            return _spec(label, _FakeProvider("ignored", "{}"), prompt=label)
        return _f

    results = dispatch.run_chained(
        [
            ChainedStep(role="a", factory=factory_for("a")),
            ChainedStep(role="b", factory=factory_for("b")),
        ],
        cwd=str(tmp_path),
    )

    assert [r.role for r in results] == ["a", "b"]
    assert captured_priors == [[], ["a"]]


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


def test_pi_dispatch_available_uses_configured_command(tmp_path):
    fake_pi = _write_fake_pi(tmp_path / "pi", "#!/bin/sh\n")

    assert pi_dispatch_available(PiRunnerConfig(command=str(fake_pi))) is True
    assert pi_dispatch_available(PiRunnerConfig(command=str(tmp_path / "missing"))) is False


def test_select_dispatch_inline_preference_always_inline(tmp_path):
    selected = select_dispatch(
        worker_count=3, estimated_seconds=300,
        preference="inline", cwd=str(tmp_path),
    )
    assert isinstance(selected, InlineDispatch)


def test_select_dispatch_cmux_preference_reports_unavailable(tmp_path):
    with pytest.raises(DispatchSelectionError) as excinfo:
        select_dispatch(
            worker_count=3,
            estimated_seconds=300,
            preference="cmux",
            cwd=str(tmp_path),
        )

    assert excinfo.value.preference == "cmux"
    assert excinfo.value.status == "unavailable"
    assert "backend is unavailable" in str(excinfo.value)


def test_select_dispatch_pi_preference_uses_pi_when_available(tmp_path):
    fake_pi = _write_fake_pi(tmp_path / "pi", "#!/bin/sh\n")

    selected = select_dispatch(
        worker_count=2,
        estimated_seconds=300,
        preference="pi",
        cwd=str(tmp_path),
        pi_options=PiDispatchOptions(config=PiRunnerConfig(command=str(fake_pi))),
    )

    assert isinstance(selected, PiDispatch)


def test_select_dispatch_pi_preference_reports_unavailable(tmp_path):
    with pytest.raises(DispatchSelectionError) as excinfo:
        select_dispatch(
            worker_count=2,
            estimated_seconds=300,
            preference="pi",
            cwd=str(tmp_path),
            pi_options=PiDispatchOptions(
                config=PiRunnerConfig(command=str(tmp_path / "missing"))
            ),
        )

    assert excinfo.value.preference == "pi"
    assert excinfo.value.status == "unavailable"
    assert "backend is unavailable" in str(excinfo.value)


def test_select_dispatch_explicit_incompatible_preference_fails_closed(tmp_path):
    with pytest.raises(DispatchSelectionError) as excinfo:
        select_dispatch(
            worker_count=2,
            estimated_seconds=300,
            preference="inline",
            cwd=tmp_path,
            requirements=RoutingRequirements(
                frozenset({"native_coordination"})
            ),
        )

    assert excinfo.value.status == "incompatible"
    assert "native_coordination" in str(excinfo.value)


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


def test_backend_capability_descriptors_have_stable_surface_order():
    assert tuple(BACKEND_CAPABILITIES) == ("inline", "cmux", "omp", "pi")
    assert not BACKEND_CAPABILITIES[SURFACE_INLINE].supports(
        {"native_coordination"}
    )


def test_omp_capabilities_are_mode_and_session_sensitive():
    print_caps = backend_capabilities(
        SURFACE_OMP,
        OmpDispatchOptions(
            config=OmpRunnerConfig(
                coordination_surface="print",
                no_session=False,
            )
        ),
    )
    native_ephemeral_caps = backend_capabilities(
        SURFACE_OMP,
        OmpDispatchOptions(
            config=OmpRunnerConfig(
                coordination_surface="native",
                no_session=True,
            )
        ),
    )
    native_persisted_caps = backend_capabilities(
        SURFACE_OMP,
        OmpDispatchOptions(
            config=OmpRunnerConfig(
                coordination_surface="native",
                no_session=False,
            )
        ),
    )

    assert not print_caps.supports(
        {"messaging", "isolation", "durable_followup"}
    )
    assert native_ephemeral_caps.supports(
        {"messaging", "isolation", "cancellation"}
    )
    assert "durable_followup" not in native_ephemeral_caps.capabilities
    assert native_persisted_caps.supports({"durable_followup"})


def test_select_dispatch_infers_native_omp_requirement_from_workers(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "awf.core.dispatch.cmux_dispatch_available", lambda _cwd: False
    )
    monkeypatch.setattr(
        "awf.core.dispatch.omp_dispatch_available", lambda _config: True
    )
    monkeypatch.setattr(
        "awf.core.dispatch.pi_dispatch_available", lambda _config: False
    )
    worker = _spec(
        "reviewer",
        _FakeProvider("p", "{}"),
        output_schema={"type": "object"},
        schema_mode="strict",
    )

    selected = select_dispatch(
        worker_count=1,
        estimated_seconds=30,
        preference="auto",
        cwd=tmp_path,
        workers=[worker],
        omp_options=OmpDispatchOptions(
            config=OmpRunnerConfig(coordination_surface="native")
        ),
    )

    assert isinstance(selected, OmpDispatch)
    assert {"output_schema", "schema_mode"}.issubset(
        selected.selection.required_capabilities
    )


def test_select_dispatch_filters_capabilities_before_workload_heuristic(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "awf.core.dispatch.cmux_dispatch_available", lambda _cwd: True
    )
    selected = select_dispatch(
        worker_count=3,
        estimated_seconds=300,
        preference="auto",
        cwd=tmp_path,
        requirements=RoutingRequirements(frozenset({"add_dirs"})),
        routing=DispatchRoutingConfig(priority=("cmux", "inline"), configured=True),
    )

    assert isinstance(selected, InlineDispatch)
    assert "add_dirs" in selected.selection.excluded_candidates["cmux"][0]
    assert selected.selection_reason == "inline is the highest-priority eligible backend"


def test_select_dispatch_filters_estimated_cost_above_budget(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "awf.core.dispatch.cmux_dispatch_available", lambda _cwd: True
    )
    selected = select_dispatch(
        worker_count=3,
        estimated_seconds=300,
        preference="auto",
        cwd=tmp_path,
        routing=DispatchRoutingConfig(
            estimated_cost={"cmux": 2.0, "inline": 0.5},
            max_cost_budget=1.0,
            priority=("cmux", "inline"),
            configured=True,
        ),
    )

    assert isinstance(selected, InlineDispatch)
    assert "exceeds budget" in selected.selection.excluded_candidates["cmux"][0]
    assert selected.selection.estimated_cost == 0.5


def test_select_dispatch_uses_explicit_priority_for_equal_costs(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "awf.core.dispatch.cmux_dispatch_available", lambda _cwd: True
    )
    policy = DispatchRoutingConfig(
        estimated_cost={"inline": 1.0, "cmux": 1.0},
        max_cost_budget=1.0,
        priority=("inline", "cmux"),
        configured=True,
    )

    selected_names = [
        select_dispatch(
            worker_count=3,
            estimated_seconds=300,
            preference="auto",
            cwd=tmp_path,
            routing=policy,
        ).name
        for _ in range(3)
    ]

    assert selected_names == ["inline", "inline", "inline"]


def test_select_dispatch_without_routing_config_preserves_legacy_heuristic(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "awf.core.dispatch.cmux_dispatch_available", lambda _cwd: True
    )

    long_batch = select_dispatch(
        worker_count=3,
        estimated_seconds=120,
        preference="auto",
        cwd=tmp_path,
    )
    short_batch = select_dispatch(
        worker_count=3,
        estimated_seconds=30,
        preference="auto",
        cwd=tmp_path,
    )

    assert isinstance(long_batch, CmuxDispatch)
    assert isinstance(short_batch, InlineDispatch)


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
# dispatch.routing config resolution
# --------------------------------------------------------------------------


def test_resolve_routing_config_defaults_to_legacy_compatible_policy():
    policy = resolve_routing_config_from_config(None)

    assert policy.configured is False
    assert policy.required_capabilities == frozenset()
    assert policy.estimated_cost == {}
    assert policy.max_cost_budget is None
    assert policy.priority == ()


def test_resolve_routing_config_reads_capability_cost_budget_and_priority():
    policy = resolve_routing_config_from_config(
        {
            "dispatch": {
                "routing": {
                    "required_capabilities": ["structured_output"],
                    "estimated_cost": {"inline": 0.25, "cmux": 1.5},
                    "max_cost_budget": 1.0,
                    "priority": ["inline", "cmux"],
                }
            }
        }
    )

    assert policy.configured is True
    assert policy.required_capabilities == frozenset({"structured_output"})
    assert policy.estimated_cost == {"inline": 0.25, "cmux": 1.5}
    assert policy.max_cost_budget == 1.0
    assert policy.priority == ("inline", "cmux")


def test_resolve_routing_config_breaks_equal_priority_ties_canonically():
    policy = resolve_routing_config_from_config(
        {
            "dispatch": {
                "routing": {
                    "priority": {"pi": 10, "omp": 10, "inline": 1},
                }
            }
        }
    )

    assert policy.priority == ("omp", "pi", "inline")


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


def test_resolve_preference_reads_explicit_pi():
    cfg = {"dispatch": {"surface_preference": "pi"}}
    assert resolve_preference_from_config(cfg) == "pi"


def test_resolve_preference_normalizes_case_and_whitespace():
    for raw, expected in (("  CMUX ", "cmux"), ("  PI ", "pi")):
        cfg = {"dispatch": {"surface_preference": raw}}
        assert resolve_preference_from_config(cfg) == expected


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
    pi: MultiAgentDispatch = PiDispatch()
    assert inline.name == SURFACE_INLINE
    assert cmux.name == SURFACE_CMUX
    assert pi.name == SURFACE_PI
    assert callable(inline.run) and callable(cmux.run) and callable(pi.run)
