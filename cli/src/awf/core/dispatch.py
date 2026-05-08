"""Multi-agent dispatch abstraction.

Anywhere we run multiple LLM agents — multi-agent skill modes, WF dual
phases, agent teams, future Stage 1 fan-out — should go through this
interface. The point is to decouple the "where do these N agent calls
actually run" decision (inline thread pool vs. cmux-agent surfaces)
from the orchestration logic that picks workers, builds prompts, and
judges results.

Phase 2 (this commit) wires up ``CmuxDispatch`` against cmux-agent's
``.agent/`` artifact protocol with auto-spawn + lifecycle cleanup.
``cmux_dispatch_available`` and ``select_dispatch`` now require ``cwd``
because the cmux runtime is per-project (each ``.agent/`` is a separate
state machine).
"""
from __future__ import annotations

import os
import shutil
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from awf.core import _cmux_bridge as bridge
from awf.core.agent_runner import AgentResult, run_agent


Strategy = Literal["parallel", "sequential"]
Preference = Literal["auto", "inline", "cmux"]
Lifecycle = Literal["ephemeral", "reusable"]

SURFACE_INLINE = "inline"
SURFACE_CMUX = "cmux"

# Heuristic thresholds for auto selection.
_CMUX_MIN_WORKERS = 2
_CMUX_MAX_WORKERS = 5
_CMUX_MIN_DURATION_SEC = 60.0

# Extra grace added to each worker's timeout to absorb cmux warmup
# (terminal open + AI CLI boot) on first dispatch through a fresh worker.
_CMUX_WARMUP_GRACE_SEC = 30.0


@dataclass(frozen=True)
class WorkerSpec:
    """One agent invocation request.

    Mirrors the arguments accepted by ``run_agent`` plus a role label and
    an optional progress callback. ``add_dirs`` is a tuple so the spec
    stays hashable; callers can pass ``()`` when no extra dirs are needed.
    """

    role: str
    provider: Any
    prompt: str
    timeout_sec: int = 90
    require_json: bool = False
    add_dirs: tuple[str, ...] = ()
    on_progress: Callable[[float, str | None], None] | None = field(
        default=None, compare=False
    )

    def expected_seconds(self) -> float:
        return float(self.timeout_sec)


@dataclass(frozen=True)
class ChainedStep:
    """One step of a chained dispatch where each step's prompt depends on
    the prior steps' results.

    ``factory`` receives ``(prior: list[AgentResult])`` and returns a
    ``WorkerSpec`` for this step, or ``None`` to skip the step (used when
    a provider is unavailable). ``role`` is duplicated outside the spec
    so cmux backends can pin the same worker across steps without invoking
    the factory before its turn.
    """

    role: str
    factory: Callable[[list[AgentResult]], "WorkerSpec | None"] = field(
        compare=False
    )


@dataclass(frozen=True)
class CmuxDispatchOptions:
    """Configuration for ``CmuxDispatch`` derived from provider-config.

    Attributes:
        lifecycle: ``reusable`` keeps spawned workers alive across batches
            so cmux-agent stop cleans up; ``ephemeral`` tears down workers
            awf spawned at the end of each batch.
        role_to_worker: per-role provider/template/flags hints used when
            auto-spawning a missing worker. Looked up by ``spec.role``,
            falling back to a ``"default"`` key, then to bare defaults.
        spawn_timeout_sec: how long to wait for ``cmux-agent spawn`` itself
            (the cmux surface + AI CLI boot is heavier than a normal CLI).
        poll_interval_sec: inbox poll cadence while awaiting results.
    """

    lifecycle: Lifecycle = "reusable"
    role_to_worker: dict[str, dict[str, str]] = field(default_factory=dict)
    spawn_timeout_sec: float = 60.0
    poll_interval_sec: float = 0.5


class MultiAgentDispatch(Protocol):
    """Contract for any multi-agent runtime backend.

    Implementations must:
    - Run ``workers`` honoring ``strategy``
    - Preserve the input order in the returned list (callers rely on this)
    - Tag results with their backend via ``AgentResult`` fields they already
      populate (no schema change needed for Phase 1)
    """

    name: str

    def run(
        self,
        workers: list[WorkerSpec],
        *,
        cwd: str,
        strategy: Strategy = "parallel",
    ) -> list[AgentResult]: ...

    def run_chained(
        self,
        steps: list[ChainedStep],
        *,
        cwd: str,
    ) -> list[AgentResult]:
        """Run ``steps`` sequentially, threading prior results into each
        next step's prompt.

        Each step's ``factory`` is invoked with the list of completed
        ``AgentResult`` so far and must return a ``WorkerSpec`` (or
        ``None`` to skip that step entirely). Implementations preserve
        the input order: skipped steps drop out of the returned list,
        and the remainder appears in the order it ran.
        """
        ...


# ---------------------------------------------------------------------------
# InlineDispatch — wraps the existing ThreadPoolExecutor + run_agent path.
# ---------------------------------------------------------------------------


def _run_single(spec: WorkerSpec, cwd: str) -> AgentResult:
    return run_agent(
        spec.provider,
        spec.prompt,
        spec.role,
        cwd,
        timeout_sec=spec.timeout_sec,
        require_json=spec.require_json,
        add_dirs=list(spec.add_dirs) or None,
        on_progress=spec.on_progress,
    )


class InlineDispatch:
    name = SURFACE_INLINE

    def run(
        self,
        workers: list[WorkerSpec],
        *,
        cwd: str,
        strategy: Strategy = "parallel",
    ) -> list[AgentResult]:
        if not workers:
            return []
        if strategy == "sequential" or len(workers) == 1:
            return [_run_single(spec, cwd) for spec in workers]

        results: list[AgentResult | None] = [None] * len(workers)
        with ThreadPoolExecutor(max_workers=len(workers)) as pool:
            futures = {
                pool.submit(_run_single, spec, cwd): idx
                for idx, spec in enumerate(workers)
            }
            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()
        return [r for r in results if r is not None]

    def run_chained(
        self,
        steps: list[ChainedStep],
        *,
        cwd: str,
    ) -> list[AgentResult]:
        completed: list[AgentResult] = []
        for step in steps:
            spec = step.factory(list(completed))
            if spec is None:
                continue
            completed.append(_run_single(spec, cwd))
        return completed


# ---------------------------------------------------------------------------
# CmuxDispatch — bridges to cmux-agent's `.agent/` artifact protocol.
# ---------------------------------------------------------------------------


class CmuxDispatch:
    """Dispatch backend that drives a cmux-agent run.

    The dispatch contract:
    1. There is an active cmux-agent run in ``cwd/.agent/`` (otherwise raise).
    2. ``awf-orchestrator`` is registered as an ORCHESTRATOR-role agent so
       the cmux-agent broker can route worker results back to its inbox.
    3. For each ``WorkerSpec`` we either reuse an existing matching worker
       or auto-spawn one via ``cmux-agent spawn``.
    4. Dispatch artifacts are written to ``outbox/`` and tagged with a
       per-batch dispatch_id; result artifacts written by workers come back
       through the broker into ``inbox/awf-orchestrator/`` and we poll for
       matching dispatch_ids.
    5. Cleanup runs in a finally block: stale processed/ files are purged,
       and (when lifecycle == "ephemeral") workers awf spawned are torn down.
    """

    name = SURFACE_CMUX

    def __init__(self, options: CmuxDispatchOptions | None = None) -> None:
        self._options = options or CmuxDispatchOptions()

    def run(
        self,
        workers: list[WorkerSpec],
        *,
        cwd: str,
        strategy: Strategy = "parallel",
    ) -> list[AgentResult]:
        if not workers:
            return []

        state = bridge.find_active_run(cwd)
        if state is None:
            raise CmuxDispatchError(
                f"cmux dispatch requested but no active cmux-agent run was "
                f"found in {cwd}. Start one with "
                f"'cmux-agent start --attach-orchestrator' from that "
                f"directory before retrying."
            )

        bridge.ensure_orchestrator_registered(state)

        batch_id = uuid.uuid4().hex[:12]
        spawned_now: list[bridge.WorkerInfo] = []
        try:
            assignments = self._assign_workers(state, workers, cwd, spawned_now)

            if strategy == "sequential":
                payloads = self._run_sequential(state, batch_id, assignments)
            else:
                payloads = self._run_parallel(state, batch_id, assignments)

            return [
                self._to_agent_result(spec, worker, payload, elapsed)
                for spec, worker, payload, elapsed in zip(
                    workers,
                    [a.worker for a in assignments],
                    payloads,
                    [a.elapsed for a in assignments],
                )
            ]
        finally:
            self._cleanup(state, batch_id, spawned_now)

    def run_chained(
        self,
        steps: list[ChainedStep],
        *,
        cwd: str,
    ) -> list[AgentResult]:
        if not steps:
            return []

        state = bridge.find_active_run(cwd)
        if state is None:
            raise CmuxDispatchError(
                f"cmux dispatch requested but no active cmux-agent run was "
                f"found in {cwd}. Start one with "
                f"'cmux-agent start --attach-orchestrator' from that "
                f"directory before retrying."
            )

        bridge.ensure_orchestrator_registered(state)

        batch_id = uuid.uuid4().hex[:12]
        spawned_now: list[bridge.WorkerInfo] = []
        # Pin one worker per role across the chain so a step's terminal
        # carries the prompt history the prior step left behind.
        role_to_worker: dict[str, bridge.WorkerInfo] = {}
        completed: list[AgentResult] = []

        try:
            for seq, step in enumerate(steps):
                spec = step.factory(list(completed))
                if spec is None:
                    continue
                if spec.role != step.role:
                    raise CmuxDispatchError(
                        f"chained step #{seq} declared role {step.role!r} but "
                        f"factory returned spec with role {spec.role!r}"
                    )

                worker = role_to_worker.get(step.role)
                if worker is None:
                    worker = self._assign_one_worker(
                        state, step.role, cwd, spawned_now, set()
                    )
                    role_to_worker[step.role] = worker

                start = time.monotonic()
                bridge.write_dispatch_artifact(
                    state,
                    batch_id=batch_id,
                    worker_idx=seq,
                    recipient=worker.name,
                    role=spec.role,
                    prompt=spec.prompt,
                    require_json=spec.require_json,
                )
                deadline = start + spec.timeout_sec + _CMUX_WARMUP_GRACE_SEC
                raw = bridge.poll_results(
                    state,
                    batch_id=batch_id,
                    deadlines={seq: deadline},
                    poll_interval=self._options.poll_interval_sec,
                )
                elapsed = time.monotonic() - start
                completed.append(
                    self._to_agent_result(spec, worker, raw.get(seq), elapsed)
                )
            return completed
        finally:
            self._cleanup(state, batch_id, spawned_now)

    # -- worker assignment -----------------------------------------------

    def _assign_workers(
        self,
        state: bridge.CmuxRunState,
        workers: list[WorkerSpec],
        cwd: str,
        spawned_now: list[bridge.WorkerInfo],
    ) -> list[_Assignment]:
        used: set[str] = set()
        assignments: list[_Assignment] = []
        for spec in workers:
            worker = self._assign_one_worker(
                state, spec.role, cwd, spawned_now, used
            )
            used.add(worker.name)
            assignments.append(_Assignment(spec=spec, worker=worker))
        return assignments

    def _assign_one_worker(
        self,
        state: bridge.CmuxRunState,
        role: str,
        cwd: str,
        spawned_now: list[bridge.WorkerInfo],
        used: set[str],
    ) -> bridge.WorkerInfo:
        existing = bridge.list_workers(state)
        for cand in existing:
            if cand.role_hint != role or cand.name in used:
                continue
            return cand

        hint = self._options.role_to_worker.get(
            role, self._options.role_to_worker.get("default", {})
        )
        desired_name = f"worker-{role}"
        ok, message = bridge.spawn_worker_subprocess(
            cwd=cwd,
            name=desired_name,
            role=role,
            provider=hint.get("provider"),
            template=hint.get("template"),
            flags=hint.get("flags"),
            timeout_sec=self._options.spawn_timeout_sec,
        )
        if not ok:
            raise CmuxDispatchError(
                f"failed to spawn cmux worker for role {role!r}: {message}"
            )
        # Re-resolve from SQLite — cmux-agent may have appended a numeric
        # suffix on collision and we need the surface_id.
        refreshed = {w.name: w for w in bridge.list_workers(state)}
        picked = refreshed.get(desired_name)
        if picked is None:
            suffixed = sorted(
                (
                    w
                    for w in refreshed.values()
                    if w.name.startswith(desired_name)
                ),
                key=lambda w: w.name,
            )
            picked = suffixed[-1] if suffixed else None
        if picked is None:
            raise CmuxDispatchError(
                f"spawn reported success but no worker row was found "
                f"for role {role!r}"
            )
        bridge.mark_spawned(state, picked.name)
        spawned_now.append(picked)
        return picked

    # -- dispatch + poll -------------------------------------------------

    def _run_parallel(
        self,
        state: bridge.CmuxRunState,
        batch_id: str,
        assignments: list[_Assignment],
    ) -> list[dict | None]:
        start = time.monotonic()
        deadlines: dict[int, float] = {}
        for idx, a in enumerate(assignments):
            bridge.write_dispatch_artifact(
                state,
                batch_id=batch_id,
                worker_idx=idx,
                recipient=a.worker.name,
                role=a.spec.role,
                prompt=a.spec.prompt,
                require_json=a.spec.require_json,
            )
            deadlines[idx] = (
                start + a.spec.timeout_sec + _CMUX_WARMUP_GRACE_SEC
            )

        raw = bridge.poll_results(
            state,
            batch_id=batch_id,
            deadlines=deadlines,
            poll_interval=self._options.poll_interval_sec,
        )
        finished = time.monotonic()
        for idx, a in enumerate(assignments):
            a.elapsed = finished - start
        return [raw.get(idx) for idx in range(len(assignments))]

    def _run_sequential(
        self,
        state: bridge.CmuxRunState,
        batch_id: str,
        assignments: list[_Assignment],
    ) -> list[dict | None]:
        out: list[dict | None] = []
        for idx, a in enumerate(assignments):
            start = time.monotonic()
            bridge.write_dispatch_artifact(
                state,
                batch_id=batch_id,
                worker_idx=idx,
                recipient=a.worker.name,
                role=a.spec.role,
                prompt=a.spec.prompt,
                require_json=a.spec.require_json,
            )
            deadline = start + a.spec.timeout_sec + _CMUX_WARMUP_GRACE_SEC
            raw = bridge.poll_results(
                state,
                batch_id=batch_id,
                deadlines={idx: deadline},
                poll_interval=self._options.poll_interval_sec,
            )
            a.elapsed = time.monotonic() - start
            out.append(raw.get(idx))
        return out

    # -- result conversion -----------------------------------------------

    def _to_agent_result(
        self,
        spec: WorkerSpec,
        worker: bridge.WorkerInfo,
        payload: dict | None,
        elapsed: float,
    ) -> AgentResult:
        provider_name = f"cmux:{worker.name}"
        if payload is None:
            return AgentResult(
                provider_name=provider_name,
                role=spec.role,
                stdout="",
                stderr="cmux dispatch timed out before worker responded",
                returncode=124,
                elapsed_sec=elapsed,
                timed_out=True,
            )

        message = str(payload.get("message", "") or "")
        parsed: dict | None = None
        parse_error = False
        if spec.require_json:
            try:
                import json as _json

                parsed_obj = _json.loads(message)
                parsed = parsed_obj if isinstance(parsed_obj, dict) else None
                if parsed is None:
                    parse_error = True
            except ValueError:
                parse_error = True

        return AgentResult(
            provider_name=provider_name,
            role=spec.role,
            stdout=message,
            stderr="",
            returncode=0,
            elapsed_sec=elapsed,
            timed_out=False,
            parse_error=parse_error,
            parsed=parsed,
        )

    # -- cleanup ---------------------------------------------------------

    def _cleanup(
        self,
        state: bridge.CmuxRunState,
        batch_id: str,
        spawned_now: list[bridge.WorkerInfo],
    ) -> None:
        # Always purge processed/ artifacts for this batch.
        try:
            bridge.remove_processed_for_batch(state, batch_id)
        except Exception:
            pass

        # Tear down workers awf spawned this batch only when ephemeral.
        if self._options.lifecycle == "ephemeral":
            # Re-resolve from SQLite to grab fresh surface_ids.
            refreshed = {w.name: w for w in bridge.list_workers(state)}
            for stub in spawned_now:
                live = refreshed.get(stub.name, stub)
                try:
                    bridge.teardown_worker(state, live)
                except Exception:
                    pass


@dataclass
class _Assignment:
    spec: WorkerSpec
    worker: bridge.WorkerInfo
    elapsed: float = 0.0


class CmuxDispatchError(RuntimeError):
    """Raised when cmux dispatch cannot run (no active run, spawn failure)."""


# ---------------------------------------------------------------------------
# Selection — heuristic + provider-config preference override.
# ---------------------------------------------------------------------------


def cmux_dispatch_available(cwd: str | os.PathLike[str]) -> bool:
    """True only when ``cmux-agent`` is on PATH AND ``cwd`` has an active run.

    Per-project rather than per-host: each ``.agent/`` is its own state
    machine, so "is cmux ready?" is always a question about a specific
    working directory.
    """
    if shutil.which("cmux-agent") is None:
        return False
    return bridge.find_active_run(cwd) is not None


def select_dispatch(
    *,
    worker_count: int,
    cwd: str | os.PathLike[str],
    estimated_seconds: float = _CMUX_MIN_DURATION_SEC,
    preference: Preference = "auto",
    options: CmuxDispatchOptions | None = None,
) -> MultiAgentDispatch:
    """Pick a dispatch backend.

    - ``"inline"`` → always InlineDispatch
    - ``"cmux"`` → CmuxDispatch if available, otherwise InlineDispatch with a
      stderr warning so operators see the fallback
    - ``"auto"`` → CmuxDispatch only when worker_count is 2-5 AND each call
      is expected to take ≥60s AND the backend is available; otherwise
      InlineDispatch
    """
    if preference == SURFACE_INLINE:
        return InlineDispatch()
    if preference == SURFACE_CMUX:
        if cmux_dispatch_available(cwd):
            return CmuxDispatch(options)
        print(
            "warning: dispatch surface_preference=cmux requested but cmux "
            "backend is unavailable; falling back to inline",
            file=sys.stderr,
        )
        return InlineDispatch()

    # auto
    if (
        _CMUX_MIN_WORKERS <= worker_count <= _CMUX_MAX_WORKERS
        and estimated_seconds >= _CMUX_MIN_DURATION_SEC
        and cmux_dispatch_available(cwd)
    ):
        return CmuxDispatch(options)
    return InlineDispatch()


def resolve_preference_from_config(provider_config: dict | None) -> Preference:
    """Read ``dispatch.surface_preference`` from provider-config.json.

    Defaults to ``"auto"`` when the section is missing or malformed.
    """
    section = (provider_config or {}).get("dispatch", {})
    if not isinstance(section, dict):
        return "auto"
    value = str(section.get("surface_preference", "auto")).strip().lower()
    if value in (SURFACE_INLINE, SURFACE_CMUX, "auto"):
        return value  # type: ignore[return-value]
    return "auto"


def resolve_cmux_options_from_config(
    provider_config: dict | None,
) -> CmuxDispatchOptions:
    """Read ``dispatch.{worker_lifecycle,role_to_worker}`` from config.

    Unknown lifecycle values silently fall back to the default ``reusable``.
    """
    section = (provider_config or {}).get("dispatch", {})
    if not isinstance(section, dict):
        return CmuxDispatchOptions()
    lifecycle_raw = str(section.get("worker_lifecycle", "reusable")).strip().lower()
    lifecycle: Lifecycle = (
        lifecycle_raw if lifecycle_raw in ("ephemeral", "reusable") else "reusable"
    )  # type: ignore[assignment]
    role_to_worker_raw = section.get("role_to_worker", {})
    role_to_worker: dict[str, dict[str, str]] = {}
    if isinstance(role_to_worker_raw, dict):
        for role, hints in role_to_worker_raw.items():
            if isinstance(hints, dict):
                role_to_worker[str(role)] = {
                    k: str(v) for k, v in hints.items() if isinstance(v, (str, int))
                }
    return CmuxDispatchOptions(
        lifecycle=lifecycle, role_to_worker=role_to_worker
    )
