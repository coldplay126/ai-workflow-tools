---
name: phase-verify
version: 1.1.0
description: "Phase 5: 검증. 스코프 + spec 준수 검증 및 G5 게이트."
type: workflow-phase
phase: verify
gate: G5

capabilities:
  - file_read
  - file_write
  - code_analysis

conditions:
  trigger: "orchestrator가 verify 실행을 지시하거나, 수동 실행"
  skip: "다른 Phase 진행 중, G4 미통과"

cli:
  command: "awf wf next --phase verify"

contract_template: "repo/claude/skills/wf-orchestrator/templates/agent-cards/verify.json"
runtime_contract: ".workflow/agent-cards/verify.json"
---

# Phase 5: 검증 (Verification) — Evaluator-Optimizer

## 게이트 프리앰블

1. `.workflow/state.json` 읽기.
2. **G4 통과 확인**: `gates.G4.passed` true 확인.
3. `phases.verify.retries`가 2 이상이면 중단.
4. TTL 경고.

## 실행 흐름

### 1. state.json 업데이트
`phases.verify.status: "in_progress"`.

### 2. 스코프 검증 (닫힌계 강제)
- base branch 확인 (state.json의 branch에서 추론, 또는 staging/main/master)
- `git diff --name-only <base-branch>...HEAD` 실행
- `.workflow/artifacts/allowed-files.json`의 `planned_files`와 비교
- 결과:
  - **planned & changed**: 정상 (스코프 내 수정)
  - **planned & not changed**: 경고 (계획했지만 수정 안 함)
  - **not planned & changed**: **SCOPE_VIOLATION** (스코프 외 수정)

스코프 위반 시 verification-report.md에 CRITICAL로 기록.

### 3. Spec 준수 검증 (spec-verifier 에이전트, fork context)

spec.md의 각 requirement(FR-NNN)에 대해:
- 해당 requirement를 구현한 코드가 있는지 확인
- tasks.md에서 매핑된 task의 대상 파일을 읽고, requirement 반영 여부 판단
- 결과: pass / warn / fail

spec.md의 각 acceptance scenario에 대해:
- Given/When/Then이 코드에 반영되었는지 확인
- 테스트 코드가 있으면 해당 시나리오 커버 여부 확인

### 4. 코드 품질 검토
변경 파일에서 명백한 문제 확인:
- 에러 처리 누락 (try-catch 없는 async 호출)
- 보안 취약점 (SQL injection, XSS, 하드코딩된 시크릿)
- 프로젝트 컨벤션 위반 (AGENTS.md/CLAUDE.md 규칙 기반)
- 심각한 문제만 보고 (cosmetic 무시)

### 4.5. 세컨드 검증 (오케스트레이터 위임)

> **Note**: 세컨드 검증 (외부 LLM 호출)는 오케스트레이터가 `provider-config.json`에 따라 자동으로 처리합니다.
> 이 SKILL.md는 **인라인 실행 전용** 지침입니다. Provider Dispatch 로직은 `wf-orchestrator/SKILL.md`의
> Step 5B/5C를 참조하세요.

### 5. verification-report.md 생성

```markdown
# Verification Report

## 요약
- 스코프: <planned>개 파일 중 <changed>개 변경, <violations>개 위반
- Spec 준수: <pass>/<total> (<percentage>%)
- 코드 품질: <issues>개 이슈

## 스코프 검증
| 파일 | 상태 | 비고 |
|------|------|------|
| src/foo.ts | ✓ planned & changed | |
| src/bar.ts | ⚠ planned & not changed | task T005 미구현? |
| src/utils.ts | ✗ SCOPE_VIOLATION | allowed-files에 없음 |

## Spec 준수
| Requirement | Status | Evidence | Notes |
|-------------|--------|----------|-------|
| FR-001 | PASS | src/foo.ts:42 | null 체크 구현 |
| FR-003 | FAIL | — | 구현 증거 없음 |

## 코드 품질
| ID | Severity | File | Issue | Recommendation |
|----|----------|------|-------|----------------|
| Q1 | HIGH | src/foo.ts:78 | async 에러 미처리 | try-catch 추가 |

## Metrics
- Scope: <violations> violations
- Spec compliance: <percentage>%
- Quality issues: <count>

## Multi-LLM Analysis
<!-- 세컨드 검증 실행 시에만 포함. 미실행 시: "Secondary: SKIPPED" 한 줄 -->

| Role | Provider | Model | Status | Compliance % |
|------|----------|-------|--------|-------------|
| Primary | claude | opus | ✓ | XX% |
| Secondary | <provider> | <model> | ✓/SKIP | XX% |

### Consensus (양쪽 일치)
| FR-NNN | Status | Summary |

### Primary-only / Secondary-only
| FR-NNN | Source | Status | Notes |

### Conflicts (수동 판단 필요)
| FR-NNN | Primary | Secondary | Resolution |
```

### 6. Gate G5 검증
- [ ] SCOPE_VIOLATION 0건
- [ ] Spec 준수 fail 항목 0건
- [ ] Spec 준수율 >= 90% (Codex 병용 시 양쪽 평균)
- [ ] 코드 품질 CRITICAL 0건
- [ ] REVIEW_CONFLICT 0건 (Codex 병용 시)

**G5 통과 시:**
- state.json: `phases.verify: completed`, `gates.G5.passed: true`, `currentPhase: "test"`
- 출력: `✓ G5 게이트 통과`

**G5 실패 — 분기 결정 트리:**

```
SCOPE_VIOLATION 존재?
├── Yes → "스코프 외 파일 변경. 재승인 필요."
│         state: currentPhase → "approve"

Spec compliance fail 존재?
├── 신규 코드에서 fail → "구현 버그. 수정 필요."
│                         state: currentPhase → "impl"
├── 기존 코드에서 fail → "아키텍처 이슈. 계획 수정 필요."
│                         state: currentPhase → "plan"

Coverage gap?
└── "tasks.md에 누락된 task. 계획 수정 필요."
    state: currentPhase → "plan"
```

`phases.verify.retries += 1` 후 해당 phase로 회귀.

## 주의사항

- 스코프 검증은 `git diff` 기반 (commit되지 않은 변경사항 포함).
- Spec 준수 검증은 LLM 판단 의존. 100% 정확하지 않을 수 있음.
- SCOPE_VIOLATION은 항상 CRITICAL. 의도적 추가라면 재승인 필요.
