---
name: wf
version: 1.0.0
description: This skill should be used when the user invokes "/wf", "/wf init <concept>", "/wf resume", "/wf status", or "/wf reset <action>", or asks to start, resume, inspect, or reset the gated workflow through one lifecycle entrypoint.
type: workflow
---

# Workflow Lifecycle Dispatcher

Treat `/wf` as a slash-skill entrypoint, not as AWF CLI syntax. Parse only these forms:

- `/wf init <concept>`
- `/wf resume`
- `/wf status`
- `/wf reset <action>` (allow an omitted action for the reset skill's interactive flow)
- `/wf`

Reject unknown subcommands with the supported forms. Do not reinterpret an unknown argument as a concept.

## Ownership

Keep `.workflow/state.json` and `.workflow/artifacts/*` as canonical workflow state. Read state only to choose a lifecycle route. Never mutate phase state, gates, scope hashes, or artifacts in this dispatcher.

Delegate all phase selection and execution to `wf-orchestrator`. Delegate status rendering to `wf-status`. Delegate every reset action, including archive, delete, and phase rollback, to `wf-reset`. Do not copy their phase or mutation logic into this skill.

## State Classification

Read `.workflow/state.json` from the repository root before routing bare, init, or resume requests.

Classify the workflow as active only when the file is valid JSON and `currentPhase` is one of `plan`, `review`, `approve`, `impl`, `verify`, `test`, or `done`. Treat `currentPhase: "completed"`, `currentPhase: "aborted"`, or a missing state file as inactive. Report malformed JSON or an unrecognized phase as an explicit state error; do not initialize or resume through ambiguous state.

Never overwrite an existing state with `--force`. For an active state, offer resume, status, or reset. For terminal state that still occupies `.workflow/`, require an explicit `wf-reset` action before initializing another workflow.

## Routes

### Init

Require a non-empty concept. If omitted, request the concept and stop.

When no workflow state occupies `.workflow/`, run the existing deterministic initialization gate from the repository root:

```bash
awf ready --gate workflow-init --repo-root . --json
```

Proceed only for an `allow` decision. For `dry_run_only`, report the restriction without creating workflow state. For any blocking result, stop and present its `recommended_next` command when supplied.

Initialize with the real CLI command, passing the concept as one argv value rather than constructing an interpolated shell string:

```text
awf wf init "<concept>" --repo-root .
```

After successful initialization, continue through the Resume route. Do not implement Phase 1 in this dispatcher.

### Resume

Require active state. If state is missing or terminal, request a concept for `/wf init <concept>` and mention reset/archive first when terminal `.workflow/` remains.

Run the existing deterministic workflow gate:

```bash
awf ready --gate workflow-run --repo-root . --json
```

For `allow` or `dry_run_only`, first run the existing deterministic preview:

```bash
awf wf next --repo-root . --dry-run --output-format json
```

Stop if the preview fails or does not identify a coherent next phase, prompt, and artifact path. For `dry_run_only`, report the preview and do not invoke a provider. For `allow`, delegate the previewed phase to `wf-orchestrator`. For any blocking gate result, stop and present its `recommended_next` command when supplied.

### Status

Delegate directly to `wf-status`. Keep this route read-only. When the user explicitly asks for the CLI equivalent, the existing command is:

```bash
awf wf status --repo-root .
```

Do not run a workflow phase while servicing status.

### Reset

Pass `<action>` unchanged to `wf-reset`; with no action, invoke its interactive selection. Let `wf-reset` validate supported actions and perform archive, delete, or phase rollback behavior.

Do not translate reset actions into AWF CLI flags. In particular, never claim that commands such as `awf wf reset archive`, `awf wf reset delete`, or `awf wf reset --phase ...` exist. The slash-skill reset contract and the CLI's separate `awf wf reset` contract are not interchangeable.

### Bare Invocation

For `/wf` with no arguments:

1. Resume through the Resume route only when active state exists.
2. Otherwise request the workflow concept and show `/wf init <concept>`.
3. Never infer a concept, initialize automatically, or resume terminal state.

## Same-Host OMP Routing

When workflow routing selects same-host OMP execution, use the current host's `task` and `hub` capabilities before any external execution surface.

- Require both host capabilities and every capability required by the selected agent card, schema, and isolation policy.
- Pass phase control to `wf-orchestrator`; let it batch independent workers through the current host and preserve returned task/history provenance.
- If `task`, `hub`, or a required capability is unavailable or incompatible, report the capability mismatch and stop that route explicitly.
- Never launch a nested `omp` process, switch to print coordination, or silently fall back to inline/cmux after same-host OMP has been selected. A print-coordination path is valid only when explicitly selected independently, not as recovery for unavailable same-host execution.

Keep approve/done decisions and canonical state mutation in the parent workflow path, never in OMP workers or follow-up sessions.
