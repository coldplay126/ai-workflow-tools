---
name: wf-discovery
version: 1.1.0
description: |
  워크플로우 프로젝트 디스커버리. 기능 설명을 분석하여 관련 프로젝트를 식별하고 추천.
  TRIGGER when: /wf-discovery 실행 시,
               사용자가 어느 프로젝트에서 작업해야 할지 질문할 때,
               기능 요구사항이 여러 레포에 걸칠 수 있을 때.
  DO NOT TRIGGER when: 이미 프로젝트가 결정된 상태,
                      wf pipeline 진행 중,
                      단순 코드 질문.
type: workflow-utility
allowed-tools:
  - Agent
---

# wf-discovery: 프로젝트 디스커버리

이 스킬은 트리거 역할만 합니다. 실행은 `project-discoverer` 에이전트에 위임합니다.

## 실행

Agent tool을 사용하여 `project-discoverer` 에이전트를 호출하세요:
- 사용자의 기능 설명을 그대로 전달
- 에이전트가 프로젝트를 식별하고 결과를 반환
- 결과를 사용자에게 표시

## 다음 단계 안내

에이전트 결과를 받은 후:
- 단일 프로젝트만 관련된 경우 바로 `/wf-orchestrator` 실행을 안내
- 다중 프로젝트인 경우 배포 순서에 따른 작업 순서를 제안
