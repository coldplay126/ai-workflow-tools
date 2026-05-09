---
context_prs: [#37]
date: 2026-05-09
last_compiled_at: 2026-05-09T07:26:59.146508+00:00
status: accepted
title: awf ready as first repo automation check
source_commits: [987510d]
confidence: high
---
# Decision

`awf ready` is the first repo-level automation check. It is read-only and combines config, provider, skill, heuristic scan, workflow, and operations-wiki status into an automation-level report plus recommended next commands.

## Context

The CLI had useful pieces (`doctor`, `scan`, `skills list`, `wf status`, `wiki init`) but no single command that answered the first adoption question: "what can AWF safely do in this repo right now?"

A short installation guide would not solve that problem because AWF is not a five-minute demo tool. The useful path is to align automation with repo structure: inspect first, classify the safe automation level, and only then recommend artifact generation, provider execution, workflow edits, or operations recording.

## Options considered

- **Option A — expand `awf init` into onboarding orchestration**: attractive because users already run init early. Rejected for this step because init writes `.awf.toml`; a first trust-building check should not mutate the repo.
- **Option B — add `awf ready` as a read-only report** (chosen): preserves existing commands while giving users one entry point for current capability, caution, and next commands. It can be run before any write.
- **Option C — only improve documentation**: lower implementation cost, but it leaves users manually composing `doctor`, `scan`, and `skills list` outputs. That keeps the structure/automation mismatch in place.

## Decision rationale

Option B makes AWF's structure explicit without hiding the underlying tools. `ready` reuses existing core collectors instead of shelling out to subcommands, and it keeps provider execution out of the default path. The output is a structured report with both human and JSON forms, so future orchestrators can consume the same readiness model.

The automation levels deliberately distinguish "safe" from "possible with caution." CLI providers whose auth is not verified remain caution, not fully ready. Workflow automation is also caution until `.workflow/state.json` exists, even if skills are installed.

Workspace roots are handled separately from single-project roots: if no root-level units are found but direct subprojects have project markers, `ready` recommends scanning the detected subproject instead of pretending root analysis is ready.

## Consequences

- **Now**: first-run guidance starts with `awf ready --repo-root .`, not a scattered sequence of `doctor`, `scan`, `skills list`, and docs inspection.
- **Locks-in**: readiness is a repo-level model with automation levels L0-L4. Future onboarding automation should update this model rather than adding separate one-off checks.
- **Revisit when**: `awf init` becomes safe to run in a dry-run/planning mode, or when live provider auth probes are available and can promote CLI providers from caution to ready.


## Source PR excerpt

#37
Title: feat(cli): add repo readiness summary
URL: https://github.com/coldplay126/ai-workflow-tools/pull/37

Summary
- Add awf ready as a read-only repo readiness report that combines config, provider, skill, scan, workflow, and operations status.
- Classify automation capabilities by level and print recommended next commands, including workspace/subproject scan hints.
- Document ready as the first project check and cover the report/CLI output with focused tests.

Test plan
- cd cli && uv run --group dev pytest -q tests/test_ready_command.py
- cd cli && uv run --group dev pytest -q --ignore=tests/test_e2e_live.py
- uv run --project cmux-agent --group dev python -m pytest cmux-agent/tests -q
