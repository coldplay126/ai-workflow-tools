---
title: dispatch run_chained — extend protocol (Option A) over per-step run() calls (Option B)
date: 2026-05-09
status: accepted
context_prs: [#27]
last_compiled_at: 2026-05-09T00:00:00+00:00
source_commits: [541e3b5]
confidence: high
related: [2026-05-09-cmux-dispatch-lifecycle-reusable-default.md]
---
# Decision

`MultiAgentDispatch` gains a second method `run_chained(steps, *, cwd) -> list[AgentResult]`. critical mode is migrated onto it. Each step is a `ChainedStep(role, factory)` where `factory(prior_results)` builds the next `WorkerSpec` lazily. CmuxDispatch pins one worker per role across the chain so terminal context accumulates the same way it would in a manual chat session.

## Context

Phase 1 (#25) routed cross strategy through `MultiAgentDispatch.run(workers, *, cwd, strategy)` — a fixed-list parallel/sequential dispatch. critical mode (codex precision → sonnet impact → primary judgment) couldn't fit: each step's prompt incorporates earlier outputs, so the spec list isn't known up front.

Two routes to making critical fit the abstraction were available.

## Options considered

- **Option A — extend the protocol with `run_chained`** (chosen): one new method, factory pattern threads `prior_results` into each step, role-pinned workers in cmux backend. Generalizes beyond critical (Phase 5 agent teams' leader→workers handoff lines up exactly).
- **Option B — N×1 calls to existing `run()`**: critical mode itself loops, calling `dispatch.run([single_spec])` once per step and threading prior results outside the dispatch interface. No protocol change. Cons: cmux backend pays per-call batch overhead, worker reuse becomes implicit, and Phase 5 ends up rebuilding the same chain pattern on its own.

## Decision rationale

Phase 5 (agent teams) was the deciding factor. Leader→workers is structurally a chain: the Leader produces a mission, workers execute against it, results come back to the Leader. If we ship Option B now, Phase 5 either rebuilds the chain pattern or routes through Option A retroactively. Better to absorb the abstraction cost once.

The protocol extension is small — one method, one new dataclass — and InlineDispatch's implementation is six lines. CmuxDispatch's chained variant reuses the same poll_results helper that the parallel path uses, just with single-element deadline maps. The role-pinning behavior also gives cmux a real advantage over inline for chained workloads: the same cmux terminal carries Step N's context into Step N+1, which inline can't replicate.

## Consequences

- **Now**: critical mode's three steps are factories that consult `prior_results`. cmux dispatch reuses one worker per role across the chain.
- **Locks-in**: any chain-shaped workload should use `run_chained` rather than `run` with sequential strategy. Phase 5 will follow this pattern.
- **Revisit when**: a workload appears that's neither parallel nor chained (e.g., DAG-shaped dependencies). `run_chained` covers linear chains; DAGs would need a third method or a graph-aware variant.
