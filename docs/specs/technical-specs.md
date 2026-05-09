# Technical Specs

> Constitution(C1-C11)을 구현으로 연결하는 기계 판독 가능한 계약 정의.
> 각 스키마는 관련 Constitution 원칙을 명시하고, 현재 구현과의 차이를 기록한다. §1-§10은 Analysis/WF 파이프라인, §11-§13은 A2A 세션 운영 계약.

---

## 1. Observation Schema (Layer 2 Output)

> 관련 원칙: **C1** (관찰/판단 분리), **C4** (다각도 확인)
> 현재: `stage1-file.md` → JSON `{role, imports, exports, summary, complexity}`
> 변경: 구조화된 observation 마크다운 + 필수 JSON 블록

### 1.1 Per-File Observation 형식

Layer 2는 파일별로 아래 형식의 observation을 생성한다. **심각도 판정, 결론, 권장 사항은 포함하지 않는다** (C1).

```markdown
# {file_path}

## 기본 정보
- role: {router|controller|service|dao|entity|model|util|test|config|middleware|component|module|other}
- language: {typescript|python|java|kotlin|swift|go|other}
- lines: {number}

## 엔드포인트
(controller/router인 경우)
- {METHOD} {path} — auth: {guard명|none}, params: {목록}

## 테이블/엔티티 참조
- {TABLE_NAME} (읽기|쓰기|읽기/쓰기: {용도 1줄})

## 외부 서비스 호출
- {ServiceName}.{method}() → {대상: SQS|Redis|HTTP|gRPC 등}

## 의존성 (import)
- {ClassName} ({relative_path|package_name})

## 비즈니스 로직 요약
(메서드별)
1. {methodName}: {단계별 요약}

## 관찰된 신호
(코드에서 보이는 패턴을 사실로만 기록 — 심각도 없음)
- {사실 기술}
```

### 1.2 필수 JSON 블록

마크다운 본문과 함께, 기계 파싱용 JSON 블록을 observation 말미에 포함한다:

```json
{
  "path": "src/notification/notification.service.ts",
  "role": "service",
  "language": "typescript",
  "lines": 245,
  "endpoints": [],
  "tables": [
    {"name": "T_AWF_USER_NOTIFICATIONS", "access": "read", "usage": "unread count 집계"},
    {"name": "T_AWF_WEB_NOTIFICATIONS", "access": "read_write", "usage": "웹 알림 CRUD"}
  ],
  "external_calls": [
    {"service": "PushNotification", "method": "enqueue", "target": "SQS FIFO"},
    {"service": "RedisCacheManager", "method": "mget", "target": "Redis"}
  ],
  "imports": [
    {"name": "PushNotification", "source": "./push.notification"},
    {"name": "DataSource", "source": "typeorm"}
  ],
  "business_logic": [
    {"method": "sendNotification", "steps": ["userIds 중복 제거", "5000명 단위 분할", "SQS 적재"]},
    {"method": "getUnitNotificationCount", "steps": ["팔로우 검증", "일반+공통 알림 unread 합산", "채팅 unread 합산"]},
    {"method": "saveWebNotification", "steps": ["10분 merge window", "동일 이벤트면 actorCount 증가", "아니면 신규 생성"]},
    {"method": "findWebNotification", "steps": ["페이지네이션 조회", "actorUser relation", "i18n title 생성"]}
  ],
  "signals": [
    "payload.tid = this.getRequestContext().id — context가 없을 때 undefined.id 접근 경로 존재",
    "saveWebNotification 기존 알림 조회에 ORDER BY 없음"
  ]
}
```

**마크다운/JSON 동일 의미 규칙**: 마크다운 본문과 JSON 블록은 동일한 정보를 담아야 한다. 마크다운에 있는 항목이 JSON에 없거나 그 반대이면 안 된다. 마크다운은 사람이 읽는 형식, JSON은 기계가 파싱하는 형식이며, 둘 사이에 정보 누락이 발생하면 Writer가 한쪽만 읽는 구현에서 품질 저하가 발생한다.

### 1.3 필수/선택 필드

| 필드 | 필수 | 설명 |
|------|------|------|
| path | 필수 | 파일 경로 |
| role | 필수 | 파일 역할 |
| language | 필수 | 언어 |
| lines | 필수 | 라인 수 |
| endpoints | 조건부 | controller/router인 경우 필수 |
| tables | 선택 | 테이블/엔티티 참조가 있는 경우 |
| external_calls | 선택 | 외부 서비스 호출이 있는 경우 |
| imports | 필수 | 비어 있어도 빈 배열 |
| business_logic | 필수 | 메서드별 단계 요약. 비어 있어도 빈 배열 |
| signals | 필수 | 관찰된 신호. 없으면 빈 배열 |

### 1.4 Observation 워커 행동 규칙

Layer 2 observation 워커는 아래 규칙을 준수한다. 이 규칙은 프롬프트에 명시하여 워커 행동을 제한한다.

**범위 제한**
- 지정된 파일만 분석한다. 다른 파일의 내용을 추측하거나 분석하지 않는다.
- 다른 파일과의 연관은 **입출력 인터페이스(타입, 파라미터, 반환값)만** 기록한다.
- 파일 내 중복 코드 패턴은 확인하되, 다른 파일과의 중복은 인터페이스 수준에서만 언급한다. 파일 간 중복 감지는 비교 판단이므로 Layer 3 리뷰어 책임이다 (C1).

**git history 확인**
- 해당 파일의 최근 변경 이력을 사실만 기록한다: 변경 시점, 커밋 메시지, 변경 빈도.
- 변경의 추정 원인이나 의도 해석은 포함하지 않는다 (C1 — 판단은 Layer 3 책임).
- git history는 워커 호출 전에 정적으로 추출하여 프롬프트에 포함한다 (최근 10건 이내).

**로직 기술 — 3계층 분리**
- 메서드별로 **정상 흐름**, **에러 처리**, **경계 조건**을 분리하여 단계별로 기술한다.
- 정상 흐름: 입력 → 처리 단계 → 출력의 순서
- 에러 처리: try-catch, 조기 반환, fallback 경로를 각각 기록
- 경계 조건: 빈 입력, null, 상한/하한, 부수 효과(side effect) 등

**출력 제한**
- observation 본문에 코드 스니펫을 포함하지 않는다. 위치 참조(파일:행번호)로 대체한다.
- JSON 블록(§1.2)은 기계 파싱용으로 유지한다.

**세션 격리**
- 파일별로 독립 세션에서 실행한다. 이전 파일의 observation을 입력으로 받지 않는다.
- 이전 파일 분석 결과를 참조하거나 언급하지 않는다.

### 1.5 현재 구현과의 차이

| 항목 | 현재 (stage1-file.md) | v3 (observation) |
|------|----------------------|------------------|
| 출력 | JSON만 | 마크다운 + JSON 블록 |
| 내용 | role, imports, summary, complexity | 엔드포인트, 테이블, 외부 호출, 비즈니스 로직, 관찰된 신호 |
| 심각도 | 없음 (이미 준수) | 없음 (C1 유지) |
| 캐시 | 없음 | 파일별 hash 기반 (K1 hashes.json 재사용) |

### 1.6 캐시 계약

```json
{
  "cache_key": "{file_path}:{sha256_hash}",
  "invalidation": "per_file_hash",
  "storage": ".tmp/observations/{sha256(file_path)[:12]}.observation.md"
}
```

저장 경로는 파일 경로의 SHA-256 앞 12자를 사용하여 basename 충돌을 방지한다 (예: `src/foo/index.ts`와 `src/bar/index.ts`가 같은 파일명을 공유하지 않도록). 캐시 조회 시에는 `cache_key`(full path + content hash)로 유효성을 판단한다.

변경된 파일만 observation을 재수집하고, 나머지는 캐시된 observation을 재사용한다.

---

## 2. Domain Bundle Scale Policy

> 관련 원칙: **C5** (위험도 비례 투자)
> 현재: 모든 scale에서 코드 전문을 domain-bundle에 포함
> 변경: scale별 차등 정책

### 2.1 Scale 분류

| Scale | 파일 수 | 기준 |
|-------|--------|------|
| small | ≤ 10 | 비용보다 정확도 우선 |
| standard | 11-30 | 관찰 기반 + 선택적 fallback |
| large | 31+ | 관찰 기반 + 선택적 fallback |

### 2.2 Domain Bundle 구성

| Scale | bundle 내용 | 코드 포함 |
|-------|-----------|----------|
| small | observation + 코드 전문 | 항상 포함 |
| standard | observation만 | fallback 시에만 (confidence=low) |
| large | observation만 | fallback 시에만 (confidence=low) |

### 2.3 코드 Fallback 계약

```json
{
  "trigger": "writer_confidence_low",
  "max_files": 3,
  "max_tokens_per_file": 4000,
  "decision_by": "judge",
  "scope": "standard, large only"
}
```

fallback 발동 조건: Writer가 특정 observation에 대해 `confidence: low`를 표시하고, Judge가 해당 파일의 코드 재주입이 필요하다고 판단.

---

## 3. Writer/Judge Schema (Layer 3 Contract)

> 관련 원칙: **C1** (관찰/판단 분리), **C4** (다관점), **C7** (역할 분리)
> 현재: findings 배열 `{id, category, severity, locations, summary}`
> 변경: claim/evidence/confidence 기반 스키마 + Writer/Judge 역할 명확화

### 3.1 Writer Output Schema

모든 Writer는 동일한 구조로 출력한다:

```json
{
  "writer": "{writer_id}",
  "mode": "{document|review|investigate}",
  "claims": [
    {
      "id": "C1",
      "type": "{endpoint|table|external_call|business_logic|signal|finding}",
      "claim": "주장 내용",
      "evidence": "observation 또는 코드에서의 근거",
      "source_files": ["file1.ts", "file2.ts"],
      "confidence": "high|medium|low"
    }
  ],
  "output_sections": {
    "{section_name}": "해당 Writer가 생성한 산출물 내용"
  }
}
```

### 3.2 Writer 유형별 구성

**문서화 모드:**

| Writer ID | 관점 | output_sections | claim types |
|-----------|------|----------------|-------------|
| structure | API + 데이터 모델 | api-spec, data-model | endpoint, table, external_call |
| behavior | 흐름 + 연동 | domain-overview, external-integration | business_logic, external_call |

**리뷰 모드:**

| Writer ID | 관점 | output_sections | claim types |
|-----------|------|----------------|-------------|
| security | 보안 | security-findings | finding (보안) |
| quality | 품질 | quality-findings | finding (품질) |
| performance | 성능 | performance-findings | finding (성능) |

**조사 모드 (C4 예외):**

| Writer ID | 관점 | output_sections | 특수 권한 |
|-----------|------|----------------|----------|
| investigator | 코드 경로 추적 | investigation-report | 코드 원문 접근, 단일 Writer 허용 |

조사 모드의 단일 Writer 허용 조건 (C4): Code Path Inventory 포함 + 검증 수준 기록(C9).

### 3.3 Judge Input/Output Schema

**Judge 입력**: 모든 Writer의 claims + output_sections

**Judge 출력:**

```json
{
  "judge": "layer3_judge",
  "verdict": "merged",
  "merged_claims": [
    {
      "id": "M1",
      "original_claims": ["structure:C1", "behavior:C3"],
      "resolution": "merged|selected|conflict",
      "conflict_note": "모순이 있는 경우 기록 (없으면 null)"
    }
  ],
  "merged_output": {
    "api-spec.json": "...",
    "data-model.md": "...",
    "domain-overview.md": "...",
    "external-integration.md": "..."
  },
  "consistency_checks": [
    {
      "check": "api-spec endpoint ↔ overview 흐름 대조",
      "result": "pass|fail",
      "detail": "불일치 설명 (있는 경우)"
    }
  ]
}
```

### 3.4 Judge 권한 제약 (C7)

- **할 수 있는 것**: claim 선별, 중복 제거, 모순 발견 시 confidence 높은 쪽 채택, 일관성 검증
- **할 수 없는 것**: evidence chain 수정, 새로운 claim 생성, Writer가 보지 않은 파일 참조

---

## 4. Plan Contract (WF v2 Execution Contract)

> 관련 원칙: **C2** (계약 기반), **C3** (결정론적 gate), **C5** (위험도 비례)
> 현재: `allowed-files.json` (`planned_files` + graph 기반 `expanded_files` + audit)
> 변경: 완전한 실행 계약

### 4.1 plan-contract.json Schema

```json
{
  "version": "2.0",
  "change_class": "small|standard|high-risk",
  "scope": {
    "allowed_files": ["src/quest/quest.service.ts"],
    "forbidden_paths": ["src/common/", "src/auth/"],
    "max_new_files": 2
  },
  "change_type": {
    "migration_allowed": false,
    "public_api_change": false,
    "manual_steps_required": [],
    "rollback_impact": "low|medium|high",
    "expected_artifacts": ["src/quest/quest.service.ts"]
  },
  "requirements": [
    {
      "id": "R1",
      "description": "요구사항 설명",
      "test_scenario": "검증 시나리오",
      "acceptance": "수락 기준"
    }
  ],
  "unresolved": [
    {
      "id": "U1",
      "description": "미결정 사항",
      "decision_needed": "누구의 결정이 필요한가"
    }
  ],
  "test_requirements": {
    "unit": true,
    "integration": false,
    "coverage_threshold": 80
  }
}
```

### 4.2 계약 강제 시점

| 시점 | 검증 내용 | 실패 시 |
|------|----------|---------|
| plan 직후 (기본 hook) | `awf wf expand-scope --direction dependents`가 import graph reverse-dependents를 `expanded_files`로 추가하고 `graph_expansion` audit를 기록. graph 없음/추가 파일 없음은 no-op | — |
| impl 시작 전 (선제 scope gate) | executor 프롬프트에 `planned_files ∪ expanded_files` + forbidden_paths 주입 | — |
| impl 실행 중 (patch 적용 전) | changed_files ⊂ (planned ∪ expanded), forbidden path 미접근, 신규 파일 수 ≤ max_new_files | patch 거부 → fix_feedback |
| impl 실행 후 (verify, G5) | `awf wf scope-check`가 결정론적으로 분류 (planned/expanded/violation), `.workflow/` 경로는 자동 제외 | 구조화된 피드백 → 재시도 |

### 4.3 Reclassify 트리거

plan-contract 생성 후, 다음 조건에 해당하면 change_class를 승격한다. 승격은 단방향(올리기만)이며, 현재 등급보다 높은 등급으로만 이동한다.

| 신호 | 승격 대상 | 적용 조건 |
|------|----------|----------|
| planned_files 또는 expanded_files에 민감 경로 포함 (auth/, payment/, migration/) | → high-risk | 현재 등급 불문 |
| migration_allowed: true | → high-risk | 현재 등급 불문 |
| public_api_change: true | → high-risk | 현재 등급 불문 |
| unresolved 항목 ≥ 2 | small → standard | small일 때만 승격. standard/high-risk는 유지 |

### 4.4 기존 산출물과의 관계

plan-contract.json은 기계가 읽는 계약이다. 기존 spec.md + plan.md + tasks.md는 사람이 읽는 문서로 유지한다. plan phase는 양쪽을 모두 생성한다.

---

## 5. Gate Configuration (WF v2 Deterministic Gate)

> 관련 원칙: **C3** (기계가 잡는 것은 기계가), **C5** (위험도 비례)
> 현재: `gates.py` 내 Python 함수로 하드코딩
> 변경: provider-config.json에 선언적 정의

### 5.1 Gate 정의 (provider-config.json 확장)

```json
{
  "gates": {
    "format": {
      "command_changed": "npx prettier --check {changed_files}",
      "command_full": "npx prettier --check .",
      "required": true
    },
    "lint": {
      "command_changed": "npx eslint {changed_files}",
      "command_full": "npx eslint .",
      "required": true
    },
    "typecheck": {
      "command_changed": "npx tsc --noEmit",
      "command_full": "npx tsc --noEmit",
      "required": true
    },
    "test": {
      "command_changed": "npx jest --findRelatedTests {changed_files}",
      "command_full": "npx jest",
      "required": true
    }
  },
  "gate_scope": {
    "small": "changed_only",
    "standard": "changed_first_then_full",
    "high-risk": "full"
  },
  "gate_policy": {
    "small": ["format", "lint"],
    "standard": ["format", "lint", "typecheck", "test"],
    "high-risk": ["format", "lint", "typecheck", "test"]
  },
  "gate_baseline": {
    "enabled": true,
    "file": ".workflow/gate-baseline.json"
  }
}
```

### 5.2 Gate 실행 순서

```
1. gate_baseline.json 로드 (있으면)
2. gate_policy[change_class]에서 실행할 gate 목록 결정
3. gate_scope[change_class]에서 범위 결정
4. 순서대로 실행: format → lint → typecheck → test
5. 각 gate 결과에서 baseline 오류 필터링
6. 신규 오류가 있으면 FAIL → fix_feedback 생성
7. 모두 통과하면 AI 리뷰 진입
```

### 5.3 Gate 결과 스키마

`changed_first_then_full` scope에서는 gate가 2회 실행된다(changed → full). 각 실행을 `runs` 배열에 개별 기록한다.

```json
{
  "gate_id": "lint",
  "status": "pass|fail|skip",
  "scope": "changed_only|changed_first_then_full|full",
  "runs": [
    {
      "phase": "changed",
      "status": "pass|fail",
      "errors_total": 2,
      "errors_baseline": 1,
      "errors_new": 1,
      "details": [
        {
          "file": "src/quest/quest.service.ts",
          "line": 45,
          "rule": "no-unused-vars",
          "message": "...",
          "is_new": true
        }
      ]
    },
    {
      "phase": "full",
      "status": "pass|fail",
      "errors_total": 5,
      "errors_baseline": 5,
      "errors_new": 0,
      "details": []
    }
  ]
}
```

- `scope: changed_only` → `runs`에 `phase: "changed"` 1건
- `scope: full` → `runs`에 `phase: "full"` 1건
- `scope: changed_first_then_full` → `runs`에 `phase: "changed"` + `phase: "full"` 2건 (changed가 fail이면 full은 실행하지 않음)
- 최종 `status`는 모든 runs가 pass일 때만 pass

### 5.4 Gate 미정의 시 동작 (C3 예외 조항)

gate가 `provider-config.json`에 정의되지 않은 경우:
1. gate를 **skip** (실행하지 않음)
2. verify 결과에 `"deterministic_verification": "not_configured"` 기록
3. change_class를 한 단계 보수적으로 승격 (small → standard)
4. auto approve 불가

---

## 6. Fix Feedback Schema (WF v2 Structured Feedback)

> 관련 원칙: **C8** (구조화된 피드백 + 예산)
> 현재: `MultiAgentResult.fix_feedback: dict[str, Any] | None`
> 변경: 정형화된 스키마

### 6.1 fix_feedback Schema

```json
{
  "attempt": 2,
  "max_attempts": 5,
  "budget_remaining": 3,
  "findings": [
    {
      "severity": "major|minor",
      "location": "src/quest/quest.service.ts:45",
      "category": "scope_violation|lint|type_error|logic_error|test_failure|security",
      "description": "무엇이 잘못되었는가",
      "suggestion": "어떻게 수정하는가",
      "source": "gate|ai_review|scope_check"
    }
  ],
  "repeated_issues": [
    {
      "category": "scope_violation",
      "count": 2,
      "escalate": true
    }
  ],
  "gate_results": {
    "format": "pass|fail|skip",
    "lint": "pass|fail|skip",
    "typecheck": "pass|fail|skip",
    "test": "pass|fail|skip"
  }
}
```

### 6.2 Retry Budget Model

```json
{
  "retry_budget": {
    "total_attempts": 5,
    "gate_only_fix_cost": 1,
    "spec_conforming_fix_cost": 1,
    "spec_changing_fix_cost": 2,
    "same_category_repeat_limit": 2
  }
}
```

| 유형 | 예산 차감 | 최대 횟수 |
|------|----------|----------|
| gate-only fix (lint/format 자동 수정) | 1 | total_attempts 내 |
| spec-conforming fix (AI 리뷰 피드백 반영) | 1 | total_attempts 내 |
| spec-changing fix (plan-contract 수정 필요) | 2 | 최대 1회 |
| same category 2회 반복 | — | → 즉시 escalate |

### 6.3 Escalation 조건

다음 중 하나라도 해당하면 자동 수정을 중단하고 사람에게 에스컬레이션:
- `budget_remaining <= 0`
- `repeated_issues`에 `escalate: true`인 항목 존재
- spec-changing fix를 2회 이상 시도

### 6.4 Impl 프롬프트 주입 형식

fix_feedback가 존재하면 impl 재실행 시 다음 형식으로 주입:

```
이전 시도에서 다음 문제가 발견되었습니다 (시도 {attempt}/{max_attempts}):
- [{severity}] {location} — {category}: {description}
  → {suggestion}

반복 이슈: {category}가 {count}회 연속. {추가 지시}.
```

---

## 7. Reference Policy (Analysis v3 Layer 3)

> 관련 원칙: **C4** (다각도 확인), **C2** (계약 기반)
> 현재: 없음 (참조 확장 규칙 미정의)
> 변경: 우선순위, 상한, 이유 기록이 있는 규칙

### 7.1 참조 우선순위

```json
{
  "reference_policy": {
    "priority": [
      {"level": 1, "source": "current_unit_observations", "description": "현재 unit의 Layer 2 observation"},
      {"level": 2, "source": "related_domains", "description": "analysis-config의 related_domains에 명시된 unit의 .ai-context"},
      {"level": 3, "source": "project_context", "description": "project-context.md (K6 누적 지식)"}
    ],
    "limits": {
      "max_reference_documents": 5,
      "max_reference_tokens": 8000,
      "require_reason": true
    }
  }
}
```

### 7.2 규칙

- Level 1은 항상 포함 (별도 토큰 예산에서 제외)
- Level 2는 `analysis-config.json`의 `related_domains`에 명시된 것만 (자동 탐색 없음)
- Level 3은 `project-context.md`가 존재할 때만
- 총 참조 토큰 상한 8000 — 초과 시 우선순위 낮은 것부터 제거
- 각 참조에 이유 기록: `{"document": "common/.ai-context/data-model.md", "reason": "T_AWF_USER_NOTIFICATIONS가 common unit에도 정의되어 있어 참조"}`

### 7.3 참조 기록 스키마

```json
{
  "references_used": [
    {
      "document": "common/.ai-context/data-model.md",
      "level": 2,
      "reason": "T_AWF_USER_NOTIFICATIONS 테이블 정의 참조",
      "tokens": 1200
    }
  ],
  "references_dropped": [
    {
      "document": "project-context.md",
      "level": 3,
      "reason": "토큰 상한 초과로 제거"
    }
  ],
  "total_reference_tokens": 4800
}
```

---

## 8. Change Class (WF v2 Risk Routing)

> 관련 원칙: **C5** (위험도 비례 투자)
> 현재: 없음 (모든 작업 동일 7-phase)
> 변경: 2단계 판정 + 등급별 라우팅

### 8.1 2단계 판정

| 단계 | 시점 | 입력 | 판정 방식 |
|------|------|------|----------|
| provisional | wf init | concept 텍스트 + 레포 구조 | 보수적 (확실한 small만 small) |
| reclassify | plan 후 | plan-contract.json | 민감 경로/DB 변경/API 변경 확인 |

### 8.2 등급별 라우팅 정책

모든 등급이 **동일한 phase 목록**(init → plan → review → approve → impl → verify → done)을 사용한다. 등급에 따라 **phase를 skip**하거나, **phase 내부 동작을 축약**한다. 별도의 phase를 추가하지 않는다.

| 등급 | skip되는 phase | verify 내부 동작 | 승인 | 격리 | gate 범위 |
|------|---------------|----------------|------|------|----------|
| small | review, approve | 결정론적 gate만 (AI 리뷰 없음) | auto 가능 | local | changed_only |
| standard | 없음 | 결정론적 gate → AI 리뷰 | auto 조건부 | worktree | changed_first_then_full |
| high-risk | 없음 | 결정론적 gate → AI 리뷰 다수 | 사람 필수 | isolated worktree | full |

**phase skip 규칙**:
- skip된 phase는 `state.json`에서 `"status": "skipped"`로 기록된다
- **impl은 어떤 등급에서도 skip할 수 없다** — 변경을 수행하는 단계가 없으면 verify가 검사할 대상도, done에서 commit/PR을 만들 근거도 없다
- **verify는 어떤 등급에서도 skip할 수 없다** — small에서는 AI 리뷰를 생략하되, 결정론적 gate는 반드시 실행한다

### 8.3 Auto Approve 조건

다음 **모두** 충족해야 auto approve 가능:
- change_class == small
- plan validation PASS (plan-contract.json의 필수 필드 존재 + unresolved 항목 0개 + forbidden_paths 미사용을 기계적으로 검증. review phase의 AI 리뷰와는 다름)
- gate 미정의가 아님
- forbidden_paths 접근 없음
- **push/PR은 auto approve 대상이 아님** (항상 사람 확인)

**plan validation vs plan review**: plan validation은 plan-contract.json의 구조적 완전성을 기계적으로 검증하는 것이고, plan review는 AI가 spec↔plan↔tasks의 일관성을 분석하는 것이다. small은 review phase를 skip하므로 AI 리뷰는 없지만, plan validation은 plan phase 종료 시 항상 수행된다.

---

## 9. Analysis Pipeline Configuration

> 관련 원칙: **C10** (목적별 교체), **C5** (비례 투자)
> 현재: analysis-state.json의 stage 필드
> 변경: Layer 기반 설정

### 9.1 analysis-pipeline.json Schema

```json
{
  "version": "3.0",
  "layer2": {
    "provider": "codex",
    "max_concurrent": 5,
    "cache": {
      "enabled": true,
      "invalidation": "per_file_hash",
      "storage": ".tmp/observations/"
    }
  },
  "transitive_invalidation": {
    "enabled": true
  },
  "layer3": {
    "document": {
      "writers": ["structure", "behavior"],
      "writer_provider": "sonnet",
      "judge_provider": "sonnet",
      "code_fallback": {
        "max_files": 3,
        "trigger": "low_confidence"
      }
    },
    "review": {
      "writers": ["security", "quality", "performance"],
      "writer_provider": "sonnet",
      "judge_provider": "opus"
    },
    "investigate": {
      "writers": ["investigator"],
      "writer_provider": "opus",
      "judge_provider": null,
      "code_access": true,
      "require_code_path_inventory": true
    }
  },
  "reference_policy": {
    "max_documents": 5,
    "max_tokens": 8000
  }
}
```

`transitive_invalidation`은 Stage 1 incremental resume의 import graph 기반 reverse-dependent 무효화를 제어하는 최상위 운영 설정이다.

| Field | Type | Default | Semantics |
|-------|------|---------|-----------|
| `transitive_invalidation.enabled` | boolean | `true` | `false`로 두면 직접 변경 파일만 Stage 1 재분석 대상으로 삼고, import graph 기반 간접 무효화는 건너뛴다. 누락 또는 잘못된 타입은 default-on으로 처리한다. |

응급 비활성화는 `AWF_DISABLE_TRANSITIVE_INVALIDATION=1` 환경변수가 우선한다. 환경변수가 truthy이면 `analysis-pipeline.json`에서 `enabled: true`를 지정해도 transitive invalidation은 꺼진다.

### 9.2 Provider 교체 가능 원칙

§9.1의 provider 값(`codex`, `sonnet`, `opus`)은 **기본 권장값**이며, 동일한 입출력 계약(§1 observation, §3 Writer/Judge 스키마)을 만족하는 어떤 LLM이든 교체 가능해야 한다.

- provider 교체 시 프롬프트, 호출 인터페이스, 출력 파싱이 정상 동작해야 한다
- 특정 모델의 고유 기능(tool use, code execution, sandbox 등)에 의존하는 경우, 해당 의존을 `model_dependencies` 필드에 기록하고 fallback 방안을 명시한다
- 호출 인터페이스(MCP, CLI subprocess, API 등)의 차이는 provider adapter 레이어에서 추상화한다

```json
{
  "model_dependencies": [
    {
      "provider": "codex",
      "dependency": "sandbox_read_only",
      "fallback": "claude --print with file content in prompt",
      "affected_layers": ["layer2"]
    }
  ]
}
```

### 9.3 목적별 Layer 3 차이 (C10 명세)

| 항목 | document | review | investigate |
|------|----------|--------|------------|
| Writer 수 | 2 | 3 | 1 (C4 예외) |
| Judge | 있음 | 있음 | 없음 |
| 코드 접근 | fallback만 | fallback만 | 직접 접근 가능 |
| Code Path Inventory | 선택 | 선택 | **필수** |
| 검증 수준 기록 | 불필요 | 불필요 | **필수** (C9) |

---

## 10. Workflow State v2 Extensions

> 현재 state.json에 v2 필드를 추가한다.
> 기존 필드는 호환 유지.

### 10.1 추가 필드

```json
{
  "change_class": {
    "provisional": "standard",
    "confirmed": "standard",
    "reclassified": false,
    "override": null
  },
  "plan_contract": "artifacts/plan-contract.json",
  "retry_budget": {
    "total_attempts": 5,
    "used": 0,
    "category_counts": {}
  },
  "gate_baseline": "gate-baseline.json",
  "worktree": {
    "path": null,
    "branch": null,
    "created_at": null
  },
  "deterministic_gates": {
    "configured": true,
    "last_run": null,
    "results": {}
  }
}
```

### 10.2 기존 필드와의 관계

| 기존 필드 | 유지 | 변경 사항 |
|----------|------|----------|
| currentPhase | 유지 | phase 목록은 동일. small 등급 시 일부 phase가 `"skipped"` 상태로 건너뜀 |
| phases.{phase}.status | 유지 | 가능한 값에 `"skipped"` 추가 (pending\|in_progress\|completed\|failed\|**skipped**) |
| gates.G1~G7 | 유지 | 결정론적 gate 결과가 추가로 기록됨 |
| loop.replanCount | 유지 | retry_budget으로 확장 (기존은 replan만 추적) |
| history | 유지 | fix_feedback 이력도 추가 기록 |

### 10.3 Phase Skip과 상태 전이

small 등급에서의 상태 전이 예시:

```
plan(completed) → review(skipped) → approve(skipped) → impl(in_progress) → verify(in_progress) → done
```

- skip된 phase는 `currentPhase` 전이 시 자동으로 `"skipped"`로 마킹하고 다음 phase로 넘어간다
- verify는 skip되지 않지만, small에서는 내부적으로 결정론적 gate만 실행하고 AI 리뷰를 생략한다
- `deterministic_gates.results`에 gate 실행 결과가 기록되고, verify의 `status`는 gate 결과에 따라 completed 또는 failed로 설정된다

---

## 11. Session Decision Contract (A2A Session Routing)

> 관련 원칙: **C11** (세션 분리 + 최소 문맥), **C6** (상태 외부화)
> 현재: 세션 유지/분리 판단이 대화 문맥에 암묵적으로 존재
> 변경: 세션 판정과 워커 위임 여부를 구조화해 기록

### 11.1 session_decision Schema

```json
{
  "session_decision": {
    "session_mode": "reuse|new",
    "execution_mode": "orchestrator|worker",
    "decision": "reuse_direct|reuse_delegate|new_direct|new_delegate",
    "reason_codes": [
      "same_goal",
      "strong_dependency",
      "goal_changed",
      "mode_changed",
      "context_overload",
      "parallelizable"
    ],
    "briefing_required": true,
    "source_session_id": "optional-session-id",
    "target_session_id": "optional-session-id-or-worker-id"
  }
}
```

### 11.2 decision 값 의미

| 값 | 의미 |
|----|------|
| reuse_direct | 기존 세션 유지 + 오케스트레이터 직접 수행 |
| reuse_delegate | 기존 세션 유지 + 워커에 최소 문맥으로 위임 |
| new_direct | 새 세션 시작 + 오케스트레이터 직접 수행 |
| new_delegate | 새 세션/워커 스레드 시작 + 독립 위임 |

### 11.3 reason_codes 최소 집합

| 코드 | 의미 |
|------|------|
| same_goal | 같은 deliverable 묶음의 연속 작업 |
| strong_dependency | 강한 의존 아티팩트가 존재 |
| goal_changed | 목표 또는 성공 기준 변경 |
| mode_changed | 설계/구현/검증 등 작업 모드 전환 |
| context_overload | 과거 문맥이 과밀하거나 오염 위험이 큼 |
| parallelizable | 독립 하위 작업으로 병렬 수행 가능 |

---

## 12. Worker Brief / Result Contract (A2A I/O)

> 관련 원칙: **C11** (최소 충분 문맥), **C2** (계약 기반), **C7** (역할 분리)
> 현재: 워커 입력/출력이 자유 텍스트에 가까움
> 변경: 워커 입력/출력 최소 계약 정의

### 12.1 worker_brief Schema

```json
{
  "worker_brief": {
    "goal": "무엇을 끝내야 하는가",
    "current_state": "현재까지 완료된 상태",
    "constraints": [
      "반드시 지켜야 할 규칙/예외"
    ],
    "references": [
      {
        "path": "docs/specs/technical-specs.md",
        "strength": "strong|weak",
        "reason": "직접 읽어야 하는 이유"
      }
    ],
    "expected_output": "결과물 형식",
    "validation": "완료 판정 방식"
  }
}
```

### 12.2 worker_result Schema

```json
{
  "worker_result": {
    "summary": "무엇을 완료했는가",
    "evidence": [
      "어떤 문서/코드/상태를 근거로 판단했는가"
    ],
    "changed_files": [
      "수정 또는 생성한 파일"
    ],
    "risks": [
      "남아 있는 위험 또는 미확정 사항"
    ],
    "needs_decision": [
      "오케스트레이터 판단이 필요한 항목"
    ]
  }
}
```

### 12.3 강한/약한 의존

| strength | 의미 |
|----------|------|
| strong | 없으면 작업이 성립하지 않는 계약/상태/핵심 설계 문서 |
| weak | 참고하면 좋지만 없어도 작업 진행이 가능한 문서 |

워커에는 가능한 한 **strong reference 우선**, weak reference는 필요한 경우에만 전달한다.

---

## 13. Session Metrics Contract (A2A Efficiency Metrics)

> 관련 원칙: **C11** (운영 효율), **C6** (상태 외부화)
> 현재: 세션 효율을 정량 추적하지 않음
> 변경: 재브리핑/재작업/전환 비용을 기록하는 메트릭 정의

### 13.1 session_metrics Schema

```json
{
  "session_metrics": {
    "briefing_lines": 8,
    "direct_dependency_count": 3,
    "strong_dependency_count": 1,
    "mode_transition": "design_to_implementation|implementation_to_verification|none",
    "rework_required": false,
    "integration_turns": 1,
    "context_switch_risk": "low|medium|high"
  }
}
```

### 13.2 운영 메트릭 해석

| 필드 | 의미 |
|------|------|
| briefing_lines | 새 세션/워커 시작 시 재브리핑 길이 |
| direct_dependency_count | 직접 참조한 아티팩트 수 |
| strong_dependency_count | 강한 의존 아티팩트 수 |
| mode_transition | 작업 모드 전환 여부 |
| rework_required | 1차 결과 재작업 필요 여부 |
| integration_turns | 워커 결과를 통합하는 데 추가로 든 턴 수 |
| context_switch_risk | 문맥 오염/누락 위험 평가 |

---

## 스키마 간 의존 관계

```
Observation (§1) ──→ Domain Bundle (§2) ──→ Writer Input (§3)
                                              │
                                              ↓
                                         Judge Output (§3)
                                              │
                                              ↓
                                         Layer 4 산출물

Plan Contract (§4) ──→ Scope Gate ──→ Gate Config (§5)
                          │
                          ↓
                     fix_feedback (§6) ──→ Impl 재실행
                          │
                          ↓
                     Retry Budget (§6) ──→ Escalation

Reference Policy (§7) ──→ Writer Input (§3) 에 참조 문서 주입

Change Class (§8) ──→ Gate Policy (§5) 결정
                  ──→ Phase 라우팅 (§8.2) 결정
                  ──→ Auto Approve (§8.3) 결정

Pipeline Config (§9) ──→ Layer 2 provider 선택
                     ──→ Layer 3 Writer/Judge topology 결정

Session Decision (§11) ──→ Worker Brief (§12) 생성
                       ──→ Workflow State (§10)에 세션 전환 기록

Worker Result (§12) ──→ Orchestrator 통합 판단
                    ──→ Session Metrics (§13) 갱신
```

---

## 참고

- docs/specs/constitution.md — 각 스키마가 준수해야 하는 원칙
- docs/analysis-pipeline-v3-plan-2026-04-07.md — §1, §2, §3, §7, §9의 설계 배경
- docs/wf-pipeline-v2-plan-2026-04-07.md — §4, §5, §6, §8, §10의 설계 배경
- docs/v3-implementation-roadmap-2026-04-07.md — 구현 순서
- docs/session-efficiency-guidelines.md — §11, §12, §13의 운영 배경
