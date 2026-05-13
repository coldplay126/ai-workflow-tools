# BLIP Gem Phase 1 cycle 운영 중 발견된 이슈 (2026-05-13)

> 대상 cycle: `2026-05-11-blip-gem-phase-1-schema-migration-api-16ep-walle`
> 기간: 2026-05-11 ~ 2026-05-13 (3일)
> 산출물: 33 task + 8 fix sessions, G1~G5 PASS, G6 deferred
> 시스템: `awf` CLI (ai-workflow-tools) + `cmux-agent` broker + `awf workflow`

본 cycle은 ai-workflow-tools + cmux-agent를 실 운영하면서 발견된 한계/버그/개선점을 정리한다. 카테고리별로 증상·원인·영향·적용된 우회 또는 fix·후속 follow-up을 기록한다.

---

## 1. awf CLI

### 1.1 `awf wf gate impl` 미구현
- **증상**: `error: deterministic gate not yet implemented for 'impl'. supported: plan, review, verify`
- **영향**: impl 종료 시 G4 marking을 awf CLI로 자동 처리 불가. state.json 수동 update 또는 `awf wf next` 우회 필요
- **우회**: state.json을 Master inline으로 직접 edit (`impl.status=completed`, `gates.G4.passed=true`) + history 추가
- **resolution (2026-05-13, partial)**: `_GATE_SUPPORTED_PHASES`에 `impl`/`test` 추가, `awf wf gate impl --result-file` 명령이 동작. 단 deterministic 검증 규칙(lint/build/tasks/commits)은 후속 agent card 작업으로 분리. commit (Group A).
- **follow-up (잔여)**: agent-cards/impl.json + agent-cards/test.json에 deterministic pass_conditions 정의

### 1.2 `awf wf apply-result` impl 미지원
- **증상**: `apply-result {review,verify}` — impl 미지원
- **영향**: impl phase의 executor result를 state.json에 자동 반영 불가
- **resolution (2026-05-13)**: `apply_workflow_result` 가 review/verify/impl/test 4개 phase 지원. `render_impl_report` + `render_test_report` 추가 (implementation-report.md / test-report.md 산출). G4/G6 자동 marking. commit (Group A).

### 1.3 `awf wf gate verify`가 stream-json result 파싱 실패
- **증상**: `invalid_json: Extra data: line 1 column 146 (char 145)` → `structured_result_shape FAIL`
- **원인**: `awf wf next`의 verify executor가 `claude --output-format stream-json --include-partial-messages`로 실행 → result file에 multi-line JSON event stream 포함. gate evaluator가 single JSON envelope만 파싱 시도
- **영향**: result file의 `conclusion` content는 PASS인데 gate FAIL 처리. G5 자동 marking 실패
- **우회**: state.json에 G5=PASS_MANUAL 직접 marking (verdict='PASS_MANUAL', note에 limitation 명시)
- **resolution (2026-05-13)**: `_parse_result_json`에 stream-json detection + 마지막 `{"type": "result"}` event의 `result` 필드 unwrap 로직 추가. 7 신규 단위 테스트로 single doc / embedded / stream multi-line / nested-prose / partial-corrupted / malformed 케이스 검증. commit (Group A).

### 1.4 `awf wf next` 재호출 시 phase=in_progress executor 중복 가동
- **증상**: phase=in_progress 상태에서 `awf wf next` 호출 시 `warning: phase already in_progress; re-running delegated execution` 메시지 + executor 다시 실행 (~5분 작업 중복)
- **영향**: 비용·시간 낭비. cmux-agent worker가 이미 같은 작업 했어도 awf executor가 또 별도 Claude 인스턴스로 실행
- **resolution (2026-05-13)**: `--force` flag 추가 + `_find_fresh_result_file`로 `.workflow/tmp/result-{phase}-*.txt`의 30분 이내 mtime 검출. 신선한 result가 있고 `--force` 없으면 abort + apply-result 명령 힌트 출력. 신선한 result 없으면 (진짜 stall) 경고 후 재실행. commit (Group A).

### 1.5 `awf wf scope-check`가 wf root git diff만 검사 (multi-repo 미지원)
- **증상**: planned_files에 `blip-market-api/...`, `blip-market-manager/...` 명시되어 있으나 wf root의 `git diff origin/main..HEAD`만 분석 → sibling repo 변경 0건 인식
- **영향**: actual 작업이 sibling repo에 있는데 "planned but not changed (103)" 경고. 또 wf root에 pre-workflow Phase 2.6 docs commit이 base 차이로 violation으로 분류
- **우회**: worker (verify executor) 측에서 cross-repo scope reconciliation 수동 수행 → 결과 보고
- **follow-up**:
  - `awf wf scope-check`가 planned_files의 prefix를 인식하여 multi-repo `git diff` 수행
  - 또는 cycle init 시 sibling repo path/branch를 manifest.json에 등록
  - base ref를 wf root cycle 시작 commit으로 (현재는 main 비교 → pre-workflow commits 포함)

### 1.6 `awf wf decide`는 phase=deciding state 외엔 fail
- **증상**: `error: Phase 'impl' is not in deciding state` (impl=in_progress에서 호출 시)
- **영향**: continue/replan/abort 결정을 in_progress 직접 transition으로 처리 불가
- **follow-up**: `awf wf decide --force-from <state>` 옵션 추가 또는 manual transition CLI 분리

### 1.7 state.json 자동 transition 부재 (cmux-agent와 awf 분리)
- **증상**: cmux-agent worker가 작업 진행하는 동안 state.json 갱신 0건. 마지막 갱신은 G3 approve 시점에서 멈춤
- **영향**: 사용자가 진행도 파악 못함. state.json이 stale → cycle 상태 추적 불가
- **우회**: Master(사람)가 수동으로 state.json edit + history 보강
- **follow-up**:
  - cmux-agent broker.py의 `_inject_and_notify`에 awf CLI hook 추가 (dispatch 시 `awf wf next`, result 시 `awf wf apply-result`)
  - 또는 worker prompt에 "각 task 완료 시 awf 명령 호출" instruction 명시 (WORKER-IMPL.md / ORCHESTRATOR.md protocol 수정)
- **resolution (2026-05-13, 경량판)**: 두 번째 follow-up 채택. broker가 dispatch 시 cycle root의 `.workflow/state.json`을 감지하여 active workflow가 있으면 prompt에 안내 prepend (`_active_workflow_context` + `_workflow_state_hint`). review/verify phase는 `awf wf apply-result --phase {phase} --result-file <path>` 명령을, 그 외 phase는 state.json 수동 갱신을 안내한다. commit `eb69f96`.
- **resolution (2026-05-13, hard hook 격상)**: §1.2가 impl/test apply-result를 지원하면서 hint가 모든 review/verify/impl/test에 적용. 추가로 broker `_maybe_auto_apply_result`가 worker result artifact의 `result_file`+`phase` 필드를 검출하면 subprocess로 `awf wf apply-result` 자동 실행 (3-tier guard: active workflow + 지원 phase + result file 존재). worker는 result 작성 후 broker가 state.json까지 자동 갱신. commit (Group A).

---

## 2. cmux-agent broker

### 2.1 `cmd_start`의 timing race (AI CLI 시작 직후 startup prompt lost)
- **증상**: `cmd_start`가 surface에 `claude\n` send_text 직후 즉시 startup_prompt + send_key("enter") 호출. claude CLI splash 화면 표시 시간(3~8초) 동안 prompt가 input box에 들어가지만 enter가 무시되어 stuck
- **원인**: send_text 후 sleep 없음 + claude CLI ready 검출 없음
- **우회**: Master가 surface별 수동 enter 발송
- **적용된 fix** (설치본 buffer): `time.sleep(10)` 추가 + send_text(prompt) 후 `time.sleep(0.8)` + send_key("enter")
- **resolution (2026-05-13)**: 위 buffer fix를 `ai-workflow-tools/cmux-agent/cmux_agent/cli/commands.py`에 sync 완료. commit `cc0aa54`. 설치본은 `uv tool install --force`로 동기됨. 폴링 기반 ready 검출은 P3 항목으로 분리.
- **follow-up (잔여)**:
  - polling 기반 AI CLI ready 검출 (`read-screen`에서 prompt symbol `❯` 감지)

### 2.2 prompting.py schema mismatch (`message` vs `task`)
- **증상**: `build_injection_prompt`이 `payload.get("message", "")`만 봄. 그러나 orchestrator/worker가 dispatch artifact에 `task` 필드 사용 → broker가 보낸 prompt에 작업 내용 항상 **empty wrapper만**
- **원인**: orchestrator의 prompt protocol과 broker의 prompt builder 간 schema 불일치
- **증거**: 13개 inbox dispatch artifact 모두 `message_len=0, task_len=2~5KB`
- **결과 (T011~T030 진행 가능했던 이유)**: worker(claude code)가 inbox 디렉토리 자체 탐색하여 task content 읽음 (의도치 않은 emergent behavior)
- **적용된 fix**: `payload.get("message") or payload.get("task") or ""` fallback
- **resolution (2026-05-13)**: buffer fix를 `cmux_agent/application/prompting.py`에 sync 완료. commit `cc0aa54`. protocol/schema 명세 정합 (ORCHESTRATOR.md 갱신)은 후속 cleanup으로 분리.
- **follow-up (잔여)**: protocol/schema 명세 정합 (ORCHESTRATOR.md + builder + agent card)

### 2.3 `_inject_and_notify`의 send_text → send_key 사이 sleep 없음
- **증상**: 긴 prompt (3~5KB) 송신 중에 send_key("enter")가 도달 → claude TUI가 paste mode finalize 전이라 enter 무시 → dispatch text가 `[Pasted text #N +M lines]` placeholder로 input box에 stuck
- **원인**: cmux send + cmux send-key가 동기 명령처럼 보이지만 cmux 내부 input queue로 비동기 처리
- **적용된 fix**: `time.sleep(max(1.5, len(text) / 1000.0))` 추가 + verification + 최대 3회 retry
- **resolution (2026-05-13)**: `_send_with_verification(max_retries=3)` 헬퍼로 sync. commit `cc0aa54`.
- **follow-up (잔여)**: prompt 길이별 adaptive sleep + post-enter input box clear 검증 강화

### 2.4 권한 dialog 자동 처리 부재
- **증상**: claude code의 file write/cd 등 권한 prompt ("Do you want to proceed? 1. Yes / 2. ... / 3. No")가 매번 발생. 사용자 cmux GUI 개입 없으면 worker stuck
- **원인**: cmux-agent 기본 design이 worker에게 자율 권한 결정 위임 안 함. settings.local.json의 `permissions.allow=["Bash","Write",...]` 매핑도 claude code의 internal permission system과 정확히 맞지 않음 (glob 패턴 필요)
- **적용된 fix**:
  - broker.py에 `_detect_permission_dialog` + `_approve_permission_dialog` (send-text "1" + enter)
  - cycle별 `.cmux/agents.json`에 `flags: "--permission-mode acceptEdits"`
  - `.claude/settings.local.json`에 `permissions.allow=["Bash","Read","Edit","Write","Glob","Grep","TodoWrite","Task","WebFetch"]`
  - Master 측 auto-handler polling (background bash + cmux send-text "1" + send-key enter)
- **resolution (2026-05-13)**: broker helper (`_detect_permission_dialog`, `_approve_permission_dialog`, `_drain_permission_dialogs`) sync 완료 (`cc0aa54`). 추가로 모든 claude entry 템플릿에 `--permission-mode acceptEdits` flag 영속화 (`f4a1abf`).
- **follow-up (잔여)**:
  - claude code permission API와 cmux-agent broker 통합 (settings 자동 생성)
  - 또는 `--dangerously-skip-permissions` flag 기본화 (cycle 격리 sandbox 가정)

### 2.5 사용자 GUI 직접 입력 텍스트의 unstuck 불가
- **증상**: 사용자가 cmux app GUI에서 surface input box에 직접 텍스트 입력 → `cmux send-key enter`로 submit 안 됨. 다만 broker의 `cmux send <text>`로 새 텍스트 inject 후 enter는 작동
- **원인**: cmux의 input mode가 GUI 직접 입력 vs API send-text 입력을 다르게 처리 (또는 internal buffer split)
- **우회**: Master가 send-text로 같거나 다른 텍스트로 input box를 replace한 후 send-key enter
- **follow-up**: cmux 측 input mode 통합 또는 broker가 stuck 검출 시 send-text replace fallback 강화

### 2.6 dispatch text가 worker 작업 직후 lost
- **증상**: worker가 result outbox 작성 직후 잠시 cleanup/processing 진행. 그 사이 broker가 다음 dispatch send_text inject → claude TUI가 stdin 차단 상태 → text lost (input box empty)
- **영향**: broker는 "✓ 전달 완료" logging하지만 worker 화면에 dispatch text 0자
- **우회**: Master가 nudge로 inbox file path를 alarm style로 전달 (worker가 inbox 자체 read)
- **적용된 fix**: `_wait_for_idle` (busy patterns 사라질 때까지 polling, 최대 180s) 추가
- **resolution (2026-05-13)**: `_wait_for_idle(max_wait=180.0)` sync 완료. dispatch 전 idle 대기 후 send. commit `cc0aa54`.
- **follow-up (잔여)**:
  - inbox path nudge style을 default로 (긴 prompt 대신 짧은 nudge + worker가 file read)
  - 또는 worker idle 검출 강화 (sleep state 누적 X초 후 진짜 idle 판정)

### 2.7 busy 검출 false positive (`⏺` 일반 출력)
- **증상**: 초기 `_BUSY_PATTERNS`에 `⏺` 포함. 그러나 claude code는 `⏺`를 일반 출력 line prefix로 사용. 화면에 이전 `⏺` 라인 남아있으면 busy로 잘못 검출 → 180s idle wait timeout → "did not become idle" WARNING
- **적용된 fix**: `⏺` 제거 + busy verb는 `verb + …` 또는 `verb for Xs` 형태만 매치 + 마지막 8줄만 검사 (scrollback 잔재 회피)
- **resolution (2026-05-13)**: `_BUSY_PATTERNS`, `_BUSY_VERBS` 정밀화 sync 완료. NOTE 주석에 `⏺` 추가 금지 명시. commit `cc0aa54`.
- **follow-up (잔여)**: claude code TUI version별 busy indicator 명세화 + 자동 매칭

### 2.8 multi-watcher race
- **증상**: cmux-agent run이 stop된 후에도 watcher process가 orphan으로 살아남음. 새 run start 시 또 watcher 추가 → 2개 watcher가 같은 `.agent/outbox/` polling → file race
- **로그**: `artifact 파싱 실패: ... — [Errno 2] No such file or directory` (한 watcher가 file 이동 후 다른 watcher가 시도)
- **우회**: 두 watcher process kill 후 새 run 시작
- **resolution (2026-05-13, Group B)**: `cmux_agent/infrastructure/pid_lock.py` 신설 + `cmd_watch`에 `.agent/.watcher.pid` 파일 기반 lock 통합. 살아있는 watcher가 있으면 새 watch는 exit code 1로 abort + 안내 메시지, stale lock(이미 죽은 pid)이면 silently 인계하고 메시지 출력. signal 0 검사로 cross-platform 동작. commit (Group B).

### 2.9 workspace auto-close 부재
- **증상**: `cmux-agent stop` 시 SQLite 상태만 정리. cmux GUI의 workspace surfaces는 그대로 잔여 → 사용자 혼동 ("done인지 멈춤인지 모름")
- **우회**: Master가 수동으로 `cmux close-workspace --workspace <id>` 호출
- **resolution (2026-05-13, Group B)**: `cmd_stop`이 등록된 모든 agent의 `surface_id`를 순회하며 `cmux.close_surface()` 호출 후 `cmux.close_workspace()`로 workspace 정리. `--keep-workspace` flag로 opt-out 가능 (디버깅용). close 실패는 logger.warning에 그치고 정리 흐름은 계속. commit (Group B).

### 2.10 cmux-agent dual mode worker spawn 실패
- **증상**: `awf wf next`의 dual mode가 secondary worker (plan_conformance, quality_validation)를 cmux로 spawn 시도 → `failed to spawn cmux worker for role: worker 생성 실패: Workspace not found` → 즉시 solo downgrade
- **원인**: dual mode의 worker spawn target workspace가 active run의 workspace를 정확히 찾지 못함
- **영향**: cross-validation 효과 zero, primary executor만으로 verify
- **follow-up**: dispatch 모듈의 `_assign_workers` 경로 + cmux workspace selection 로직 점검

### 2.11 stream-json output 누적
- **증상**: `claude --output-format stream-json` result file이 누적된 partial messages 포함 — last message가 `status:completed` envelope이지만 첫 line부터 line별 JSON event가 stream
- **영향**: awf gate evaluator 파싱 실패 (위 1.3과 연관)
- **follow-up**: executor wrapper가 stream을 buffer해서 마지막 envelope만 추출하여 별도 file로 저장

---

## 3. workflow 운영

### 3.1 cmux-agent broker의 dispatch가 사용자 GUI 입력과 같은 input box 공유
- **증상**: 사용자가 cmux GUI에서 worker surface에 직접 명령 입력 시도하는 사이 broker가 새 dispatch send_text → 두 입력 혼합 또는 race
- **영향**: 추적 어려움 + 의도와 다른 명령 처리
- **follow-up**: broker가 worker surface 입력 시 cmux input mode를 lock 또는 사용자 GUI 입력 차단 indicator 표시

### 3.2 verify executor의 회귀적 finding 발견 (무한 fix loop 위험)
- **증상**: 매 verify 회마다 새 CRITICAL/HIGH finding 발견 → fix → re-verify → 또 새 finding. 본 cycle은 7차 verify까지 진행
- **원인**: verify executor가 매번 fresh analysis로 다른 angle/depth 강조. spec ↔ code 매핑이 비결정적
- **영향**: cycle 종료 시점 불명확. 사용자 결정 의존
- **follow-up**:
  - verify spec을 결정적으로 정의 (CRITICAL=배포 차단, HIGH=배포 가능 with debt 등)
  - 또는 verify 후보 finding을 cycle 시작 시 freeze (review/plan 단계에서 미리 식별)
  - 또는 fix loop 최대 N회 정책 (예: 3회 후 자동 PASS_WITH_DEBT)
- **resolution (2026-05-13, Group C)**: 세 번째 follow-up 채택. `mark_phase_in_progress`가 phase별 `executions` counter를 증가 (retries와 별도 — apply_gate_result FAIL 시점이 아닌 awf wf next 시점 카운트). `awf wf next`가 verify phase의 다음 execution이 warn threshold(3) 도달 시 경고, hard limit(5) 초과 시 abort + replan/continue/--force 안내. PASS_WITH_DEBT 자동 변환은 보수적으로 생략하고 사람 결정(`awf wf decide`)을 강제. commit (Group C).

### 3.3 worker가 base branch 자동 검증 안 함 (main 직커밋 사고)
- **증상**: worker가 작업 시 working tree branch 검증 없이 commit + push 진행 → manager repo의 본 cycle 작업 2 commits이 main에 직접 들어감
- **영향**: 정상 워크플로우 (feat branch → PR → merge) 우회. branch protection 우회 (admin 권한). 운영 deploy 자동화 (CD repo manifest auto-update + Argo CD)에 영향 가능
- **우회**: Master가 force-with-lease로 main reset + feat branch 생성 + 본 cycle commits 이동
- **follow-up**:
  - cmux-agent broker에 pre-dispatch validator (working tree branch 검증)
  - 또는 worker prompt protocol에 "main/master/production branch 작업 금지, 시작 시 base branch 명시" instruction 강화
  - dispatch artifact에 `required_branch` 필드 추가 (broker가 검증)
  - post-fix validator (commit 후 push 직전에 branch 재확인 + 의도된 branch인지 검증)
- **resolution (2026-05-13)**: broker에 `_current_branch` + `_branch_safety_warning` + `_is_forbidden_branch` 추가. cycle root가 `main|master|production|prod|release/*|prod/*` 위면 dispatch prompt에 CRITICAL 경고 prepend. multi-repo cycle에서 sibling repo는 보지 못하므로 hard block 대신 soft enforce + worker에게 각 repo에서 직접 확인하도록 안내. commit `eb69f96`.
- **follow-up (잔여)**:
  - dispatch artifact에 `required_branch` 필드 추가하여 phase별 강제 branch 지정
  - post-commit/pre-push validator (worker side script 또는 git pre-push hook 자동 설치)

### 3.4 PR 자동 생성 부재
- **증상**: cycle 종료 후 PR 수동 생성 필요. branch push까지만 자동
- **follow-up**: `awf wf done` 또는 cycle close 시 양쪽 repo gh pr create 자동 호출
- **resolution (2026-05-13, Group C)**: `awf wf pr` 신규 subcommand. state.json + concept.md를 PR title/body로 합성 후 `gh pr create` 호출. flags: `--base`(default main) `--title`/`--body` override, `--draft`, `--no-fill`, `--dry-run`. `gh` 미설치/실패 시 graceful error. 자동 호출(cycle close 트리거)은 별도 cycle로 분리 — 다음 cycle에서 phase=done 진입 시 hook 추가 권장. commit (Group C).

### 3.5 result file 명명 규약 없음
- **증상**: `result-verify-claude-code.txt` 같은 generic 이름 — 차수 구분 불가, 누적 시 overwrite
- **follow-up**: `result-{phase}-{cycle_round}-{timestamp}.json` 형식 강제 + 누적 보관
- **resolution (2026-05-13, Group C)**: `save_workflow_result`가 `result-{phase}-r{round}-{epoch_ms}-{provider}.txt` 형식으로 저장. round는 state의 phases[phase].retries에서 derive (없으면 0). 같은 round 내 여러 호출도 epoch_ms로 구분되어 overwrite 없음. `_find_fresh_result_file`은 기존 glob 패턴(`result-{phase}-*.txt`)으로 신·구 이름 모두 mtime 정렬해 픽업하므로 backward compat 유지. commit (Group C).

### 3.6 CD repo / Argo CD 연동 (production manifest auto-update)
- **증상**: main push 시 ci.yml의 argo job이 CD repo (Space-Oddity-Inc/CD)의 `{repo}/{env}` branch에 deployment manifest commit. 본 cycle main 직커밋 사고로 manager production manifest가 본 cycle image hash로 갱신 (`bump version up to 5d84b11...`). Argo CD는 manual sync라 production cluster는 안전했지만 manifest는 OutOfSync 상태
- **영향**: main reset 후 CD repo manifest는 stale (수동 정리 필요)
- **follow-up**:
  - branch reset/force-push 시 CD repo manifest 자동 revert 또는 안내 message
  - cycle 시작 시 production deploy 경로 (CD repo, Argo app) 인식 + reset 시 영향도 보고

---

## 4. claude code (worker AI CLI) 측 이슈

### 4.1 commit message format (commitlint)
- **증상**: worker가 작성한 commit message가 `subject-case` 위반 (대문자 `F-V16` 등 conventional commit format 어김) → husky commit-msg hook reject
- **우회**: Master가 lowercase로 재시도 (`fix(config): apikey template literal undefined bypass (f-v16)`)
- **follow-up**: worker prompt에 "commit message는 conventional commit format + subject lowercase" 명시. 또는 broker가 commit hook validator pre-run

### 4.2 claude code session token 재사용
- **증상**: `awf wf next`가 매번 새 `--session-id` 생성. 매 verify마다 context 전체 다시 빌드 (~217KB prompt)
- **영향**: token 비용 + 시간 (~15-21분/verify)
- **follow-up**: cycle 단위로 session 재사용 (verify 1차 → 2차 컨텍스트 carry-over). 또는 cache 활용

### 4.3 worker가 의도 외 작업 trigger
- **증상**: dispatch와 별개로 cmux GUI에 잔여 사용자 입력 (이전 turn `T011-FIX 잔여 작업 처리해줘` 등)이 input box에 stuck → enter 받으면 의도 외 작업 시작
- **follow-up**: broker가 dispatch 전 input box 검증 (비어있지 않으면 clear 또는 reject)

### 4.4 worker가 base branch 자동 따름 (main 직커밋 원인 4.3.3과 연관)
- **증상**: worker가 작업 시작 시 `git branch --show-current` 확인 없이 progressing
- **follow-up**: 위 3.3

---

## 5. 메타 — cmux-agent 자체 update 시 fix 보존

### 5.1 설치본 buffer 직접 수정 vs repo sync
- **증상**: 본 cycle의 broker.py/prompting.py/commands.py fix는 `~/.local/share/uv/tools/cmux-agent/lib/python3.13/site-packages/cmux_agent/` 직접 수정. `uv tool upgrade cmux-agent` 시 덮어쓰임
- **follow-up**:
  - 본 cycle fix를 cmux-agent repo (`/Users/steven/Documents/GitHub/cmux-agent/`)에 sync
  - upstream (`ttalkkag/cmux-agent`)에 PR 생성
  - 적어도 사용자 fork (`coldplay126/cmux-agent`)에 commit 보존
- **resolution (2026-05-13)**: `uv tool` 설치 source 확인 결과 standalone repo (`/Users/steven/Documents/GitHub/cmux-agent`)가 아닌 **`/Users/steven/Documents/GitHub/ai-workflow-tools/cmux-agent`** (monorepo 내부 subdirectory)였음 (uv-receipt.toml로 확인). 따라서 fix를 ai-workflow-tools subdir에 sync 완료. `uv cache clean cmux-agent && uv tool install --force` 사이클로 설치본 재동기 검증. standalone cmux-agent repo는 stale upstream(`ttalkkag/cmux-agent`)이 404이므로 별도 PR 무의미. commit `cc0aa54`.

---

## 6. 권장 enhancement 우선순위

| 우선순위 | 영역 | 항목 | 상태 |
|---|---|---|---|
| **P0 (사용량 폭발 — 최우선)** | model routing | impl/test phase에 sonnet 강제 (8장). 컨셉 자체 미동작. 즉시 적용 가능한 우회 8.8 | ✅ **FIXED** (`f4a1abf`) |
| **P0 (반복 발생, 매번 수동 우회)** | broker | timing race fix sync to repo (2.1) + dispatch send sleep (2.3) + 권한 dialog 자동 처리 (2.4) + busy 검출 정밀화 (2.7) | ✅ **FIXED** (`cc0aa54`) |
| **P0** | awf CLI | state.json 자동 transition (1.7) — cmux-agent ↔ awf hook | ✅ **FIXED (lightweight + hard hook)** (`eb69f96` + Group A) |
| **P0** | workflow | base branch validator (3.3) — main 직커밋 사고 재발 방지 | ✅ **FIXED** (`eb69f96`) |
| **P1** | awf CLI | gate evaluator stream-json 파싱 (1.3) + scope-check multi-repo (1.5) | ✅ **§1.3 FIXED** (Group A) / §1.5 open |
| **P1** | awf CLI | apply-result impl/test (1.2) + in_progress duplicate guard (1.4) | ✅ **FIXED** (Group A) |
| **P1** | broker | workspace auto-close (2.9) + multi-watcher singleton (2.8) | ✅ **FIXED** (Group B) |
| **P2** | workflow | verify fix loop 종결 정책 (3.2) — verdict 결정 기준 명확화 | ✅ **FIXED** (Group C) |
| **P2** | workflow | PR 자동 생성 (3.4) + result file 명명 (3.5) | ✅ **FIXED** (Group C) |
| **P3** | meta | cmux-agent repo sync + upstream PR (5.1) | ✅ **FIXED** (`cc0aa54`) |
| **P3** | enhancement | dual mode worker spawn fix (2.10) + session reuse (4.2) | open |

---

## 7. 본 cycle에서 적용된 fix 요약 (설치본 buffer)

`~/.local/share/uv/tools/cmux-agent/lib/python3.13/site-packages/cmux_agent/` 경로 직접 수정:

1. **`cli/commands.py`** `cmd_start`:
   - `claude\n` send_text 후 `time.sleep(10)` 추가
   - send_text(startup_prompt) 후 `time.sleep(0.8)` 추가
   - `send_key("enter")` 호출 후 `time.sleep(0.5)` 추가

2. **`application/prompting.py`** `build_injection_prompt`:
   - `payload.get("message") or payload.get("task") or ""` schema fallback

3. **`application/broker.py`** `_inject_and_notify` 강화:
   - `_BUSY_PATTERNS` 정밀화 (`⏺` 제거 + verb+`…`/`for Xs` 형태만)
   - `_BUSY_VERBS` 확장 (Cascading/Churned/Grooving 등 14개 verb)
   - `_is_busy` 마지막 8줄만 검사 (scrollback 잔재 회피)
   - `_wait_for_idle(max_wait=180)` 추가
   - `_detect_permission_dialog` + `_approve_permission_dialog` (send-text "1" + enter)
   - `_drain_permission_dialogs` 최대 5회 dialog 자동 처리
   - `_send_with_verification(max_retries=3)` — send + sleep(len/1000) + enter + 검증 + retry

4. **`.cmux/agents.json`** (cycle별):
   - `flags: "--permission-mode acceptEdits"`

5. **`.claude/settings.local.json`** (cycle별):
   - `permissions.allow: ["Bash","Read","Edit","Write","Glob","Grep","TodoWrite","Task","WebFetch"]`

---

## 8. **Model routing 불일치 — 사용량 폭발 (P0 CRITICAL)**

> **resolution (2026-05-13)**: 두 레이어에서 fix 완료 — commit `f4a1abf`.
> (a) `awf cli`: `_apply_phase_effort`가 `phase_models.{phase}.inline_model`을 읽어 `ClaudeCodeProvider.set_model()`로 CLI `--model` flag 주입. impl/test에 `inline_model: "sonnet"` 지정 시 실제로 sonnet 모델이 가동된다.
> (b) `cmux-agent templates`: 모든 claude entry에 모델 명시 — orchestrator/plan/review/verify는 `--model claude-opus-4-7 --effort max --permission-mode acceptEdits`, worker-impl/fix/test의 claude entry/fallback은 `--model claude-sonnet-4-6 --permission-mode acceptEdits`. codex/gemini entry는 기존 reasoning/approval flag 유지 (영향 없음).
> 테스트: awf 584/584 + cmux-agent 100/100 PASS. 후속 작업으로 §8.7-P1 telemetry는 P1 cycle 진행 권장.

본 cycle 운영 후 발견된 가장 중대한 비용 issue. ai-workflow-tools 컨셉(기획/설계=고성능, 구현=가성비)이 실제 실행에 적용되지 않음.

### 8.1 컨셉 (설계 의도)
- **기획/설계**: 고성능 모델 (Claude opus / Codex xhigh) — plan, review, verify
- **구현**: 가성비 모델 (Claude sonnet) — impl, test
- 비용 효율을 위해 단계별 모델 분리

### 8.2 provider-config.json 설정 (cycle별)
```json
{
  "phase_models": {
    "plan":   { "effort": "max",  "codex_reasoning": "xhigh" },
    "review": { "effort": "max",  "codex_reasoning": "xhigh" },
    "impl":   { "inline_model": "sonnet", "effort": "high", "codex_reasoning": "xhigh" },
    "verify": { "effort": "max",  "codex_reasoning": "xhigh" },
    "test":   { "inline_model": "sonnet", "effort": "high", "codex_reasoning": "xhigh" }
  }
}
```
impl/test는 `inline_model=sonnet` + `effort=high` (가성비)

### 8.3 실제 동작 — 모두 opus로 가동

**awf wf next 실행 시 process 명령** (실측):
```
/Users/steven/.local/bin/claude --session-id <uuid> --settings {...}
  --permission-mode default --verbose --output-format stream-json
  --include-partial-messages --effort max --json-schema {...}
```
- 모든 phase에서 `--effort max` flag만 전달
- `--model claude-sonnet-4-6` 같은 model flag **부재**
- **`effort max`는 사용자 환경에서 opus → 결과적으로 impl/test도 opus**

**cmux-agent worker** (orchestrator + worker-impl + worker-impl-2 + plan_conformance + quality_validation):
- `.cmux/agents.json`의 flags에 `--permission-mode acceptEdits`만 명시
- model 명시 없음 → 사용자 cmux app default → opus

### 8.4 코드 layer 확인

**`cli/src/awf/commands/wf.py`** `_apply_phase_effort`:
- `phase_model.get("effort")`만 provider.effort에 적용
- `phase_model.get("inline_model")`는 별도 resolution (registry/aliases)만, **실제 claude CLI에 `--model` flag 미전달**

**`cli/src/awf/providers/claude_sdk.py`**:
- default `claude-sonnet-4-6`
- `AWF_CLAUDE_SDK_MODEL` env override
- 다만 inline executor (cmux/native)는 SDK 아니라 CLI 사용 — 이 default 안 통함

### 8.5 사용량 영향 (본 cycle)
- impl phase: cmux-agent worker로 ~30+ dispatches × 평균 5-10분 × max effort (opus)
- verify phase: **7회** × ~15-20분 × max effort (opus) × prompt 217KB
- 모두 opus 사용 → 사용량 폭발

### 8.6 사용자가 fast mode 토글 시 영향
- claude 4.6 fast mode = opus 4.6 (fast output). 그러나 fast mode는 opus 4.6에서만 작동
- 사용자가 fast 토글해도 여전히 opus tier
- sonnet으로 가려면 `--model claude-sonnet-4-6` 명시 필요

### 8.7 권장 fix

**P0 (cmux-agent broker)**:
1. `cmd_start`의 AI CLI launch command에 phase-aware model flag 추가
   - `claude --model claude-sonnet-4-6 --effort high` (impl/test worker)
   - `claude --model claude-opus-4-7 --effort max` (plan/review/verify worker)
2. `.cmux/agents.json`에 phase별 model 필드 지원:
   ```json
   {
     "orchestrator": { "provider": "claude", "flags": "--model claude-opus-4-7 --permission-mode acceptEdits" },
     "worker-impl":  { "provider": "claude", "flags": "--model claude-sonnet-4-6 --permission-mode acceptEdits" }
   }
   ```

**P0 (awf wf next)**:
1. `_apply_phase_effort`가 inline_model을 실제 `--model` flag로 전달
2. impl/test phase executor 실행 시 `--model claude-sonnet-4-6` 강제
3. plan/review/verify phase executor는 `--model claude-opus-4-7 --effort max`

**P1 (사용량 telemetry)**:
- cycle 종료 시 phase별 token 사용량 + 추정 비용 report (history에 기록)
- model mismatch 경고 (impl phase가 opus 사용 검출 시 warning)

### 8.8 즉시 적용 가능한 우회 (운영 측)
- `~/.cmux/agent.json` (글로벌) 또는 cycle별 `.cmux/agents.json`에 명시:
  ```json
  {
    "worker-impl": { "provider": "claude", "flags": "--model claude-sonnet-4-6 --permission-mode acceptEdits" }
  }
  ```
- 이렇게 하면 cmux-agent가 worker-impl 시작 시 sonnet으로 가동 → 본 cycle 같은 큰 impl 작업의 비용 대폭 절감

### 8.9 비용 영향 추정
- 본 cycle impl + verify가 모두 opus로 진행됨
- sonnet으로 동작했다면 token 비용 약 **1/3 ~ 1/5 절감** (모델별 가격 차이)
- 본 cycle 같은 7차 verify + 30+ dispatch면 절감 효과 매우 큼

---

## 부록 A. 본 cycle 진행 통계

- 총 task: 33 (Group A 10 + Group B 11 + Group C 6 + Group D 4 + T033 + T028/T029)
- fix sessions: 8회 (F1/F2/F3 → F-V01 → T015+compliance → HIGH 4건 → F-V16 → 86 violations+CRITICAL 2)
- verify rounds: **7회** (1차 FAIL → 2차 FAIL → 3차 FAIL → 4차 FAIL → 5차 FAIL → 6차 FAIL → 7차 PASS)
- 총 commits: api 9 + manager 5 + docs 3 + CD repo 1 = **18 commits**
- expanded_files: 6 → 124 (+118)
- 결정 (constitution): D-001 ~ D-069 closed
- 소요 시간: 3일 (2026-05-11 ~ 13)

## 부록 B. 결과 평가

- 코드 차원: ✅ G1~G5 PASS, 33 task 100%, 양쪽 build/lint clean, production 영향 zero
- 도구 차원: ⚠️ 매 단계 Master(사람) 수동 개입 다수 필요. 자율 순환의 한계 노출
- 가장 큰 자율 sequence: 단일 자율 dispatch로 5~7 task 연속 처리 (Phase 2A api fix + Phase 2B manager fix)
- 가장 큰 자율 시간: ~12분 단일 dispatch (T015 + compliance 3건 fix)

본 문서를 토대로 cmux-agent broker maturity cycle 또는 awf workflow enhancement cycle을 별도 진행할 것을 권장한다.

---

## 9. Resolution log (2026-05-13)

본 cycle 직후 진행된 후속 fix session 요약. 모든 P0 항목 처리 완료.

### 9.1 변경 요약 (branch `fix/cmux-broker-buffer-sync` — 3 commits)

| commit | 범위 | 처리된 gap 항목 |
|---|---|---|
| `cc0aa54` | broker buffer fix → repo sync (broker.py / commands.py / prompting.py / cmux.py + tests) | §2.1, §2.2, §2.3, §2.4, §2.6, §2.7, §5.1 |
| `f4a1abf` | model routing — awf `_apply_phase_effort`의 inline_model → claude `--model` flag, ClaudeCodeProvider.set_model() 추가, cmux 4개 템플릿 claude entry에 model 명시 | §8 (model routing — 사용량 폭발 P0 critical) |
| `eb69f96` | dispatch-time guards — base branch validator + workflow state hint (broker.py 내 `_branch_safety_warning`, `_active_workflow_context`, `_workflow_state_hint`) | §3.3, §1.7 (경량) |

### 9.2 변경 메트릭

| repo / area | 파일 수 | LOC delta |
|---|---|---|
| `cmux-agent/cmux_agent/` | 4 | +180 / -3 |
| `cmux-agent/tests/` | 3 | +220 / -2 |
| `cli/src/awf/` | 2 | +25 / -1 |
| `cli/tests/` | 1 | +35 / -0 |
| `templates/cmux/` | 4 | +9 / -9 |
| **합계** | **14 files** | **+469 / -15** |

### 9.3 검증

- 단위: `awf cli` 584/584 + `cmux-agent` 104/104 PASS
- 설치본 동기: `uv cache clean cmux-agent && uv tool install --force <ai-workflow-tools/cmux-agent>` 후 source ↔ buffer drift = 0
- smoke: `cmux-agent doctor` 통과, 새 helper 8건 (`_workflow_state_hint`, `_branch_safety_warning`, `_active_workflow_context`, `_is_forbidden_branch` 등) 설치본에서 검출됨

### 9.4 잔여 작업 (별도 cycle 권장)

| 항목 | 우선순위 | 비고 |
|---|---|---|
| §1.2 `awf wf apply-result` impl 지원 | P1 | §1.7과 묶음 |
| §1.3 gate evaluator stream-json 파싱 | P1 | §1.2와 함께 |
| §1.4 in_progress phase 재호출 시 중복 방지 | P1 | confirm prompt 또는 `--force` 분리 |
| §1.5 scope-check multi-repo | P1 | manifest 확장 |
| §2.8 multi-watcher singleton | P1 | PID file 또는 cwd lock |
| §2.9 workspace auto-close | P1 | `cmd_stop`에 `close-workspace` |
| §3.2 verify fix loop 종결 정책 | P2 | verify spec 결정성 |
| §3.4 PR 자동 생성 | P2 | `awf wf done` 확장 |
| §3.5 result file 명명 규약 | P2 | round/timestamp 포함 |
| §2.10 dual mode worker spawn 실패 | P3 | workspace selection |
| §4.2 session 재사용 | P3 | cycle 단위 token 절감 |
| §8.7-P1 model routing telemetry | P1 | phase별 token + 비용 report |

### 9.5 운영 절차 변경점

다음 cycle부터 cmux-agent dispatch 시 prompt에 자동 prepend되는 안내:

1. **base branch 안내** (모든 dispatch):
   - cycle root이 `main|master|production|prod|release/*|prod/*` 위면 **CRITICAL** 경고
   - 그 외에는 일반 reminder (각 대상 repo에서 `git branch --show-current` 확인)

2. **workflow state hint** (`.workflow/state.json` 있을 때):
   - `currentPhase ∈ {review, verify}`: `awf wf apply-result --phase X --result-file <path>` 실행 안내
   - 그 외 phase: `state.json` 수동 갱신 (phases[].status + history) 안내

3. **template claude entry 모델**:
   - 분석/검토 (orchestrator/plan/review/verify): `claude-opus-4-7` + `--effort max`
   - 구현/수정/테스트 (worker-impl/fix/test): `claude-sonnet-4-6`
   - 모든 claude entry에 `--permission-mode acceptEdits` 명시 (권한 dialog 감소)

---

## 10. Group A Resolution log (2026-05-13 — feat/awf-wf-hardening-group-a)

P1 잔여 중 awf CLI 하드닝 4개 항목 후속 처리.

### 10.1 변경 요약

| commit | 범위 | 처리된 gap 항목 |
|---|---|---|
| (Group A 단일 commit 또는 logical 그룹) | `_parse_result_json` stream-json detection + last-result-event unwrap | §1.3 |
| 동일 | `apply_workflow_result` impl/test phase 지원 + `render_impl_report`/`render_test_report` + CLI parser 4-phase whitelist + `wf gate` impl/test 추가 | §1.1 (partial) / §1.2 |
| 동일 | `awf wf next --force` flag + `_find_fresh_result_file` mtime 검사로 in_progress 중복 가동 방지 | §1.4 |
| 동일 | broker `_maybe_auto_apply_result` — result artifact의 `result_file`+`phase` 필드 검출 시 subprocess로 apply-result 자동 호출 (hard hook) | §1.7 (격상) |

### 10.2 변경 메트릭

| repo / area | 파일 수 | LOC delta |
|---|---|---|
| `cli/src/awf/` | 4 (commands/wf.py, cli.py, core/workflow_results.py, core/gates.py 미수정) | +180 / -10 |
| `cli/tests/` | 3 신규 (test_workflow_results_parse.py, test_workflow_results_apply.py, test_wf_fresh_result.py) | +260 / -0 |
| `cmux-agent/cmux_agent/` | 1 (application/broker.py) | +70 / -5 |
| `cmux-agent/tests/` | 1 (tests/test_broker.py) | +120 / -5 |

### 10.3 검증

- `awf cli` 602/602 PASS (이전 591 → +18 신규 케이스: parser 7, apply 5, fresh-result 6)
- `cmux-agent` 110/110 PASS (이전 104 → +6 신규: workflow_state_hint impl, auto-apply hook 5)
- 신규 단위 테스트가 §1.2/1.3/1.4/1.7 hard hook 흐름을 회귀 가드

### 10.4 운영 절차 추가 변경점

이번 Group A로 다음 동작이 새로 가능:

1. **`awf wf apply-result impl <result-file>`**: implementation-report.md 산출 + G4 자동 marking
2. **`awf wf apply-result test <result-file>`**: test-report.md 산출 + G6 자동 marking
3. **stream-json 형식 result 파일**: `awf wf apply-result` / `awf wf gate`가 자동으로 마지막 `{"type": "result"}` event를 unwrap. workaround로 별도 envelope 추출하던 절차 불필요.
4. **`awf wf next` 안전망**: in_progress 단계에서 30분 이내 result 파일이 있으면 abort + apply-result 힌트. `--force`로 의도적 재실행 가능.
5. **worker → broker auto state**: result artifact에 `result_file`/`phase` 필드를 포함하면 broker가 cycle root에서 `awf wf apply-result`를 자동 실행. workflow state.json이 worker 작업과 동시에 fresh 유지된다.

### 10.5 잔여 (P1+ 미해결)

| 항목 | 비고 |
|---|---|
| §1.1 deterministic gate 규칙 | impl/test agent card pass_conditions 정의 |
| §1.5 scope-check multi-repo | manifest 확장 |
| §1.6 `awf wf decide` 상태 강제 | force-from CLI |
| §3.2 verify fix-loop 종결 정책 | verify spec 결정성 |
| §3.4 PR 자동 생성 | `awf wf done` 확장 |
| §3.5 result file 명명 규약 | round/timestamp |
| §2.10 dual-mode worker spawn | workspace selection |
| §4.2 session 재사용 | cycle 단위 token 절감 |
| §8.7-P1 model routing telemetry | phase별 token + 비용 report |

---

## 11. Group B Resolution log (2026-05-13 — feat/cmux-agent-runtime-hardening-group-b)

cmux-agent 런타임 안정성 P1 항목 처리. Group A에 비해 변경 면적이 작지만 사용자 경험에 직접적 — orphan watcher 정리, 종료 시 workspace 자동 close.

### 11.1 변경 요약

| 영역 | 처리된 gap 항목 |
|---|---|
| `cmux_agent/infrastructure/pid_lock.py` (신규) + `cmd_watch` 통합 | §2.8 — `.agent/.watcher.pid` 기반 singleton lock |
| `cmd_stop` + `--keep-workspace` flag + `CmuxAdapter.close_surface`/`close_workspace` 호출 | §2.9 — 종료 시 cmux GUI 자동 정리 |

### 11.2 변경 메트릭

| repo / area | 파일 수 | LOC delta |
|---|---|---|
| `cmux-agent/cmux_agent/` | 3 (cli/__init__.py, cli/commands.py, infrastructure/pid_lock.py 신규) | +160 / -65 |
| `cmux-agent/tests/` | 3 (test_pid_lock.py 신규, test_cmd_stop.py 신규, test_runtime_full_flow.py FakeCmux 확장) | +180 / -0 |

### 11.3 검증

- `cmux-agent` 120/120 PASS (이전 110 → +10 신규: pid_lock 6, cmd_stop 4)
- `awf cli` 602/602 PASS (변경 없음, 회귀 가드)

### 11.4 운영 절차 추가 변경점

1. **`cmux-agent watch` 중복 방지**:
   - 첫 watch 실행 시 `.agent/.watcher.pid` 작성 (현재 pid)
   - 두 번째 watch 실행 시 lock 검출 → 이미 실행 중이면 exit 1 + "pid=N (lock=path) 종료하려면..."
   - 이전 watch가 SIGKILL 등으로 죽었을 때(stale pid) silently 인계 + "stale watcher pid=N 정리하고 새로 시작합니다" 메시지
   - 정상 종료/Ctrl+C 시 try/finally로 lock 파일 자동 제거

2. **`cmux-agent stop` workspace 정리**:
   - 기본: 모든 등록 agent의 surface 닫기 + workspace 닫기
   - `--keep-workspace`: 둘 다 건너뛰기 (디버깅/검증용)
   - 실패해도 cmd_stop은 정상 종료 (best-effort)

### 11.5 잔여 (Group A/B 이후 P1+ 미해결)

| 항목 | 비고 |
|---|---|
| §1.1 deterministic gate 규칙 | impl/test agent card pass_conditions |
| §1.5 scope-check multi-repo | manifest 확장 |
| §1.6 `awf wf decide` 상태 강제 | force-from CLI |
| §3.2 verify fix-loop 종결 정책 | verify spec 결정성 |
| §3.4 PR 자동 생성 | `awf wf done` 확장 |
| §3.5 result file 명명 규약 | round/timestamp |
| §2.10 dual-mode worker spawn | workspace selection |
| §4.2 session 재사용 | cycle 단위 token 절감 |
| §8.7-P1 model routing telemetry | phase별 token + 비용 report |

---

## 12. Group C Resolution log (2026-05-13 — feat/awf-workflow-ops-group-c)

workflow 운영 자동화/정책 P2 항목 3건 처리. §3.2 verify fix-loop guard, §3.4 PR 자동 생성, §3.5 result file 명명 규약.

### 12.1 변경 요약

| 영역 | 처리된 gap 항목 |
|---|---|
| `cli/src/awf/core/workflow_prompt.py` `save_workflow_result` | §3.5 — `result-{phase}-r{round}-{epoch_ms}-{provider}.txt` 누적 |
| `cli/src/awf/core/state.py` `mark_phase_in_progress` | §3.2 prerequisite — phase별 `executions` counter |
| `cli/src/awf/commands/wf.py` `_verify_fix_loop_status` + dispatcher | §3.2 — verify warn at 3 / hard abort at 5 |
| `cli/src/awf/commands/wf_pr.py` (신규) + cli.py 등록 | §3.4 — `awf wf pr` subcommand |
| `cli/README.md` | apply-result/gate phase 4종 + pr 명령 안내 |

### 12.2 변경 메트릭

| repo / area | 파일 수 | LOC delta |
|---|---|---|
| `cli/src/awf/core/` | 2 (workflow_prompt.py, state.py) | +45 / -5 |
| `cli/src/awf/commands/` | 2 (wf.py, wf_pr.py 신규) | +240 / -1 |
| `cli/src/awf/cli.py` | 1 | +22 / -0 |
| `cli/tests/` | 3 신규 (test_workflow_result_naming.py, test_wf_fix_loop.py, test_wf_pr.py) | +260 / -0 |
| `cli/README.md` | 1 | +3 / -2 |

### 12.3 검증

- `awf cli` 622/622 PASS (이전 608 → +20 신규: naming 6, fix-loop 7, pr 7)
- cmux-agent 120/120 PASS (회귀 가드)

### 12.4 운영 절차 추가 변경점

1. **`awf wf pr [--base main] [--draft] [--dry-run]`**: state.json + concept.md를 합성한 PR title/body로 `gh pr create`. CI/cycle 마감 시 수동 명령 하나로 PR 생성.
2. **verify fix-loop guard**: 4번째 verify 실행 시 stderr 경고, 6번째 시 hard abort + `awf wf decide replan|continue` 안내. `--force`로 override 가능 (단 history에 사유 기록 권장).
3. **result file 누적**: `result-{phase}-r{round}-{epoch_ms}-{provider}.txt`. 재실행해도 이전 결과 보존. fresh-result 검출은 mtime 기반이라 신·구 모두 호환.

### 12.5 다음 cycle 권장 — Codex MCP vs cmux-agent 라우팅 (NEW, 2026-05-13)

운영 중 추가 발견 (Group C 머지 직후 사용자 보고):

- **증상**: claude가 codex worker를 cmux-agent dispatch가 아닌 `mcp__codex__codex` MCP 호출로 실행. JSON 직렬화 + tool-call 왕복 + claude session 한 번 더 점유로 오버헤드 큼. 동일 작업을 cmux-agent에서는 background tab에서 진행하지만 MCP 경로는 claude 본 세션을 점유.
- **영향**: precise/cross/critical 모드 + #precise/#cross 해시태그 사용 시 응답이 수십 초 ~ 분 단위로 느려짐. cycle 자율 진행률 저하.
- **진단 결론 (2026-05-13)**: MCP 호출은 `awf wf next` 경로(subprocess CodexProvider)가 아니라 **Claude 본 세션이 `~/.claude/CLAUDE.md`의 `#precise`/`#cross`/`#critical` 프로토콜을 해석할 때** `mcp__codex__codex` tool을 호출하기 때문. snippet (`snippets/claude-md-multi-agent.md`)에 명시적으로 MCP 도구가 지정되어 있어, cmux-agent 활성 여부와 무관하게 MCP 경로로 가도록 안내됨.
- **resolution (2026-05-13, §12.5 fix)**:
  - `snippets/claude-md-multi-agent.md`에 "Slave dispatch 경로 선택 (우선순위)" 섹션 신설. cmux-agent 활성 시 broker (`cmux-agent send`), 미활성 시 MCP fallback으로 분기 명시.
  - 각 mode(#precise/#cross/#critical) 본문에 cmux-agent 활성 여부 분기 추가.
  - `cmux-agent agents --json` 옵션 추가 — Claude(Master)가 Bash로 빠르게 활성 worker 목록 검증 가능 (예: `cmux-agent agents --json | jq '.agents | length'`).
  - 별도 cycle 권장 (남은 follow-up):
    - `awf wf next` provider routing 로깅 추가 (어느 경로로 dispatch 했는지)
    - 사용자가 `~/.claude/CLAUDE.md`에 snippet을 다시 install (setup.sh re-run 또는 manual replace)
    - SKILL 파일에 동일 분기 반영 (예: `claude/skills/wf-orchestrator/SKILL.md`)
- commit (§12.5 fix branch).

### 12.6 잔여 (Group A/B/C 이후)

| 항목 | 비고 |
|---|---|
| §1.1 deterministic gate 규칙 | impl/test agent card pass_conditions |
| §1.5 scope-check multi-repo | manifest 확장 |
| §1.6 `awf wf decide` 상태 강제 | force-from CLI |
| §2.10 dual-mode worker spawn | workspace selection |
| §4.2 session 재사용 | cycle 단위 token 절감 |
| §8.7-P1 model routing telemetry | phase별 token + 비용 report |
| §12.5 Codex MCP routing (NEW) | dispatch path 진단 + cmux-agent 우선 강제 |
| §3.4 PR auto-trigger on phase=done | 현재는 수동 명령만, 자동 hook 별도 |
