# ai-workflow-tools

[한국어](#한국어) | [English](#english)

## 한국어

Claude Code, Codex, local CLI workflow를 위한 AI 작업 오케스트레이션 및
분석 도구입니다. 이 저장소는 workflow 계약, provider adapter, CLI, agent
prompt를 한 곳에 모아 재사용 가능한 자동화 루프를 제공합니다.

### 구성

```text
ai-workflow-tools/
├── cli/            # Python CLI: awf analyze, awf wf, awf chat, awf doctor
├── claude/         # Claude Code skills and agent definitions
├── codex/          # Codex runner and delegated worker rules
├── cmux-agent/     # cmux worker support package
├── docs/           # Architecture, specs, and operating guides
├── snippets/       # CLAUDE.md snippets
├── templates/      # cmux protocol templates
└── setup.sh        # Claude Code skill/agent symlink installer
```

### CLI

```bash
uv run --project cli --no-editable awf --help
uv run --project cli --no-editable awf ready --repo-root .
uv run --project cli --no-editable awf doctor --repo-root . --json --ci
uv run --project cli --no-editable awf wf status --repo-root .
uv run --project cli --no-editable awf analyze sample-api health --repo-root . --dry-run
```

`awf ready`는 프로젝트에서 가장 먼저 실행하는 read-only 점검입니다. 설정,
provider, skill, scan, workflow, operations wiki 상태를 한 번에 모아 현재
안전한 자동화 레벨과 다음 명령을 알려줍니다.

### 첫 workflow 순서

처음에는 작은 gated loop로 시작합니다.

```bash
awf ready --repo-root .
awf scan cli --no-ai
awf analyze ai-workflow-tools <unit> --repo-root . --dry-run
awf wf init "small scoped improvement" --repo-root .
awf ready --repo-root . --gate workflow-run
awf wf next --repo-root .
awf ready --repo-root .
```

Pi는 기본 dispatch surface가 아니라 opt-in runner입니다. Pi를 쓰려면 먼저
field-smoke evidence를 남기고 `ready`가 그 결과를 읽게 합니다.

```bash
python3 cli/tests/run_pi_field_smoke.py --json --write-result
awf doctor --repo-root . --json
awf ready --repo-root .
```

자세한 첫 작업 흐름:

- [첫 ai-workflow-tools 작업 흐름](docs/manuals/08-first-workflow.ko.md)
- [First Workflow](docs/manuals/08-first-workflow.en.md)

### 운영 wiki

`awf wiki`는 `.awf-operations/` 아래에 프로젝트 단위 운영 지식 레이어를
관리합니다. `stage1_invalidation`, `scope_check`, `dispatch_complete`,
`dual_strategy_engaged`, `analysis_complete` 이벤트가 JSONL로 누적되고,
`awf wiki compile`이 이를 결정적 operations page로 합성합니다. raw events와
compiled operations pages는 local telemetry이고, 결정/ADR page는 commit
대상입니다.

### 테스트

```bash
cd cli && uv run --group dev pytest -q --ignore=tests/test_e2e_live.py
uv run --project cmux-agent --group dev python -m pytest cmux-agent/tests -q
```

### 핵심 개념

- `.workflow/`는 gated feature 작업의 phase state와 artifact를 보관합니다.
- `.ai-context/`는 분석 결과를 보관합니다.
- `.awf-operations/`는 운영 evidence와 후속 판단 입력을 보관합니다.
- provider adapter는 Claude, Codex, OpenAI, subprocess, fixture 실행을 정규화합니다.
- runner backend는 workflow state와 분리됩니다. inline/cmux/Pi는 실행 surface이고, awf state가 canonical source입니다.

## English

AI workflow and analysis tooling for Claude Code, Codex, and local CLI workflows.

This repository keeps the reusable workflow contracts, provider adapters, CLI, and agent prompts in one place. It is imported as a clean personal repository with no upstream Git history.

## Contents

```text
ai-workflow-tools/
├── cli/            # Python CLI: awf analyze, awf wf, awf chat, awf doctor
├── claude/         # Claude Code skills and agent definitions
├── codex/          # Codex runner and delegated worker rules
├── cmux-agent/     # cmux worker support package
├── docs/           # Architecture, specs, and operating guides
├── snippets/       # CLAUDE.md snippets
├── templates/      # cmux protocol templates
└── setup.sh        # Claude Code skill/agent symlink installer
```

## CLI

```bash
uv run --project cli --no-editable awf --help
uv run --project cli --no-editable awf ready --repo-root .
uv run --project cli --no-editable awf doctor --repo-root . --json --ci
uv run --project cli --no-editable awf wf status --repo-root .
uv run --project cli --no-editable awf analyze sample-api health --repo-root . --dry-run
```

The Python package is `awf-cli`, and the console entrypoint is `awf`.

`awf ready` is the first read-only check for a project. It combines config,
provider, skill, scan, workflow, and operations-wiki readiness into one report,
then prints the next safe commands instead of assuming the repo is ready for
provider-backed automation.

### First workflow sequence

Start new repositories with a small, gated loop:

```bash
awf ready --repo-root .
awf scan cli --no-ai
awf analyze ai-workflow-tools <unit> --repo-root . --dry-run
awf wf init "small scoped improvement" --repo-root .
awf ready --repo-root . --gate workflow-run
awf wf next --repo-root .
awf ready --repo-root .
```

Pi remains opt-in. When using Pi dispatch, first persist field-smoke evidence
and let `ready` incorporate the result:

```bash
python3 cli/tests/run_pi_field_smoke.py --json --write-result
awf doctor --repo-root . --json
awf ready --repo-root .
```

Full guides:

- [First Workflow](docs/manuals/08-first-workflow.en.md)
- [첫 ai-workflow-tools 작업 흐름](docs/manuals/08-first-workflow.ko.md)

#### 첫 workflow 순서 (한국어 요약)

처음에는 `ready → scan → analyze dry-run → wf init → ready gate → wf next →
ready 재확인` 순서로 진행한다. `ready`가 `block`을 반환하면 workflow 실행보다
추천 명령을 먼저 수행한다. Pi를 사용할 때는
`run_pi_field_smoke.py --write-result`로 최신 evidence를 남긴 뒤
`awf ready`의 `recommended_next`를 확인한다.

### Stage 1 transitive cache invalidation

`awf analyze` builds an import graph alongside its analysis output and uses it on the next run to re-analyze not just files whose content changed, but also their reverse dependents whose imported source moved. This is why a unit you did not touch can still be re-analyzed — its dependency's exported surface changed.

- `awf analyze {service} --check` flags both direct and transitive stale candidates per unit.
- `awf analyze {service} --cycles` reports import cycles using the same saved graph.
- Disable transitive invalidation in an emergency with `AWF_DISABLE_TRANSITIVE_INVALIDATION=1`, or persistently via `analysis-pipeline.json` → `transitive_invalidation.enabled = false`. Direct-change incremental still works without it.

See [docs/patterns/analysis-pipeline/03-resume-optimization.md](docs/patterns/analysis-pipeline/03-resume-optimization.md) for the full contract.

### Operations wiki

`awf wiki` keeps a project-scoped knowledge layer under `.awf-operations/`:
operational events (`stage1_invalidation`, `scope_check`, `dispatch_complete`,
`dual_strategy_engaged`, `analysis_complete`) stream into JSONL, and
`awf wiki compile` deterministically synthesizes them into four
`wiki/operations/<topic>.md` pages — `stage1-invalidation`, `scope-check`,
`dispatch-performance`, `dual-strategy-promotions`. The compiler is
stdlib-only (no LLM calls), so output is reproducible and citable from ADRs.
Decision pages live under `wiki/decisions/` and are committed; raw events,
`log.md`, and the compiled `operations/` pages are gitignored as local
telemetry. See [docs/architecture/awf-cli-architecture.md §3.6](docs/architecture/awf-cli-architecture.md) for the full layout.

#### 운영 wiki (한국어 요약)

`awf wiki` 는 `.awf-operations/` 아래 프로젝트 단위 지식 레이어를 관리한다.
운영 이벤트 5종 (`stage1_invalidation` / `scope_check` / `dispatch_complete` /
`dual_strategy_engaged` / `analysis_complete`) 이 JSONL 로 누적되고,
`awf wiki compile` 이 4 개 `wiki/operations/<topic>.md` 페이지
(`stage1-invalidation` / `scope-check` / `dispatch-performance` /
`dual-strategy-promotions`) 로 결정적 합성한다. LLM 호출 없는 stdlib-only
구현이라 결과가 재현 가능하고 ADR evidence 로 인용 가능. ADR (`wiki/decisions/`)
은 commit 대상, 원본 events / `log.md` / 합성된 `operations/` 페이지는
gitignore (local 텔레메트리). 자세한 layout 은
[docs/architecture/awf-cli-architecture.md §3.6](docs/architecture/awf-cli-architecture.md).

## Tests

```bash
cd cli && uv run --group dev pytest -q --ignore=tests/test_e2e_live.py
uv run --project cmux-agent --group dev python -m pytest cmux-agent/tests -q
```

## cmux-agent Runtime

`cmux-agent` provides a visual multi-agent runtime on top of cmux. It creates a controller, an orchestrator, and configured workers, then routes JSON artifacts through `.agent/outbox` and `.agent/inbox`.

Quickstart: [cmux-agent Quickstart](docs/manuals/cmux-agent-quickstart.md).

By default the orchestrator runs in its own cmux surface. Use
`start --attach-orchestrator` when the current Claude Code or Codex CLI session
should act as the orchestrator while controller and workers run in cmux.

The orchestrator can request dynamic workers by writing a `control` artifact with `action: "spawn_agent"`. You can also create one manually:

```bash
uv run --project cmux-agent cmux-agent spawn worker-api --provider codex
uv run --project cmux-agent cmux-agent spawn --worker-template review --provider codex
```

Runtime diagnostics are available from `cmux-agent` and the read-only `awf cmux` observer:

```bash
uv run --project cmux-agent cmux-agent doctor
uv run --project cmux-agent cmux-agent smoke
uv run --project cmux-agent cmux-agent status --failures
uv run --project cmux-agent cmux-agent failures
uv run --project cmux-agent cmux-agent events --failures
uv run --project cli --no-editable awf cmux failures --repo-root .
```

## Claude Code Setup

```bash
./setup.sh
```

The setup script links the skills under `claude/skills/` and agents under `claude/agents/` into `~/.claude`. It does not register any company-specific MCP server.

Optional snippets:

- `snippets/agents-md-awf-preflight.md`
- `snippets/claude-md-multi-agent.md`
- `snippets/claude-md-wf-pipeline.md`

## Core Ideas

- `.workflow/` holds phase state and artifacts for gated feature work.
- `.ai-context/` holds generated analysis output.
- Provider adapters normalize Claude, Codex, OpenAI, subprocess, and fixture execution.
- Runner backends stay separate from workflow state: inline dispatch and cmux-agent manage execution surfaces, while Pi is detected as a planned terminal harness integration.
- The same contracts can be driven from Claude skills, Codex runner scripts, or the `awf` CLI.

## Import Notes

This repo intentionally excludes company-specific material from the source repository, including internal memory files, per-repository AI configuration backups, private documentation MCP configuration, and archived workflow snapshots.
