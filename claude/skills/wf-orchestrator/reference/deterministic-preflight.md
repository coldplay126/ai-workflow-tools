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

- provider-config가 선택한 surface를 사용합니다. 기본 template는 persisted
  `OMP native` coordinator를 선택하며, routing 설정이 없는 `auto`만 기존
  inline/cmux heuristic을 유지합니다.
- OMP native는 한 host session에서 독립 작업을 한 번의 batch `task`로 실행하고
  structured output, per-agent isolation, capacity, cancellation과 완료 partial-result
  보존을 적용합니다.
- OMP task ID와 `agent://`/`history://`는 schema-v2 provenance로 기록합니다.
  `awf agents followup-omp`는 exact task에 먼저 `hub send`하고 registry task가
  unavailable일 때만 exact history에서 lineage-linked successor 하나를 생성합니다.
- `coordination_surface=print`는 worker별 subprocess 호환 경로이며 native isolation,
  strict schema, structured cancellation, durable follow-up을 지원하지 않습니다.
- 명시한 surface가 unavailable/incompatible이면 inline으로 암묵적 fallback하지
  않고 실패합니다.
- OMP todo, registry와 transcript는 실행 provenance일 뿐 awf phase/gate 상태를
  대신하지 않습니다. approve/done HIL은 항상 parent session이 처리합니다.
- cmux-agent는 `awf ready --repo-root . --gate workflow-run --json`이 해당 repo의
  dispatch readiness를 보고할 때만 사용합니다.
- cmux-agent는 `cmux-agent doctor`, `cmux-agent smoke`,
  `cmux-agent start --attach-orchestrator`로 준비하고
  `awf cmux runs/tail/failures --repo-root .`로 관찰합니다.
- `dispatch.surface_preference=pi`는 기존 Pi print-mode 호환 adapter입니다.
  OMP의 native task/hub 실행과 동일시하지 않으며, fresh field-smoke evidence와
  provider quota가 있을 때만 opt-in으로 사용합니다.
