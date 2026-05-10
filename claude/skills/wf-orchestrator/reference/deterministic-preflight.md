# Deterministic Preflight Contract

Claude skills must not infer repository readiness from prose alone. They first
read deterministic `awf` output, then decide whether provider-backed execution
is allowed.

## First Adoption Sequence

Use this sequence when a repository is being introduced to ai-workflow-tools:

```bash
awf ready --repo-root . --json
awf scan . --no-ai
awf analyze <service> <unit> --repo-root . --dry-run --output-format json
awf wf init "작업 설명" --repo-root .
awf ready --repo-root . --gate workflow-run --json
awf wf next --repo-root . --dry-run --output-format json
```

## Analysis Skill Preflight

Run the analysis gate before provider-backed analysis:

```bash
awf ready --gate analysis --repo-root . --json
```

When the target service or unit is not already clear, run deterministic
discovery and preview the provider prompt first:

```bash
awf scan . --no-ai
awf analyze <service> <unit> --repo-root . --dry-run --output-format json
```

Only continue when the gate allows execution and the dry-run JSON clearly
identifies the target unit, input paths, and artifact paths.

## Workflow Skill Preflight

Before creating workflow state:

```bash
awf ready --gate workflow-init --repo-root . --json
```

Before running an existing workflow:

```bash
awf ready --gate workflow-run --repo-root . --json
awf wf next --repo-root . --dry-run --output-format json
```

Only continue when the gate allows execution and the dry-run JSON clearly
identifies the next phase, provider prompt, and artifact paths.

## Phase Skill Preflight

Manual `phase-*` execution must start with the workflow-run gate and a
phase-specific dry-run:

```bash
awf ready --gate workflow-run --repo-root . --json
awf wf next --phase <phase> --repo-root . --dry-run --output-format json
```

- Exit code `0` with `decision: "allow"` allows the inline phase instructions
  only when the dry-run phase matches the current skill.
- Exit code `10` with `decision: "dry_run_only"` limits the skill to dry-run or
  status reporting.
- Other non-zero exits stop the phase; propose only `gate.recommended_next`.

## Dispatch Surface Policy

`.workflow/state.json` and `.workflow/artifacts/*` are canonical workflow state.
Inline, cmux-agent, and Pi are execution surfaces only.

- Default execution is inline or the provider selected by provider-config.
- Use cmux-agent only when `awf ready --repo-root . --gate workflow-run --json`
  reports dispatch readiness for this repository.
- Prepare cmux-agent with `cmux-agent doctor`, `cmux-agent smoke`, and
  `cmux-agent start --attach-orchestrator`; observe it with
  `awf cmux runs/tail/failures --repo-root .`.
- Pi is opt-in. Do not select Pi as the default surface unless fresh field-smoke
  evidence and provider quota are available.
