# System Overview — AI 워크플로우 시스템 설계 패턴

규범 설계 문서. **목표 설계(이데아)**를 기술한다.

## 문서 목차

| 문서 | 설명 |
|------|------|
| [00-diagram.md](00-diagram.md) | 파이프라인 구조, 데이터 흐름, 역할 분리, 상태 전이, Provider 계층 |
| [01-overview.md](01-overview.md) | 4대 설계 원칙 (Spec-as-Truth, State Externalization, Tool Neutrality, Provider Pluggability) |
| [02-architecture.md](02-architecture.md) | 5개 아키텍처 구성요소 (Config, Provider, Skill, Event, Permission) |

## 핵심 불변식

| ID | 불변식 |
|----|--------|
| S1 | 시스템은 analysis, workflow, multi-agent 3개 파이프라인으로 구성된다 |
| S2 | 명세 파일이 시스템 동작의 유일한 진실 공급원이다 (Spec-as-Truth) |
| S3 | 모든 실행 상태는 파일 시스템에 외부화한다 (State Externalization) |
| S4 | 같은 명세가 어떤 AI 도구에서든 동작한다 (Tool Neutrality) |
| S5 | Provider Protocol 추상화로 AI 모델을 동적 선택한다 (Provider Pluggability) |

## 원칙 충돌 시 우선순위

1. State Externalization — 상태 소실 시 다른 원칙이 무의미
2. Spec-as-Truth — 명세 훼손 시 동작 제어 불가
3. Tool Neutrality — 도구 종속은 장기 유지보수 저해
4. Provider Pluggability — 비용 최적화는 기능 정확성 이후

## 관련 문서

- [Analysis Pipeline](../analysis-pipeline/) — 계층적 분석 파이프라인
- [Workflow Pipeline](../workflow-pipeline/) — 게이트 기반 워크플로우
- [Multi-Agent](../multi-agent/) — 다중 에이전트 오케스트레이션
- [Reference](../../reference/system-overview.md) — 운영값, 설정 키, 이벤트 목록
- [Classification Criteria](../../standards/classification-criteria.md) — 정제 기준서
