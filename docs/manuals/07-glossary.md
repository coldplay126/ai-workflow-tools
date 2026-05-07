# 용어 사전

awf 문서에서 사용하는 주요 용어를 정리합니다.

## 워크플로우

| 용어 | 설명 |
|------|------|
| **Phase** | 워크플로우의 단계. `plan → review → approve → impl → verify → test → done` |
| **Gate** | Phase 사이의 품질 관문. PASS/FAIL로 다음 단계 진입 여부 결정 |
| **Closed-Loop** | escape 발생 시 자동으로 판단하고 다음 행동을 결정하는 폐쇄 루프 |
| **Replan** | 현재 계획을 폐기하고 특정 phase부터 재시작 |
| **Replan Budget** | replan 허용 횟수 (`loop.maxReplans`, 기본 3) |
| **Agent Card** | Phase별 AI 역할 정의 (`.workflow/agent-cards/{phase}.json`) |
| **Manifest** | 프로젝트 메타데이터 (언어, 테스트 명령 등) (`.workflow/manifest.json`) |

## Worker / Envelope

| 용어 | 설명 |
|------|------|
| **Worker** | 워크플로우의 한 phase를 실행하는 AI 에이전트 |
| **Envelope** | worker 결과의 표준 래퍼. `{status, phase, provider, result, escape, meta}` |
| **Escape** | worker가 정상 완료할 수 없을 때 보내는 탈출 신호 |
| **Severity** | escape의 심각도. `blocking` / `degraded` / `advisory` |
| **Auto-wrap** | 레거시 bare JSON 결과를 자동으로 envelope 형태로 변환 |
| **Idempotency Key** | 중복 실행 방지 키. `wf_id:phase:replanCount` |

## 분석

| 용어 | 설명 |
|------|------|
| **Stage** | 분석 파이프라인의 단계. Stage 1 (파일별 XML 번들 분석, 저비용 provider) → Stage 2 (단위 합성, 중간 provider) → Stage 3 (프로젝트 정제, 고비용 provider) |
| **Unit (분석 단위)** | 독립적으로 분석 가능한 파일 그룹. 프로젝트 구조에 따라 도메인, 모듈, 컴포넌트, 패키지 등 |
| **Stage Routing** | `analysis-pipeline.json`의 `stage_routing`으로 scale별 stage→provider 매핑 |
| **Include Patterns** | `analysis-config.json`에서 파일 수집 대상 확장자를 지정하는 glob 패턴 목록 |
| **Bundle** | 분석 대상 코드를 하나의 파일로 압축한 것 |
| **Unit Bundle** | 특정 분석 단위의 코드만 모은 번들 (이전 명칭: Domain Bundle) |
| **Project Bundle** | 프로젝트 전체 코드를 모은 번들 |
| **.ai-context** | 분석 결과가 저장되는 디렉토리. 4개 필수 파일 포함 |
| **Fanout** | Stage 2에서 여러 writer에게 작업을 분배하는 패턴 |
| **Resume** | 중단된 분석을 이어서 진행하는 기능 |

## Provider / 도구

| 용어 | 설명 |
|------|------|
| **Provider** | AI 서비스 추상화. `claude` (Anthropic) / `codex` (OpenAI) |
| **Provider Config** | 프로젝트별 provider 설정 (`.workflow/provider-config.json`) |
| **Phase Models** | `provider-config.json`의 `phase_models`로 WF phase별 provider 선택. 예: impl=sonnet |
| **Dual Mode** | review/verify phase에서 두 provider를 동시에 사용하는 모드 |
| **MCP** | Model Context Protocol. AI 도구에 외부 기능을 제공하는 표준 |

## 통합 (cmux-agent)

| 용어 | 설명 |
|------|------|
| **Runtime** | cmux-agent가 관리하는 에이전트 생명주기, 메시지 전달 계층 |
| **Governance** | awf-cli가 관리하는 phase/gate/decision 규칙 계층 |
| **Artifact** | 에이전트 간 교환되는 구조화된 메시지. `dispatch` / `result` / `decision` |
| **Broker** | cmux-agent의 메시지 라우팅 + artifact 감지 컴포넌트 |
| **Decision Authority Rule** | 모든 상태 전이는 `awf wf decide`를 통해서만 발생한다는 규칙 |
| **Standalone Compatibility** | awf-cli가 cmux-agent 없이도 독립 실행 가능하다는 보장 |

## 멀티에이전트 프로토콜

| 용어 | 설명 |
|------|------|
| **#precise** | Codex 분석 → Claude 검증 모드 |
| **#cross** | Codex + Sonnet 병렬 → Opus 종합 모드 |
| **#critical** | Codex 순차 → Claude 심층 분석 모드 |
| **Judge Rules** | slave 결과를 종합할 때 적용하는 결정론적 규칙 |
| **4-Block Format** | 결론 / 근거 / 리스크 / 실행안 출력 형식 |

## 파일 / 경로

| 파일 | 위치 | 설명 |
|------|------|------|
| `state.json` | `.workflow/` | 워크플로우 상태 |
| `concept.md` | `.workflow/` | 워크플로우 요구사항 |
| `manifest.json` | `.workflow/` | 프로젝트 메타데이터 |
| `provider-config.json` | `.workflow/` | Provider 설정 |
| `.analysis-state.json` | `.ai-context/` | 분석 상태 |
| `agent-cards/*.json` | `.workflow/` | Phase별 AI 역할 정의 |
| `CLAUDE.md` | 프로젝트 루트 | Claude Code 전역 지시 |
| `AGENTS.md` | 프로젝트 루트 | Codex 전역 지시 |
