---
name: phase-verify
version: 1.2.0
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

## Deterministic Phase Preflight

수동으로 이 phase를 실행할 때도 [wf-orchestrator/reference/deterministic-preflight.md](../wf-orchestrator/reference/deterministic-preflight.md)의
Phase Skill Preflight를 따릅니다. 이 phase의 dry-run 명령은 다음과 같습니다:

```bash
awf wf next --phase verify --repo-root . --dry-run --output-format json
```

## 게이트 프리앰블

1. `.workflow/state.json` 읽기.
2. **G4 통과 확인**: `gates.G4.passed` true 확인.
3. `phases.verify.retries`가 2 이상이면 중단.
4. TTL 경고.

## 실행 흐름

### 1. state.json 업데이트
`phases.verify.status: "in_progress"`.

### 2. 스코프 검증 (닫힌계 강제)

**반드시 아래 CLI 명령으로 검증합니다** (LLM 판단이 아닌 결정론적 Python 분류기 사용):

```bash
awf wf scope-check --json
```

이 명령은 base branch 자동 추론 → `git diff --name-only <base>...HEAD` → `allowed-files.json`의 `planned_files ∪ expanded_files` 비교를 한 번에 수행하고 파일별 분류와 종료 코드(0=PASS, 1=FAIL)를 반환합니다.

분류 결과:
- **planned**: 정상 (사용자가 명시한 스코프)
- **expanded**: 정상 (`expand-scope`가 graph로 추가한 dependent/import; reason 필드에 사유 포함)
- **violation**: **SCOPE_VIOLATION** (어느 셋에도 없음)

`--no-expanded` 플래그는 expanded_files를 무시하고 legacy 동작(planned_files만 비교)으로 fallback합니다. expanded 영역을 의도적으로 좁게 가두려는 정책에서만 사용하세요.

위반이 있으면 `verification-report.md`의 "스코프 검증" 표에 CRITICAL로 기록하고, 각 행에 `awf wf scope-check`의 reason을 그대로 인용합니다.

### 2.5 데이터베이스 evidence 검증

DB 신호가 있으면 G5 전에 `awf wf db-check --stage verify --json`을 실행한다.
이 단계는 plan stage의 current production schema hash와 같은 schema를
확인하고, selected option에 대한 equivalence, integrity, query plan,
migration, rollback 상태를 검증한다. 문서의 주장이나 verification-report.md의
요약은 `.workflow/artifacts/database-validation-evidence.json`을 대신할 수 없다.

DDL과 planner 확인은 production engine과 같은 engine의 local 환경에서 수행한다.
DuckDB는 profiling 또는 equivalence 분석에 사용할 수 있지만 same-engine DDL/planner
검증을 대신하지 않는다. DB driver, 복제본 생성, masking은 AWF가 구현하는 기능이
아니며 project-provided command가 책임진다.

Production primary is never a verify/test benchmark or executable-query target. Production provides only read-only schema metadata; data comes only from an explicitly approved replica, warehouse, or sanitized local dataset.

### 2.6 verify evidence execution contract

verify command evidence에는 `engine`, `execution_target`,
`production_primary_queries`: false, `raw_production_rows`: false가 필요하다.
`execution_target`은 `local_same_engine` 또는 `approved_read_replica`다.
index를 포함한 structural surface는 `local_same_engine`과 production schema의
동일 `engine`을 요구한다. query planner 확인만 `approved_read_replica`를 사용할 수
있다. DuckDB, cross-engine 실행, production primary는 이 verify evidence의 target이
될 수 없다.

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
- [ ] DB 신호 시 production schema, equivalence, integrity, query plan, migration, rollback evidence 통과

`awf wf next`가 stderr에 출력한 `result: /actual/result/path`의 실제 경로를
`VERIFY_RESULT`에 설정한 뒤 다음 block을 실행한다.

```bash
: "${VERIFY_RESULT:?set from the result path emitted by awf wf next}"
awf wf db-check --stage verify --repo-root . --json
awf wf gate verify --repo-root . --result-file "$VERIFY_RESULT" --json
```
**G5 통과 시:**
- state.json: `phases.verify: completed`, `gates.G5.passed: true`, `currentPhase: "test"`
- 출력: `✓ G5 게이트 통과`

**G5 실패 — 분기 결정 트리:**

`db-check` exit `1`은 G5를 평가하지 않는다. decision/profile/production schema
blocker 또는 stale evidence는 `plan`으로 보내 계획 evidence를 새로 만들고,
equivalence, integrity, query plan, migration, rollback failure는 `impl`로 보내
구현을 고친다. exit `2`는 command 또는 환경 설정을 운영자가 고칠 때까지 verify를
중단한다.

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

`db-check`가 통과한 뒤 G5가 실패하면 `phases.verify.retries += 1` 후 해당 phase로 회귀.

## 주의사항

- 스코프 검증은 `git diff` 기반 (commit되지 않은 변경사항 포함).
- Spec 준수 검증은 LLM 판단 의존. 100% 정확하지 않을 수 있음.
- SCOPE_VIOLATION은 항상 CRITICAL. 의도적 추가라면 재승인 필요.
