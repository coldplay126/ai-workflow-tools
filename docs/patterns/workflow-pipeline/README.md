# Workflow Pipeline Pattern

게이트 기반 N-Phase 워크플로우 파이프라인 패턴의 규범 설계 문서.
이 문서군은 **목표 설계(이데아)**를 기술한다. 현재 구현 상태와 gap은 별도 문서에서 관리한다.

## 문서 목차

| 파일 | 설명 |
|------|------|
| [00-diagram.md](00-diagram.md) | 파이프라인 흐름, 상태 머신, 의사결정 트리, 생명주기 다이어그램 |
| [01-overview.md](01-overview.md) | Phase 구조, Agent Card 계약, Result Envelope, 상태 외부화, 실행 카운터 |
| [02-gates-closed-loop.md](02-gates-closed-loop.md) | Gate 평가 규칙, Retry Budget, on_fail 라우팅, Closed-Loop 의사결정, 에러 분류 |
| [03-risk-routing.md](03-risk-routing.md) | 변경 등급 감지, 위험 비례 투자, HIL, 전제조건 검증, Replan 방향 제약 |

## 핵심 불변식

| ID | 불변식 |
|----|--------|
| I1 | 정방향 phase order는 PHASE_ORDER를 따른다. 역행은 replan으로만 가능하다 |
| I2 | HIL 여부는 phase 고정값이 아니라 policy/change class 결과이다 |
| I3 | Policy에 의해 phase가 skipped 될 수 있다 |
| I4 | Skip된 phase는 downstream precondition evaluation이 가능하도록 equivalent gate satisfaction을 남겨야 한다 |
| I5 | Plan phase는 spec-kit 루틴(constitution/spec/plan/tasks/test criteria)으로 기획서를 구조화할 수 있으며, policy에 따라 에이전트 팀의 교차 검증과 human arbiter 확정을 활용할 수 있다 |

## 핵심 패턴 요약

| 패턴 | 목적 | 핵심 메커니즘 |
|------|------|-------------|
| N-Phase Gate | 단계별 품질 보증 | 각 Phase를 Gate로 분리, 통과 조건 충족 시에만 다음 단계 |
| Closed-Loop Decision | 자동 복구 및 재계획 | severity * reason 규칙으로 continue/replan/abort/escalate 자동 판정 |
| Agent Card Contract | Phase별 런타임 계약 | 입출력 명세, Gate 조건, Retry Budget, 라우팅 규칙 정의 |
| Risk-Based Routing | 위험도 비례 검증 | change class에 따라 리뷰 깊이와 승인 경로 차등, phase skip 포함 |
| State Externalization | LLM 비의존 상태 관리 | 모든 상태를 파일 시스템에 JSON으로 외부화 |

## 관련 문서

- [System Overview](../system-overview/) — 전체 아키텍처와 설계 원칙
- [Analysis Pipeline](../analysis-pipeline/) — 계층적 분석 파이프라인
- [Multi-Agent](../multi-agent/) — 다중 에이전트 오케스트레이션
- [Reference](../../reference/workflow-pipeline.md) — 운영값, 스키마 상세, Phase별 설정
- [Classification Criteria](../../standards/classification-criteria.md) — 정제 기준서
