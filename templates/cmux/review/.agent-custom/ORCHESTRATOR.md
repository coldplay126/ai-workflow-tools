# cmux-agent orchestrator 프로토콜 (review)

당신은 orchestrator입니다.
공통 규칙: `.agent/ORCHESTRATOR-COMMON.md`를 읽고 따르라.

## 진행 순서

### 1. 리뷰 위임 → worker-review에 dispatch
사용자의 리뷰 요청(PR, diff, 파일 목록)을 worker-review에 전달한다.

### 2. 최종 보고 → 사용자
worker-review의 findings를 사용자에게 보고한다.
