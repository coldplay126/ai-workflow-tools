---
name: wf-reset
version: 1.1.0
description: "워크플로우 초기화/폐기. 삭제, 아카이브, 되돌리기."
type: workflow-utility

capabilities:
  - file_read
  - file_write

conditions:
  trigger: "워크플로우 삭제/아카이브/되돌리기 요청"
  skip: "워크플로우 진행 중 자동 호출"
cli:
  command: "awf wf reset"
---

## User Input

```text
$ARGUMENTS
```

## 워크플로우 초기화/폐기

이 스킬은 현재 워크플로우를 초기화하거나 폐기합니다.

### 실행 흐름

1. **`.workflow/state.json` 읽기**: 없으면 "활성 워크플로우가 없습니다." 출력 후 종료.

2. **현재 상태 표시**: `/wf.status`와 동일한 요약을 먼저 보여줍니다.

3. **사용자에게 선택지 제시**:

   ```
   워크플로우 <id>를 어떻게 처리하시겠습니까?

   1. 완전 삭제 — .workflow/ 디렉토리 전체 삭제
   2. 아카이브 — .workflow/를 .workflow-archive/<id>/로 이동 (나중에 참고 가능)
   3. 특정 단계로 되돌리기 — 선택한 단계부터 재시작
   4. 취소
   ```

4. **옵션 1: 완전 삭제**:
   - `.workflow/` 디렉토리 전체 삭제
   - "워크플로우가 삭제되었습니다. `/wf`로 새 워크플로우를 시작하세요."

5. **옵션 2: 아카이브**:
   - `.workflow-archive/` 디렉토리 생성 (없으면)
   - `.workflow/`를 `.workflow-archive/<id>/`로 이동
   - `.workflow-archive/`도 `.gitignore`에 추가
   - "워크플로우가 아카이브되었습니다. `/wf`로 새 워크플로우를 시작하세요."

6. **옵션 3: 특정 단계로 되돌리기**:
   ```
   어느 단계부터 재시작하시겠습니까?

   1. 기획 (Phase 1) — spec/plan/tasks부터 다시
   2. 검토 (Phase 2) — 리뷰부터 다시
   3. 승인 (Phase 3) — 승인부터 다시
   4. 작업 (Phase 4) — 구현부터 다시
   5. 검증 (Phase 5) — 검증부터 다시
   6. 테스트 (Phase 6) — 테스트부터 다시
   ```

   선택한 단계 이후의 상태를 초기화:
   - 해당 단계 및 이후 단계: `status: "pending"`, `retries: 0`
   - 해당 게이트 및 이후 게이트: `passed: null`
   - `currentPhase`를 선택한 단계로 설정
   - `history`에 `{"phase": "<selected>", "action": "reset", "at": "<timestamp>"}` 추가
   - 해당 단계의 artifacts 파일은 **삭제하지 않음** (덮어쓰기로 갱신)

7. **`$ARGUMENTS` 직접 처리**:
   - `/wf.reset delete` → 확인 없이 옵션 1 실행
   - `/wf.reset archive` → 확인 없이 옵션 2 실행
   - `/wf.reset plan` → 확인 없이 Phase 1로 되돌리기
   - `/wf.reset review` → Phase 2로 되돌리기
   - `/wf.reset approve` → Phase 3으로 되돌리기
   - `/wf.reset impl` → Phase 4로 되돌리기
   - 인자 없으면 대화형 선택

### 주의사항

- 완전 삭제는 **되돌릴 수 없습니다**. 아카이브를 먼저 제안하세요.
- 되돌리기 시 이전 단계의 artifacts는 보존됩니다 (덮어쓰기로 갱신 가능).
- 되돌리기 시 git 상태는 변경하지 않습니다 (구현된 코드는 그대로 유지).
