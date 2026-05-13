# 2026-05-13 BLIP Gem cycle 후속 작업 — 다음 세션 핸드오버

> **상태**: 26개 P0-P2 항목 모두 머지 완료 (PR #107-#115, 8 cycles).
> 잔여 2개는 L 규모로 별도 cycle 권장.
> 본 문서는 다음 Claude Code 세션이 이어서 작업할 수 있도록 컨텍스트를 압축.

---

## 1. 현재 상태 요약

### 1.1 머지된 변경 (origin/main 기준)

```
ae64589 Group G — cmux-agent recover (§2.10) (#115)
e32945d Group F — deterministic impl/test gates + telemetry (§1.1, §8.7-P1) (#114)
6bd5e9a Group E — spawn workspace precheck + auto gh pr (§2.10 진단, §3.4) (#113)
5ac3ba2 Group D — SKILL routing sync + dispatch log + decide --force-from + cycle hook + README (#112)
d536e3d §12.5 — broker-first routing (snippet + cmux-agent agents --json) (#111)
cd1cd70 Group C — verify fix-loop guard + awf wf pr + result file naming (§3.2/3.4/3.5) (#110)
156b830 Group B — watcher singleton + workspace auto-close (§2.8/2.9) (#109)
db0d5b2 Group A — apply-result impl/test + stream-json + in_progress guard + hard hook (§1.2/1.3/1.4/1.7) (#108)
8e0fb36 post-BLIP P0 — buffer fix sync + model routing + dispatch guards (§2.x/§3.3/§8/§1.7 경량) (#107)
```

### 1.2 테스트 상태

- `awf cli` **650/650** PASS
- `cmux-agent` **133/133** PASS
- CI green on 모든 PR

### 1.3 잔여 P1+ (1개, L 규모)

| 항목 | 규모 | 트리거 |
|---|---|---|
| ~~§1.5 multi-repo scope-check~~ | ~~L~~ | **완료 (2026-05-13)** — `docs/specs/multi-repo-scope.md` + `cli/src/awf/core/wf_scope.py` 멀티 레포 지원, 11개 신규 테스트. PR 미생성 상태 |
| §4.2 session 재사용 | L | telemetry(§8.7-P1) 데이터 1-2 cycle 누적 후 ROI 확인 |

상세 정의: `docs/gaps/2026-05-13-blip-gem-cycle-operational-issues.md` §16.5

---

## 2. 새 명령/기능 빠른 참조

다음 세션에서 즉시 활용 가능:

### awf CLI

| 명령 | 용도 | 주의 |
|---|---|---|
| `awf wf status` | cycle 상태 + **telemetry 블록** (Group F) | telemetry 데이터는 cycle 실행 누적 후 표시 |
| `awf wf apply-result {review\|verify\|impl\|test} <file>` | 4개 phase 모두 지원 (Group A) | impl/test도 자동 G4/G6 marking |
| `awf wf gate {phase} --result-file` | impl/test deterministic gate 평가 (Group F) | 8개 pass_conditions 자동 평가 |
| `awf wf next --force` | in_progress 30분 이내 fresh result 있어도 강제 재실행 (Group A) | 없으면 abort + apply-result 힌트 |
| `awf wf next` (verify) | 3차 경고 / 6차 hard abort + replan/continue 안내 (Group C) | `--force` 로 override 가능 |
| `awf wf decide --force-from {status\|any}` | deciding 외 상태에서도 decide (Group D) | history에 `force_decide` audit |
| `awf wf pr [--base main] [--draft] [--dry-run]` | cycle 종료 후 PR 자동 생성 (Group C) | `--dry-run`으로 안전 미리보기 |

### cmux-agent

| 명령 | 용도 | 주의 |
|---|---|---|
| `cmux-agent agents --json` | Claude(#precise/#cross/#critical)에서 broker 활성 검출 (§12.5) | `jq '.agents \| length' >= 1` |
| `cmux-agent watch` | PID lock 자동 (Group B) | 중복 실행 시 즉시 exit 1 |
| `cmux-agent stop [--keep-workspace]` | workspace/surface 자동 close (Group B) | 디버깅 시 `--keep-workspace` |
| `cmux-agent recover [--force]` | stale workspace 검출 + run FAILED marking + watcher lock 제거 (Group G) | `cmux-agent start` 전 실행 |

---

## 3. provider-config.json 옵트인 설정

다음 cycle에서 활용 가능한 새 옵션 (모두 default off, 명시적 opt-in):

```jsonc
{
  "phase_models": {
    "plan":   { "effort": "max",  "codex_reasoning": "xhigh" },
    "review": { "effort": "max",  "codex_reasoning": "xhigh" },
    "impl":   { "inline_model": "sonnet", "effort": "high", "codex_reasoning": "xhigh" },  // §8 — 자동 sonnet 라우팅
    "verify": { "effort": "max",  "codex_reasoning": "xhigh" },
    "test":   { "inline_model": "sonnet", "effort": "high", "codex_reasoning": "xhigh" }
  },
  "dispatch": {
    "surface_preference": "auto"  // "cmux" 로 명시하면 broker 강제 (§12.5)
  },
  "pr_creation": {                  // Group E §3.4 — cycle 완료 시 자동 PR
    "auto": false,                   // true면 phase=done에서 `awf wf pr` 자동 실행
    "base": "main",
    "draft": false,
    "dry_run": false                 // 첫 운영 시 dry_run: true 권장
  }
}
```

---

## 4. impl/test worker가 발행해야 하는 result JSON 스키마

Group F의 deterministic gate가 PASS 되려면 worker가 다음 필드를 채워야 합니다:

### impl (G4)
```json
{
  "status": "completed",
  "result": {
    "tasks_completed": ["T001", "T002"],
    "tasks_pending": [],
    "lint_clean": true,
    "build_passed": true,
    "commits": ["abc123"],
    "conclusion": "PASS"
  }
}
```

### test (G6)
```json
{
  "status": "completed",
  "result": {
    "suites": [{"name": "unit", "passed": 100, "failed": 0}],
    "regressions": [],
    "acceptance": {"passed": 5, "total": 5},
    "coverage": {"percentage": 85},
    "conclusion": "PASS"
  }
}
```

→ worker prompt 또는 `claude/agents/implementer.md` / `claude/agents/happy-path-tester.md`에 이 schema를 명시해야 자동 PASS.

---

## 5. 다음 세션 시작 시 체크리스트

```bash
# 1. 환경 sanity (awf, cmux-agent 설치 확인)
awf --help | head -3
cmux-agent --help | head -3

# 2. 새 명령 smoke (active workflow 없어도 동작)
awf wf pr --help
awf wf decide --help
cmux-agent recover --help
cmux-agent agents --json   # "활성 run이 없습니다" stderr는 정상

# 3. 작업 중인 cycle이 있으면 (cd <cycle root>)
awf wf status                # telemetry 블록 + 새 dispatch log 확인
```

---

## 6. 다음 cycle 시작 시 두 경로

### (A) §1.5 multi-repo scope-check 본격 진행

사전 작업:
1. `docs/specs/multi-repo-scope.md` 신설 — sibling repo path/branch를 어떻게 manifest.json에 등록할지 결정
2. `.workflow/manifest.json` 스키마 확장:
   ```jsonc
   {
     "sibling_repos": [
       { "path": "../blip-market-api", "branch": "feat/x" },
       { "path": "../blip-market-manager", "branch": "feat/x" }
     ]
   }
   ```
3. `awf wf scope-check`가 각 sibling repo에서 git diff 실행 후 합산

추정 규모: spec 30분 + impl 1-2시간 + test 30분 = **반나절**

### (B) §4.2 session 재사용

사전 작업:
1. 1-2 cycle 동안 §8.7-P1 telemetry로 실제 input_tokens 누적량 확인
2. Anthropic prompt cache (5분 TTL) + `--session-id` 보존 패턴 검토
3. cycle 시작 시 session-id를 `.workflow/session.json`에 기록, `awf wf next`가 매번 동일 session-id 사용

추정 규모: spec 1시간 + impl 2-3시간 + test 1시간 = **하루**

---

## 7. 새 이슈 발생 시 표준 절차 (BLIP cycle 패턴)

```bash
# 1) 새 gap doc
$EDITOR docs/gaps/$(date +%Y-%m-%d)-<설명>.md
# 형식은 docs/gaps/2026-05-13-blip-gem-cycle-operational-issues.md 참고

# 2) §6 priority table 작성 (P0/P1/P2)

# 3) Group X branch + PR 패턴
git checkout -b feat/awf-ops-group-x
# ... 항목별 fix + 테스트 ...
git push -u origin feat/awf-ops-group-x
gh pr create --base main --head feat/awf-ops-group-x
# CI green 후
gh pr merge <#> --squash --delete-branch
```

---

## 8. 참조 문서

| 문서 | 내용 |
|---|---|
| `docs/gaps/2026-05-13-blip-gem-cycle-operational-issues.md` | 본 cycle 전체 (§1~§16, 각 항목별 resolution 포함) |
| `README.md` | 최상위 사용법 (Group D에서 갱신: `awf wf pr`, `apply-result` 4-phase, broker routing) |
| `cli/README.md` | awf 명령 상세 |
| `cmux-agent/AGENTS.md` | cmux-agent 동작 원칙 |
| `snippets/claude-md-multi-agent.md` | `~/.claude/CLAUDE.md` Multi-Agent Protocol 섹션 source |
| `claude/skills/wf-orchestrator/SKILL.md` | wf-orchestrator 스킬 (Group D에서 broker-first routing 미러링됨) |
| `claude/skills/wf-orchestrator/templates/agent-cards/*.json` | phase별 agent card (impl/test pass_conditions Group F에서 갱신) |
| `templates/cmux/*/cmux-agent.json` | cmux 템플릿 (post-BLIP P0에서 모델 명시 추가됨) |

---

## 9. 다음 세션에서 본 문서를 어떻게 활용할까

다음 세션 시작 시 다음 한 줄로 컨텍스트 복원:

> "`docs/gaps/2026-05-13-handover-next-session.md` 읽고 §1 머지 상태 + §3 옵트인 설정 확인 후 작업 시작"

또는 구체적 작업 진입:

> "`docs/gaps/2026-05-13-handover-next-session.md` §6.A 따라 multi-repo scope-check spec 작성"

---

**작성**: 2026-05-13, Claude Opus 4.7 (1M context)
**다음 갱신 시점**: 새 cycle 진행 또는 잔여 L 항목 진입 시
