---
name: wf-status
version: 1.1.0
description: "워크플로우 상태 조회. 현재 단계, 게이트, 이력 표시."
type: workflow-utility

capabilities:
  - file_read

conditions:
  trigger: "워크플로우 상태 확인 요청"
  skip: "워크플로우 실행/진행 요청 (orchestrator 담당)"
cli:
  command: "awf wf status"
---

# /wf-status — 워크플로우 상태 조회

## 워크플로우 상태 조회

이 스킬은 `.workflow/state.json`을 읽고 현재 워크플로우의 진행 상태를 시각적으로 표시합니다.

### 실행 흐름

1. **`.workflow/state.json` 읽기**: 없으면 "활성 워크플로우가 없습니다. `/wf-orchestrator`로 시작하세요." 출력.

2. **manifest.json 읽기**: 프로젝트 설정 요약.

3. **파이프라인 진행도 표시**:

   아래 형식으로 현재 위치를 시각적으로 표시:
   ```
   워크플로우: <id>
   브랜치: <branch>
   시작: <createdAt> (<경과 시간>)

   [✓] 기획  → [✓] 검토  → [▶] 승인  → [ ] 작업  → [ ] 검증  → [ ] 테스트 → [ ] 확인
    G1:PASS    G2:PASS    G3:---      G4:---    G5:---    G6:---
   ```

   **Provider Config가 있으면** (`.workflow/provider-config.json` 존재 시):
   파이프라인 진행도 아래에 라우팅 테이블 추가 표시:
   ```
   라우팅:
   Phase    Mode       Provider          Model    Status
   plan     inline     —                 —        —
   review   dual       codex (secondary) —        format_retry
   approve  inline     —                 —        —
   impl     inline     —                 sonnet   —
   verify   dual       codex (secondary) —        —
   test     inline     —                 sonnet   —
   done     inline     —                 —        —
   ```
   - Mode: provider-config.json의 `phase_routing[phase].mode`
   - Provider: `secondary` 프로바이더명 (dual), `primary` 프로바이더명 (delegated), 또는 `—` (inline)
   - Model: `phase_models[phase].inline_model` (설정된 Phase만, 예: impl=sonnet)
   - Status: state.json `gates[G*].provider_status` (실행된 Phase만)

   범례:
   - `[✓]` = completed
   - `[▶]` = in_progress (현재 단계)
   - `[ ]` = pending
   - `[✗]` = failed (게이트 실패로 회귀 중)

4. **게이트 상세 정보**:
   각 통과한 게이트에 대해:
   - 통과 시각
   - artifact_hashes (있으면)
   - 이슈 수 (있으면)

5. **Retry 이력**:
   retries > 0인 단계가 있으면:
   ```
   Retry 이력:
   - 기획: 1회 재시도 (review-feedback.md 반영)
   - 작업: 2회 재시도 (ralph-loop)
   ```

6. **History 요약** (최근 5개):
   ```
   최근 이력:
   - 2026-03-24 10:00 plan started
   - 2026-03-24 10:15 plan completed
   - 2026-03-24 10:16 review started
   - 2026-03-24 10:25 review completed (G2 PASS)
   - 2026-03-24 10:26 approve started
   ```

7. **TTL 경고**: `createdAt`이 7일 이상 지났으면:
   ```
   ⚠ 이 워크플로우는 N일 경과되었습니다. /wf-reset으로 정리하거나 계속 진행하세요.
   ```

8. **다음 액션 안내**:
   현재 단계에 따른 다음 명령어를 안내합니다:
   ```
   다음 단계: /phase-approve
   ```
   게이트 실패로 회귀 중이면:
   ```
   회귀 중: G2 CRITICAL → /phase-plan으로 돌아가세요 (review-feedback.md 참조)
   ```

9. **산출물 목록**:
   `.workflow/artifacts/` 내 존재하는 파일 목록:
   ```
   산출물:
   - artifacts/spec.md (2.3KB)
   - artifacts/plan.md (4.1KB)
   - artifacts/tasks.md (1.8KB)
   - artifacts/review-report.md (1.2KB)
   ```

### 주의사항

- 이 스킬은 **읽기 전용**입니다. state.json을 수정하지 않습니다.
- `.workflow/`가 없으면 에러 없이 안내 메시지만 출력합니다.
