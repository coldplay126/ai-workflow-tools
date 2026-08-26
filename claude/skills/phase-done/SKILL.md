---
name: phase-done
version: 1.3.0
description: "Phase 7: 최종확인. 읽기 전용 evidence 요약 후 parent-only 명시 확인을 기록한다."
type: workflow-phase
phase: done

capabilities:
  - file_read
  - code_analysis

conditions:
  trigger: "G6 통과 후 parent가 최종 확인을 요청할 때"
  skip: "다른 Phase 진행 중, G6 미통과"

cli:
  command: "awf wf confirm --decision complete --actor human --repo-root . --json"

contract_template: "repo/claude/skills/wf-orchestrator/templates/agent-cards/done.json"
runtime_contract: ".workflow/agent-cards/done.json"
---

# /phase-done — Phase 7 최종확인

## Deterministic Preflight

1. `.workflow/state.json`에서 `gates.G6.passed: true`, `currentPhase: "done"`,
   `phases.test.status: "completed"`를 확인합니다.
2. `awf wf status --repo-root .`의 요약과 읽기 전용 OMP evidence panel을 표시합니다.
3. provider, OMP worker, `awf wf next --phase done`은 Done을 실행하거나 기록할 수 없습니다.
   해당 명령은 `--dry-run`과 non-interactive 호출을 포함해 차단됩니다.

## Parent-only 명시 확인

Done은 자동 단계가 아닙니다. 요약을 검토한 **parent**가 다음 중 하나를 직접 실행합니다.
`--actor`는 감사 기록용 라벨일 뿐 authorization credential이나 worker 권한 위임 수단이
아닙니다.

```bash
# 완료: strict confirmation.json, state, history를 기록
awf wf confirm --decision complete --actor "<audit-label>" --repo-root . --json

# 기존 PR URL을 감사 정보로만 연결 (PR 생성·조회·merge를 하지 않음)
awf wf confirm --decision complete --actor "<audit-label>" \
  --pr-url "https://github.com/<owner>/<repo>/pull/<number>" --repo-root . --json

# 보류: Done을 pending으로 유지하고 state/history에 보류 결정만 기록
awf wf confirm --decision hold --actor "<audit-label>" --repo-root . --json
```

- `complete`는 G6가 현재 Done 상태에서 통과한 경우에만 strict
  `.workflow/artifacts/confirmation.json`을 기록하고 workflow를 완료합니다.
- `hold`는 최종 confirmation artifact를 만들지 않으며, 나중에 parent가 같은
  `confirm --decision complete`를 다시 실행할 수 있습니다.
- `--pr-url`은 canonical GitHub pull-request URL만 허용하는 선택적 감사 필드입니다.
  URL이 있다고 PR의 생성·상태·merge·배포 상태를 확인하거나 추론하지 않습니다.
- `--non-interactive`, `--yes`, `--force`, provider/OMP 호출 같은 자동화 escape는 Done
  명령에 없습니다. worker/provider는 confirmation/state를 기록할 코드 경로가 없습니다.

## OMP Evidence Panel 및 managed lease

Done 요약에는 parent session이 `awf wf status --repo-root .`에서 제공하는 **읽기 전용**
OMP evidence panel을 포함할 수 있습니다. 이 panel은 evidence/provenance일 뿐 독립 gate나
Done 확인의 근거를 대체하지 않습니다.

- 표시 대상: workflow/phase/attempt/dispatch run 상관관계, worker role·status, strict
  schema status, timeout·cancellation(`requested`/`acknowledged`/`final`/`partial`/
  `unresolved`), partial result, checkpoint·follow-up lineage, patch scope status, model,
  worker **reported** usage/cost, provenance 상대 경로와 SHA-256.
- phase primary **estimated** usage와 OMP worker **reported** usage는 출처를 나누어
  표시하며 합산하거나 서로 대체하지 않습니다. 누락된 값은 `unknown`입니다.
- dispatch artifact가 없으면 `unknown`, 손상되었거나 strict evidence를 검증할 수 없으면
  `blocked`로 표시합니다. 이를 PASS로 축약하거나 숨기지 않습니다.
- 원문 prompt, worker response, follow-up message, secret, transcript 본문은 panel·요약·
  confirmation에 포함하지 않습니다.
- OMP는 worktree를 수정하거나 PR 생성·merge·cleanup을 실행할 수 없고 deployment health를
  추론할 수 없습니다. Done HIL과 confirmation 기록은 언제나 parent-only입니다.

워크플로우에 managed lease가 연결되어 있으면 **parent가** 다음 non-destructive preflight를
실행하고 반환된 JSON의 lease 상태·blocker·warning을 Done 요약에 표시할 수 있습니다.

```bash
awf wt status --repo-root . --refresh --json
```

`workflow done`, PR merged, local test pass는 deployment healthy를 뜻하지 않습니다.
deployment health는 별도의 rollout/health evidence가 있을 때만 표시합니다. 이 상태 조회는
`--apply`, PR 생성, merge, cleanup을 수행하지 않습니다.
