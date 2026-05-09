---
context_prs: [#38]
date: 2026-05-09
last_compiled_at: 2026-05-09T08:37:13.132863+00:00
status: accepted
title: Deterministic ready preflight gates
source_commits: [07e32ea]
confidence: high
---
# Decision

`awf ready --gate` is the deterministic preflight contract for Claude/Codex entrypoints. It returns `allow`, `dry_run_only`, or `block` in JSON and uses non-zero exit codes for non-allow decisions.

## Context

PR #37 made `awf ready` the first repo-level automation check, but it was still advisory: an agent could read the report and continue based on natural-language interpretation. That is too weak for first-use and mid-workflow automation because the failure mode is exactly "the model says this looks okay" and then calls a provider or mutates `.workflow/`.

The desired boundary is machine-enforced: Claude Code skills, the Codex host runner, and copied `AGENTS.md` snippets should all consult the same CLI decision and obey the exit code before analysis, workflow execution, or operations-wiki writes.

## Options considered

- **Option A — add `awf ready --gate` as an external preflight contract** (chosen): small extension to the existing readiness report; works for Claude skills, Codex runner, and shell usage; keeps the first check read-only. The tradeoff is that callers can still bypass it if they ignore the entrypoint contract.
- **Option B — enforce readiness inside every mutating command**: stronger against bypass, but higher blast radius. `awf analyze`, `awf wf next`, and wiki commands have existing automation and tests that would need command-specific bypass semantics.
- **Option C — add a project policy file before gating**: flexible long-term, but too much schema surface for the immediate onboarding problem.

## Decision rationale

Option A strengthens the agent boundary without turning all existing commands into policy engines. The gate decisions are derived from the same structured report as `awf ready`, so the human and machine views cannot drift.

The gate names map to real automation intents: `inspect`, `analysis`, `workflow-init`, `workflow-run`, and `operations`. `block` is reserved for deterministic missing prerequisites such as no analysis unit, no `.workflow/state.json`, missing workflow skills, or no operations profile. Provider `caution` is allowed because CLI auth is not yet machine-verifiable for `claude-code`/`codex`; known provider `blocked` status downgrades provider-backed work to `dry_run_only`.

The Codex runner now calls the gate itself before status/dispatch/prompt/secondary execution. Claude skills and snippets document the same exit-code contract so first-use behavior is tied to a CLI result rather than prose.

## Consequences

- **Now**: Claude/Codex entrypoints can stop on `awf ready --gate` exit codes before provider execution or workflow progression.
- **Locks-in**: readiness gates are intent-level, not command-specific. Future entrypoints should consume this contract first instead of inventing separate checks.
- **Revisit when**: live auth probes can promote CLI providers from caution to ready, or when bypasses become common enough to justify command-internal enforcement.


## Source PR excerpt

#38
Title: feat(cli): add deterministic ready gates
URL: https://github.com/coldplay126/ai-workflow-tools/pull/38

## Summary
- add `awf ready --gate` decisions for inspect, analysis, workflow init/run, and operations with deterministic exit codes
- wire Claude skills, Codex host runner, AGENTS/snippets, and docs to use the preflight gates
- make `python -m awf.cli` invoke the CLI entrypoint for checkout-local runner fallback

## Test plan
- `cd cli && uv run --group dev pytest -q --ignore=tests/test_e2e_live.py`
- `uv run --project cmux-agent --group dev python -m pytest cmux-agent/tests -q`
- `bash -n codex/run-wf.sh`
- `PYTHONPYCACHEPREFIX=/private/tmp/awf-pycache python3 -m py_compile cli/src/awf/core/ready.py cli/src/awf/commands/ready.py cli/src/awf/cli.py`
- `PYTHONPATH=cli/src cli/.venv/bin/python -m awf.cli ready --repo-root . --gate workflow-init --json`
