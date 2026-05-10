# AI Workflow Patterns — 일반화 패턴 문서

ai-workflow-tools에서 추출한 도메인 비종속적 AI 워크플로우 패턴입니다.
특정 기술 스택(TypeScript, NestJS 등)이나 서비스 구조에 의존하지 않으며,
어떤 AI 기반 코드 분석/개발 시스템에도 적용할 수 있습니다.

## 문서 구조

| 디렉토리 | 설명 | 핵심 패턴 |
|----------|------|----------|
| [system-overview](system-overview/) | 전체 아키텍처와 설계 원칙 | Spec-as-Truth, State Externalization, Role Separation |
| [analysis-pipeline](analysis-pipeline/) | 계층적 분석 파이프라인 | 4-Layer Pipeline, 3-Stage Provider Routing, Resume Protocol |
| [workflow-pipeline](workflow-pipeline/) | 게이트 기반 워크플로우 | N-Phase Gate, Closed-Loop Decision, Risk-Based Routing |
| [multi-agent](multi-agent/) | 다중 에이전트 오케스트레이션 | 5-Mode Execution, Judge Rules, Escalation Chain, Team Blackboard |

## 패턴 간 관계

```
┌─────────────────────────────────────────────────┐
│              System Overview                     │
│  Spec-as-Truth · State Externalization · Roles  │
└────────┬──────────────┬──────────────┬──────────┘
         │              │              │
    ┌────▼────┐   ┌─────▼─────┐  ┌────▼────┐
    │Analysis │   │ Workflow  │  │ Multi-  │
    │Pipeline │   │ Pipeline  │  │ Agent   │
    │(분석)   │   │(개발)     │  │(검증)   │
    └─────────┘   └───────────┘  └─────────┘
```

## 핵심 설계 원칙 (Constitution)

1. **C1 — Observation/Judgment 분리**: 사실 수집과 판단을 계층으로 분리
2. **C3 — 결정론적 Gate 우선**: 도구 기반 검증 → AI 리뷰 순서
3. **C5 — 위험 비례 투자**: 변경 위험도에 따라 검증 깊이 조절
4. **C6 — 상태 외부화**: 모든 상태를 파일로 관리 (LLM 컨텍스트 아님)
5. **C7 — 역할 분리**: Orchestrator, Executor, Reviewer, Judge (analysis/multi-agent), Gate

## 용어 사전 (Glossary)

문서군 간 동일 용어가 다른 의미로 쓰이는 것을 방지하기 위한 canonical term 정의.

### Execution Unit

| 용어 | 소속 | 정의 |
|------|------|------|
| **phase** | workflow | 워크플로우의 실행 단위. plan, review, approve, impl, verify, test, done |
| **stage** | analysis | 분석 파이프라인의 처리 단계. Stage 1 (파일별), Stage 2 (단위 합성), Stage 3 (교차 검증) |
| **mode** | multi-agent | 다중 에이전트 실행 모드. solo, quick, precise, cross, critical |
| **unit** | analysis | 한 번의 분석 대상으로 묶는 코드 그룹. DDD domain일 필요는 없으며, script repo의 `collectors/`, `analyzers/`, `importers/` 같은 root-level source directory도 unit이 될 수 있다 |

### Evaluator

| 용어 | 소속 | 정의 |
|------|------|------|
| **gate** | workflow | Phase 완료 후 pass_conditions를 평가하는 결정론적 관문 |
| **judge** | 공통 | 결과를 종합하여 판정하는 추상 역할. 하위 유형으로 analysis judge, multi-agent judge가 있다 |
| **analysis judge** | analysis | Writer 출력을 병합하고 일관성을 검증하는 synthesis 역할. evidence를 변조하지 않음 |
| **multi-agent judge** | multi-agent | 에이전트 결과를 5단계 규칙으로 PASS/FAIL 판정하는 verdict engine |

### Severity

| 용어 | 소속 | 체계 | 용도 |
|------|------|------|------|
| **escape severity** | workflow | advisory / degraded / warning / critical | Worker escape 시 자동 판정 규칙 입력 |
| **finding severity** | multi-agent | CRITICAL / HIGH / MAJOR / MEDIUM / LOW | Judge Rules 판정 기준 |

### Collaboration Pattern

| 용어 | 정의 |
|------|------|
| **서브에이전트** | 부모 세션 안에서 실행되는 자식 에이전트. 결과만 부모에게 반환. 기본 워커 패턴 |
| **에이전트 팀** | 독립 컨텍스트의 동료 에이전트. 직접 메시지로 토론/반박. 설계 리뷰, QA에 적합 |
| **A2A** | 외부 시스템의 에이전트와 표준 프로토콜로 연동. Agent Card 기반 발견 |
| **human arbiter** | 사람이 최종 승인/판단을 내리는 역할. analysis judge, multi-agent judge와 다른 개념 |

### Risk

| 용어 | 소속 | 정의 |
|------|------|------|
| **change class** | workflow | concept 텍스트의 위험도 분류. small / standard / high_risk. 한국어 병기: 변경 등급 |

## 참고 자료

- [Classification Criteria](../standards/classification-criteria.md) — 문서 정제 기준서
- [Reference Documents](../reference/) — 운영값, 스키마, 설정 상세
