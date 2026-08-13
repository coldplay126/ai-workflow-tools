"""Phase 5 — verify team_runner routes worker execution through dispatch.

These tests focus on the integration boundary: WorkerSpec shape, surface
preference plumbing, and per-mode dispatch strategy. Orchestration
(turn loop, termination, mission building) is covered by test_wf_team.py
and stays untouched.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.agent_runner import AgentResult
from awf.core.team_runner import run_team


# --------------------------------------------------------------------------
# Lightweight fakes — must match test_wf_team.py shape so the runtime
# accepts them via _resolve_provider + run_agent.
# --------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode
        self.usage = None


class _FakeProvider:
    def __init__(self, name: str, output: dict):
        self.name = name
        self._output = json.dumps(output)

    def complete(
        self,
        prompt: str,
        cwd: str | None = None,
        add_dirs: list | None = None,
        timeout_sec: int | None = None,
    ):
        return _FakeResult(self._output)


class _FakeRegistry:
    def __init__(self, providers: dict[str, _FakeProvider]):
        self._providers = providers

    def supports(self, name: str) -> bool:
        return name in self._providers

    def get(self, name: str):
        return self._providers[name]


# --------------------------------------------------------------------------
# Spy dispatch — records the calls team_runner makes so we can verify
# WorkerSpec shape + strategy without booting a real backend.
# --------------------------------------------------------------------------


class _SpyDispatch:
    name = "spy"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, workers, *, cwd, strategy="parallel"):
        self.calls.append({
            "method": "run",
            "strategy": strategy,
            "cwd": cwd,
            "specs": list(workers),
        })
        # Synthesize a PASS AgentResult per spec so downstream blackboard
        # writes succeed and termination evaluates correctly.
        return [
            AgentResult(
                provider_name=getattr(spec.provider, "name", "fake"),
                role=spec.role,
                stdout=json.dumps({"conclusion": "PASS", "findings": []}),
                stderr="",
                returncode=0,
                elapsed_sec=0.01,
                parsed={"conclusion": "PASS", "findings": []},
            )
            for spec in workers
        ]

    def run_chained(self, steps, *, cwd):
        # Not used by team_runner today; included to satisfy the protocol.
        raise NotImplementedError


def _team_config(execution: str, *, max_turns: int = 1) -> dict:
    return {
        "name": "spy-team",
        "roles": [
            {"id": "happy_path", "provider": "fake-a"},
            {"id": "adversarial", "provider": "fake-b"},
        ],
        "execution": execution,
        "max_turns": max_turns,
        "timeout_sec": 30,
    }


def _registry_with_two_providers() -> _FakeRegistry:
    return _FakeRegistry({
        "fake-a": _FakeProvider("fake-a", {"conclusion": "PASS", "findings": []}),
        "fake-b": _FakeProvider("fake-b", {"conclusion": "PASS", "findings": []}),
    })


# --------------------------------------------------------------------------
# Parallel mode — one dispatch call carries all available specs.
# --------------------------------------------------------------------------


def test_parallel_team_routes_all_workers_through_one_dispatch_call():
    spy = _SpyDispatch()
    with tempfile.TemporaryDirectory() as tmp, \
         patch("awf.core.team_runner._resolve_team_dispatch", return_value=spy):
        result = run_team(
            "test",
            "prompt",
            _team_config("parallel"),
            _registry_with_two_providers(),
            tmp,
        )
    assert result.judge_verdict == "PASS"
    # One dispatch call, two specs, parallel strategy.
    [call] = [c for c in spy.calls if len(c["specs"]) == 2]
    assert call["strategy"] == "parallel"
    roles = [s.role for s in call["specs"]]
    assert roles == ["happy_path", "adversarial"]


def test_parallel_team_skips_unavailable_provider_before_dispatch():
    spy = _SpyDispatch()
    # Only fake-a registered; fake-b is referenced by config but missing.
    registry = _FakeRegistry({"fake-a": _FakeProvider("fake-a", {"conclusion": "PASS", "findings": []})})
    with tempfile.TemporaryDirectory() as tmp, \
         patch("awf.core.team_runner._resolve_team_dispatch", return_value=spy):
        run_team("test", "prompt", _team_config("parallel"), registry, tmp)
    # Only one spec reached dispatch; the unavailable role was skipped earlier.
    [call] = spy.calls
    assert [s.role for s in call["specs"]] == ["happy_path"]


def test_parallel_team_emits_no_dispatch_when_all_providers_unavailable():
    spy = _SpyDispatch()
    registry = _FakeRegistry({})
    with tempfile.TemporaryDirectory() as tmp, \
         patch("awf.core.team_runner._resolve_team_dispatch", return_value=spy):
        run_team("test", "prompt", _team_config("parallel"), registry, tmp)
    # No dispatch call at all — the function returns [] before resolving a backend.
    assert spy.calls == []


# --------------------------------------------------------------------------
# Sequential mode — one dispatch call PER worker so each can see prior
# blackboard state at prompt-build time.
# --------------------------------------------------------------------------


def test_sequential_team_dispatches_each_worker_separately():
    spy = _SpyDispatch()
    with tempfile.TemporaryDirectory() as tmp, \
         patch("awf.core.team_runner._resolve_team_dispatch", return_value=spy):
        run_team(
            "test",
            "prompt",
            _team_config("sequential"),
            _registry_with_two_providers(),
            tmp,
        )
    # Two separate dispatch calls, each with a single spec.
    assert len(spy.calls) == 2
    for call in spy.calls:
        assert len(call["specs"]) == 1
    # Order matches role list.
    assert [c["specs"][0].role for c in spy.calls] == ["happy_path", "adversarial"]


def test_sequential_team_passes_per_worker_timeout_to_spec():
    spy = _SpyDispatch()
    with tempfile.TemporaryDirectory() as tmp, \
         patch("awf.core.team_runner._resolve_team_dispatch", return_value=spy):
        # Tight 30s budget split across 2 workers → each gets ≤30s but ≥10s.
        run_team(
            "test",
            "prompt",
            _team_config("sequential"),
            _registry_with_two_providers(),
            tmp,
        )
    timeouts = [c["specs"][0].timeout_sec for c in spy.calls]
    for t in timeouts:
        assert 10 <= t <= 30


# --------------------------------------------------------------------------
# Surface preference plumbing — provider_config flows into select_dispatch.
# --------------------------------------------------------------------------


def test_team_dispatch_resolution_reads_surface_preference_from_config():
    captured: dict = {}

    def fake_resolve(
        *, cwd, worker_count, estimated_seconds, provider_config, workers=None
    ):
        captured["provider_config"] = provider_config
        captured["workers"] = workers
        return _SpyDispatch()

    cfg = {
        "dispatch": {"surface_preference": "inline"},
        "phase_models": {},
    }
    with tempfile.TemporaryDirectory() as tmp, \
         patch("awf.core.team_runner._resolve_team_dispatch", side_effect=fake_resolve):
        run_team(
            "test",
            "prompt",
            _team_config("parallel"),
            _registry_with_two_providers(),
            tmp,
            provider_config=cfg,
        )
    # Provider config including the surface_preference reaches the resolver.
    assert captured["provider_config"] is cfg
    assert [worker.role for worker in captured["workers"]] == [
        "happy_path",
        "adversarial",
    ]


def test_team_dispatch_failure_synthesizes_failure_rows_for_blackboard():
    """If dispatch.run raises, all specs get synthetic failure AgentResults
    so the blackboard termination logic still sees a complete turn."""
    class _ExplodingDispatch:
        name = "explode"
        def run(self, workers, *, cwd, strategy="parallel"):
            raise RuntimeError("backend died")
        def run_chained(self, steps, *, cwd):
            raise NotImplementedError

    with tempfile.TemporaryDirectory() as tmp, \
         patch("awf.core.team_runner._resolve_team_dispatch", return_value=_ExplodingDispatch()):
        result = run_team(
            "test",
            "prompt",
            _team_config("parallel", max_turns=1),
            _registry_with_two_providers(),
            tmp,
        )
    # All workers recorded with failure rows; team verdict reflects the failure.
    assert len(result.agents) == 2
    assert all(a.returncode != 0 for a in result.agents)
    assert all("backend died" in a.stderr for a in result.agents)
