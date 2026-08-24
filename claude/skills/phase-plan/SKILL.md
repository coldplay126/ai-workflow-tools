---
name: phase-plan
version: 2.3.0
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
`.workflow/concept.md`를 읽는다. 실행을 막는 필수 사실이 없을 때만 보충을 요청하며, 되돌릴 수 있는 선호나 material하지 않은 선택을 확인하려고 질문하지 않는다.

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
  `physical_design_assessment`, `covered_surfaces`, `surface_assessments`,
  `read_write_cost`, `operational_risks`, `transition_risks`, `rollback_or_exit`.
- Every candidate covers every decision surface. `covered_surfaces`는 정렬된
  `change_surfaces`와 정확히 같고, `surface_assessments`는 같은 key를 모두 가지며
  각 value는 비어 있지 않은 평가다. index surface에서는 각 holistic option이
  add, reject, 또는 maintain 결론을 평가한다. index를 자동으로 선택하지 않는다.
- `kind`는 `maintain`, `query_change`, `physical_design`, `normalize`,
  `denormalize` 중 하나이며 candidate의 dominant strategy다. surface마다 별도의
  candidate kind를 강제하지 않는다. `applicable: true`이면 `unavailable_reason`은
  null이고, false이면 구체적인 사유가 필요하다. `operational_risks`와
  `transition_risks`는 string 목록이다.
- `normalization_assessment`는 null 또는 설명 string이다. `column`, `constraint`,
  `erd` surface가 있으면 selected candidate에서 비어 있을 수 없다.
  `denormalize` kind의 `denormalization_assessment`는
  `source_of_truth`, `consistency_window`, `reconciliation`, `rollback`을 가진
  object이고 다른 kind에서는 null이다. `physical_design` kind의
  `physical_design_assessment`는 `read_benefit`, `write_amplification`, `storage`,
  `build_or_lock`, `rollback`을 가진 object이고 다른 kind에서는 null이다.
- `change_surfaces`는 필요한 것만 `query`, `index`, `column`, `constraint`,
  `erd`, `normalize`, `denormalize`, `partition`, `schema_object`로 기록한다.

| emitted signal | required surface |
|---|---|
| `text:table_ddl` | `erd` |
| `text:column_ddl` | `column` |
| `text:index_ddl` | `index` |
| `text:constraint_ddl` | `constraint` |
| `text:schema_ddl`, `text:database_ddl`, `text:type_ddl`, `text:sequence_ddl`, `text:trigger_ddl`, `text:routine_ddl` | `schema_object` |
| `text:view_ddl` | `schema_object`, `query` |
| `text:migration`, `path:migration:`, `path:prisma:` | any structural surface |
| `text:normalization`, `text:denormalization`, `text:erd`, key signals, `text:partition` | their matching `normalize`, `denormalize`, `erd`, `constraint`, `partition` surface |
| `text:sql syntax`, `text:order by` | `query` |

`path:models:`와 generic model/model paths는 DB signal이 아니지만
`path:database/models:`는 DB signal이다. `artifact_error:`는 artifact를 고칠 때까지
결정을 차단한다. Signal-derived `requires_query_plan`과
`requires_migration_rollback`은 declared surface와 독립적인 hard gate다.

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

### 6.3 Planning Options lifecycle

manifest의 `planning_options.required`가 `true`이면
`.workflow/artifacts/planning-options.json`을 canonical decision artifact로 작성한다.
이 artifact는 planner의 prose, chat transcript, 또는 worker result가 대신할 수 없다.

- 질문은 요구사항과 프로젝트 관례로 해결할 수 없는 **material** decision에만 만든다.
  되돌릴 수 있는 선호, 표기, 파일명, 이미 확정된 제약은 decision이나 질문으로 만들지
  않는다. materiality는 `external_behavior`, `compatibility_migration`, `security_slo`,
  `scope_delivery_risk`, `lifecycle_reversibility` 축으로 기록한다.
- 각 decision은 `D-NNN` ID, question, materiality axes, 2개 또는 3개의 서로 다른
  `O-NNN` option을 가진다. option의 `summary`, `affected_work`, `acceptance_delta`,
  `work_risks`, `transition_risks`, `rollback_or_exit`는 실질적으로 달라야 한다.
- recommendation을 option 목록보다 먼저 제시한다. `recommended_option_id`는 첫
  option을 가리키고, `recommendation_rationale`은 비어 있을 수 없다.
- material decision이 없으면 `status: "no_decision_required"`와 non-empty
  `no_decision_reason`, 빈 `decisions`/`selection_history`를 기록한다. 이 경로는
  사용자 선택 없이 G1으로 진행한다.
- material decision이 있으면 selected field가 비어 있는
  `status: "selection_required"` artifact를 기록하고, G1을 통과시키지 않는다.
  worker는 `recommended_action: "user_decision"` escape를 반환해 parent workflow가
  plan을 `deciding`으로 전환하게 한다. `hil`을 true로 바꾸거나 worker가 state를
  직접 mutation하지 않는다.

#### Canonical schema and validation

Write `schema_version: 1` and exactly these top-level fields:
`schema_version`, `status`, `no_decision_reason`, `decisions`, `selection_history`.
Every decision has exactly `id`, `question`, `materiality_axes`, `options`,
`recommended_option_id`, `recommendation_rationale`, `selected_option_id`,
`selected_by`, `selected_at`. Every option has exactly `id`, `summary`,
`affected_work`, `acceptance_delta`, `work_risks`, `transition_risks`,
`rollback_or_exit`. Every history entry has exactly `decision_id`,
`previous_option_id`, `selected_option_id`, `selected_by`, `selected_at`,
`source`, and `source` is `cli`.

`no_decision_required` requires a non-empty `no_decision_reason` and empty
`decisions`/`selection_history`. `selection_required` requires
`no_decision_reason: null`, at least one decision, and at least one decision
whose `selected_option_id`/`selected_by`/`selected_at` are all null; any
selected triple is all-present and must have matching append-only history.
`selected` requires `no_decision_reason: null`, at least one decision, every
selected triple all-present, and matching append-only history. The canonical
loader validates the written artifact before any `user_decision` escape; a
loader failure is `artifact_invalid`, not an escape.

#### Canonical fixture: no_decision_required

```json
{
  "schema_version": 1,
  "status": "no_decision_required",
  "no_decision_reason": "Requirements and project conventions determine the implementation.",
  "decisions": [],
  "selection_history": []
}
```

#### Canonical fixture: selection_required

```json
{
  "schema_version": 1,
  "status": "selection_required",
  "no_decision_reason": null,
  "decisions": [
    {
      "id": "D-001",
      "question": "Which compatibility strategy should the API use?",
      "materiality_axes": ["external_behavior", "compatibility_migration"],
      "options": [
        {
          "id": "O-001",
          "summary": "Ship a versioned endpoint and preserve the existing endpoint.",
          "affected_work": ["Add the v2 route and contract tests."],
          "acceptance_delta": "Existing clients retain their response shape.",
          "work_risks": ["Two endpoint contracts require temporary maintenance."],
          "transition_risks": ["Consumers may migrate later than planned."],
          "rollback_or_exit": "Remove v2 before publishing it."
        },
        {
          "id": "O-002",
          "summary": "Replace the existing endpoint with the new response contract.",
          "affected_work": ["Update every client and delete the previous route."],
          "acceptance_delta": "All clients must use the new response shape.",
          "work_risks": ["Coordinated client releases are required."],
          "transition_risks": ["Unmigrated consumers fail at cutover."],
          "rollback_or_exit": "Restore the prior route from the release branch."
        }
      ],
      "recommended_option_id": "O-001",
      "recommendation_rationale": "It preserves compatibility while clients migrate deliberately.",
      "selected_option_id": null,
      "selected_by": null,
      "selected_at": null
    }
  ],
  "selection_history": []
}
```

#### Canonical fixture: selected

```json
{
  "schema_version": 1,
  "status": "selected",
  "no_decision_reason": null,
  "decisions": [
    {
      "id": "D-001",
      "question": "Which compatibility strategy should the API use?",
      "materiality_axes": ["external_behavior", "compatibility_migration"],
      "options": [
        {
          "id": "O-001",
          "summary": "Ship a versioned endpoint and preserve the existing endpoint.",
          "affected_work": ["Add the v2 route and contract tests."],
          "acceptance_delta": "Existing clients retain their response shape.",
          "work_risks": ["Two endpoint contracts require temporary maintenance."],
          "transition_risks": ["Consumers may migrate later than planned."],
          "rollback_or_exit": "Remove v2 before publishing it."
        },
        {
          "id": "O-002",
          "summary": "Replace the existing endpoint with the new response contract.",
          "affected_work": ["Update every client and delete the previous route."],
          "acceptance_delta": "All clients must use the new response shape.",
          "work_risks": ["Coordinated client releases are required."],
          "transition_risks": ["Unmigrated consumers fail at cutover."],
          "rollback_or_exit": "Restore the prior route from the release branch."
        }
      ],
      "recommended_option_id": "O-001",
      "recommendation_rationale": "It preserves compatibility while clients migrate deliberately.",
      "selected_option_id": "O-001",
      "selected_by": "workflow-owner",
      "selected_at": "2026-08-24T00:00:00Z"
    }
  ],
  "selection_history": [
    {
      "decision_id": "D-001",
      "previous_option_id": null,
      "selected_option_id": "O-001",
      "selected_by": "workflow-owner",
      "selected_at": "2026-08-24T00:00:00Z",
      "source": "cli"
    }
  ]
}
```

사용자는 recommendation-first output을 검토한 뒤 아래의 **정확한 실행 명령**으로
한 decision을 선택한다. `AWF_OPERATOR`에는 `planner` 같은 placeholder가 아니라
실제 human 또는 service actor identity를 설정한다. 다른 flag 순서, positional
alias, 또는 state 직접 수정은 사용하지 않는다.

```bash
awf wf select-option --decision-id D-001 --option-id O-001 --actor "${AWF_OPERATOR:?set operator identity}" --repo-root . --json
```

선택이 모두 끝나면 artifact는 `status: "selected"`가 되고 append-only
`selection_history`에 `source: "cli"` record가 남는다. 다음 plan rerun은 selected
artifact를 input으로 로드하여 selection을 다시 묻지 않고 spec/plan/tasks/test
criteria를 생성한 뒤 G1을 다시 평가한다. `no_decision_required` artifact도 같은
rerun input이며, reason을 보존하고 질문 없이 계속한다.

이미 G1을 통과한 뒤 다른 option을 선택하면 CLI는 `replanned`를 반환한다. canonical
selection 변화는 plan부터 done까지 pending으로 reset하고 retries/executions,
runtime/skip marker, G1–G6 initial shape(`G3.scope_hash: null` 포함)를 초기화하며
`loop.replanCount`와 history semantics를 보존한다. 같은 canonical selection hash를
다시 제출한 경우는 `reuse`이며 replan하지 않는다.

manifest가 없거나 `planning_options` profile이 없고 artifact도 없으면
`legacy_not_required`로 계속한다. `required: false`와 artifact 부재도
`not_required`다. artifact가 존재하면 profile과 무관하게 strict validation한다:
malformed profile 또는 artifact는 fail closed이며 G1 detail은
`profile_invalid` 또는 sanitized `artifact_invalid`이다.

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
- Planning Options의 `planning_options.artifact`, `.shape`, `.selection`,
  `.recommendation`, `.materiality` 조건. `selected`와
  `no_decision_required`는 통과하며 `selection_required`는
  `decision_selection_required` detail로 사용자 결정을 요구한다.

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
- `planning_options.selection`이 `decision_selection_required`이면 retry나 추측을
  하지 않는다. worker escape에 따라 plan은 `deciding`에서 대기하고, 위의
  `awf wf select-option` 명령으로 선택한 뒤 selected artifact input으로 plan을
  rerun한다. 일부 선택만 남으면 `selected_pending`, 모두 선택되면 `continued`다.
- `artifact_invalid`, `profile_invalid`, 또는 required artifact missing은 먼저
  canonical artifact/profile을 고친 뒤 rerun한다. legacy/no-decision path는
  질문 없이 G1 평가를 계속한다.

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
