# Codex Portability Guide

## 목적

Claude Code 중심으로 작성된 멀티에이전트 프로토콜과 `/wf` 파이프라인을 Codex에서도 실행할 수 있도록 공통 코어와 도구별 어댑터를 분리하는 가이드입니다.

핵심 원칙은 다음과 같습니다.

1. 워크플로우 코어는 도구 중립적으로 유지한다.
2. Claude/Codex는 각자의 UX에 맞는 어댑터만 가진다.
3. provider 전환은 `provider-config.json` 하나로 제어한다.

## 공통 코어

다음 요소는 Claude와 Codex가 공통으로 사용합니다.

| 구성 | 역할 |
|------|------|
| `.workflow/state.json` | 현재 phase, gate, retry, history |
| `.workflow/agent-cards/*.json` | phase별 입출력 계약 |
| `.workflow/provider-config.json` | phase 라우팅, provider, fallback 순서 |
| `.workflow/artifacts/*` | spec/plan/tasks/review/test 등 산출물 |
| JSON 4-Block 스키마 | delegated worker의 공통 응답 형식 |

이 구조 덕분에 host LLM이 Claude든 Codex든 phase state machine은 동일하게 유지됩니다.

## 어댑터 구조

### Claude 어댑터

- 입력 UX: slash command (`/wf`, `/wf.review`, `/wf.status`)
- 실행 단위: `claude/commands/*.md`, `claude/skills/*`
- secondary provider 호출: Codex MCP, Claude CLI

### Codex 어댑터

- 입력 UX: shell command 또는 prompt convention
- 실행 단위: `codex/run-wf.sh`, `codex/templates/*`, `codex/AGENTS.md`
- secondary provider 호출: Claude CLI 또는 Codex 자체 sub-agent

권장 구조:

```text
사용자
├── Claude → claude/commands + claude/skills
└── Codex  → codex/run-wf.sh + codex/templates
             ↓
          공통 .workflow + provider-config.json
```

## 멀티에이전트 프로토콜 이식

Claude의 `#precise`, `#cross`, `#critical`은 Codex에서 다음처럼 대응합니다.

| Protocol | Claude 의미 | Codex 이식 방식 |
|----------|-------------|----------------|
| `#precise` | Codex 정밀 분석 후 host 검증 | Codex local 분석 + optional secondary validation |
| `#cross` | Codex + Sonnet 병렬 | Codex primary + Claude CLI secondary 병렬/순차 비교 |
| `#critical` | Codex → Claude 순차 | Codex precision pass 후 Claude synthesis |

Codex에는 slash command 개념이 없으므로, 권장 방식은 아래 둘 중 하나입니다.

1. shell wrapper: `./codex/run-wf.sh review`
2. prompt prefix: `"WF protocol: cross. Respect .workflow artifacts."`

## `/wf` 이식 전략

### 최소 구현

Codex runner가 다음만 수행해도 실사용 가능합니다.

1. `.workflow/state.json` 읽기
2. 현재 phase 확인
3. `agent-cards/{phase}.json` 읽기
4. `provider-config.json`에 따라 inline/delegated/dual 결정
5. 결과 JSON 파싱 후 gate 평가
6. 상태/아티팩트 기록

### 권장 phase 매핑

| Phase | Codex 권장 역할 |
|------|----------------|
| plan | 초안 생성 또는 Claude pre-validate |
| review | Codex primary review + Claude secondary |
| approve | Human-in-the-loop 유지 |
| impl | Codex primary implementation |
| verify | Codex primary verify + Claude secondary |
| test | Codex test execution/판정 |
| done | Human-in-the-loop 유지 |

## Provider 양방향 구성

현재 provider 모델은 본질적으로 host-agnostic입니다. 따라서 아래 두 구성이 모두 가능합니다.

### A. Claude host, Codex secondary

```json
{
  "phase_routing": {
    "review": { "mode": "dual", "primary": "inline", "secondary": "codex" },
    "verify": { "mode": "dual", "primary": "inline", "secondary": "codex" }
  },
  "fallback_chain": ["codex", "claude:sonnet"]
}
```

### B. Codex host, Claude secondary

```json
{
  "phase_routing": {
    "review": { "mode": "dual", "primary": "inline", "secondary": "claude:sonnet" },
    "verify": { "mode": "dual", "primary": "inline", "secondary": "claude:sonnet" }
  },
  "fallback_chain": ["claude:sonnet", "codex"]
}
```

주의할 점은 `inline`의 의미가 host tool 기준이라는 것입니다.

- Claude host에서 `inline` = Claude skill 실행
- Codex host에서 `inline` = Codex local execution

## Codex에서 Claude fallback 쓰기

Codex host에서는 보통 아래 조건에서 Claude CLI로 전환합니다.

| 조건 | 전환 이유 |
|------|----------|
| Codex timeout | secondary provider로 계속 진행 |
| JSON format retry 실패 | Claude가 더 잘 따를 수 있음 |
| 예산/쿼터 제한 | host 교체 |
| 상충 판정 발생 | Claude synthesis로 해소 |

실행 흐름:

```text
Codex runner
→ primary 실행
→ 실패/타임아웃/format retry 실패
→ fallback_chain 다음 provider 조회
→ `claude --print --bare --output-format json ...`
→ 동일 schema로 정규화
→ gate 평가
```

## 구현 우선순위

### 1단계

- `provider-config.json`을 공통 계약으로 고정
- Codex runner에서 current phase dispatch
- Claude CLI fallback 호출

### 2단계

- `#precise/#cross/#critical`를 Codex prompt convention으로 매핑
- review/verify dual merge 로직 구현

### 3단계

- plan/impl/test delegated mode 확장
- format retry와 provider history 저장 자동화

## 제약 사항

- Claude의 slash command/skill UX를 Codex에서 그대로 복제할 수는 없습니다.
- Codex 쪽은 runner/script 또는 명시적 프롬프트 규약이 필요합니다.
- approve/done phase는 host가 누구든 human-in-the-loop를 유지해야 합니다.

## 권장 운영 방식

가장 안정적인 운영 방식은 다음과 같습니다.

1. 워크플로우 코어는 이 레포에서 관리
2. Claude/Codex는 각자 얇은 실행 어댑터만 유지
3. 모든 provider 전환은 `provider-config.json`으로 제어
4. delegated 응답은 항상 동일 JSON schema를 강제
