---
name: phase-plan
version: 2.2.0
description: "Phase 1: 기획. spec-kit 루틴으로 5 산출물 생성 (constitution/spec/plan/tasks/test-criteria) 및 G1 게이트."
type: workflow-phase
phase: plan
gate: G1

capabilities:
  - file_read
  - file_write
  - code_analysis

conditions:
  trigger: "orchestrator가 plan 실행을 지시하거나, 수동 실행"
  skip: "다른 Phase 진행 중, 워크플로우 미초기화"

cli:
  command: "awf wf next --phase plan"

contract_template: "repo/claude/skills/wf-orchestrator/templates/agent-cards/plan.json"
runtime_contract: ".workflow/agent-cards/plan.json"
---

# Phase 1: 기획 (Planning)

## Deterministic Phase Preflight

수동으로 이 phase를 실행할 때도 [wf-orchestrator/reference/deterministic-preflight.md](../wf-orchestrator/reference/deterministic-preflight.md)의
Phase Skill Preflight를 따릅니다. 이 phase의 dry-run 명령은 다음과 같습니다:

```bash
awf wf next --phase plan --repo-root . --dry-run --output-format json
```

## 게이트 프리앰블

1. `.workflow/state.json` 읽기. 없으면: "워크플로우가 초기화되지 않았습니다. `/wf-orchestrator`를 먼저 실행하세요." 중단.
2. `.workflow/manifest.json` 읽기.
3. `createdAt`이 7일 이상 경과했으면 경고.
4. `phases.plan.retries`가 3 이상이면: "기획 단계가 3회 실패했습니다. 수동 개입이 필요합니다." 중단.

## 실행 흐름

### 1. 피드백 확인
`.workflow/review-feedback.md`가 존재하면 (Phase 2에서 회귀한 경우):
- 피드백 내용 출력: "이전 검토에서 다음 피드백이 있었습니다:"
- 이후 단계에서 각 항목 반영
- 반영 완료 후 피드백 파일 삭제

### 2. concept.md 확인
`.workflow/concept.md` 읽기. 내용이 부족하면 사용자에게 보충 요청.

### 3. 컨텍스트 수집

**3a. Constitution 로드 (3단계 severity)**

| 상태 | severity | 동작 |
|------|----------|------|
| `constitution_path` absent/null in manifest | info | "constitution 미설정. 프로젝트 규칙 없이 진행합니다." 출력 후 계속 |
| `constitution_path` 명시됐지만 파일 missing | **error** | "constitution 경로가 지정되었으나 파일을 찾을 수 없습니다: {path}" → G1 실패 |
| 파일 로드 성공 | - | `.workflow/artifacts/constitution.md`에 복사 후 이후 단계에서 참조 |

**3b. 프로젝트 컨텍스트 수집**

manifest.json의 `context_providers` 순회:
- `type: "mcp"` → MCP 서버에서 관련 문서 조회 (analysis-docs MANIFEST.md → 관련 README, ROUTES.md)
- `type: "file"` → 해당 파일 읽기 (AGENTS.md, CLAUDE.md에서 핵심 참고사항)

수집 결과 정리:
- 프로젝트 원칙/제약사항 (constitution 포함)
- 도메인 지식
- 기술 스택

### 4. 산출물 생성 (Spec-Kit)

**ID 태그 규칙**: 모든 산출물은 explicit ID로 교차 참조한다. 이 태그는 G1 gate의 deterministic 검증에 사용된다.

**spec.md:**
```markdown
# Feature Specification: <feature-name>

## Overview
<concept.md 기반 1-2문단 요약>

## User Scenarios & Testing
### User Story 1 — <title> (Priority: P1)
- Why: <이유>
- Acceptance Scenarios:
  - Given: <전제 조건>
  - When: <사용자 행동>
  - Then: <기대 결과>

## Requirements
- FR-001: <기능 요구사항>
- FR-002: ...

## Success Criteria
- SC-001: <측정 가능한 성공 기준>

## Assumptions
- <가정 사항>
```

**plan.md** — 각 phase에 `[FR-NNN]` 태그 필수:
```markdown
# Implementation Plan: <feature-name>

## Summary
<1-2문장 요약>

## Technical Context
- Language: <manifest.language>
- Framework: <manifest.framework>
- Test: <manifest.test_command>

## Project Structure
<변경/생성할 파일 목록>

## Implementation Phases
### Phase 1: <name> [FR-001, FR-004]
<상세 설명>

### Phase 2: <name> [FR-002, FR-003]
<상세 설명>
```

**tasks.md** — 각 task에 `[FR-NNN]` 태그 필수:
```markdown
# Tasks: <feature-name>

## Format: `- [ ] [ID] [FR-NNN] [P?] [Story?] Description — file/path`

## Phase 1: Setup
- [ ] T001 [FR-001] <설명> — <파일 경로>

## Phase 2: Core
- [ ] T002 [FR-002] [US1] <설명> — <파일 경로>
- [ ] T003 [FR-002] [P] [US1] <설명> — <파일 경로>

## Phase 3: Polish
- [ ] T010 [FR-005] <설명>

## Dependencies
- Phase 1 완료 후 Phase 2 시작
- T002, T003은 병렬 가능

## Implementation Strategy
- MVP: Phase 1-2 완료 시 기본 기능 동작
```

### 5. test-criteria.md 생성

spec.md의 모든 FR에 대응하는 수락 기준을 별도 파일로 생성한다.

```markdown
# Test Criteria: <feature-name>

## Acceptance Test Criteria

### ATC-001 [FR-001]: <test 제목>
- Given: <전제 조건>
- When: <사용자 행동>
- Then: <기대 결과>
- Verification: <검증 방법 — manual/automated/command>

### ATC-002 [FR-002, FR-003]: <test 제목>
- Given: ...
- When: ...
- Then: ...
- Verification: ...

## Coverage Matrix

| FR | ATC | Status |
|----|-----|--------|
| FR-001 | ATC-001 | pending |
| FR-002 | ATC-002 | pending |
| FR-003 | ATC-002 | pending |
```

### 6. allowed-files.json 생성
tasks.md에서 파일 경로 추출:
```json
{
  "planned_files": ["src/controllers/example.ts", "..."],
  "extracted_from": "tasks.md",
  "generated_at": "<ISO-8601>"
}
```

#### 6.1 import graph 기반 스코프 확장

`allowed-files.json` 생성 직후 아래 명령을 실행해 reverse-dependent 파일을 결정론적으로 추가합니다. 분석된 import graph가 없거나 추가할 파일이 없으면 명령은 정상 no-op으로 끝나며, plan 흐름은 그대로 진행됩니다.

```bash
awf wf expand-scope --direction dependents
```

이 단계는 G5 SCOPE_VIOLATION false positive를 줄이기 위한 기본 hook입니다. 확장 시 `expanded_files` 필드와 `graph_expansion` audit (direction/depth/항목별 reason/coverage)이 추가됩니다.

수동 조정이 필요한 경우에만 아래 변형을 사용합니다:

```bash
awf wf expand-scope --direction both             # consumer + dependency 양방향
awf wf expand-scope --dry-run                    # 미리보기만
```

`allowed-files.json` 누락, JSON 파싱 오류, 명령 실패는 G1 검증 전에 해결해야 합니다.

### 6.2 데이터베이스 변경 결정과 계획 evidence

tasks.md, plan.md, allowed-files.json에 DB 변경 신호가 있으면
`.workflow/artifacts/database-decision.json`을 생성한다. 이 파일은 설명문이
아니라 선택을 비교할 수 있는 구조화된 결정 artifact다. `schema_version: 1`,
`status: "selected"`, `change_surfaces`, baseline/recommended/selected option ID,
`candidates`, `recommendation_rationale`를 포함한다.

- `candidates`는 정확히 2개 또는 3개여야 하며, `maintain` baseline은 항상
  포함한다. ID나 문장만 바꾼 후보는 별도 선택지가 아니다. decision의
  `baseline_option_id`, `recommended_option_id`, `selected_option_id`는 candidate
  ID를 가리켜야 하고, baseline은 `maintain` kind여야 하며 recommendation과
  selection은 `applicable: true` candidate만 가리킨다.
- 모든 candidate는 다음 정확한 field를 가진다: `id`, `kind`, `applicable`,
  `unavailable_reason`, `summary`, `equivalence_plan`, `integrity_plan`,
  `normalization_assessment`, `denormalization_assessment`,
  `physical_design_assessment`, `read_write_cost`, `operational_risks`,
  `transition_risks`, `rollback_or_exit`.
- `kind`는 `maintain`, `query_change`, `physical_design`, `normalize`,
  `denormalize` 중 하나다. `applicable: true`이면 `unavailable_reason`은 null이고,
  false이면 구체적인 사유가 필요하다. `operational_risks`와 `transition_risks`는
  string 목록이다.
- `normalization_assessment`는 null 또는 설명 string이다. `column`, `constraint`,
  `erd` surface가 있으면 selected candidate에서 비어 있을 수 없다.
  `denormalize` kind의 `denormalization_assessment`는
  `source_of_truth`, `consistency_window`, `reconciliation`, `rollback`을 가진
  object이고 다른 kind에서는 null이다. `physical_design` kind의
  `physical_design_assessment`는 `read_benefit`, `write_amplification`, `storage`,
  `build_or_lock`, `rollback`을 가진 object이고 다른 kind에서는 null이다.
- `change_surfaces`는 필요한 것만 `query`, `index`, `column`, `constraint`,
  `erd`, `normalize`, `denormalize`로 기록한다.
- index 변경은 명시적으로 선택된 physical-design 후보여야 한다. planner가 근거 없이
  index를 추가하거나 선택하지 않는다.

`local_data_test_waiver`는 decision의 optional top-level field다. 기본값은
null or omitted다. `test_command`가 없고 waiver를 선택할 때만 nonempty
`reason`, `approver`, `timestamp`를 가진 object를 기록한다. `timestamp`는 UTC ISO 8601 형식이어야 하며,
waiver는 local data test만 면제한다.

Correctness, equivalence, and integrity are hard gates. A candidate without an
equivalence and integrity plan cannot be recommended or selected.

- 사용자에게 질문할 수 있는 경우는 선택 가능한 후보가 2개 이상이고 요구사항과
  프로젝트 관례만으로 선택할 수 없는 **material** 차이가 있을 때뿐이다. 후보의
  표기, 파일명, 이미 결정된 제약을 확인하려고 질문하지 않는다.

DB 신호가 있으면 production schema는 mandatory다. manifest의
`database_validation` profile은 프로젝트가 제공하는 schema/verify/test command
계약이며, AWF가 DB driver를 제공하거나 data masking을 수행한다는 약속이 아니다.

Production primary is never a verify/test benchmark or executable-query target. Production provides only read-only schema metadata; data comes only from an explicitly approved replica, warehouse, or sanitized local dataset.
`awf wf db-check`가 검증·기록한
`.workflow/artifacts/database-validation-evidence.json`만 gate evidence로
사용한다. plan.md의 서술, command 출력 요약, agent finding은 그 evidence를
대체하지 않는다.

### 7. Gate G1 검증

**반드시 아래 CLI 명령으로 검증합니다** (LLM 판단이 아닌 결정론적 Python 검증기 사용):

```bash
awf wf db-check --stage plan --repo-root . --json
awf wf gate plan --repo-root . --json
```

이 명령은 `evaluate_plan_gate()`를 호출하여 다음을 자동 검증합니다:
- 4개 필수 artifact 존재 + 읽기 가능 (spec.md, plan.md, tasks.md, test-criteria.md)
- spec.md에 `[NEEDS CLARIFICATION]` 마커 0개
- tasks.md에 최소 1개 task (`- [ ] T` 패턴)
- spec.md의 모든 FR-NNN이 plan.md/tasks.md/test-criteria.md에 태그로 존재
- manifest.constitution_path 설정 시 constitution 파일 존재

명령 결과가 `G-plan: PASS`이면 통과, `FAIL`이면 실패.
JSON 상세 결과가 필요하면 `awf wf gate plan --json`.

**주의**: 이 명령은 평가만 수행합니다. state.json 업데이트는 아래 "G1 통과 시" 절차를 직접 수행하세요.

**G1 통과 시:**
- state.json 업데이트:
  - `phases.plan.status: "completed"`, `phases.plan.completedAt: <timestamp>`
  - `gates.G1.passed: true`, `gates.G1.checkedAt: <timestamp>`
  - `gates.G1.artifact_hashes`: spec.md, plan.md, tasks.md, test-criteria.md 해시
  - `currentPhase: "review"`
- history에 기록

**G1 실패 시:**
- `db-check` exit `1`이면 G1을 평가하지 않는다. profile, decision, production schema
  blocker를 수정하고 plan에 남아 위의 두 명령을 같은 순서로 다시 실행한다.
- exit `2`이면 사용법, repo root, command 실행 환경을 운영자가 수정할 때까지 중단한다.

- `db-check`가 통과한 뒤 G1이 실패하면 `phases.plan.retries += 1`
- CLI 출력에서 ✗ 표시된 조건을 확인하고 해당 산출물을 수정
- 수정 후 위의 두 명령을 같은 순서로 재실행

## 주의사항

- spec.md는 WHAT/WHY에 집중. HOW는 plan.md에.
- tasks.md의 각 task는 파일 경로를 반드시 포함.
- 모든 산출물의 FR-NNN 태그는 spec.md에 정의된 ID와 정확히 일치해야 한다.
- test-criteria.md의 각 ATC는 최소 1개의 FR을 참조해야 한다.
- context_providers가 없어도 동작해야 함 (빈 프로젝트 대응).
- constitution이 없는 프로젝트는 info 레벨로 진행 (error 아님).
- AI workflow project는 analysis-docs MCP에서 도메인 컨텍스트 자동 수집.
