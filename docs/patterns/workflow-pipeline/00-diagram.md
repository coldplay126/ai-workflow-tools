# Workflow Pipeline 다이어그램

워크플로우 파이프라인의 구조와 흐름을 시각화한 다이어그램 모음.

---

## 1. Phase 파이프라인 (Gate 포함)

Canonical phase order를 따르며, policy에 의해 일부 phase가 skip될 수 있다.
Skip된 phase는 equivalent gate satisfaction을 남겨 downstream precondition을 보장한다.

```mermaid
flowchart TD
    PLAN["Phase 1: plan"] --> G1{"Gate G1"}
    G1 -->|PASS| REVIEW["Phase 2: review"]
    G1 -->|FAIL| PLAN
    REVIEW --> G2{"Gate G2"}
    G2 -->|PASS| APPROVE["Phase 3: approve"]
    G2 -->|FAIL: CRITICAL| PLAN
    APPROVE --> G3{"Gate G3"}
    G3 -->|PASS| IMPL["Phase 4: impl"]
    G3 -->|FAIL| PLAN
    IMPL --> G4{"Gate G4"}
    G4 -->|PASS| VERIFY["Phase 5: verify"]
    G4 -->|FAIL| IMPL
    VERIFY --> G5{"Gate G5"}
    G5 -->|PASS| TEST["Phase 6: test"]
    G5 -->|FAIL| IMPL
    TEST --> G6{"Gate G6"}
    G6 -->|PASS| DONE["Phase 7: done"]
    G6 -->|FAIL| IMPL

    PLAN -.->|"policy skip"| IMPL
    REVIEW -.->|"policy skip"| IMPL

    style PLAN fill:#4a90d9,color:#fff
    style REVIEW fill:#4a90d9,color:#fff
    style APPROVE fill:#e67e22,color:#fff
    style IMPL fill:#4a90d9,color:#fff
    style VERIFY fill:#4a90d9,color:#fff
    style TEST fill:#4a90d9,color:#fff
    style DONE fill:#e67e22,color:#fff
```

**범례**:
- 파란색: 자동 실행 가능 Phase
- 주황색: HIL 여부가 policy에 의해 결정되는 Phase
- 점선: Policy에 의한 skip 경로 (equivalent gate satisfaction 필요)

---

## 2. Phase 상태 머신

하나의 Phase가 거치는 상태 전이.

```mermaid
stateDiagram-v2
    [*] --> pending: 워크플로우 초기화

    pending --> in_progress: Phase 시작

    in_progress --> completed: Gate PASS
    in_progress --> escaped: Worker가 escape 반환
    in_progress --> failed: Gate FAIL

    escaped --> deciding: severity * reason 규칙 평가

    deciding --> in_progress: continue (같은 Phase 재시도)
    deciding --> pending: replan (대상 Phase로 리셋)
    deciding --> aborted: abort (제약 위반)
    deciding --> escalated: escalate_user (규칙 미매칭 / budget 소진)

    failed --> in_progress: retry (budget 내)
    failed --> aborted: retry budget 소진
    failed --> pending: on_fail replan

    completed --> [*]: 다음 Phase로 진행
    aborted --> [*]: 워크플로우 중단
    escalated --> [*]: 사용자 판단 대기
```

### Phase 상태 정의

| 상태 | 설명 | 전이 조건 |
|------|------|----------|
| `pending` | 실행 대기 | 초기화 또는 replan 리셋 |
| `in_progress` | 실행 중 | Phase 시작 |
| `completed` | 정상 완료 | Gate PASS |
| `escaped` | Worker가 완료 불가 보고 | Result Envelope status=escaped |
| `failed` | Gate 실패 | Gate FAIL |
| `deciding` | 자동 규칙 평가 중 | escape 후 severity*reason 매칭 |
| `aborted` | 워크플로우 중단 | 제약 위반 또는 retry budget 소진 |
| `escalated` | 사람에게 위임 | 규칙 미매칭 또는 replan budget 소진 |

---

## 3. Closed-Loop 의사결정 트리

Result Envelope 수신 후 자동 판정 규칙 적용 흐름.

```mermaid
flowchart LR
    ENVELOPE["Result Envelope 수신"] --> STATUS{"status 확인"}

    STATUS -->|completed| COMPLETED["Gate 평가 진행"]
    STATUS -->|escaped| ESCAPED["escape 메타데이터 추출"]
    STATUS -->|failed| FAILED["즉시 Gate FAIL"]

    ESCAPED --> JUDGE{"severity * reason 평가"}

    JUDGE -->|"advisory 또는 degraded+quality"| CONTINUE["continue"]
    JUDGE -->|"spec_divergence 또는 scope 이탈"| REPLAN["replan"]
    JUDGE -->|"constraint_violation"| ABORT["abort"]
    JUDGE -->|"규칙 미매칭 또는 budget 소진"| ESCALATE["escalate_user"]

    style CONTINUE fill:#27ae60,color:#fff
    style REPLAN fill:#f39c12,color:#fff
    style ABORT fill:#c0392b,color:#fff
    style ESCALATE fill:#8e44ad,color:#fff
```

---

## 4. 전체 워크플로우 생명주기

워크플로우 초기화부터 완료까지의 전체 흐름.

```mermaid
flowchart TD
    INIT["워크플로우 초기화"] --> RISK["변경 등급 감지"]
    RISK --> PHASE_LOOP["현재 Phase 결정"]
    PHASE_LOOP --> EXEC_CHECK{"실행 카운터 한도 내?"}
    EXEC_CHECK -->|예| PRECOND{"선행 Gate 통과?"}
    EXEC_CHECK -->|아니오| ABORT["워크플로우 중단"]
    PRECOND -->|통과| RUN["Phase 실행"]
    PRECOND -->|미충족| FAIL_HANDLER["실패 처리"]
    RUN --> RECEIVE["Result Envelope 수신"]
    RECEIVE --> GATE["Gate 평가"]
    GATE --> RESULT{"Gate 결과"}
    RESULT -->|PASS| NEXT["다음 Phase"]
    RESULT -->|FAIL / ESCAPED| FAIL_HANDLER
    NEXT -->|done 아님| PHASE_LOOP
    NEXT -->|done 완료| DONE["워크플로우 완료"]
    FAIL_HANDLER -->|retry / replan| PHASE_LOOP
    FAIL_HANDLER -->|abort / escalate| ABORT

    style DONE fill:#27ae60,color:#fff
    style ABORT fill:#c0392b,color:#fff
```
