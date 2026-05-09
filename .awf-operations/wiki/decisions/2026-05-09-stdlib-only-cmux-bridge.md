---
title: cmux dispatch — stdlib-only bridge instead of cmux_agent import
date: 2026-05-09
status: accepted
context_prs: [#26]
last_compiled_at: 2026-05-09T00:00:00+00:00
source_commits: [95cdaed]
confidence: high
related: [2026-05-09-cmux-dispatch-lifecycle-reusable-default.md]
---
# Decision

`awf.core._cmux_bridge` talks to cmux-agent's `.agent/` filesystem and SQLite directly, and shells out to the `cmux-agent` CLI for spawn/stop. It does not import the `cmux_agent` Python package.

## Context

Phase 2 of the dispatch series (PR #26) needed to drive cmux-agent's broker for real, not the stub left behind by Phase 1 (#25). The natural-looking option — import `cmux_agent.application.AgentRuntime` and reuse its abstractions — would have given the cleanest API surface in awf, but introduced two costs.

`cli/pyproject.toml` declares `requires-python = ">=3.9"`. `cmux-agent/pyproject.toml` declares `requires-python = ">=3.11"`. Importing `cmux_agent.*` would have forced awf-cli's floor up to 3.11 across the board. Both packages run on 3.13 in practice (per the existing memory note about `~/.local/bin/cmux-agent`), but bumping the documented floor is a breaking change to anyone still on 3.9/3.10.

The deeper concern was version skew: `AgentRuntime`, `CmuxAdapter`, and `MessageBroker` are cmux-agent internals. Pinning awf to a specific shape would couple two release cadences. The public surface — the on-disk `.agent/` layout and the `cmux-agent` CLI — is documented and intended for integration.

## Options considered

- **Option A — Direct import of `cmux_agent.*`**: short awf code, but forces `>=3.11` floor and couples awf to cmux-agent internals. Breaks if cmux-agent refactors `AgentRuntime`.
- **Option B — stdlib `sqlite3` + filesystem IO + `cmux-agent` CLI subprocess** (chosen): no Python floor change, integration boundary is the on-disk layout (stable) plus the CLI (versioned). Slight subprocess fork cost on spawn (~100-300ms), acceptable because spawn is per-batch.
- **Option C — Subprocess everything (including event read/write)**: maximally decoupled but loses millisecond-scale event polling needed for dispatch latency.

## Decision rationale

Option B keeps awf-cli's Python floor at 3.9 and treats the `.agent/` layout as the public contract. The subprocess fork cost only applies to `cmux-agent spawn`, which already pays a 10-30 second AI CLI boot cost — the fork is in the noise. SQLite reads against `control-plane.sqlite3` use the same schema cmux-agent itself relies on; if cmux-agent ever changes that schema, both projects break together rather than diverging silently.

The new module `cli/src/awf/core/_cmux_bridge.py` isolates every touch point (find_active_run, list_workers, ensure_orchestrator_registered, write_dispatch_artifact, poll_results, teardown_worker). Tests use a fixture `.agent/` tree without booting cmux-agent.

## Consequences

- **Now**: awf-cli stays installable on Python 3.9. cmux-agent can move to newer Python versions without forcing awf updates.
- **Locks-in**: the SQLite schema for `runs`/`agents`/`messages` is part of the cross-package contract. A schema change in cmux-agent must be coordinated.
- **Revisit when**: cmux-agent ships a public Python API (e.g., `cmux_agent.api`) that we could depend on without coupling to internals, OR when the on-disk layout becomes a maintenance burden.
