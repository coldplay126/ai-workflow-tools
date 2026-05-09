---
title: team_runner — migrate to dispatch.run, not run_chained
date: 2026-05-09
status: accepted
context_prs: [#31]
last_compiled_at: 2026-05-09T00:00:00+00:00
source_commits: []
confidence: high
related: [2026-05-09-run-chained-option-a.md, 2026-05-09-cmux-dispatch-lifecycle-reusable-default.md]
---
# Decision

team_runner's parallel mode now uses `dispatch.run(specs, strategy="parallel")` and its sequential mode uses per-worker `dispatch.run([spec], strategy="sequential")` calls. **It does not use `run_chained`** even though Phase 3's memo originally framed Phase 5 around the leader→workers chained handoff.

## Context

Phase 5 of the dispatch series was supposed to bring agent teams onto the `MultiAgentDispatch` abstraction, mirroring how Phase 1 (cross) and Phase 3 (critical) had migrated. The Phase 3 memo specifically said `run_chained` would be reused for the leader→workers handoff in Phase 5. When the actual code was inspected, two things contradicted that hypothesis.

First, team_runner's sequential prior threading is **blackboard-mediated, not result-list-mediated**. `_build_worker_prompt(bb, turn, role_cfg)` calls `_gather_discussion_context(bb, turn, role_id)`, which reads earlier workers' outputs from the blackboard. The factory pattern in `run_chained` threads `prior_results: list[AgentResult]` directly — a different data shape and different write semantics (the blackboard is a side-effect store, not a return-value store).

Second, team workers have **distinct roles** (`happy_path` vs `adversarial`, `spec_writer` vs `constitution_reviewer`, etc.). cmux dispatch's role-pinning — the main reason `run_chained` exists for cmux — pins a single worker per role across a chain so terminal context accumulates. With distinct roles, every chain step would spawn a different cmux surface, defeating the optimization.

## Options considered

- **Option A — `run_chained` for sequential, `run` for parallel**: matches the Phase 3 memo. Requires a factory that does blackboard side-effects (persist prior result before reading state for prompt build) — a code smell because `run_chained` was designed for pure prior_results threading.
- **Option B — `dispatch.run` for both** (chosen): parallel uses one bulk call; sequential uses one call per worker so each can build its prompt against the latest blackboard state. No new abstraction, no factory side-effects.
- **Option C — Skip the migration entirely**: keep `ThreadPoolExecutor + run_agent` directly. Pros: zero risk. Cons: team workers wouldn't honor `dispatch.surface_preference` and wouldn't emit `dispatch_complete` telemetry, breaking consistency with cross/critical that *do* go through dispatch.

## Decision rationale

Option B keeps the existing per-turn loop in team_runner unchanged — including `_build_worker_prompt`, blackboard writes, and event emission — while routing each worker call through `dispatch.run`. Team workers now honor `provider-config.json::dispatch.surface_preference` like every other multi-agent path, and `dispatch_complete` events are recorded automatically.

Sequential mode pays a small overhead (N dispatch calls instead of one), but the alternative (single `run_chained` call with side-effecting factory) was strictly worse: it forced state mutation inside a callback that's supposed to be a pure prompt-builder, and it didn't unlock cmux role-pinning anyway. The Phase 3 memo's `run_chained` hypothesis was framed before the blackboard-mediated nature of team prior threading was front-of-mind; this ADR records the correction.

The original Phase 3 memo entry stays as-is rather than being rewritten — the hypothesis was a reasonable starting point, and recording its rejection here gives future readers the trail.

## Consequences

- **Now**: team mode workers participate in dispatch backend selection (inline vs cmux). `dispatch_complete` events fire for team turns, completing the operations data picture for multi-agent paths.
- **Locks-in**: dispatch backend changes that affect single-spec calls also affect team sequential mode. Per-worker dispatch overhead exists but is bounded (one fork per worker on cmux backend).
- **Revisit when**: `run_chained` gains a hook for between-step side effects, OR a future agent-team variant adopts non-distinct roles (e.g., 3 reviewers all using the same `reviewer` role) where cmux pinning would apply. In that case Option A becomes attractive and the choice should be re-examined.
