# Workflow Pipeline Reference

운영값, 스키마 상세, Phase별 설정 등 `docs/patterns/workflow-pipeline/`에서 분리된 구현 세부와 운영값.
pattern 문서의 불변식/파생 규칙이 참조하는 구체적 수치와 구조를 정의한다.

---

## 1. Operational Values

### 실행 카운터

| 항목 | 값 | 근거 |
|------|------|------|
| `MAX_TOTAL_EXECUTIONS` | 30 | 7 Phase × (평균 retry 2회 + replan 2회) = 28, 여유 포함 |
| 증가 시점 | Phase 시작 + Gate 결과 적용 | 양쪽 모두 증가하여 단일 Phase 무한 반복 방지 |
| 초과 시 동작 | RuntimeError, 워크플로우 중단 | 수동 리셋(`reset`) 필요 |

### Phase별 Retry Budget

| Phase | retry.max | 근거 |
|-------|-----------|------|
| plan | 3 | 설계 단계이므로 여러 번 수정 가능 |
| review | 2 | 교차 검증 결과 정정 기회 |
| approve | 1 | 사람 승인이므로 최소한의 재시도 |
| impl | 5 | 구현 단계에서 가장 많은 재시도 허용 |
| verify | 2 | 검증 결과 수정 기회 |
| test | 3 | 테스트 실패 수정 기회 |
| done | 0 | 최종 확인이므로 재시도 없음 |

### Replan Budget

| 항목 | 값 |
|------|------|
| `loop.maxReplans` | 3 |
| 소진 시 | escalate_user |

### 변경 등급 감지 임계값

| 항목 | 값 |
|------|------|
| 텍스트 길이 임계값 | 30자 |
| 분류 방식 | 문자 수(char count) 기반, CJK 호환 |

### 고위험 키워드 패턴

| 도메인 | 키워드 (영문) | 키워드 (CJK) |
|--------|-------------|------------|
| 인증/인가 | auth, authentication, authorization | 인증, 인가 |
| 결제 | payment, billing | 결제 |
| 데이터 삭제 | delete, drop, truncate | 삭제 |
| 마이그레이션 | migration | 마이그레이션 |
| 민감 정보 | secret, credential, token | 비밀키 |
| 인프라 | infra, infrastructure, terraform, k8s, kubernetes | |
| 프로덕션 | production, prod | 프로덕션 |

---

## 2. Agent Card 스키마

### JSON 구조

```json
{
  "name": "phase-{phase}",
  "version": "1.0.0",
  "description": "Phase의 역할 설명",

  "input": {
    "required_artifacts": [
      { "key": "artifact_name", "path": "artifacts/file.md" }
    ],
    "required_state": {
      "gates": { "G_prev": { "passed": true } }
    },
    "optional_context": [
      { "key": "context_name", "path": "optional-file.md" }
    ]
  },

  "output": {
    "artifacts": [
      { "key": "output_name", "path": "artifacts/output.md", "format": "markdown", "required": true }
    ],
    "structured_result": {
      "field_name": "type_description"
    }
  },

  "gate": {
    "id": "G_N",
    "pass_conditions": ["condition_expression"],
    "on_pass": { "next_phase": "next_phase_name" },
    "on_fail": {
      "failure_type": { "next_phase": "target_phase" }
    }
  },

  "retry": { "max": 3 },
  "hil": false
}
```

### Phase별 Agent Card 비교

| Phase | Gate ID | retry.max | hil | on_pass | on_fail 주요 분기 |
|-------|---------|-----------|-----|---------|------------------|
| plan | G1 | 3 | false | review | missing_artifact → plan |
| review | G2 | 2 | false | approve | critical_found → plan, high_only → prompt_user |
| approve | G3 | 1 | true | impl | revision → plan, rejected → 중단 |
| impl | G4 | 5 | false | verify | incomplete_tasks → impl |
| verify | G5 | 2 | false | test | scope_violation → approve, impl_bug → impl |
| test | G6 | 3 | false | done | regression_failure → impl |
| done | -- | 0 | true | 완료 | -- |

---

## 3. Phase별 on_fail 상세 라우팅

| Phase | 실패 유형 | 동작 | 대상 Phase |
|-------|----------|------|-----------|
| plan | missing_artifact | prompt_user + replan | plan |
| plan | clarification_needed | prompt_user + replan | plan |
| review | critical_found | replan + feedback 생성 | plan |
| review | high_only | prompt_user | -- |
| approve | revision | replan + feedback 생성 | plan |
| approve | rejected | 워크플로우 중단 | null |
| impl | incomplete_tasks | retry | impl |
| verify | scope_violation | replan | approve |
| verify | impl_bug | replan | impl |
| verify | arch_issue | replan | plan |
| test | regression_failure | replan | impl |

---

## 4. Phase별 전제조건

| Phase | 필요한 선행 Gate | 의미 |
|-------|----------------|------|
| plan | (없음) | 첫 Phase |
| review | G1 passed | plan 완료 필요 |
| approve | G2 passed | review 통과 필요 |
| impl | G3 passed | approve 통과 필요 |
| verify | G4 passed | impl 완료 필요 |
| test | G5 passed | verify 통과 필요 |
| done | G6 passed | test 통과 필요 |

---

## 5. structured_result_shape 필수 필드

| Phase | 필수 필드 | 검증 내용 |
|-------|---------|----------|
| review | `findings` (array), `coverage` (object), `coverage.percentage` | findings가 배열, coverage에 percentage 존재 |
| verify | `scope` (object), `compliance` (object), `quality` (object) | violations, fail, percentage, critical 존재 |

### pass_conditions 표현식 예시

| 조건 표현식 | 적용 Phase |
|------------|-----------|
| `findings.count(severity=CRITICAL) == 0` | review |
| `coverage.percentage >= 80` | review |
| `scope.violations == 0` | verify |
| `compliance.percentage >= 90` | verify |
| `quality.critical == 0` | verify |

---

## 6. Database validation route

The route is signal-gated: a workflow without a detected database change reports
`not_applicable`; a detected change must pass its stage-specific checks before
the enclosing gate can pass. The exact operator sequence is:

```bash
awf wf db-check --stage plan --repo-root <repo-root> --json
awf wf gate plan --repo-root <repo-root> --json

awf wf db-check --stage verify --repo-root <repo-root> --json
awf wf gate verify --repo-root <repo-root> --result-file <verify-result> --json

awf wf db-check --stage test --repo-root <repo-root> --json
awf wf gate test --repo-root <repo-root> --result-file <test-result> --json
```

| Stage | Required database conditions when signaled |
|-------|--------------------------------------------|
| plan | signal classification, high-risk route, selected comparative decision, current production schema |
| verify | production schema, equivalence, integrity, query plan, migration, rollback |
| test | production schema and local test, or a recorded waiver for the missing local test command |

`database-decision.json` contains two or three materially different candidates,
including a `maintain` baseline. Each candidate documents equivalence and
integrity plans, operational and transition risk, read/write cost, and rollback
or exit. The decision names only the relevant change surfaces: query, index,
column, constraint, ERD, normalize, or denormalize.

Production schema evidence is mandatory. Use a same-engine local environment
for DDL and planner work; DuckDB may support profiling or equivalence analysis
but cannot stand in for same-engine evidence. A project-specific replica sample
requires explicit opt-in. raw primary rows are prohibited, and the test result
must state that its rows are masked. A waiver applies only to the absence of a
local test command and requires a decision reason, approver, and timestamp.

The project schema command must return only current production metadata:

```json
{
  "schema_version": 1,
  "kind": "production_schema",
  "target_class": "production_metadata",
  "read_only": true,
  "schema_only": true,
  "engine": "mysql",
  "engine_version": "8.0",
  "captured_at": "2026-08-24T00:00:00Z",
  "schema_hash": "<sha256>",
  "object_counts": {
    "tables": 1,
    "columns": 8,
    "indexes": 2,
    "constraints": 3
  }
}
```

The metadata command is read-only and schema-only. It must not access rows or
run executable work against the production primary.

The CLI validates sanitized JSON supplied by project commands. It does not
implement a database driver, masking, or replica provisioning.

---

## 7. 에러 유형 상세


| 에러 유형 | 복구 가능 | 복구 동작 | backoff (초) | 판별 키워드 |
|----------|----------|----------|-------------|-----------|
| `timeout` | 예 | retry | 10 | timeout, timed out |
| `rate_limited` | 예 | retry | 60 | rate, limit |
| `budget_exceeded` | 아니오 | abort | 0 | budget, token limit, context length |
| `format_error` | 예 | retry | 0 | format, json, parse |
| `provider_unavailable` | 예 | fallback | 5 | not found, unavailable, connection |
| `permission_denied` | 아니오 | abort | 0 | permission, denied, forbidden |
| `invalid_state` | 아니오 | abort | 0 | invalid state, missing workflow |
| `unknown` | 예 | escalate_user | 0 | (위 패턴 미매칭) |

---

## 8. 상태 디렉토리 구조

```
.workflow/
├── state.json
├── manifest.json
├── concept.md
├── agent-cards/
│   ├── plan.json
│   ├── review.json
│   ├── approve.json
│   ├── impl.json
│   ├── verify.json
│   ├── test.json
│   └── done.json
├── artifacts/
│   ├── spec.md
│   ├── plan.md
│   ├── tasks.md
│   ├── allowed-files.json
│   ├── review-report.md
│   ├── approval.json
│   ├── impl-log.md
│   ├── verification-report.md
│   ├── test-report.md
│   ├── database-decision.json
│   ├── database-validation-evidence.json
│   └── confirmation.json
└── tmp/
```

### state.json 핵심 필드

| 필드 | 용도 | 갱신 시점 |
|------|------|----------|
| `currentPhase` | 현재 활성 Phase 또는 종료 상태 | Phase 시작, Gate 결과, replan, abort |
| `phases.{phase}.status` | 개별 Phase 상태 | 모든 상태 전이 |
| `phases.{phase}.retries` | Phase별 누적 재시도 횟수 | Gate FAIL 시 증가 |
| `gates.{gate_id}.passed` | Gate 통과 여부 | Gate 평가 완료 |
| `totalExecutions` | 전체 실행 횟수 | Phase 시작 및 Gate 결과마다 증가 |
| `loop.replanCount` | 누적 replan 횟수 | replan 실행 시 증가 |
| `changeClass` | 변경 위험 등급 | 워크플로우 초기화 |
| `history` | 모든 상태 전이 이력 | 모든 상태 변경 |

---

## 9. allowed-files.json 그래프 확장 (`awf wf expand-scope`)

`allowed-files.json`은 plan SKILL이 `tasks.md`에서 추출한 `planned_files` 목록이다. LLM이 직접 만들기 때문에 dependent / dependency 파일이 누락되어 G5 SCOPE_VIOLATION false positive가 자주 발생한다.

plan SKILL은 `allowed-files.json` 생성 직후 `awf wf expand-scope --direction dependents`를 기본 hook으로 실행한다. 분석된 graph가 없거나 추가 파일이 없으면 정상 no-op으로 끝나며, graph가 있으면 unit별 `import-graph.json`을 사용해 결정론적으로 `expanded_files`를 추가하고 audit trail을 남긴다.

```bash
# plan SKILL 기본 hook: 직접 dependent만 추가 (1-hop)
awf wf expand-scope --direction dependents

# 수동 조정: 1-hop dependents + imports 양방향
awf wf expand-scope --direction both

# 수동 조정: 전체 transitive closure (위험 — 의도적으로만)
awf wf expand-scope --direction dependents --depth 0

# 수동 점검: 작성 안 하고 미리보기
awf wf expand-scope --dry-run --json
```

| 옵션 | 기본 | 설명 |
|------|------|------|
| `--direction` | `dependents` | `dependents` (consumers) / `imports` (deps) / `both` |
| `--depth` | `1` | 그래프 traversal 깊이. `0` 또는 음수 → 전체 closure |
| `--service` | (모든 서비스) | 검색 범위를 특정 서비스로 제한 (반복 가능) |
| `--runtime-only` | off | type-only edge 제외. analysis pipeline 기본은 포함. |
| `--dry-run` | off | 파일 수정 없이 결과만 출력 |

쓰기 시 `allowed-files.json`에 추가되는 필드:
- `expanded_files`: 정렬·중복 제거된 추가 경로 목록
- `graph_expansion`: 사용된 direction/depth, 항목별 사유(`dependent_of:X` / `import_of:X`), planned_files coverage 진단

## 10. G5 결정론적 스코프 검증 (`awf wf scope-check`)

verify SKILL은 `awf wf scope-check`를 호출해 결정론적으로 SCOPE_VIOLATION을 판정한다. LLM이 git diff와 allowed-files를 직접 비교하지 않는다.

```bash
awf wf scope-check --json
```

동작:
1. base branch 추론 (`state.baseBranch` → `main`/`master`/`staging` 순)
2. `git diff --name-only <base>...HEAD` 실행, `.workflow/` 경로는 자동 제외 (WF 인프라)
3. 각 변경 파일을 다음으로 분류:
   - `planned`: `planned_files`에 명시
   - `expanded`: `expanded_files`에 있음 (reason 필드에 `dependent_of:X` / `import_of:X` 사유)
   - `violation`: 어느 셋에도 없음 → SCOPE_VIOLATION
4. 종료 코드: 위반 1건 이상이면 1, 아니면 0

| 옵션 | 기본 | 설명 |
|------|------|------|
| `--base-branch` | (자동 추론) | `state.baseBranch` → `main`/`master`/`staging` |
| `--no-expanded` | off | `expanded_files`를 무시하고 legacy 동작 (planned_files만 비교) |
| `--json` | off | 분류 결과 + 위반 목록을 JSON으로 출력 (verify SKILL이 그대로 인용) |
