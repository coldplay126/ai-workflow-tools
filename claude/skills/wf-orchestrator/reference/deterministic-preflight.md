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

`.workflow/state.json`과 `.workflow/artifacts/*`가 canonical workflow state입니다.
Inline, cmux-agent, OMP, legacy Pi는 실행 surface일 뿐입니다.

- 기본 실행은 inline 또는 provider-config가 선택한 provider입니다.
- OMP 호스트에서 `task`/`hub`가 제공되면, 독립적인 작업은 OMP의 batch task,
  structured output, per-agent isolation을 사용하고 후속 조정은 기존 agent를
  `hub`로 재사용합니다.
- OMP의 todo, agent registry, `agent://`/`history://` artifact는 실행 provenance로만
  취급합니다. awf phase/gate 상태를 대신하거나 직접 통과시키지 않습니다.
- approve/done HIL은 항상 parent session이 처리합니다. headless subagent가 사용자
  승인이나 gate 통과를 대행하면 안 됩니다.
- cmux-agent는 `awf ready --repo-root . --gate workflow-run --json`이 해당 repo의
  dispatch readiness를 보고할 때만 사용합니다.
- cmux-agent는 `cmux-agent doctor`, `cmux-agent smoke`,
  `cmux-agent start --attach-orchestrator`로 준비하고
  `awf cmux runs/tail/failures --repo-root .`로 관찰합니다.
- `dispatch.surface_preference=pi`는 기존 Pi print-mode 호환 adapter입니다.
  OMP의 native task/hub 실행과 동일시하지 않으며, fresh field-smoke evidence와
  provider quota가 있을 때만 opt-in으로 사용합니다.
