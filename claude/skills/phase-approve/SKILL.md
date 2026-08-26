---
name: phase-approve
version: 1.2.0
description: "Phase 3: 승인. 사용자 확인 후 scope hash 잠금 및 G3 게이트."
type: workflow-phase
phase: approve
gate: G3

capabilities:
  - file_read
  - file_write
  - code_analysis

conditions:
  trigger: "orchestrator가 approve 실행을 지시하거나, 수동 실행"
  skip: "다른 Phase 진행 중, G2 미통과"

cli:
  command: "awf wf approve --decision approve --actor human --repo-root . --json"
contract_template: "repo/claude/skills/wf-orchestrator/templates/agent-cards/approve.json"
runtime_contract: ".workflow/agent-cards/approve.json"
---

# /phase-approve — Phase 3 승인

## Deterministic Phase Preflight

수동으로 이 phase를 실행할 때도 [wf-orchestrator/reference/deterministic-preflight.md](../wf-orchestrator/reference/deterministic-preflight.md)의
Phase Skill Preflight를 따릅니다. 승인 전에 provider를 실행하지 않고 현재 상태를
읽기 전용으로 확인합니다:

```bash
awf wf status --repo-root . --json
```

## 수동 실행 모드

이 스킬은 Phase 3(승인)을 **수동으로** 실행하는 진입점입니다.

### User Input

```text
$ARGUMENTS
```

### 실행 방법

1. `.workflow/state.json`, review report, allowed files를 읽고 승인 요약을 표시합니다.
2. 사용자에게 승인·수정요청·거부 중 하나를 명시적으로 선택받습니다.
3. parent host가 실제 사용자 identity를 `AWF_OPERATOR`에 넣어 정확히 한 명령을 실행합니다:

```bash
awf wf approve --decision approve --actor "${AWF_OPERATOR:?set operator identity}" --repo-root . --json
awf wf approve --decision revise --actor "${AWF_OPERATOR:?set operator identity}" --reason "<reason>" --repo-root . --json
awf wf approve --decision reject --actor "${AWF_OPERATOR:?set operator identity}" --reason "<reason>" --repo-root . --json
```

`awf wf next --phase approve`는 provider/worker 승인을 차단합니다. `approval.json`,
G3 scope hash, state/history는 위 parent-only CLI만 기록합니다. small 변경의 phase
skip 여부는 deterministic change-class policy가 소유하며 worker auto-approval이 아닙니다.

### 다음 단계
- 승인 시: `/phase-impl`
- 수정요청 시: `/phase-plan`으로 회귀
- 거부 시: workflow rejected
