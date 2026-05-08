"""Multi-agent dispatch abstraction.

Anywhere we run multiple LLM agents — multi-agent skill modes, WF dual
phases, agent teams, future Stage 1 fan-out — should go through this
interface. The point is to decouple the "where do these N agent calls
actually run" decision (inline thread pool vs. cmux-agent surfaces)
from the orchestration logic that picks workers, builds prompts, and
judges results.

Phase 1 (this commit) ships ``InlineDispatch`` only. ``CmuxDispatch``
exists as a stub so callers can already declare a preference; it
raises ``NotImplementedError`` if actually selected. The cmux backend
lands in a follow-up PR that wires `.agent/` artifact dispatch.
"""
from __future__ import annotations

import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

from awf.core.agent_runner import AgentResult, run_agent


Strategy = Literal["parallel", "sequential"]
Preference = Literal["auto", "inline", "cmux"]

SURFACE_INLINE = "inline"
SURFACE_CMUX = "cmux"

# Heuristic thresholds for auto selection.
_CMUX_MIN_WORKERS = 2
_CMUX_MAX_WORKERS = 5
_CMUX_MIN_DURATION_SEC = 60.0


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
        # All slots are filled because as_completed iterates over every future.
        return [r for r in results if r is not None]


# ---------------------------------------------------------------------------
# CmuxDispatch — stub. Real implementation lands in a follow-up PR that
# bridges this interface to cmux-agent's `.agent/outbox`/`processed/`
# artifact protocol.
# ---------------------------------------------------------------------------


class CmuxDispatch:
    name = SURFACE_CMUX

    def run(
        self,
        workers: list[WorkerSpec],
        *,
        cwd: str,
        strategy: Strategy = "parallel",
    ) -> list[AgentResult]:
        raise NotImplementedError(
            "Cmux dispatch backend is not yet wired up. Set "
            "dispatch.surface_preference = \"inline\" or \"auto\" in "
            "provider-config.json (auto stays on inline until the cmux "
            "backend lands)."
        )


# ---------------------------------------------------------------------------
# Selection — heuristic + provider-config preference override.
# ---------------------------------------------------------------------------


def cmux_dispatch_available() -> bool:
    """True only when both `cmux-agent` is on PATH AND the cmux backend
    is implemented.

    Phase 1 hard-codes the second condition to ``False`` so no caller
    accidentally routes work through the unimplemented stub.
    """
    backend_implemented = False  # flipped to True when CmuxDispatch is real
    if not backend_implemented:
        return False
    return shutil.which("cmux-agent") is not None  # pragma: no cover


def select_dispatch(
    *,
    worker_count: int,
    estimated_seconds: float = _CMUX_MIN_DURATION_SEC,
    preference: Preference = "auto",
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
        if cmux_dispatch_available():
            return CmuxDispatch()
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
        and cmux_dispatch_available()
    ):
        return CmuxDispatch()
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
