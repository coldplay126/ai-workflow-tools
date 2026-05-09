---
name: phase-plan
version: 2.0.0
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

## 게이트 프리앰블

1. `.workflow/state.json` 읽기. 없으면: "워크플로우가 초기화되지 않았습니다. `/wf`를 먼저 실행하세요." 중단.
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

### 7. Gate G1 검증

**반드시 아래 CLI 명령으로 검증합니다** (LLM 판단이 아닌 결정론적 Python 검증기 사용):

```bash
awf wf gate plan
```

이 명령은 `evaluate_plan_gate()`를 호출하여 다음을 자동 검증합니다:
- 4개 필수 artifact 존재 + 읽기 가능 (spec.md, plan.md, tasks.md, test-criteria.md)
- spec.md에 `[NEEDS CLARIFICATION]` 마커 0개
- tasks.md에 최소 1개 task (`- [ ] T` 패턴)
- spec.md의 모든 FR-NNN이 plan.md/tasks.md/test-criteria.md에 태그로 존재
- manifest.constitution_path 설정 시 constitution 파일 존재

명령 결과가 `G-plan: PASS`이면 통과, `FAIL`이면 실패.
JSON 상세 결과가 필요하면 `awf wf gate --json plan`.

**주의**: 이 명령은 평가만 수행합니다. state.json 업데이트는 아래 "G1 통과 시" 절차를 직접 수행하세요.

**G1 통과 시:**
- state.json 업데이트:
  - `phases.plan.status: "completed"`, `phases.plan.completedAt: <timestamp>`
  - `gates.G1.passed: true`, `gates.G1.checkedAt: <timestamp>`
  - `gates.G1.artifact_hashes`: spec.md, plan.md, tasks.md, test-criteria.md 해시
  - `currentPhase: "review"`
- history에 기록

**G1 실패 시:**
- `phases.plan.retries += 1`
- CLI 출력에서 ✗ 표시된 조건을 확인하고 해당 산출물을 수정
- 수정 후 `awf wf gate plan` 재실행

## 주의사항

- spec.md는 WHAT/WHY에 집중. HOW는 plan.md에.
- tasks.md의 각 task는 파일 경로를 반드시 포함.
- 모든 산출물의 FR-NNN 태그는 spec.md에 정의된 ID와 정확히 일치해야 한다.
- test-criteria.md의 각 ATC는 최소 1개의 FR을 참조해야 한다.
- context_providers가 없어도 동작해야 함 (빈 프로젝트 대응).
- constitution이 없는 프로젝트는 info 레벨로 진행 (error 아님).
- AI workflow project는 analysis-docs MCP에서 도메인 컨텍스트 자동 수집.
