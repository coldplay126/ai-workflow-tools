---
title: cmux dispatch — reusable worker lifecycle as default over ephemeral
date: 2026-05-09
status: accepted
context_prs: [#26]
last_compiled_at: 2026-05-09T00:00:00+00:00
source_commits: [95cdaed]
confidence: medium
related: [2026-05-09-stdlib-only-cmux-bridge.md]
---
# Decision

`CmuxDispatchOptions.lifecycle` defaults to `"reusable"`. Workers spawned by awf are *not* torn down at the end of each dispatch batch; they live for the lifetime of the cmux-agent run and get cleaned up by `cmux-agent stop`.

## Context

A cmux dispatch worker is a cmux terminal pane running an AI CLI (Claude Code, codex, etc.). Booting one costs 10-30 seconds because the AI CLI itself takes that long to initialize. cross strategy's default `WorkerSpec.timeout_sec=90` would lose a meaningful fraction of its budget to warmup if every batch had to spawn fresh workers.

But ephemeral workers have a real upside: clean isolation between batches. Conversation context doesn't leak from one cross run to the next, and if a worker drifts into a bad state, the next batch starts fresh. For consumer projects that run cross sporadically (once per WF gate), the cost trade-off changes.

## Options considered

- **Option A — `ephemeral` default**: tear down spawn-by-awf workers at the end of each batch. Pros: clean isolation. Cons: pays warmup on every batch, dilutes cross's effective per-spec budget.
- **Option B — `reusable` default** (chosen): keep workers alive within the run; let `cmux-agent stop` clean up. Pros: warmup amortized across batches, predictable per-batch latency. Cons: terminal context accumulates, theoretical contamination risk.
- **Option C — `idle_evict` (TTL-based)**: keep workers alive for N minutes after last use. Compromise. Defer.

## Decision rationale

Option B fits the realistic call pattern. cross runs typically come in clusters — a developer iterating on a phase-review will trigger several within a few minutes. Paying 30s warmup on every iteration would dominate end-to-end latency. Workers are AI CLIs that already handle conversation reset themselves (`/clear`, `/new`); contamination is bounded.

`ephemeral` stays available via `provider-config.json::dispatch.worker_lifecycle = "ephemeral"`. The flag is a per-project knob, not hidden behind an env var, so consumers who do want isolation know it's there.

`idle_evict` was punted because it requires a daemon or hook to trigger eviction. That's out of scope for this PR; if a future operations event shows worker reuse leading to drift, we can revisit.

## Consequences

- **Now**: first cross run pays 30s warmup, subsequent runs in the same cmux-agent session reuse workers. cleanup happens at `cmux-agent stop`.
- **Locks-in**: callers can't assume "fresh worker every batch" without explicit config. Tests of cross strategy must not depend on per-batch isolation.
- **Revisit when**: dispatch_complete events show success_count regression after worker reuse, OR a consumer reports cross-batch contamination as a bug. Both are detectable from `awf wiki events --type dispatch_complete`.
