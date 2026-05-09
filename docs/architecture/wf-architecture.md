# WF 파이프라인 아키텍처

## 7-Phase Gate 파이프라인

```
Phase      | Gate | 통과 시              | 실패 시
-----------|------|---------------------|---------------------------
plan (P1)  | G1   | → review (자동)      | retry (max 3)
review (P2)| G2   | → approve (자동)     | CRITICAL → plan, HIGH → 사용자 선택
approve(P3)| G3   | → impl (자동)        | 수정 → plan, 거부 → 중단
impl (P4)  | G4   | → verify (자동)      | retry (max 5)
verify(P5) | G5   | → test (자동)        | scope → approve, bugs → impl
test (P6)  | G6   | → done (자동)        | test fail → impl (max 3)
done (P7)  | —    | 완료                 | —
```

## Gate 조건

| Gate | 조건 |
|------|------|
| **G1** | spec/plan/tasks.md 존재, `[NEEDS CLARIFICATION]` 없음, task ≥ 1 |
| **G2** | CRITICAL 0건, HIGH 해결/확인, coverage ≥ 80% |
| **G3** | 사용자 승인 + scope hash 계산 |
| **G4** | 모든 task `[X]`, lint 에러 0건, 각 phase에 commit 존재 |
| **G5** | `awf wf scope-check` (planned ∪ expanded 결정론적 비교) 위반 0건, spec 준수율 ≥ 90%, CRITICAL 품질 이슈 0건 |
| **G6** | 회귀 테스트 통과, 수락 테스트 통과, 수동 항목 서명 |

## .workflow/ 디렉토리 구조

```
.workflow/
├── state.json                  ← 전역 상태 (currentPhase, gates, retries, history)
├── manifest.json               ← 프로젝트 자동 탐지 결과
├── concept.md                  ← 초기 기능 설명
├── provider-config.json        ← Dual Mode 설정 (글로벌 template에서 자동 복사)
├── artifacts/
│   ├── spec.md                 ← P1: 요구사항, 사용자 스토리, 수락 기준
│   ├── plan.md                 ← P1: 기술 계획
│   ├── tasks.md                ← P1: 작업 분해
│   ├── allowed-files.json      ← P1: planned_files + 선택적 expanded_files (graph 확장) + graph_expansion audit
│   ├── review-report.md        ← P2: 교차 검증 결과
│   ├── approval.json           ← P3: 승인 기록 + scope hash
│   ├── impl-log.md             ← P4: 구현 로그
│   ├── verification-report.md  ← P5: 검증 결과
│   ├── test-report.md          ← P6: 테스트 결과
│   └── confirmation.json       ← P7: 최종 확인 + PR URL
└── agent-cards/                ← Phase별 I/O 계약
    ├── plan.json, review.json, approve.json,
    ├── impl.json, verify.json, test.json, done.json
```

## Dual Mode (provider-config.json)

오케스트레이터가 Phase를 실행할 때 3가지 모드를 지원합니다:

| 모드 | 설명 | 사용 Phase |
|------|------|-----------|
| **inline** | Claude가 직접 실행 (기본) | plan, approve, impl, test, done |
| **delegated** | 외부 워커에게 전체 위임 | - |
| **dual** | inline + 외부 워커 병합 | review, verify |

### Dual 전략

| 전략 | 동작 | 사용 Phase |
|------|------|-----------|
| `parallel_evaluate` | 양쪽 독립 평가 후 결과 병합 | review, verify |
| `generate_then_validate` | Primary 생성 → Secondary 사전 검증 | plan |
| `implement_then_review` | Primary 구현 → Secondary git diff 리뷰 | impl |

#### parallel_evaluate 자동 활성화

`awf wf next` 의 `--mode` 플래그가 미지정이면 review/verify phase 에서 자동으로 `cross` 모드로 승격된다. 결과 병합은 `synthesize_workflow_multi_provider_results` (cli/src/awf/core/judge.py) 가 처리하며 review 는 coverage 기반, verify 는 compliance 기반 selection 을 적용한다. 명시적 `--mode solo` 가 opt-out 경로. 자동 승격 phase 목록은 `provider-config.json` 의 `wf.dual_strategy_phases` 로 override 가능 (기본 `["review", "verify"]`, 빈 리스트 명시 시 비활성). 자동 승격 시 `.awf-operations/events/<date>.jsonl` 에 `dual_strategy_engaged` 이벤트가 기록된다.

### 기본 provider-config.json

```json
{
  "version": "2.0.0",
  "phase_routing": {
    "plan":    { "mode": "inline" },
    "review":  { "mode": "dual", "primary": "inline", "secondary": "codex" },
    "approve": { "mode": "inline" },
    "impl":    { "mode": "inline" },
    "verify":  { "mode": "dual", "primary": "inline", "secondary": "codex" },
    "test":    { "mode": "inline" },
    "done":    { "mode": "inline" }
  },
  "providers": {
    "codex": {
      "type": "mcp",
      "tool": "mcp__codex__codex",
      "timeout_seconds": 300
    }
  },
  "fallback_chain": ["codex", "claude:sonnet"],
  "defaults": { "mode": "inline", "timeout_seconds": 300 }
}
```

프로젝트에 맞게 `.workflow/provider-config.json`을 수정할 수 있습니다.

### Host-agnostic 규칙

`provider-config.json`은 host LLM에 종속되지 않도록 해석해야 합니다.

- `inline`: 현재 host가 직접 phase를 수행
- `delegated`: 외부 provider에게 phase 전체를 위임
- `dual`: host inline 결과와 secondary provider 결과를 병합

예:

- Claude host에서 `inline` = Claude skill 실행
- Codex host에서 `inline` = Codex local runner 실행

따라서 같은 `.workflow/` 상태를 Claude와 Codex가 공유할 수 있습니다.

### 양방향 fallback

기본 템플릿은 Claude host에서 Codex를 secondary/fallback으로 두지만, 역방향도 가능합니다.

| Host | review/verify secondary | fallback_chain 예시 |
|------|--------------------------|---------------------|
| Claude | codex | `["codex", "claude:sonnet"]` |
| Codex | claude:sonnet | `["claude:sonnet", "codex"]` |

Codex host 예시는 [provider-config.codex-primary.json](../codex/templates/provider-config.codex-primary.json)에 있습니다.

## 안전장치

- **총 실행 상한**: 30회 초과 시 자동 중단 (무한 루프 방지)
- **Retry budget**: plan(3), review(2), approve(1), impl(5), verify(2), test(3)
- **HIL 필수**: approve/done은 자동 통과 불가, 위임 불가
- **Scope hash**: 승인 후 spec 변경 감지
- **TTL**: 7일 경과 시 경고

## 스킬 구조

| 스킬 | 역할 |
|------|------|
| **wf-orchestrator** | Phase 라우팅 엔진. agent-card + provider-config 기반 |
| **phase-plan** | P1: spec/plan/tasks 생성 |
| **phase-review** | P2: 교차 검증 + 도메인 리뷰 |
| **phase-impl** | P4: tasks 구현 + lint + commit |
| **phase-verify** | P5: 스코프 + spec 준수 검증 |
| **phase-test** | P6: 회귀/수락 테스트 |
| **artifact-reviewer** (agent) | P2 실행: 교차 검증 평가자 |
| **spec-verifier** (agent) | P5 실행: spec 준수 검증자 |
| **wf-discovery** | 사전 분석: 기능 → 레포 매핑 (project-discoverer 에이전트 위임) |
