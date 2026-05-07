# Workflow Pipeline

## 개요

7-Phase 게이트 파이프라인. 기능 구현을 plan → review → approve → impl → verify → test → done 단계로 진행한다.

## Phase 흐름

```mermaid
flowchart LR
    PLAN["plan<br/>G1"] --> REVIEW["review<br/>G2"]
    REVIEW --> APPROVE["approve<br/>G3"]
    APPROVE --> IMPL["impl<br/>G4"]
    IMPL --> VERIFY["verify<br/>G5"]
    VERIFY --> TEST["test<br/>G6"]
    TEST --> DONE["done"]
```

## Phase 실행 시퀀스

```mermaid
sequenceDiagram
    participant U as 사용자
    participant CLI as awf wf next
    participant P as Provider<br/>(claude-code/sonnet)
    participant S as state.json
    participant A as artifacts/

    U->>CLI: awf wf next --phase review
    CLI->>S: mark_phase_in_progress(review)
    CLI->>CLI: build_workflow_prompt()
    Note over CLI: spec_loader로 prompts/ 로드<br/>agent card로 artifact 참조
    CLI->>P: 프롬프트 전달
    P-->>CLI: Worker Result Envelope (JSON)
    alt status: completed
        CLI->>A: review-report.md 저장
        CLI->>S: apply_gate_result(review, PASS/FAIL)
        CLI->>S: currentPhase = approve
    else status: escaped
        CLI->>CLI: auto orchestrator decision
        alt continue
            CLI->>S: 재시도
        else replan
            CLI->>S: target phase로 이동
        else abort/escalate
            CLI->>S: 종료/대기
        end
    end
```

## Closed-Loop (Escape → Decision)

```mermaid
stateDiagram-v2
    [*] --> in_progress: Phase 시작
    in_progress --> completed: Gate PASS
    in_progress --> escaped: Worker escape
    escaped --> deciding: severity×reason 자동 규칙

    deciding --> continued: advisory 또는 degraded+quality
    deciding --> replanned: spec/scope divergence
    deciding --> aborted: constraint violation
    deciding --> escalated: 규칙 매칭 없음 또는 budget 소진

    continued --> in_progress: 같은 Phase 재시도
    replanned --> pending: target Phase로 리셋
    completed --> [*]: 다음 Phase
    aborted --> [*]
    escalated --> [*]: 사용자 판단 대기
```

## Replan Budget

```
loop.replanCount / loop.maxReplans (기본: 3)
```

replan 시 count 증가. budget 소진 시 escalate_user.

## Provider 라우팅 (phase_models)

`provider-config.json`의 `phase_models`:

| Phase | inline_model | 설명 |
|-------|-------------|------|
| plan | (default=opus) | 설계 판단, 높은 추론 |
| review | (default=opus) | 교차 검증, 높은 추론 |
| impl | sonnet | 코드 작성, 중간 |
| verify | (default=opus) | 스코프 검증, 높은 추론 |
| test | sonnet | 테스트 실행, 중간 |

## 진실 공급원

| 관심사 | 소스 | 런타임 |
|--------|------|--------|
| Phase 계약 (gate, artifacts) | `templates/agent-cards/{phase}.json` | `.workflow/agent-cards/{phase}.json` |
| Phase 설명/조건 | `skills/phase-*/SKILL.md` | — |
| 프롬프트 템플릿 | `skills/wf-orchestrator/prompts/*.md` | — |
| 워크플로우 상태 | — | `.workflow/state.json` |
