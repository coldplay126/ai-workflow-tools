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

### 기능 지도

`ai-workflow-tools`는 하나의 기능만 제공하는 CLI가 아니라, AI 작업을
준비, 분석, 실행, 검증, 운영 기록까지 이어 주는 도구 묶음입니다.

| 영역 | 역할 | 주요 상태/산출물 | 문서 |
|------|------|------------------|------|
| `awf ready` / `awf doctor` | repo가 어느 수준까지 자동화 가능한지 read-only로 점검하고 다음 명령을 추천 | provider readiness, skill discovery, scan/workflow/wiki 상태, gate decision | [첫 작업 흐름](docs/manuals/08-first-workflow.ko.md) |
| `awf analyze` | 코드 단위를 분석해 도메인 문서를 생성하거나 갱신 | `.ai-context/.analysis-state.json`, `.ai-context/.tmp/*`, transitive invalidation cache | [Analysis Pipeline](docs/reference/analysis-pipeline.md) |
| `.ai-context/` | 분석 결과를 Claude Code, Codex, CLI가 함께 읽을 수 있는 tool-agnostic 계약으로 보관 | `api-spec.json`, `data-model.md`, `domain-overview.md`, `external-integration.md`, `ANALYSIS_REPORT.md` | [.ai-context 사양](docs/specs/ai-context-specification.md) |
| `awf wf` | 기능 작업을 7-phase gated workflow로 진행 | `.workflow/state.json`, `.workflow/artifacts/*`, `.workflow/tmp/*` | [Workflow Pipeline](docs/architecture/02-wf-pipeline.md) |
| 멀티에이전트 | review/verify, cross/critical 모드에서 독립 평가와 synthesis를 수행 | subagent 결과 envelope, judge verdict, fallback chain | [Multi-Agent Reference](docs/reference/multi-agent.md) |
| `cmux-agent` / Pi | worker terminal surface와 dispatch runtime을 제공. Pi는 opt-in field-smoke 기반 runner | `.agent/events.jsonl`, Pi smoke result, dispatch surface preference | [cmux Quickstart](docs/manuals/cmux-agent-quickstart.md), [Pi 검증](docs/manuals/pi-field-validation.md) |
| `awf wiki` | 작업 중 생긴 운영 evidence와 결정 기록을 프로젝트 로컬 wiki로 누적 | `.awf-operations/events/*.jsonl`, `wiki/decisions/*`, compiled operations pages | [CLI Architecture](docs/architecture/awf-cli-architecture.md) |
| Claude/Codex 통합 | Claude skills, Codex runner 규칙, snippets를 통해 같은 계약을 다른 agent 환경에서 사용 | `claude/skills/*`, `codex/*`, `snippets/*` | [Claude Code Setup](#claude-code-setup) |

### 작동 방식 요약

일반적인 흐름은 `ready`로 안전 레벨을 확인하고, `scan`/`analyze`로
`.ai-context` 분석 컨텍스트를 만든 뒤, 실제 변경은 `awf wf`의
`plan → review → approve → impl → verify → test → done` 게이트를 통과시키는
방식입니다. 멀티에이전트는 이 흐름의 별도 제품이 아니라, 분석 fan-out,
workflow review/verify, critical mode 같은 고위험 구간에서 실행 품질을
높이는 평가/합성 레이어입니다.

상태의 진실 공급원은 실행 surface가 아니라 repo-local artifact입니다.
`.workflow`는 기능 작업 상태, `.ai-context`는 분석 결과, `.awf-operations`는
운영 evidence를 보관합니다. inline, cmux, Pi는 실행 표면이고, canonical
state는 awf가 관리합니다.

### 주요 플로우

| 플로우 | 언제 쓰나 | 대표 순서 | 주요 산출물/확인 |
|--------|-----------|-----------|------------------|
| 첫 도입 | 새 repo나 subproject에서 자동화 가능 수준을 확인할 때 | `ready → scan → analyze --dry-run → wf init → ready --gate workflow-run → wf next` | automation level, 추천 명령, `.workflow/state.json` |
| 분석 문서화 | 코드 단위를 `.ai-context` 문서로 만들거나 갱신할 때 | `scan → analyze → output split → check/catalog` | `.ai-context/*`, `.analysis-state.json`, `hashes.json` |
| 기능 작업 | 실제 변경을 gated workflow로 진행할 때 | `wf init → plan → review → approve → impl → verify → test → done` | `.workflow/artifacts/*`, gate 결과, phase state |
| 멀티에이전트 검증 | review/verify 또는 고위험 분석을 교차 검증할 때 | `phase/run request → subagents → judge/synthesis → gate result` | result envelope, verdict, fallback decision |
| cmux/Pi 실행 | worker terminal surface나 Pi runner를 사용할 때 | `doctor/field-smoke → dispatch preference → worker run → events 확인` | `.agent/events.jsonl`, Pi smoke evidence, dispatch diagnostics |
| 운영 wiki | 반복 작업의 evidence와 결정을 남길 때 | `events 기록 → wiki compile → decision 작성 → wiki lint` | `.awf-operations/events/*`, `wiki/decisions/*`, operations pages |
| Claude/Codex 통합 | CLI 계약을 agent 환경에서 재사용할 때 | `setup/snippets/skills → awf ready → awf/analyze/wf 계약 실행` | `claude/skills/*`, `codex/*`, project-local artifacts |

### CLI

```bash
uv run --project cli awf --help
uv run --project cli awf ready --repo-root .
uv run --project cli awf doctor --repo-root . --json --ci
uv run --project cli awf wf status --repo-root .
uv run --project cli awf analyze sample-api health --repo-root . --dry-run
```

패키지 wheel 자체를 검증할 때만 editable checkout 대신 재설치된 wheel을 사용합니다:

```bash
uv run --project cli --no-editable --reinstall-package awf-cli awf --help
```

`awf ready`는 프로젝트에서 가장 먼저 실행하는 read-only 점검입니다. 설정,
provider, skill, scan, workflow, operations wiki 상태를 한 번에 모아 현재
안전한 자동화 레벨과 다음 명령을 알려줍니다.
`awf scan --no-ai`는 deterministic 탐색을 우선합니다. Python 프로젝트는
`pyproject.toml`/`setup.py`뿐 아니라 `requirements.txt`, `setup.cfg`,
`Pipfile`, `poetry.lock`만 있어도 인식하며, `src/*` 구조가 없어도
`collectors/`, `analyzers/`, `importers/` 같은 root-level 소스 디렉토리를
분석 단위로 잡을 수 있습니다.

Gemini CLI를 기본 provider로 쓰려면 `provider.default = "gemini"`를 설정합니다.
`provider.gemini.model`을 비워두면 Gemini CLI Auto가 작업에 맞는 Gemini 3
모델을 고릅니다. 특정 모델을 고정하려면 `AWF_GEMINI_MODEL=gemini-3.1-pro`
처럼 환경변수나 `.awf.toml`로 지정합니다.

### 첫 workflow 순서

처음에는 작은 gated loop로 시작합니다.

```bash
awf ready --repo-root .
awf scan <repo-or-subproject> --no-ai
awf analyze <service> <unit> --repo-root . --dry-run --output-format json
awf wf init "small scoped improvement" --repo-root .
awf ready --repo-root . --gate workflow-run
awf wf next --repo-root . --dry-run --output-format json
awf wf next --repo-root .
awf ready --repo-root .
```

`--output-format json`을 붙인 dry-run은 자동화에서 소비할 수 있는 구조화된
prompt preview를 출력합니다. `.workflow/`가 프로젝트 `.gitignore`에 있으면
`awf ready`가 local-only workflow state 경고를 표시합니다.

Pi는 기본 dispatch surface가 아니라 opt-in runner입니다. Pi를 쓰려면 먼저
field-smoke evidence를 남기고 `ready`가 그 결과를 읽게 합니다.

```bash
python3 cli/tests/run_pi_field_smoke.py --json --write-result
awf doctor --repo-root . --json
awf ready --repo-root .
```

자세한 첫 작업 흐름:

- [처음 쓰는 개발자를 위한 온보딩 가이드](docs/manuals/09-colleague-onboarding.ko.md)
- [실제 레포 필드 트라이얼 체크리스트](docs/manuals/10-field-trial-checklist.ko.md)
- [첫 ai-workflow-tools 작업 흐름](docs/manuals/08-first-workflow.ko.md)
- [First Workflow](docs/manuals/08-first-workflow.en.md)
- [Workflow Pipeline](docs/architecture/02-wf-pipeline.md)
- [Analysis Pipeline](docs/reference/analysis-pipeline.md)
- [.ai-context 사양](docs/specs/ai-context-specification.md)
- [Multi-Agent Reference](docs/reference/multi-agent.md)

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
- `.ai-context/`는 분석 결과와 resume/incremental 상태를 보관합니다.
- `.awf-operations/`는 운영 evidence와 후속 판단 입력을 보관합니다.
- provider adapter는 Claude, Codex, Gemini, OpenAI, subprocess, fixture 실행을 정규화합니다.
- runner backend는 workflow state와 분리됩니다. inline/cmux/Pi는 실행 surface이고, awf state가 canonical source입니다.
- 멀티에이전트는 별도 상태 저장소가 아니라 review/verify/analyze 구간에서 신뢰도를 높이는 실행 전략입니다.

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

## Project Map

`ai-workflow-tools` is a toolkit rather than a single-purpose CLI. It connects
readiness checks, source analysis, gated implementation workflows,
multi-agent review, dispatch surfaces, and local operating evidence.

| Area | Purpose | Main state or output | Docs |
|------|---------|----------------------|------|
| `awf ready` / `awf doctor` | Read-only project readiness and next-command recommendation | provider readiness, skill discovery, scan/workflow/wiki state, gate decisions | [First Workflow](docs/manuals/08-first-workflow.en.md) |
| `awf analyze` | Analyze a code unit and generate or refresh domain documentation | `.ai-context/.analysis-state.json`, `.ai-context/.tmp/*`, transitive invalidation cache | [Analysis Pipeline](docs/reference/analysis-pipeline.md) |
| `.ai-context/` | Tool-agnostic analysis contract shared by Claude Code, Codex, and the CLI | `api-spec.json`, `data-model.md`, `domain-overview.md`, `external-integration.md`, `ANALYSIS_REPORT.md` | [.ai-context spec](docs/specs/ai-context-specification.md) |
| `awf wf` | Run feature work through a 7-phase gated workflow | `.workflow/state.json`, `.workflow/artifacts/*`, `.workflow/tmp/*` | [Workflow Pipeline](docs/architecture/02-wf-pipeline.md) |
| Multi-agent | Run independent evaluation and synthesis for review/verify and cross/critical modes | subagent result envelopes, judge verdicts, fallback chains | [Multi-Agent Reference](docs/reference/multi-agent.md) |
| `cmux-agent` / Pi | Provide worker terminal surfaces and dispatch runtimes. Pi is an opt-in runner gated by field-smoke evidence | `.agent/events.jsonl`, Pi smoke result, dispatch surface preference | [cmux Quickstart](docs/manuals/cmux-agent-quickstart.md), [Pi validation](docs/manuals/pi-field-validation.md) |
| `awf wiki` | Capture operating evidence and decisions in a local project wiki | `.awf-operations/events/*.jsonl`, `wiki/decisions/*`, compiled operations pages | [CLI Architecture](docs/architecture/awf-cli-architecture.md) |
| Claude/Codex integration | Reuse the same contracts from Claude skills, Codex runner rules, and snippets | `claude/skills/*`, `codex/*`, `snippets/*` | [Claude Code Setup](#claude-code-setup) |

## How It Fits Together

The normal path is to run `ready`, inspect or scan the repo, create analysis
context with `awf analyze`, then move actual changes through the `awf wf`
`plan → review → approve → impl → verify → test → done` gates. Multi-agent
execution is not a separate product in this repo; it is the evaluation and
synthesis layer used in higher-risk parts of analysis and workflow execution.

The source of truth is repo-local state, not the terminal surface. `.workflow`
stores feature workflow state, `.ai-context` stores analysis output, and
`.awf-operations` stores operating evidence. Inline dispatch, cmux, and Pi are
execution surfaces; awf owns the canonical state and provenance.

## Core Flows

| Flow | When to use it | Typical sequence | Main output or check |
|------|----------------|------------------|----------------------|
| First adoption | Check how far a new repo or subproject can be automated | `ready → scan → analyze --dry-run → wf init → ready --gate workflow-run → wf next` | automation level, recommended commands, `.workflow/state.json` |
| Analysis documentation | Create or refresh `.ai-context` docs for a code unit | `scan → analyze → output split → check/catalog` | `.ai-context/*`, `.analysis-state.json`, `hashes.json` |
| Feature workflow | Run a real change through gated phases | `wf init → plan → review → approve → impl → verify → test → done` | `.workflow/artifacts/*`, gate results, phase state |
| Multi-agent validation | Cross-check review/verify or high-risk analysis | `phase/run request → subagents → judge/synthesis → gate result` | result envelopes, verdict, fallback decision |
| cmux/Pi execution | Use worker terminal surfaces or the opt-in Pi runner | `doctor/field-smoke → dispatch preference → worker run → inspect events` | `.agent/events.jsonl`, Pi smoke evidence, dispatch diagnostics |
| Operations wiki | Preserve recurring evidence and decisions | `record events → wiki compile → write decision → wiki lint` | `.awf-operations/events/*`, `wiki/decisions/*`, operations pages |
| Claude/Codex integration | Reuse the CLI contracts inside agent environments | `setup/snippets/skills → awf ready → run awf/analyze/wf contracts` | `claude/skills/*`, `codex/*`, project-local artifacts |

## CLI

```bash
uv run --project cli awf --help
uv run --project cli awf ready --repo-root .
uv run --project cli awf doctor --repo-root . --json --ci
uv run --project cli awf wf status --repo-root .
uv run --project cli awf analyze sample-api health --repo-root . --dry-run
```

Use a freshly reinstalled wheel only when validating package contents:

```bash
uv run --project cli --no-editable --reinstall-package awf-cli awf --help
```

The Python package is `awf-cli`, and the console entrypoint is `awf`.

`awf ready` is the first read-only check for a project. It combines config,
provider, skill, scan, workflow, and operations-wiki readiness into one report,
then prints the next safe commands instead of assuming the repo is ready for
provider-backed automation.
`awf scan --no-ai` starts with deterministic discovery. Python projects are
recognized from `requirements.txt`, `setup.cfg`, `Pipfile`, or `poetry.lock` in
addition to `pyproject.toml` and `setup.py`; script-style repos without `src/`
can still expose root-level units such as `collectors/`, `analyzers/`, and
`importers/`.

To use Gemini CLI as the default provider, set `provider.default = "gemini"`.
Leave `provider.gemini.model` empty for Gemini CLI Auto, or set
`AWF_GEMINI_MODEL=gemini-3.1-pro` / `.awf.toml` to pin a specific model.

### First workflow sequence

Start new repositories with a small, gated loop:

```bash
awf ready --repo-root .
awf scan <repo-or-subproject> --no-ai
awf analyze <service> <unit> --repo-root . --dry-run --output-format json
awf wf init "small scoped improvement" --repo-root .
awf ready --repo-root . --gate workflow-run
awf wf next --repo-root . --dry-run --output-format json
awf wf next --repo-root .
awf ready --repo-root .
```

Dry-runs with `--output-format json` emit structured prompt previews for
automation. If `.workflow/` is ignored by the target repo's `.gitignore`,
`awf ready` reports that workflow state is local-only.

Pi remains opt-in. When using Pi dispatch, first persist field-smoke evidence
and let `ready` incorporate the result:

```bash
python3 cli/tests/run_pi_field_smoke.py --json --write-result
awf doctor --repo-root . --json
awf ready --repo-root .
```

Full guides:

- [Onboarding Guide for First-Time Developers](docs/manuals/09-colleague-onboarding.en.md)
- [Field Trial Checklist for Real Repositories](docs/manuals/10-field-trial-checklist.en.md)
- [First Workflow](docs/manuals/08-first-workflow.en.md)
- [첫 ai-workflow-tools 작업 흐름](docs/manuals/08-first-workflow.ko.md)
- [Workflow Pipeline](docs/architecture/02-wf-pipeline.md)
- [Analysis Pipeline](docs/reference/analysis-pipeline.md)
- [.ai-context spec](docs/specs/ai-context-specification.md)
- [Multi-Agent Reference](docs/reference/multi-agent.md)

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
uv run --project cli awf cmux failures --repo-root .
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
- `.ai-context/` holds generated analysis output plus resume and incremental state.
- `.awf-operations/` holds operating evidence and follow-up decision inputs.
- Provider adapters normalize Claude, Codex, Gemini, OpenAI, subprocess, and fixture execution.
- Runner backends stay separate from workflow state: inline dispatch, cmux-agent, and Pi manage execution surfaces while awf remains the canonical state owner.
- Multi-agent mode is an execution strategy for review, verify, and analysis confidence, not a separate state store.
- The same contracts can be driven from Claude skills, Codex runner scripts, or the `awf` CLI.

## Import Notes

This repo intentionally excludes company-specific material from the source repository, including internal memory files, per-repository AI configuration backups, private documentation MCP configuration, and archived workflow snapshots.
