---
name: phase-review
version: 1.1.0
description: "Phase 2: 검토. spec↔plan↔tasks 교차 검증 및 G2 게이트."
type: workflow-phase
phase: review
gate: G2

capabilities:
  - file_read
  - file_write
  - code_analysis

conditions:
  trigger: "orchestrator가 review 실행을 지시하거나, 수동 실행"
  skip: "다른 Phase 진행 중, G1 미통과"

cli:
  command: "awf wf next --phase review"

contract_template: "repo/claude/skills/wf-orchestrator/templates/agent-cards/review.json"
runtime_contract: ".workflow/agent-cards/review.json"
---

# Phase 2: 검토 (Review)

## 게이트 프리앰블

1. `.workflow/state.json` 읽기.
2. `.workflow/manifest.json` 읽기.
3. **G1 통과 확인**: `gates.G1.passed` true. 아니면: "기획 단계(G1)가 완료되지 않았습니다." 중단.
4. **Artifact hash 검증**: G1의 `artifact_hashes`와 현재 파일 해시 비교. 불일치 시: "기획 산출물이 G1 이후 변경되었습니다. `/wf.plan`을 다시 실행하거나 `--force`로 진행하세요."
5. `phases.review.retries`가 2 이상이면 중단.
6. TTL 7일 경과 경고.

## 실행 흐름

### 1. state.json 업데이트
`phases.review.status: "in_progress"`, history에 기록.

### 2. 교차 검증

`.workflow/artifacts/`의 spec.md, plan.md, tasks.md를 읽고:

**A. 중복 탐지**: 유사 requirements 확인 → 통합 제안
**B. 모호성 탐지**: 측정 기준 없는 형용사, 미해결 플레이스홀더 (TODO, ???)
**C. 미명세 탐지**: 동사만 있고 대상/결과 없는 requirements, acceptance criteria 없는 stories
**D. Coverage 계산**: `coverage = (매핑된 requirements / 전체 requirements) * 100`
**E. 일관성 확인**: 용어 불일치, plan 파일 구조↔tasks 파일 경로 일치, task 순서 모순

### 3. 도메인 리뷰 (artifact-reviewer 에이전트)

manifest.json의 `context_providers`에 따라:

**MCP/도메인 문서가 있는 경우:**
- 기존 라우트/엔드포인트 충돌
- DB 스키마 호환성
- 배포 순서 영향
- 서비스 간 의존성

**MCP 없는 경우 (범용):**
- 코드베이스에서 기존 패턴 Grep 검색
- 변경 대상 파일의 의존관계 (import/require 추적)
- 기존 테스트 영향 범위

### 3.5. 세컨드 리뷰 (오케스트레이터 위임)

> **Note**: 세컨드 리뷰 (외부 LLM 호출)는 오케스트레이터가 `provider-config.json`에 따라 자동으로 처리합니다.
> 이 SKILL.md는 **인라인 실행 전용** 지침입니다. Provider Dispatch 로직은 `wf-orchestrator/SKILL.md`의
> Step 5B/5C를 참조하세요.
- 양쪽 상충 (PASS vs FAIL) → `REVIEW_CONFLICT` (게이트에서 CRITICAL/HIGH로 취급)

### 4. review-report.md 생성

```markdown
# Review Report

## 요약
- 교차 검증 항목: N개
- Coverage: XX%
- CRITICAL: N | HIGH: N | MEDIUM: N | LOW: N

## Findings

| ID | Category | Severity | Location | Summary | Recommendation |
|----|----------|----------|----------|---------|----------------|
| D1 | Duplication | MEDIUM | spec.md FR-003, FR-007 | 유사 요구사항 | 통합 권장 |
| C1 | Coverage | HIGH | spec.md FR-005 | 매핑된 task 없음 | task 추가 필요 |

## Coverage Table

| Requirement | Has Task? | Task IDs | Notes |
|-------------|-----------|----------|-------|
| FR-001      | ✓         | T002     |       |
| FR-005      | ✗         |          | task 누락 |

## Metrics
- Total Requirements: N
- Coverage %: XX%
- Critical Issues: N

## Multi-LLM Analysis
<!-- 세컨드 리뷰 실행 시에만 포함. 미실행 시: "Secondary: SKIPPED" 한 줄 -->

| Role | Provider | Model | Status | Findings |
|------|----------|-------|--------|----------|
| Primary | claude | opus | ✓ | N |
| Secondary | <provider> | <model> | ✓/SKIP | N |

### Consensus (양쪽 일치)
| ID | Severity | Summary |
|----|----------|---------|

### Primary-only
| ID | Severity | Summary |
|----|----------|---------|

### Secondary-only
| ID | Severity | Summary |
|----|----------|---------|

### Conflicts (수동 판단 필요)
| Requirement | Primary | Secondary | Resolution |
|-------------|---------|-----------|------------|
```

### 5. Gate G2 검증
- [ ] CRITICAL 이슈 0건
- [ ] HIGH 이슈 모두 해결 방안 기재 또는 사용자 인지
- [ ] coverage >= 80% (Codex 병용 시 양쪽 평균)
- [ ] REVIEW_CONFLICT 중 CRITICAL/HIGH 0건 (Codex 병용 시)

**G2 통과 시:**
- state.json: `phases.review: completed`, `gates.G2.passed: true`, `currentPhase: "approve"`
- 출력: `✓ G2 게이트 통과`

**G2 실패 — CRITICAL 존재 시:**
- `review-feedback.md` 생성:
  ```markdown
  ## Review Feedback → Plan Amendments
  | Finding ID | Severity | Required Change | Affected Artifacts |
  |-----------|----------|----------------|-------------------|
  | C1 | CRITICAL | coverage 갭: FR-005에 task 추가 | tasks.md |
  ```
- `phases.review.retries += 1`
- state.json: `currentPhase: "plan"` (회귀)
- 출력: `✗ G2 실패: CRITICAL N건. 기획으로 돌아갑니다.`

**G2 실패 — HIGH만 존재 시:**
- HIGH 이슈 목록 표시
- 사용자 선택: "해결하고 재검토" → plan 회귀 / "인지하고 진행" → G2 통과

## 주의사항

- 이 phase는 review-report.md, review-feedback.md 생성 외에 기존 파일을 수정하지 않음.
- CRITICAL/HIGH 심각도는 constitution 위반, 핵심 기능 영향 기준.
- coverage 80% 미만이지만 CRITICAL/HIGH 없으면 경고 후 진행 가능.
