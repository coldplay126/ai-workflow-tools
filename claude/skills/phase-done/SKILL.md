---
name: phase-done
version: 1.1.0
description: "Phase 7: 최종확인. 워크플로우 요약 및 PR 생성."
type: workflow-phase
phase: done

capabilities:
  - file_read
  - file_write
  - code_analysis

conditions:
  trigger: "orchestrator가 done 실행을 지시하거나, 수동 실행"
  skip: "다른 Phase 진행 중, G6 미통과"

cli:
  command: "awf wf next --phase done"

contract_template: "repo/claude/skills/wf-orchestrator/templates/agent-cards/done.json"
runtime_contract: ".workflow/agent-cards/done.json"
---

## 수동 실행 모드

이 스킬은 Phase 7(최종확인)을 **수동으로** 실행하는 진입점입니다.

### User Input

```text
$ARGUMENTS
```

### 실행 방법

1. `.workflow/state.json`을 읽고 현재 상태를 확인하세요.
2. `~/.claude/skills/wf-orchestrator/SKILL.md` 파일의 **Phase 7: 최종확인** 섹션을 읽으세요.
3. 해당 지침을 따라 종합 요약을 표시하고 사용자 확인을 받으세요.

> **자동 모드**: `wf-orchestrator`가 Phase 7에 도달하면 자동으로 종합 요약을 표시하고 사용자 입력을 대기합니다.

### 문서 갱신 체크 (doc-update-check)

Phase 7에서는 `git diff`를 분석하여 analysis-docs 문서 갱신이 필요한지 자동으로 확인합니다:
- 라우트/엔드포인트 변경 → 해당 서비스 ROUTES.md
- DB 스키마/마이그레이션 → databases/README.md
- 의존성(package.json) 변경 → 해당 서비스 README.md
- 환경변수/시크릿 변경 → OPERATIONS_RUNBOOK.md

갱신이 필요한 경우 analysis-docs 레포에 별도 PR을 생성할 수 있습니다.

### Work History 보존

Phase 7 완료 시 `.work_history/` 세션 디렉토리에 `summary.md`가 자동 복사됩니다.
이전 세션의 맥락은 `awf wf status`에서 확인할 수 있습니다.

### 다음 단계
- 확인 + PR 생성: 워크플로우 완료
- 확인만: PR 수동 생성
- 보류: 나중에 `/phase-done` 재실행
