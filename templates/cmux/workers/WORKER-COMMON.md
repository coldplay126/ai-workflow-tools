# cmux-agent worker 공통 프로토콜

## 에이전트 정의 참조
작업 전 아래 2개 파일을 반드시 읽고 지침을 따르라:
1. `claude/agents/<에이전트명>.md` — 행동 정의 (검증 항목, 판정 기준, 출력 형식)
2. `.workflow/agent-cards/<phase>.json` — I/O 계약 (입력 아티팩트, 출력 스키마, gate 조건)

에이전트명과 phase는 각 워커 프로토콜에 명시되어 있다.

## 결과 보고
.agent/outbox 디렉토리에 아래 형식의 JSON 파일을 생성한다.

```json
{
  "type": "result",
  "sender": "<worker-name>",
  "recipient": "orchestrator",
  "message": "<에이전트 정의의 출력 형식에 따른 JSON 문자열>"
}
```

## 출력 형식
message 필드의 JSON은 agent card의 structured_result 스키마를 따른다:
```json
{
  "conclusion": "PASS|FAIL",
  "findings": [{ "severity": "...", "category": "...", "location": "...", "description": "...", "suggestion": "..." }],
  "evidence": [],
  "risks": [],
  "action_items": [],
  "phase_metrics": {}
}
```
category 접두어와 phase_metrics 내용은 각 에이전트 정의를 따른다.

## 팀 작업
orchestrator가 명시적으로 허용한 경우 다른 worker에게 dispatch를 보낼 수 있다. 이때 sender는 자신의 worker 이름, recipient는 대상 worker 이름으로 기록한다.

## 주의
- outbox에 생성한 파일은 컨트롤러(watcher)가 감지 즉시 processed/로 이동시킨다. 파일 생성 후 확인했을 때 파일이 없어도 **정상 동작**이다. 오류가 아니므로 재생성하지 마라.
- dispatch에 명시된 파일만 수정하라. 범위 외 변경 금지.
- 기존 테스트가 깨지지 않도록 하라.
