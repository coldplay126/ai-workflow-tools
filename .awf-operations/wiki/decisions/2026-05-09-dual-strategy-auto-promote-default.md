---
title: WF dual_strategy — auto-promote solo→cross for review/verify by default
date: 2026-05-09
status: accepted
context_prs: [#30]
last_compiled_at: 2026-05-09T00:00:00+00:00
source_commits: []
confidence: high
related: [2026-05-09-run-chained-option-a.md]
---
# Decision

`awf wf next` auto-promotes execution mode from `solo` to `cross` when the user did not pass `--mode` AND the phase is in `wf.dual_strategy_phases` (default `["review", "verify"]`). Explicit `--mode solo` remains the documented opt-out.

## Context

`_PHASE_SYNTHESIS_PATTERNS` (cli/src/awf/commands/wf.py:983) had been declaring `parallel_evaluate` for review/verify phases since Phase 1, and `synthesize_workflow_multi_provider_results` (cli/src/awf/core/judge.py:143) was already implementing the corresponding combination policy with phase-specific selection (review = coverage-based, verify = compliance-based, conflict-recovery). But the multi-agent block in `run_wf_next` was guarded by `if exec_mode and exec_mode != "solo"`, so users who never typed `--mode cross` got none of that consensus benefit.

The synthesis policy is the part that's hard to get right and we already had it. The remaining piece was just turning it on by default for the two phases it was designed for.

## Options considered

- **Option A — Auto-promote by default for review/verify** (chosen): when `args.mode is None` and phase ∈ `dual_strategy_phases`, set `exec_mode = "cross"` and emit a stderr banner. Keep explicit `--mode solo` as opt-out.
- **Option B — Strict opt-in via flag**: require `--dual-strategy` or a config setting to engage. Pros: zero behavior change for existing users. Cons: defeats the point — review/verify gates are exactly where users *should* want the second evaluator, but most won't read docs to discover the flag.
- **Option C — Override even explicit `--mode solo`**: ignore the user's choice when phase qualifies. Pros: maximum consistency. Cons: violates least-surprise; explicit user input must always win.
- **Option D — Add a new dispatch path entirely** (e.g., `MultiAgentDispatch.run_dual`): a third method alongside `run` and `run_chained`. Pros: cleaner separation. Cons: review/verify already work end-to-end via `cross`; another method just adds API surface.

## Decision rationale

Option A wins because the synthesis policy was tuned for these two phases specifically — coverage-based selection for review and compliance-based selection for verify aren't generic patterns, they're phase-aware logic that has been in `judge.py` since the cross migration. Forcing users to opt in via flag means most projects never see that benefit.

Option C was rejected to preserve the explicit-input-wins invariant. The opt-out path through `--mode solo` is one CLI flag away — no project that prefers single-evaluator review is locked in.

Option D was rejected because it would duplicate the existing cross dispatch with no semantic gain. The cross strategy with codex+sonnet roles is exactly what dual_strategy needs; adding `run_dual` would just be cross with a different name.

## Consequences

- **Now**: `awf wf next` running review/verify without `--mode` produces a cross dispatch (codex + sonnet in parallel), funnels through `synthesize_workflow_multi_provider_results`, and emits a `dual_strategy_engaged` operations event for telemetry.
- **Locks-in**: review/verify gate flow is implicitly multi-evaluator. Tests and tooling that depend on single-evaluator output for these phases must explicitly pass `--mode solo`.
- **Revisit when**: telemetry shows that auto-promotion's added latency (one extra provider call) outweighs the quality gain — measurable from `dispatch_complete` event success_count by phase. Or when a third phase (e.g., `done`) develops a phase-aware synthesis selector and qualifies for auto-promotion.
