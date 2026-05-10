---
name: phase-test
version: 1.1.0
description: "Phase 6: 테스트. 회귀/수락 테스트 및 G6 게이트."
type: workflow-phase
phase: test
gate: G6

capabilities:
  - file_read
  - file_write
  - code_analysis

conditions:
  trigger: "orchestrator가 test 실행을 지시하거나, 수동 실행"
  skip: "다른 Phase 진행 중, G5 미통과"

cli:
  command: "awf wf next --phase test"

contract_template: "repo/claude/skills/wf-orchestrator/templates/agent-cards/test.json"
runtime_contract: ".workflow/agent-cards/test.json"
---

# Phase 6: 테스트 (Testing)

**Phase 4(개발자 TDD)와의 구분**: Phase 4는 구현 중 단위/통합 테스트. Phase 6는 전체 회귀 + 수락 테스트 + 수동 서명.

## Deterministic Phase Preflight

수동으로 이 phase를 실행할 때도 [wf-orchestrator/reference/deterministic-preflight.md](../wf-orchestrator/reference/deterministic-preflight.md)의
Phase Skill Preflight를 따릅니다. 이 phase의 dry-run 명령은 다음과 같습니다:

```bash
awf wf next --phase test --repo-root . --dry-run --output-format json
```

## 게이트 프리앰블

1. `.workflow/state.json` 읽기.
2. **G5 통과 확인**.
3. `phases.test.retries`가 3 이상이면 중단.
4. TTL 경고.

## 실행 흐름

### 1. state.json 업데이트
`phases.test.status: "in_progress"`.

### 2. 회귀 테스트 실행 (Selective Testing)

manifest.json의 `test_command` 확인:

**test_command 존재 시:**

먼저 **변경 범위에 따라 테스트를 선택** 실행합니다:

1. `git diff <base-branch>...HEAD --name-only`로 변경 파일 목록 추출
2. 변경 파일과 테스트 파일 매핑:
   - `src/domain/quest/quest.service.ts` → `src/domain/quest/quest.service.spec.ts`
   - `src/domain/quest/quest.service.ts` → `test/quest/quest.service.test.ts`
   - import graph 역추적으로 간접 영향받는 테스트 파일 추가
3. **change_class별 테스트 범위:**
   - `small`: 매핑된 관련 테스트만 실행
   - `standard`: 관련 테스트 + 동일 도메인 테스트
   - `high_risk`: 전체 regression 실행 (test_command 그대로)
4. 결과 캡처: 통과/실패/건너뜀 수
5. 실패한 테스트 목록 기록

change_class는 `.workflow/state.json`의 `change_class` 필드에서 읽습니다.
없으면 `standard`로 기본 처리합니다.

**test_command가 null 시:**
- 경고: "테스트 명령어가 설정되지 않았습니다. 수동 테스트로 대체합니다."
- 회귀 테스트 결과를 "N/A (수동 확인 필요)"로 기록

### 3. 수락 테스트

`.workflow/artifacts/spec.md`에서 acceptance scenarios 추출 (Given/When/Then):
- **자동 검증 가능**: 해당 시나리오를 커버하는 테스트 코드 존재
- **코드 리뷰로 확인**: 테스트 없지만 구현 코드에서 로직 확인 가능
- **수동 확인 필요**: UI, 성능, 외부 시스템 연동 등 자동화 불가

자동/코드리뷰 항목은 결과 기록, 수동 항목은 체크리스트로 표시.

### 4. 테스트 부트스트랩 (선택적, 사용자 요청 시)

acceptance scenarios에서 테스트 스켈레톤 생성:
- TypeScript/JavaScript: Jest/Vitest describe/it
- PHP/Laravel: PHPUnit test 메서드
- Python: pytest 함수
- Go: _test.go 파일
- Given → 설정, When → 실행, Then → 어설션
- `// TODO: implement` 주석 표시

### 5. test-report.md 생성

```markdown
# Test Report

## 요약
- 회귀 테스트: <pass>/<total> 통과 (<skip> 건너뜀)
- 수락 시나리오: <auto>/<total> 자동 검증, <manual>/<total> 수동 필요
- 전체 상태: <PASS/FAIL/MANUAL_PENDING>

## 회귀 테스트 결과
<test_command 출력 요약>

실패한 테스트:
- <test-name>: <failure-reason>

## 수락 시나리오

### 자동 검증 완료
| Story | Scenario | Status | Evidence |
|-------|----------|--------|----------|
| US1   | checkout creates order | PASS | tests/checkout.test.ts |

### 수동 확인 필요
- [ ] US2: 대시보드 UI가 올바르게 렌더링됨 (tester: ___)
- [ ] US3: 10초 이내 응답 확인 (tester: ___)

## Metrics
- Regression: <pass>/<total>
- Acceptance auto: <N>/<total>
- Acceptance manual: <N> items pending
```

### 6. Gate G6 검증
- [ ] 회귀 테스트 통과 (failures 0) 또는 test_command null + 수동 확인 완료
- [ ] 수락 시나리오 자동 검증 항목 모두 통과
- [ ] 수동 항목 있으면: 사용자에게 서명 요청

**G6 — 수동 항목 처리:**
수동 테스트 항목이 있는 경우:
```
수동 확인이 필요한 항목이 있습니다:
1. [ ] US2: 대시보드 UI 렌더링
2. [ ] US3: 10초 이내 응답

확인 완료된 항목 번호를 입력하세요 (예: 1,2 또는 all):
```
사용자가 확인하면 test-report.md에 `[X]`로 마킹 + tester 기록.

**G6 통과 시:**
- state.json: `phases.test: completed`, `gates.G6.passed: true`, `currentPhase: "done"`
- 출력: `✓ G6 게이트 통과`

**G6 실패 — 회귀 테스트 실패 시:**
- `phases.test.retries += 1`
- state: `currentPhase → "impl"` (Phase 4로 회귀)
- 출력: `✗ 회귀 테스트 실패. 구현 수정이 필요합니다.`
- 실패한 테스트 목록 안내

## 주의사항

- 테스트 실행은 사용자 환경에 의존 (로컬 DB, 환경변수 등).
- 테스트 없는 프로젝트도 지원 (수동 체크리스트 대체).
- 테스트 부트스트랩은 스켈레톤만 생성. 실제 구현은 사용자 몫.
- 수동 테스트 서명은 Phase 7에서도 최종 확인.
