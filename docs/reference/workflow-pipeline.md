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

## 6. 에러 유형 상세

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

## 7. 상태 디렉토리 구조

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
