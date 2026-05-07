# Multi-Agent Orchestration Pattern

다중 에이전트 오케스트레이션의 규범 설계 문서.
이 문서군은 **목표 설계(이데아)**를 기술한다. 현재 구현 상태와 gap은 별도 문서에서 관리한다.

## 문서 목차

| 문서 | 설명 |
|------|------|
| [00-diagram.md](00-diagram.md) | 5-Mode 의사결정, 병렬/순차 흐름, 승격/강등, 생명주기 다이어그램 |
| [01-overview.md](01-overview.md) | 에이전트 협업 패턴, 5가지 실행 모드, Provider 추상화, 역할 기반 프로토콜 |
| [02-judge-rules.md](02-judge-rules.md) | 결정론적 Judge Rules, Severity 계층, Tie-Breaking, 비대칭 처리 |
| [03-provider-routing.md](03-provider-routing.md) | Fallback 체인, Timeout Budget, Synthesis 패턴 |

## 핵심 불변식

| ID | 불변식 |
|----|--------|
| M1 | 작업의 위험도/복잡도에 따라 5가지 실행 모드 중 하나를 선택한다 |
| M2 | cross mode는 병렬 독립 평가 후 Judge로 판정한다 |
| M3 | critical mode는 이전 결과를 다음 입력에 누적하는 순차 체인이다 |
| M4 | Judge는 결정론적 규칙 체인으로 작동한다 (확률적 판단 아님) |
| M5 | 오케스트레이터는 Provider Protocol 뒤에 provider를 추상화한다 |
| M6 | 3가지 synthesis 패턴으로 워크플로우와 통합한다 |
| M7 | 작업 특성에 따라 서브에이전트/에이전트 팀/A2A 중 적합한 협업 패턴을 선택한다 |

## 핵심 원칙

1. **읽기 전용 분석**: Secondary는 분석만 수행. 파일 변경은 Primary만
2. **결정론적 판정**: Judge Rules는 순서가 있는 규칙 체인
3. **Policy-based 승격/강등**: 키워드와 실행 결과에 따라 모드를 자동 전환
4. **비용 인식**: 저비용 모드를 기본, 필요할 때만 고비용으로 승격
5. **Fallback 보장**: 고급 모드 실패 시 하위 모드로 강등하여 작업 완료 보장

## 관련 문서

- [Workflow Pipeline](../workflow-pipeline/) — 게이트 기반 개발 워크플로우
- [Analysis Pipeline](../analysis-pipeline/) — 계층적 분석 파이프라인
- [Reference](../../reference/multi-agent.md) — 운영값, 비용표, 키워드 목록, 타임아웃
- [Classification Criteria](../../standards/classification-criteria.md) — 정제 기준서
