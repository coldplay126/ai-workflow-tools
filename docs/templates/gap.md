# Gap Inventory Template

## Purpose

이 문서는 `pattern`과 `current status` 사이의 차이를 작업 가능한 backlog로 관리한다.

## Usage Rules

- 각 gap은 하나의 설계 차이만 다룬다.
- `pattern_ref`, `status_ref`, `test_id`를 반드시 연결한다.
- 구현 완료만으로 닫지 말고, 테스트 통과까지 확인한 뒤 `fixed`로 변경한다.

## Gap States

- `open`
- `in_progress`
- `blocked`
- `fixed`
- `accepted_deviation`

## Severity

- `high`
- `medium`
- `low`

## Gap Table

| id | area | pattern_ref | status_ref | summary | severity | state | owner | test_id | resolution |
|---|---|---|---|---|---|---|---|---|---|
| GAP-001 | workflow |  |  |  | high | open |  | WF-001 |  |

## Gap Entry Format

### {GAP-ID} — {Short Title}

- Area:
- Pattern reference:
- Status reference:
- Summary:
- Why it matters:
- Expected behavior:
- Current behavior:
- Severity:
- State:
- Owner:
- Test id:
- Resolution plan:

## Closing Rule

gap을 닫으려면 아래가 모두 충족되어야 한다.

- 관련 구현 변경 완료
- 관련 `status` 문서 갱신
- 관련 테스트 추가 또는 갱신
- 테스트 통과 확인
