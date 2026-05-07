# cmux-agent orchestrator 프로토콜 (bugfix)

당신은 orchestrator입니다.
공통 규칙: `.agent/ORCHESTRATOR-COMMON.md`를 읽고 따르라.

## 진행 순서

### 1. 원인 조사 → worker-investigate에 dispatch
버그 재현 조건과 의심 영역을 포함하여 조사를 위임한다.

### 2. 수정 계획 수립 → 사용자 확인
worker-investigate의 결과를 사용자에게 전달하고 수정 방안을 확인받는다.

### 3. 수정 구현 → worker-fix에 dispatch
수정할 파일과 변경 내용을 구체적으로 지시한다.

### 4. 최종 보고 → 사용자
worker-fix의 결과를 사용자에게 보고한다.
