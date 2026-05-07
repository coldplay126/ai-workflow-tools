# Analysis Pipeline 패턴

AI 기반 코드 분석 파이프라인의 규범 설계 문서.
이 문서군은 **목표 설계(이데아)**를 기술한다. 현재 구현 상태와 gap은 별도 문서에서 관리한다.

## 문서 목차

| 파일 | 설명 |
|------|------|
| [00-diagram.md](00-diagram.md) | 아키텍처, 상태 머신, 라우팅, 생명주기 다이어그램 |
| [01-overview.md](01-overview.md) | 4-Layer 구조, 관찰/판단 분리, 번들 구조, 분석 목적 |
| [02-stages.md](02-stages.md) | 3-Stage 처리 — 파일별 분석, Writer/Judge 합성, 교차 검증, Fanout |
| [03-resume-optimization.md](03-resume-optimization.md) | 점진적 재개, Drift 감지, Observation 캐시, 지식 축적, 원자적 상태 관리 |

## 핵심 불변식

| ID | 불변식 |
|----|--------|
| A1 | 파이프라인은 4개 독립 Layer와 명확한 입출력 계약으로 구성된다 |
| A2 | 관찰(Layer 2)과 판단(Layer 3)은 구조적으로 분리되어야 한다 |
| A3 | 실패 시 마지막 완료 stage 이후부터 재개한다 (전체 재실행 없음) |
| A4 | 상태 파일 갱신은 원자적이어야 한다 (중간 상태 노출 금지) |
| A5 | Writer/Judge는 분리되어 실행되며, Analysis Judge는 Writer의 evidence를 변조하지 않는다 |
| A6 | 각 mode는 고정된 required output files 집합을 정의하며, Writer 구성과 산출물은 mode에 의해 쌍으로 결정된다 |

## 핵심 설계 원칙

- **관찰과 판단의 분리**: 확증 편향을 방지하기 위해 사실 수집과 판단을 구조적으로 분리
- **규모 비례 투자**: 분석 대상 크기에 따라 비용과 깊이를 차등 배분
- **점진적 재개**: 실패 시 마지막 완료 지점부터 재개
- **상태 외부화**: 모든 진행 상태를 파일 시스템에 기록

## 관련 문서

- [System Overview](../system-overview/) — 전체 아키텍처 원칙
- [Workflow Pipeline](../workflow-pipeline/) — 게이트 기반 개발 워크플로우
- [Multi-Agent](../multi-agent/) — 다중 에이전트 오케스트레이션
- [Reference](../../reference/analysis-pipeline.md) — 운영값, 스키마, 임계값
- [Classification Criteria](../../standards/classification-criteria.md) — 정제 기준서
