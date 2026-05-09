---
context_prs: [#36]
date: 2026-05-09
last_compiled_at: 2026-05-09T05:26:42.025333+00:00
status: accepted
title: analysis_complete metric payload expansion
source_commits: [a34ddf0]
confidence: high
---
# Decision

`analysis_complete` events now include source file count, bundle line count, bundle token estimate, and output file count. The counts flow through the existing `ARTIFACT_CREATED(kind=analysis_bundle)` event and `AnalysisStateUpdater` before being persisted to `.awf-operations/events/`.

## Context

`analysis_complete` previously recorded only service/domain/mode/elapsed time. That was enough to know that a run finished, but not enough to compare run size, bundle pressure, or output completeness over time.

The bundle layer already persisted `fileCount`, `lineCount`, and `tokenEstimate` in `.analysis-state.json`. The missing piece was to make those values part of the operational event stream without adding a second telemetry path or widening `awf wiki compile` topics prematurely.

## Options considered

- **Option A — pass local variables directly into `_record_analysis_complete_safe`**: smallest code change. It would work for the normal run path, but it would keep resume/recovery paths and state synchronization weaker because metrics would bypass `AnalysisStateUpdater`.
- **Option B — carry bundle metrics through `ARTIFACT_CREATED` and `AnalysisStateUpdater`** (chosen): uses the same event/state path already responsible for analysis progress. Slightly more code, but it gives `.analysis-state.json::eventSync` a durable copy and lets `analysis_complete` read from the synchronized state.
- **Option C — promote `analysis_complete` to a first-class `EventType`**: structurally clean, but larger than necessary. The existing operations JSONL event is sufficient; no provider/task event consumer needs to react to analysis completion yet.

## Decision rationale

Option B matches the existing state-updater boundary. Bundle size is created at the artifact event moment, so the artifact event should carry it; completion recording can then read the synchronized state and fall back to the bundle layer for older/resume states.

`analysis_complete` stays excluded from `awf wiki compile`. The new payload is useful as raw per-run telemetry, but a deterministic aggregate topic should wait until there are enough events to define meaningful distributions and thresholds.

## Consequences

- **Now**: every successful `awf analyze` run records count metrics in the JSONL `analysis_complete` payload. Analysis state also stores the latest bundle metrics under `eventSync.analysisBundle`.
- **Locks-in**: bundle metrics are part of analysis telemetry but not yet part of operations wiki compilation.
- **Revisit when**: analysis completion events accumulate enough volume to justify a deterministic `analysis-performance` topic, or when provider-reported token usage replaces bundle token estimates.


## Source PR excerpt

#36
Title: feat(analyze): record analysis completion metrics
URL: https://github.com/coldplay126/ai-workflow-tools/pull/36

Summary
- Add typed analysis_complete operational metric recording with source file count, bundle line count, bundle token estimate, and output file count.
- Carry analysis bundle counts through ARTIFACT_CREATED events into AnalysisStateUpdater eventSync state.
- Update analysis telemetry docs and add focused tests for metric persistence.

Test plan
- cd cli && uv run --group dev pytest -q tests/test_analysis_metrics.py tests/test_operational_metrics.py tests/test_wiki_compile.py
- cd cli && uv run --group dev pytest -q --ignore=tests/test_e2e_live.py
- uv run --project cmux-agent --group dev python -m pytest cmux-agent/tests -q
