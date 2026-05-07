# Pattern Classification Criteria

`docs/patterns/`는 이상적 목표 설계를 담는 규범 문서다.
이 문서는 pattern 정제 시 어떤 내용을 남기고, 어떤 내용을 다른 문서로 이동할지 판정하는 공통 기준을 정의한다.

## Purpose

- `patterns`를 구현 설명서가 아니라 목표 설계 문서로 유지한다.
- 불변식과 구현 세부를 분리한다.
- 이후 `status`, `gap`, `test` 문서가 같은 기준을 공유하도록 한다.

## Scope

이 기준은 아래 문서군에 공통 적용한다.

- `docs/patterns/system-overview/`
- `docs/patterns/analysis-pipeline/`
- `docs/patterns/workflow-pipeline/`
- `docs/patterns/multi-agent/`

## Classification Axes

### Invariant

구현 방식이 바뀌어도 반드시 성립해야 하는 규칙이다.

판정 질문:

- 이 문장이 미래 구현에서도 반드시 참이어야 하는가?
- 이 문장이 깨지면 설계 자체가 바뀌었다고 봐야 하는가?

처리 규칙:

- `patterns`에 유지한다.
- 가능하면 짧고 검증 가능한 문장으로 다시 쓴다.

예시:

- 정방향 phase order는 `PHASE_ORDER`를 따른다. 역행은 replan으로만 가능하다.
- Skip된 phase는 downstream precondition evaluation이 가능하도록 equivalent gate satisfaction을 남겨야 한다.

### Derived Rule

불변식에서 파생된 정책 또는 설계 규칙이다.
불변식보다는 구체적이지만, 특정 파일/함수/현재 wiring에는 묶이지 않는다.

판정 질문:

- 이 문장은 설계 정책인가, 아니면 단순 구현 선택인가?
- 이 규칙이 바뀌더라도 상위 불변식은 유지되는가?

처리 규칙:

- `patterns`에는 최소한만 남긴다.
- 상세 분기표, 예시, 확장 규칙은 `reference` 또는 `status`로 이동한다.

예시:

- Change class는 phase 진입 전에 결정된다.
- Policy에 의해 phase가 skipped 될 수 있다.

### Implementation Detail

특정 파일, 함수, 경로, 데이터 구조, 알고리즘, 현재 wiring에 의존하는 설명이다.

판정 질문:

- 파일명, 함수명, JSON 키 경로, 내부 알고리즘, 현재 저장 방식이 등장하는가?
- 현재 구현을 설명하는 문장인가?

처리 규칙:

- `patterns`에서 제거한다.
- `docs/status/`로 이동한다.

예시:

- `state.py`의 `PHASE_GATE` dict가 gate 매핑을 관리한다.
- `workflow_prompt.py`가 precondition을 읽어 phase 시작 전에 검증한다.

### Operational Value

수치, 목록, timeout, retry 횟수, keyword set, 비용표, threshold 같은 운영값이다.

판정 질문:

- 이 값이 바뀌어도 설계의 본질은 유지되는가?
- 운영 튜닝이나 환경에 따라 바뀔 수 있는 값인가?

처리 규칙:

- `patterns`에서 제거한다.
- 별도 `reference` 문서로 이동한다.

예시:

- `MAX_TOTAL_EXECUTIONS = 30`
- `retry.max = 3`
- `related_domains >= 3`
- provider별 timeout 표

## Decision Outcomes

분류 후 각 항목에는 아래 decision 중 하나를 붙인다.

- `keep`: pattern에 유지
- `rewrite`: pattern에 남기되 더 추상적인 규범 문장으로 다시 작성
- `move_to_status`: 현재 구현 설명으로 이동
- `move_to_reference`: 운영값 또는 상세 표로 이동
- `drop`: 중복 또는 저가치 예시라 삭제

## Review Procedure

각 문서의 문장, 표, 다이어그램, 코드 블록을 항목 단위로 분류한다.

기본 순서:

1. 문장을 `Invariant`, `Derived Rule`, `Implementation Detail`, `Operational Value` 중 하나로 분류한다.
2. `decision`을 정한다.
3. `reason`을 기록한다.
4. 모호한 항목은 `Open Question`으로 남긴다.

권장 필드:

- `id`
- `source_ref`
- `text_summary`
- `classification`
- `decision`
- `reason`

## Pattern Editing Rules

정제된 `patterns`에는 아래만 남긴다.

- 핵심 설계 원칙
- 시스템 불변식
- 최소한의 파생 규칙
- 용어 정의

정제된 `patterns`에서 뺀다.

- 현재 코드 구조
- 현재 미구현/known gap
- 수치 중심 운영 표
- 구현 예시가 아니어도 되는 장황한 예시

## Workflow Invariants Fixed For This Round

이번 round에서 workflow 정제의 기준으로 고정된 불변식은 아래 4개다.

- `I1`: 정방향 phase order는 `PHASE_ORDER`를 따른다. 역행은 replan으로만 가능하다.
- `I2`: HIL 여부는 phase 고정값이 아니라 policy/change class 결과이다.
- `I3`: Policy에 의해 phase가 skipped 될 수 있다.
- `I4`: Skip된 phase는 downstream precondition evaluation이 가능하도록 equivalent gate satisfaction을 남겨야 한다.

## Status, Gap, Test Relationship

문서 역할은 아래처럼 분리한다.

- `patterns`: 목표 설계
- `status`: 현재 구현 사실
- `gaps`: pattern과 status의 차이
- `tests`: pattern 달성 여부를 판정하는 acceptance criteria

정제 순서:

1. `patterns` 정제
2. `reference` 신설
3. `status` 작성
4. `gap` 등록
5. `test` 설계
