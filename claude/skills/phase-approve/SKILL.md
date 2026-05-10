---
name: phase-approve
version: 1.1.0
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
  command: "awf wf next --phase approve"

contract_template: "repo/claude/skills/wf-orchestrator/templates/agent-cards/approve.json"
runtime_contract: ".workflow/agent-cards/approve.json"
---

## Deterministic Phase Preflight

수동으로 이 phase를 실행할 때도 [wf-orchestrator/reference/deterministic-preflight.md](../wf-orchestrator/reference/deterministic-preflight.md)의
Phase Skill Preflight를 따릅니다. 이 phase의 dry-run 명령은 다음과 같습니다:

```bash
awf wf next --phase approve --repo-root . --dry-run --output-format json
```

## 수동 실행 모드

이 스킬은 Phase 3(승인)을 **수동으로** 실행하는 진입점입니다.

### User Input

```text
$ARGUMENTS
```

### 실행 방법

1. `.workflow/state.json`을 읽고 현재 상태를 확인하세요.
2. `~/.claude/skills/wf-orchestrator/SKILL.md` 파일의 **Phase 3: 승인** 섹션을 읽으세요.
3. 해당 지침을 따라 승인 요약을 표시하고 사용자 확인을 받으세요.

> **자동 모드**: `wf-orchestrator`가 Phase 3에 도달하면 자동으로 승인 요약을 표시하고 사용자 입력을 대기합니다.

### 다음 단계
- 승인 시: `/phase-impl`
- 수정요청 시: `/phase-plan`으로 회귀
- 거부 시: `/wf-reset`
