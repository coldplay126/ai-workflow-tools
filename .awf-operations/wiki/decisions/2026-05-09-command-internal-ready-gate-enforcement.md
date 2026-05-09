---
context_prs: [#39]
date: 2026-05-09
last_compiled_at: 2026-05-09T12:20:08.846936+00:00
status: accepted
title: Command-internal ready gate enforcement
source_commits: [dd05508]
confidence: high
---
# Decision

Mutating and provider-backed commands now enforce the relevant `awf ready --gate` decision internally by default, with `--no-ready-gate` as the explicit escape hatch.

## Context

PR #38 established a deterministic preflight contract for Claude/Codex entrypoints, but it still depended on callers following the outer wrapper or skill instructions. Direct CLI usage could bypass the gate and run provider-backed analysis, initialize or advance workflows, or write operations wiki pages without the same readiness decision.

The invariant is that the CLI command itself should protect the state transition it performs. External agents can still run preflight first for better UX, but the command should not rely on prose instructions as the only enforcement layer.

## Options considered

- **Option A — enforce ready gates inside selected commands by default** (chosen): applies to `awf analyze`, `awf wf init`, `awf wf next`, and operations wiki write commands. It preserves read-only/dry-run flows and adds `--no-ready-gate` for explicit higher-level wrappers or fixtures.
- **Option B — enforce gates on every subcommand**: stronger in name, but blocks useful inspection paths such as `status`, `log`, `events`, `lint`, `--check`, `--catalog`, and `--dry-run`.
- **Option C — make command enforcement opt-in via config**: safer for compatibility, but it leaves the default path advisory and does not meet the deterministic onboarding goal.

## Decision rationale

Option A closes the common bypass path while keeping the first five minutes usable. A new shared helper in `awf.commands.ready_gate` calls the same `collect_ready_report` and `evaluate_ready_gate` contract as `awf ready --gate`, then emits either human-readable errors or JSON errors for JSON-output commands.

The enforcement boundary is intentionally command-specific:

- `awf analyze` gates provider-backed execution, but not `--dry-run`, `--check`, `--catalog`, `--cycles`, or missing-domain validation.
- `awf wf init` gates workflow initialization.
- `awf wf next` gates provider-backed phase execution, but not `--dry-run`.
- `awf wiki decision`, `regenerate-index`, and non-dry-run `compile` gate operations writes, while `wiki init`, `log`, `events`, and `lint` remain usable.

`--no-ready-gate` is a narrow, visible escape hatch. Test fixtures use it where they intentionally construct synthetic analysis contexts that do not match the repo-level heuristic scan. That keeps production defaults strict without forcing fixtures to masquerade as fully ready repos.

## Consequences

- **Now**: direct CLI execution gets the same deterministic stop conditions as Claude/Codex preflight.
- **Locks-in**: new mutating/provider-backed commands should declare whether they enforce a ready gate, are read-only, or require an explicit exception.
- **Revisit when**: command-specific false positives appear often enough that `evaluate_ready_gate` needs richer intent inputs, such as concrete service/domain or workflow phase.


## Source PR excerpt

#39
Title: feat(cli): enforce ready gates in commands
URL: https://github.com/coldplay126/ai-workflow-tools/pull/39

## Summary
- add a shared internal ready-gate enforcement helper for mutating/provider-backed commands
- enforce ready gates in `awf analyze`, `awf wf init`, `awf wf next`, and operations wiki write commands
- add `--no-ready-gate` as the explicit escape hatch while keeping dry-run/status/read-only flows unblocked

## Test plan
- `cd cli && uv run --group dev pytest -q --ignore=tests/test_e2e_live.py`
- `uv run --project cmux-agent --group dev python -m pytest cmux-agent/tests -q`
- `PYTHONPYCACHEPREFIX=/private/tmp/awf-pycache python3 -m py_compile cli/src/awf/commands/ready_gate.py cli/src/awf/commands/analyze.py cli/src/awf/commands/wf.py cli/src/awf/commands/wiki.py cli/src/awf/cli.py`
- `git diff --check`
