# Multi-Agent 교차 검증

## 개요

5가지 모드로 작업 복잡도에 따라 에이전트 조합을 자동 선택한다.

## 모드별 시퀀스

### solo (기본)

```mermaid
sequenceDiagram
    participant CLI as awf-cli
    participant P as Primary

    CLI->>P: prompt
    P-->>CLI: result
```

### cross (교차 검증)

```mermaid
sequenceDiagram
    participant CLI as awf-cli
    participant P as Primary
    participant C as Codex<br/>(plan conformance)
    participant S as Sonnet<br/>(quality validation)
    participant J as Judge

    CLI->>P: prompt (native mode)
    P-->>CLI: primary result

    par 병렬 실행
        CLI->>C: slave prompt
        CLI->>S: slave prompt
    end
    C-->>CLI: findings JSON
    S-->>CLI: findings JSON
    CLI->>J: Judge Rules 적용
    J-->>CLI: PASS/FAIL + reason
```

### critical (순차 심층)

```mermaid
sequenceDiagram
    participant CLI as awf-cli
    participant D as MultiAgentDispatch
    participant C as Codex<br/>(precision)
    participant S as Sonnet<br/>(quality)
    participant P as Primary
    participant J as Judge

    CLI->>D: run_chained([step1, step2, step3])
    D->>C: Step 1 spec (precision prompt)
    C-->>D: codex result
    D->>S: Step 2 spec (built from codex result)
    S-->>D: sonnet result
    D->>P: Step 3 spec (built from codex + sonnet results)
    P-->>D: primary result
    D-->>CLI: [codex, sonnet, primary] in order
    CLI->>J: Judge Rules 적용
    J-->>CLI: PASS/FAIL + reason
```

각 step은 `ChainedStep(role, factory)` 구조이며, `factory(prior_results)` 가 다음 단계의 `WorkerSpec` 을 만든다. cmux 백엔드 선택 시 같은 role 의 worker 가 chain 동안 고정되어 터미널 컨텍스트가 누적된다.

### precise (정밀)

```
Codex → Primary 순차. Codex 분석 → Primary 검증 + 보완.
```

### quick (빠른)

```
Codex only. read-only, 45초 timeout.
```

## Judge Rules

```mermaid
flowchart TD
    START["Agent 결과 수집"]
    R1{"Rule 1:<br/>CRITICAL finding?"}
    R2{"Rule 2:<br/>MAJOR ≥ 2건?"}
    R3{"Rule 3:<br/>결론 불일치?<br/>(PASS vs FAIL)"}
    PASS["✓ PASS"]
    FAIL["✗ FAIL"]

    START --> R1
    R1 -->|Yes| FAIL
    R1 -->|No| R2
    R2 -->|Yes| FAIL
    R2 -->|No| R3
    R3 -->|불일치| FAIL
    R3 -->|일치| PASS
```

## 자동 승격/다운그레이드

```mermaid
stateDiagram-v2
    solo --> cross: 보안 키워드 감지 (IAM, auth, security)
    solo --> critical: 프로덕션/배포/삭제 키워드

    cross --> solo: Slave 타임아웃 또는 파일 구조 파악 실패
    precise --> solo: Codex sandbox 제한
```

## Slave 원칙

- Slave(Codex, Sonnet)는 **read-only 분석만** 수행
- 실제 파일 변경은 Primary(Master)만 사용자 승인 후 실행
- Codex: `sandbox: read-only` (MCP 기본)
- Sonnet: `--model sonnet --permission-mode default` by default; `--yolo` switches Claude Code to `bypassPermissions` for trusted automation.

## 에이전트 정의

`claude/agents/*.md` — 단일 소스. Claude Code는 frontmatter를 네이티브 소비, Codex는 body를 base-instructions로 주입.

| 에이전트 | 역할 | 사용 모드 |
|---------|------|---------|
| `plan-validator` | 요구사항 커버리지, 스코프 준수 | cross (Codex) |
| `quality-validator` | 엣지케이스, 회귀 리스크, 보안 | cross (Sonnet) |
| `code-reviewer` | 코드/설정 정밀 분석 + 빠른 분석 | precise, critical, quick |
| `adversarial-tester` | 경계 조건, 실패 모드, 보안 취약점 | team (Codex) |
| `happy-path-tester` | 정상 시나리오 수락 기준 검증 | team (Codex) |
| `artifact-reviewer` | spec↔plan↔tasks 교차 검증 | review phase |
| `spec-verifier` | spec 준수 검증 | verify phase |
| `spec-writer` | spec-kit 산출물 생성 | plan phase |
| `implementer` | tasks.md 순서대로 구현 | impl phase (worktree) |
| `analyzer` | 소스코드 분석 | analysis pipeline |
| `project-discoverer` | 프로젝트 식별 | wf-discovery |
| `analysis-docs-explorer` | 기술 문서 탐색 | analysis-docs |

> Legacy fallback: `skills/multi-agent/protocols/*.md`는 에이전트 미매칭 시 fallback으로 유지.

## 출력 가시성

```
=== multi-agent: cross mode ===
mode: cross — 2 agents parallel
  ✓ codex/plan_conformance (66s)
    결론: FAIL - spec obligations are not fully covered
    발견: 3건 (critical=2, major=1)
    주요: analysis-docs private repo 생성 task 누락
  ✓ claude:sonnet/quality_validation (96s)
    결론: PASS - no quality issues found
    발견: 이슈 없음
  ✗ 판정: FAIL — critical finding from codex
token_usage: input=13,000 output=3,000 total=16,000
cost_estimate: ~$0.0630
=== multi-agent: cross complete ===
```

## OMP와 벤더 멀티에이전트 비교 (2026-07-29)

| 항목 | OMP | Claude Code / Agent SDK | Codex | Gemini CLI / Google ADK |
|------|-----|-------------------------|-------|-------------------------|
| 모델 범위 | Anthropic, OpenAI, Google, 로컬/게이트웨이를 같은 registry와 role alias로 선택 | Claude 모델 및 Anthropic 실행 환경 중심 | GPT/Codex 모델 중심 | Gemini 모델 및 Google 실행 환경 중심 |
| 작업 분할 | heterogeneous batch `task`, agent별 model/effort/schema/isolation | subagent 병렬 실행; experimental agent teams는 shared task와 peer messaging 제공 | 병렬 subagent와 inspect/steer 가능한 agent thread | CLI subagent는 specialist-as-tool; ADK는 graph/dynamic/collaborative/template workflow 제공 |
| 에이전트 통신 | `hub` mailbox, direct/broadcast messaging, idle/parked agent revive | subagent는 parent 반환; agent teams는 teammate direct messaging | parent가 spawn/steer/collect하는 thread 중심 | CLI subagent는 parent 반환; ADK collaborative workflow는 coordinator 중심 |
| 실행 격리 | agent별 workspace isolation, patch/branch merge, recursion/concurrency guard | permission/tool 제한과 독립 context; agent teams는 별도 Claude Code session | parent sandbox 상속, 별도 agent thread | 독립 context/tool 제한; ADK는 애플리케이션이 실행 정책 소유 |
| 관찰성 | `agent://`, `history://`, background job lifecycle, live progress, process supervision | agent panel, transcript, hooks | client별 agent activity/thread UI | CLI agent tool result; ADK event/session 추적 |
| 사람 협업 | E2E 암호화된 `/collab`, agent hub 제어, view-only link | lead UI와 teammate pane | app/CLI/IDE agent thread inspection | CLI 또는 ADK 애플리케이션 UI에 의존 |
| 워크플로우 결정성 | 런타임 primitive는 강력하지만 gate/DAG는 상위 계층이 정의 | subagent는 모델 주도, agent teams task list는 experimental | 모델 또는 SDK 코드가 orchestration | ADK graph/template이 가장 명시적인 결정론적 흐름 제공 |

### OMP가 우세한 지점

1. **벤더 중립 라우팅**: 하나의 agent graph 안에서 provider/model을 worker별로
   바꿀 수 있고, canonical model ID와 `@smol`/`@slow`/`@task` 같은 role로
   구체 모델 교체 비용을 낮춥니다.
2. **실행 substrate 완성도**: batch fan-out, per-agent structured output,
   filesystem isolation, patch merge, background job, lifecycle revive가 한 surface에
   결합되어 있습니다.
3. **수평 통신**: parent-return만 제공하는 일반 subagent보다 `hub` 기반 peer
   messaging이 긴 작업의 재조정과 교차 검증에 유리합니다.
4. **증거 보존**: 결과, transcript, patch, nested agent artifact가 안정적인 내부
   URI로 남아 gate evidence와 사후 분석에 연결하기 쉽습니다.
5. **운영자 개입**: 실행 중 agent를 inspect/steer/cancel/revive할 수 있고,
   `/collab`으로 원격 동료가 같은 세션과 subagent를 관찰하거나 제어할 수 있습니다.

### awf/wf 구현 상태와 다음 우선순위

| 상태 | 우선순위 | 항목 | 구현/다음 방향 |
|------|----------|------|----------------|
| 완료 | P0 | concrete model ID 중복 | SDK 기본값은 `model_defaults.py`에서 관리하고 CLI provider는 role alias/native auto-selection 사용 |
| 완료 | P1 | OMP 실행 surface | `surface_preference=omp` NDJSON adapter와 OMP host-native `task`/`hub` skill 경로 분리 |
| 완료 | P1 | OMP agent discovery | `awf agents sync-omp`가 `claude/agents`를 `.omp/agents`로 변환하고 manifest로 생성 파일 소유권 관리 |
| 완료 | P1 | OMP dispatch provenance | CLI adapter가 session/provider/model/usage/response hash를 `.workflow/artifacts/dispatch/omp-*.json`에 저장; host-native task URI는 phase evidence에 연결 |
| 완료 | P1 | workflow HIL 경계 | approve/done과 scope hash 승인은 parent-only, OMP runtime state는 gate evidence로만 취급 |
| 예정 | P2 | provider 선택이 정적이고 비용/지연/쿼터 피드백이 약함 | OMP role alias와 awf usage telemetry를 결합한 capability/cost-aware routing 추가 |
| 예정 | P2 | disagreement를 무조건 FAIL 처리해 false positive 비용이 큼 | evidence quality, confidence, reproducibility를 judge 입력에 추가하고 critical/high는 기존 fail-closed 유지 |
| 예정 | P2 | 벤더별 실행 결과를 비교하는 회귀 corpus가 없음 | 동일 fixture를 OMP, Claude teams/subagents, Codex subagents, Gemini/ADK에서 실행하는 conformance suite 추가 |

### 안전 경계

- OMP의 todo/agent registry는 실행 상태이며 `.workflow/state.json`을 대체하지 않습니다.
- `agent://`와 `history://`는 evidence/provenance이고 gate 자체가 아닙니다.
- headless subagent가 approve/done HIL 또는 사용자 권한 승인을 대신할 수 없습니다.
- 병렬 write는 파일 소유권 또는 isolation/merge 계약이 있을 때만 허용합니다.

### 근거 문서

- [OMP task runtime](https://github.com/can1357/oh-my-pi/blob/main/docs/tools/task.md)
- [OMP model/provider configuration](https://github.com/can1357/oh-my-pi/blob/main/docs/models.md)
- [Claude Agent SDK subagents](https://code.claude.com/docs/en/agent-sdk/subagents)
- [Claude Code agent teams](https://code.claude.com/docs/en/agent-teams)
- [Codex subagents](https://developers.openai.com/codex/multi-agent/)
- [OpenAI Agents SDK orchestration](https://openai.github.io/openai-agents-python/multi_agent/)
- [Gemini CLI subagents](https://geminicli.com/docs/core/subagents/)
- [Google ADK workflows](https://adk.dev/workflows/)
