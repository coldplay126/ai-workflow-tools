# 2026-05-14 Issue C 진입 핸드오버 — awf↔cmux 가시성 통합

> **상태**: Issue B (`cmux-agent doctor` health) 머지 직후 후속. 사용자 c2 우선 선택 후 본 세션에서는 컨텍스트 파악까지 수행. cycle 본 진행은 다음 세션.
> 관련 문서: `docs/gaps/2026-05-14-handover-post-dogfood.md`, 메모리 `project_cmux_agent_doctor_health_cycle`, `project_dispatch_cmux_remaining_issues`.

## 1. 컨텍스트

- Issue A: PR #124 머지 (awf-cli)
- Issue B: cmux-agent PR #1 머지 (`dcf9d4b`) — broker health 검사 도입
- **Issue C**: 미해결. dispatch 가시성을 awf 측에서 통합

## 2. 사용자 결정

옵션 c2 우선 — **awf-cli 자체 read-only dashboard 도입**. eventSync.tasks 기반.

## 3. 사전 조사 (본 세션 진행분)

### 3.1 인프라 현황

- `state.eventSync.tasks` 가 이미 dispatch 추적 (state_updater.py L76+):
  - task_id → `{type, provider, status, source, ...}`
  - TASK_STARTED / TASK_COMPLETED / TASK_FAILED 이벤트 인입
- `event_sync_summary.py` 가 stages/phases/tasks/workers/gates 요약 텍스트 반환
- `workflow_status.py::summarize_workflow_state` 가 text 형식으로 풀어 출력 (L41–L130)
- `.awf-operations/events/YYYY-MM-DD.jsonl` 가 영구 기록
- `operational_metrics.py`, `wiki_compile.py` 가 JSONL 그룹화/렌더링 담당

### 3.2 미커버 정보

awf-cli는 cmux-agent broker daemon health를 직접 알 수 없음. PR #1로 만들어진 `cmux-agent doctor --json --cwd <path>` 출력을 호출해야 함.

### 3.3 가능한 진입점 (다음 세션에서 결정)

| 진입점 | 변경 범위 | 의존성 |
|--------|----------|--------|
| (D1) `awf wf status --watch` Rich Live refresh | workflow_status.py + new module | rich (이미 pyproject.toml 의존성?) |
| (D2) `awf dashboard` 신규 subcommand TUI | cli.py + 신규 dashboard 모듈 | rich 또는 textual |
| (D3) `awf wf status` 출력에 `cmux_health` 섹션 추가 (cmux-agent doctor --json 호출) | workflow_status.py minor | subprocess + cmux-agent CLI |
| (D4) `awf doctor` 출력에 cmux-agent broker health mirror | doctor 모듈 minor | subprocess + cmux-agent CLI |

**예상 우선순위**: D3 (light) → D1 (live UI) → D2 (full TUI). D2는 textual 의존성 도입 부담.

D4는 doctor 카테고리지만 dispatch 가시성과는 결이 다름 — 보류.

## 4. 다음 세션 진입 절차

```bash
# 1. 환경 점검
cd /Users/steven/Documents/GitHub/ai-workflow-tools
awf doctor | grep install_freshness   # in_sync 확인
git status --short
git log --oneline -3                   # main HEAD 가 25b9cfc 인지 확인

# 2. wf cycle 시작
awf wf init "awf↔cmux 가시성 통합 (Issue C 옵션 c2): awf-cli read-only dashboard. eventSync.tasks 기반 + cmux-agent doctor --json mirror. 진행 phase / dispatch task / broker health 통합 표시." --repo-root .

# 3. agent-cards 복사 (cmux-agent에서 했던 패턴)
mkdir -p .workflow/agent-cards
cp claude/skills/wf-orchestrator/templates/agent-cards/*.json .workflow/agent-cards/

# 4. provider-config.json 작성 (cmux-agent와 동일 패턴)
# 5. wf-orchestrator 진입
```

## 5. plan phase에서 결정할 것

- D1 / D2 / D3 중 어떤 진입점을 채택할지
- 신규 의존성 (rich/textual) 도입 가능성
- cmux-agent CLI를 awf가 subprocess 호출하는 방식의 robustness (cmux-agent 미설치 환경 graceful degrade)
- read-only 보장 (eventSync.tasks를 변경하지 않음)

## 6. 본 cycle 미커버 항목

- multi-broker 동시 실행 시나리오
- `awf dashboard --remote` 같은 분산 시나리오
- web UI (out of scope, c3 영역)

## 7. 참조

- 메모리: `project_cmux_agent_doctor_health_cycle.md` (Issue B 완료 요약)
- 메모리: `project_dispatch_cmux_remaining_issues.md` (§Issue C 옵션 c1/c2/c3 정의)
- 코드: `cli/src/awf/core/state_updater.py` L76+ (tasks 스키마)
- 코드: `cli/src/awf/core/workflow_status.py` L41–L130 (eventSync 텍스트 렌더)
- 코드: `cli/src/awf/core/event_sync_summary.py` (요약 함수)
- 외부: cmux-agent merge `dcf9d4b` (`cmux-agent doctor --json` 인터페이스 안정화)

---

**작성**: 2026-05-14 후반, Claude Opus 4.7 (1M context)
**다음 갱신 시점**: 다음 세션에서 plan phase 진입 시
