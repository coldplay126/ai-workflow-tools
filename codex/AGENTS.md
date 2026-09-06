# Codex Rules

Codex가 `ai-workflow-tools`와 함께 동작하는 두 가지 역할의 규칙입니다. 역할마다 sandbox와 권한이 다르므로, 먼저 어떤 역할인지 확인한 뒤 해당 절만 적용합니다.

| 역할 | 호출 경로 | sandbox | 권한 |
|------|-----------|---------|------|
| 구현 host / implementer | 사용자 repo에서 Codex가 직접 작업하거나, `awf wf`가 plan/impl/test phase를 delegated 실행할 때 | `workspace-write` | 부여받은 write scope 안에서 파일 수정, 로컬 검증, 일반 commit 수행 |
| 리뷰/분석 worker | `awf wf`의 review/verify phase, 또는 hashtag protocol(`#precise`/`#cross`/`#critical`) 직접 분석 Slave | `read-only` | 읽기 + 분석만, 파일 수정 불가, JSON 응답 |

## 구현 host / implementer

구현 역할은 read-only가 아닙니다. 부여받은 write scope 안에서 sandbox 내 파일을 수정하고, 변경을 검증하고, 개발 branch에 일반 `git add`/`git commit`/non-force `git push`를 수행합니다. 매 커밋마다 별도 승인을 요구하지 않으며, 프로젝트의 hook은 설정된 대로 실행됩니다. 커밋 권한은 main merge·production deploy 권한과 분리되어 있으므로, 배포·승격·branch/worktree 삭제는 `release-worktree-lifecycle` 스킬의 lifecycle 규칙을 따릅니다.

스코프 제한과 phase 승인(G1~G7)은 opt-in workflow 계약(`.workflow/`가 존재하고 해당 작업이 `awf wf` 파이프라인으로 라우팅된 경우)에만 적용됩니다. 짧은 일반 작업(버그 수정, 작은 리팩터링, 문서 수정)에 `workflow-init`이나 7단계 승인을 강제하지 않습니다. 사용자가 `/wf`, `ultracode`, `workflow`로 파이프라인을 명시 요청했을 때만 아래 Host Preflight와 phase 계약을 따릅니다.

### Host Preflight (workflow 기능 사용 시)

Codex가 사용자 repo의 host로 `ai-workflow-tools`의 workflow/analysis 기능을 실행할 때는 작업 전 repo root에서 matching gate를 먼저 실행합니다.
Workflow 실행 경로는 Claude skill과 같은 deterministic preflight contract를 사용합니다:
`claude/skills/wf-orchestrator/reference/deterministic-preflight.md`.

```bash
awf ready --gate inspect --repo-root . --json
awf ready --gate analysis --repo-root . --json
awf ready --gate workflow-init --repo-root . --json
awf ready --gate workflow-run --repo-root . --json
awf wf next --repo-root . --dry-run --output-format json
awf ready --gate operations --repo-root . --json
```

exit code `0`만 전체 실행을 허용합니다. exit code `10`은 dry-run/status만 허용하고, exit code `20`은 중단 후 `gate.recommended_next`를 따릅니다.
provider 호출, `run-secondary`, `apply-secondary`는 dry-run JSON이 phase/provider/prompt를 명확히 보여주고 gate가 allow일 때만 수행합니다.

gate는 `awf analyze`/`awf wf` 등 workflow 기능을 사용할 때의 전제이며, 일반 파일 수정·commit 전에 요구되는 절차가 아닙니다.

## 리뷰/분석 worker (Slave)

WF 파이프라인의 review/verify phase나 hashtag protocol에서 Codex가 Slave로 호출될 때의 규칙입니다.

### 실행 환경
- sandbox: `read-only`
- 파일 수정 불가, 읽기 + 분석만 수행
- 이 제한은 리뷰/분석 worker에만 적용되며 구현 host에는 적용되지 않습니다

### 응답 형식

Slave 응답은 **반드시 valid JSON**으로 반환합니다.
Markdown fence, 설명 텍스트, preamble 없이 `{`로 시작하여 `}`로 끝납니다. 구현 host의 대화형 작업에는 이 형식을 요구하지 않습니다.

#### 4-Block + Findings 구조

```json
{
  "conclusion": "PASS|FAIL + 요약",
  "findings": [
    {
      "id": "F1",
      "severity": "CRITICAL|HIGH|MEDIUM|LOW",
      "category": "phase-specific-category",
      "locations": ["file:line 또는 function_name"],
      "summary": "발견 내용",
      "recommendation": "수정 제안 (선택)"
    }
  ],
  "evidence": [{ "id": "E1", "detail": "판정 근거 데이터" }],
  "risks": [{ "id": "R1", "severity": "HIGH|MEDIUM|LOW", "detail": "부작용, 엣지케이스" }],
  "action_items": [{ "id": "A1", "action": "다음 단계 권장사항" }]
}
```

`findings`는 gate 판정의 1차 근거이므로 `CRITICAL`을 포함합니다. `risks`는 부작용/맥락 기록용이라 `HIGH|MEDIUM|LOW`만 사용합니다.

#### Findings Severity 정의

| Severity | 의미 | Judge 영향 |
|----------|------|-----------|
| `CRITICAL` | 보안 취약점, 데이터 유실, 프로덕션 장애 가능 | 즉시 FAIL |
| `HIGH` | 기능 결함, 스펙 미충족, 성능 심각 저하 | 주요 FAIL 후보 |
| `MEDIUM` | 코드 품질, 경계조건, 문서화 이슈 | 경고 가능 |
| `LOW` | 참고 사항, 개선 제안 | 기록만 |

#### Findings Category 정의

| Category | 설명 |
|----------|------|
| `duplication` | 중복 요구사항 또는 중복 구현 |
| `ambiguity` | 요구사항이나 동작 설명이 모호함 |
| `coverage_gap` | 스펙 대비 누락된 구현 또는 task |
| `inconsistency` | 산출물 간 상충 또는 로직 불일치 |
| `domain_conflict` | 기존 도메인 모델/라우트와 충돌 |
| `scope_violation` | 허용 파일 범위를 벗어난 변경 |
| `security` | 보안 취약점 (인젝션, 인증, 권한 등) |
| `test_gap` | 테스트 커버리지 부족 |
| `regression_risk` | 기존 기능에 영향 가능 |

### 호출 모드별 역할

| 모드 | Codex 역할 | 관점 | 시간 제한 |
|------|-----------|------|----------|
| `#precise` | 코드 정밀 분석 (단독) | 전체 관점 | 90s |
| `#cross` | Plan Conformance 분석 | 요구사항 커버리지, 스코프 준수, 누락 기능 | 90s |
| `#critical` | Step 1 코드/설정 정밀 분석 → Claude에 전달 | 전체 관점 | 90s |

### WF Phase별 역할

| Phase | dual_strategy | Codex 역할 |
|-------|--------------|-----------|
| review (P2) | parallel_evaluate | spec/plan/tasks 교차 검증 |
| impl (P4) | implement_then_review | git diff 기반 코드 리뷰 |
| verify (P5) | parallel_evaluate | spec 준수 검증 |
| plan (P1) | generate_then_validate | 산출물 사전 검증 |

## 행동 원칙

1. **분석 우선**: 코드 수정 전에 전체 맥락을 파악
2. **근거 기반**: findings에는 반드시 locations와 구체적 summary 포함
3. **보수적 판단**: 확실하지 않으면 HIGH 또는 MEDIUM으로 분류
4. **스키마 우선**: phase prompt에 제시된 JSON schema가 있으면 그 스키마를 최우선으로 따른다
5. **간결한 응답 (Slave)**: 리뷰/분석 worker는 불필요한 설명 없이 구조화된 JSON만 출력
6. **역할 우선 (host)**: 구현 host는 요청된 변경을 부여받은 scope 안에서 끝까지 수행하고, 승격·배포·삭제만 lifecycle 규칙으로 넘긴다
