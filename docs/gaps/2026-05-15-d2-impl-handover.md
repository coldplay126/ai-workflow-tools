# 2026-05-15 D2 awf dashboard MVP — impl Phase 1~3 핸드오버

> **상태**: plan/review/approve + impl Phase 1~3 (T001~T011) 완료. scope_hash `e75fa1c0d6e16e3c` 잠금. branch `feat/awf-dashboard-mvp` 위 commit `590d747`. Phase 4 테스트(T012~T020) + Phase 5 검증(T021~T024) + verify/test/done 다음 세션 위임.
> 결정 사유: 본 세션 사용자 권장 — Phase 1~3 (impl MVP) 까지만, 테스트는 별 호흡.
> 관련: [[project_awf_d1_watch_handover]], [[project_awf_d1_docs_cycle]] — D1 cycles 후속.

## 1. 다음 세션 시작 절차

```bash
cd /Users/steven/Documents/GitHub/ai-workflow-tools
awf doctor | grep install_freshness   # in_sync 확인
git status --short
git log --oneline main..HEAD           # 590d747 (Phase 1~3)
git checkout feat/awf-dashboard-mvp    # 이미 branch 존재
awf wf status --repo-root .            # currentPhase=impl, scope_hash e75fa1c0d6e16e3c
cat .workflow/artifacts/tasks.md       # T001~T011 [X], T012~T024 [ ]
```

## 2. impl 진행 현황

### ✓ Phase 1~3 완료 (commit 590d747)
- `cli/src/awf/cli.py` — dashboard subparser 등록 완료
- `cli/src/awf/commands/dashboard.py` — handler 완료
- `cli/src/awf/core/dashboard.py` — KeyReader + run_dashboard + panel 함수 완료
  - rich import 분기 / KeyReader protocol / StdinKeyReader (termios+select)
  - q/Q/Ctrl+C 종료 / r/R 즉시 refresh + elapsed reset (M1 해소)
  - rich.layout.Layout split_column / panel 색상 분기

### ☐ Phase 4 테스트 (다음 세션)
- `cli/tests/test_dashboard.py` (신규) — T012~T018 7 케이스
- `cli/tests/test_wf_commands.py` (수정) — T019/T020 (argparse + D1 회귀)

핵심 구현 포인트:

### 1. rich import 분기 (FR-009, T005)

```python
try:
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
except ImportError:
    print(
        "error: awf dashboard requires the rich library. Install with:\n"
        "  pip install 'awf-cli[tui]'\n"
        "  or: uv tool install --with 'rich>=13.0.0' awf-cli",
        file=sys.stderr,
    )
    return 2
```

### 2. KeyReader 추상화 (L3 finding 해소)

```python
class KeyReader(Protocol):
    def read_nonblocking(self, timeout: float) -> str | None: ...

class StdinKeyReader:
    """termios + select 기반 POSIX TTY 입력. cbreak 모드 + atexit 복원."""
    ...
```

테스트에서는 `FakeKeyReader(seq=["r", "q"])` 주입.

### 3. event loop (T011, M1 해소)

```python
def run_dashboard(repo_root, interval, key_reader=None):
    interval = clamp_interval(interval)
    key_reader = key_reader or StdinKeyReader()
    layout = Layout()
    layout.split_column(Layout(name="workflow"), Layout(name="broker"))

    def refresh():
        layout["workflow"].update(render_workflow_panel(repo_root))
        layout["broker"].update(render_broker_panel(repo_root))

    refresh()
    try:
        with Live(layout, refresh_per_second=4):
            elapsed = 0.0
            tick = 0.2
            while True:
                key = key_reader.read_nonblocking(timeout=tick)
                if key and key.lower() == "q":
                    return 0
                if key and key.lower() == "r":
                    refresh()
                    elapsed = 0.0  # M1 — r 키 시점 reset
                    continue
                elapsed += tick
                if elapsed >= interval:
                    refresh()
                    elapsed = 0.0
    except KeyboardInterrupt:
        return 0
```

### 4. panel 함수 (T009/T010)

```python
def render_workflow_panel(repo_root) -> Panel:
    try:
        state = load_workflow_state(repo_root)
    except Exception as exc:
        body = Text(f"no workflow state: {exc}")
    else:
        body = Text(summarize_workflow_state(state, repo_root=repo_root))
    return Panel(body, title="Workflow")

def render_broker_panel(repo_root) -> Panel:
    health = probe_cmux_broker_health(repo_root)
    daemon = health.get("broker_daemon") or {}
    events = health.get("events_log") or {}
    sqlite = health.get("sqlite_integrity") or {}
    status_color = {"alive": "green", "stale": "yellow", "absent": "red"}.get(daemon.get("status"), "white")
    text = Text()
    text.append(f"Status: {daemon.get('status', '-')}\n", style=status_color)
    text.append(f"PID: {daemon.get('pid', '-')}\n")
    text.append(f"Events log: {events.get('status', '-')}\n")
    text.append(f"Sqlite: {sqlite.get('status', '-')}\n")
    return Panel(text, title="Cmux Broker Health")
```

## 3. Gate 통과 기준

- G4 (impl): tasks.md 모두 [X], lint clean, 1+ commits
- G5 (verify): scope.violations=0, compliance.fail=0 (FR 11/11 매핑)
- G6 (test): suites.failed=0, regressions=0, acceptance.passed≥9 (자동), coverage>=70%

## 4. PR 절차

- remote: `coldplay126/ai-workflow-tools`
- gh switch coldplay126 → push → PR 생성 → switch 복귀 stevenbigc

## 5. 본 cycle plan 산출물 (요약)

- spec.md: 11 FR / 5 US / 6 SC
- plan.md: 4 Phase + 5번째 검증 + 4 리스크 분석
- tasks.md: 24 tasks (impl 11 + test 9 + verify 4)
- test-criteria.md: 12 ATC (9 자동 + 3 수동)
- approval.json: scope_hash e75fa1c0d6e16e3c, M1 resolution_plan 기록

## 6. 참조

- D1 코드 (재사용): `cli/src/awf/core/watch_loop.py::clamp_interval`
- 기존 helper: `cli/src/awf/core/workflow_status.py::summarize_workflow_state`
- 기존 helper: `cli/src/awf/core/cmux_health.py::probe_cmux_broker_health`

---

**작성**: 2026-05-15, Claude Opus 4.7
**다음 갱신**: impl 진입 시 (T001~T024 진행)
