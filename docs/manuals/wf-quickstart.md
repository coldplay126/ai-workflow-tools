# WF 워크플로우 빠른 시작

## 개요

`/wf-orchestrator`는 기능 개발을 7단계 게이트 파이프라인으로 관리하는 Claude Code 워크플로우입니다.

```
/wf-orchestrator '기능 설명' → plan → review → approve → impl → verify → test → done → PR
```

> **명령 alias 이력**: 과거 `/wf`, `/wf.plan` 같은 shortcut은 2026-02 commands 폐기 시 제거됐습니다. 현재는 모든 기능이 skill(`wf-*`, `phase-*`)로 통합되어 있습니다.

## 시작하기

### 1. 워크플로우 초기화

```
> /wf-orchestrator '사용자 프로필에 팔로워 수 표시 기능 추가'
```

실행되면:
- `.workflow/` 디렉토리 생성
- 프로젝트 환경 자동 탐지 (언어, 프레임워크, 테스트 명령어)
- `concept.md`에 기능 설명 저장
- 오케스트레이터가 자동으로 Phase 1 진행

### 2. Phase 자동 진행

오케스트레이터가 Phase를 순차적으로 진행합니다:

**Phase 1 (plan)**: spec.md, plan.md, tasks.md 생성 → `awf wf expand-scope --direction dependents`로 reverse-dependents 자동 추가(그래프 없으면 no-op) → G1 검증
**Phase 2 (review)**: spec↔plan↔tasks 교차 검증 → G2 검증
**Phase 3 (approve)**: 사용자에게 승인 요청 (HIL) → scope hash 잠금
**Phase 4 (impl)**: tasks.md 순서대로 구현 + lint → G4 검증
**Phase 5 (verify)**: 결정론적 `awf wf scope-check` (planned ∪ expanded) + spec 준수 확인 → G5 검증
**Phase 6 (test)**: 회귀/수락 테스트 → G6 검증
**Phase 7 (done)**: 최종 요약 + PR 생성 (HIL)

### 3. 상태 확인

```
> /wf-status
```

현재 Phase, Gate 결과, retry 이력, 다음 액션을 표시합니다.

### 4. 수동 Phase 실행

특정 Phase만 재실행하고 싶을 때 해당 `phase-*` skill을 직접 호출합니다:

```
> /phase-plan      # Phase 1 수동 실행
> /phase-review    # Phase 2 수동 실행
> /phase-impl      # Phase 4 수동 실행
> /phase-verify    # Phase 5 수동 실행
> /phase-test      # Phase 6 수동 실행
```

### 5. 워크플로우 초기화/폐기

```
> /wf-reset
```

## 실전 워크쓰루

### 예시: sample-api에 새 엔드포인트 추가

```
# 1. 어느 레포에서 작업할지 모를 때
> /wf-discovery '팔로워 수 조회 API'

# 2. sample-api에서 워크플로우 시작
> /wf-orchestrator '팔로워 수 조회 API 추가 - GET /api/v1/users/:id/followers/count'

# 3. Phase 1~2 자동 진행 (spec, plan, tasks 생성 + 교차 검증)

# 4. Phase 3에서 승인 요청이 뜸
#    → 스코프, 파일 목록, 리스크 확인 후 "1. 승인" 선택

# 5. Phase 4에서 구현 자동 진행
#    → tasks.md의 각 task를 순서대로 구현 + lint + commit

# 6. Phase 5~6 자동 진행 (검증 + 테스트)

# 7. Phase 7에서 최종 확인
#    → "1. 확인 + PR 생성" 선택
```

### Gate 실패 시

Gate에서 실패하면 오케스트레이터가 자동으로 적절한 Phase로 회귀합니다:

| Gate 실패 | 회귀 대상 | 예시 |
|-----------|----------|------|
| G1 (plan) | plan 재시도 | spec에 `[NEEDS CLARIFICATION]` 남아있음 |
| G2 (review) | plan | CRITICAL 이슈 발견 |
| G4 (impl) | impl 재시도 | lint 에러 |
| G5 (verify) | approve/impl/plan | scope violation → approve로 |
| G6 (test) | impl | 테스트 실패 |

## 주요 커맨드 목록

| 커맨드 | 설명 |
|--------|------|
| `/wf-orchestrator '기능'` | 워크플로우 초기화 + 시작 |
| `/wf-status` | 상태 조회 |
| `/wf-discovery` | 프로젝트 디스커버리 |
| `/wf-reset` | 워크플로우 초기화/폐기 |
| `/phase-plan` | Phase 1 수동 실행 |
| `/phase-review` | Phase 2 수동 실행 |
| `/phase-approve` | Phase 3 수동 실행 |
| `/phase-impl` | Phase 4 수동 실행 |
| `/phase-verify` | Phase 5 수동 실행 |
| `/phase-test` | Phase 6 수동 실행 |
| `/phase-done` | Phase 7 수동 실행 |

## WF run 후 운영 wiki 컴파일

WF 가 끝나면 `dispatch_complete` / `scope_check` / `dual_strategy_engaged` 이벤트가 `.awf-operations/events/` 에 누적된다. 추세를 확인하려면:

```bash
awf wiki compile --since 14    # 최근 2주 이벤트로 4개 operations 페이지 갱신
awf wiki compile --dry-run     # 실제 갱신 없이 어떤 페이지가 어떤 confidence 로 만들어질지 점검
```

생성된 페이지(`stage1-invalidation` / `scope-check` / `dispatch-performance` / `dual-strategy-promotions`) 는 gitignore 대상이므로 머신 로컬에만 남는다. 자세한 layout 은 [awf-cli-architecture §3.6](../architecture/awf-cli-architecture.md).

### After a WF run (English)

Operational events accumulate under `.awf-operations/events/` after each `awf wf next`. Run `awf wiki compile --since 14` to refresh the four `wiki/operations/<topic>.md` pages from the last two weeks of data, or `awf wiki compile --dry-run` to preview what would be written and at what confidence level. Output stays local (the directory is gitignored).
