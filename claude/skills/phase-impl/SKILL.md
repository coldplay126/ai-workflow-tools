---
name: phase-impl
version: 1.1.0
description: "Phase 4: 구현. tasks.md 순서대로 구현 및 G4 게이트."
type: workflow-phase
phase: impl
gate: G4

capabilities:
  - file_read
  - file_write
  - code_analysis

conditions:
  trigger: "orchestrator가 impl 실행을 지시하거나, 수동 실행"
  skip: "다른 Phase 진행 중, G3 미통과"

cli:
  command: "awf wf next --phase impl"

contract_template: "repo/claude/skills/wf-orchestrator/templates/agent-cards/impl.json"
runtime_contract: ".workflow/agent-cards/impl.json"
---

# Phase 4: 작업 (Implementation)

## 게이트 프리앰블

1. `.workflow/state.json` 읽기.
2. **G3 통과 확인**: `gates.G3.passed` true 확인.
3. **Scope hash 검증 (닫힌계 핵심)**:
   - `.workflow/artifacts/`의 spec.md + plan.md + tasks.md 해시 계산
   - `gates.G3.scope_hash`와 비교
   - **불일치 시**: "승인 후 명세가 변경되었습니다. `/wf.approve`를 다시 실행하세요." 중단.
   - `--force` 플래그 시: 진행하되 history에 `"bypass": true` 기록.
4. `phases.impl.retries`가 5 이상이면 중단.
5. TTL 경고.

## 실행 흐름

### 1. state.json 업데이트
`phases.impl.status: "in_progress"`.

### 2. tasks.md 파싱
- 모든 task 추출: `- [ ] T### ...` 패턴
- 완료된 task 확인: `- [X] T### ...` 또는 `- [x] T### ...`
- Phase별 그룹화
- 병렬 가능 task [P] 식별
- 미완료 task 목록 생성

### 3. Resume 지원
이전 실행에서 중단된 경우:
- 완료된 task 건너뜀
- 미완료 첫 번째 Phase부터 재시작
- 출력: "이전 진행 상태를 복구합니다. T### 부터 재시작."

### 4. 구현 엔진 선택

**A. Ralph-loop 사용 (가능 시):**
- 미완료 task 목록을 ralph-loop에 전달
- 프롬프트:
  ```
  .workflow/artifacts/tasks.md의 미완료 작업을 순서대로 구현하세요.
  .workflow/artifacts/plan.md의 기술 계획을 따르세요.
  각 task 완료 시 tasks.md에서 해당 항목을 [X]로 마킹하세요.
  모든 task가 완료되면: <promise>ALL_TASKS_COMPLETE</promise>
  ```
- max_iterations: 20

**B. 직접 실행 (기본):**
- Phase별로 순차 실행:
  1. Phase의 task 목록 표시
  2. 각 task를 순서대로 구현
  3. task 완료 시 tasks.md에 `[X]` 마킹
  4. [P] 마킹된 task는 가능한 경우 병렬 처리
  5. Phase 완료 후 다음 Phase로

### 5. Deterministic Inner Gate (각 task 완료 후 — 건너뛸 수 없음)

각 task 완료 후, 다음 task 시작 전에 **반드시** 실행:

1. **lint_command** 실행 (manifest.json에 정의된 경우)
   - 에러 0이 아니면: 자동 수정 시도 → lint 재실행
   - 내부 재시도 최대 5회
   - 5회 초과 시: 해당 task를 실패 처리, impl-log.md에 기록
2. **typecheck_command** 실행 (manifest.json에 정의된 경우)
   - 에러 0이 아니면: 자동 수정 시도 → typecheck 재실행
   - 내부 재시도 최대 5회
3. **단위 테스트** (manifest.json `test_command` + 변경 파일 관련 테스트만)
   - 변경 파일과 매칭되는 `.spec.ts`/`.test.ts` 파일만 선택 실행
   - 실패 시: 자동 수정 시도 → 재실행 (내부 재시도 최대 3회)

**이 게이트는 파이프라인의 필수 단계입니다.**
에이전트가 이 단계를 건너뛰면 G4 게이트에서 반드시 실패합니다.
"2회 재시도로 해결 안 되면 사용자에게 표면화" — 무한 토큰 소모 방지.

### 5-1. Phase 완료 시 최종 검증
- 전체 lint_command 재실행으로 Phase 전체 정합성 확인
- 에러 해결 안 되면: impl-log.md에 기록, 사용자에게 안내

### 6. Phase별 git commit
각 Phase 완료 시 conventional commit:
```
feat(<scope>): Phase N - <phase-description>
```
commit 실패 시: 에러 기록, 다음 Phase 계속

### 7. impl-log.md 기록
```markdown
# Implementation Log

## Phase 1: Setup
- T001 ✓ — <설명> (완료: <timestamp>)
- T002 ✓ — <설명> (완료: <timestamp>)
- Commit: abc1234

## Phase 2: Core
- T003 ✓ — <설명> (완료: <timestamp>)
- T004 ✗ — <설명> (실패: lint error in src/foo.ts)
- T004 ✓ — <설명> (재시도 후 완료: <timestamp>)
- SCOPE_WARNING: src/utils/helper.ts 수정 (allowed-files에 없음)
- Commit: def5678
```

### 8. 스코프 경고
`allowed-files.json`에 없는 파일 수정 시:
- impl-log.md에 `SCOPE_WARNING` 기록
- Phase 5에서 최종 검증 (detective control)

### 9. Gate G4 검증
- [ ] tasks.md의 모든 task가 `[X]`
- [ ] lint_command 에러 0건 (lint_command 있는 경우)
- [ ] 모든 Phase에 commit 존재

**G4 통과 시:**
- state.json: `phases.impl: completed`, `gates.G4.passed: true`, `currentPhase: "verify"`
- 출력: `✓ G4 게이트 통과. 모든 task 완료.`

**G4 실패 시:**
- `phases.impl.retries += 1`
- 미완료 task 목록 표시
- 출력: `✗ G4 실패: N개 task 미완료.`

## 주의사항

- tasks.md는 **구현의 유일한 진실 소스**. plan.md와 다르면 tasks.md를 따름.
- `[X]` 마킹은 실제 구현 후에만. 건너뛰기 불허.
- ralph-loop 사용 시 max_iterations 소진되면 자동 중단.
- SCOPE_WARNING은 즉시 차단하지 않음 (Phase 5에서 검증).
