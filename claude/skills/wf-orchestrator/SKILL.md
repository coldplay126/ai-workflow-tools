---
name: wf-orchestrator
version: 2.1.0
description: "7-Phase 게이트 워크플로우 오케스트레이터. Agent Card 기반 라우팅으로 Phase를 실행."
type: workflow

capabilities:
  - file_read
  - file_write
  - shell_exec
  - code_analysis
  - code_modification

conditions:
  trigger: ".workflow/state.json이 존재하고 워크플로우가 활성 상태일 때, 워크플로우 진행 요청 시"
  skip: "wf.status, wf.reset 실행 시, 워크플로우와 무관한 작업, .workflow/가 없는 프로젝트"

cli:
  command: "awf wf next"
  args:
    phase: { description: "실행할 Phase (기본: 자동 결정)" }
    mode: { choices: ["solo", "quick", "precise", "cross", "critical"], description: "멀티에이전트 모드" }
    provider: { description: "Provider 지정 (기본: phase_models에서 결정)" }

prompts:
  base: "prompts/base.md"

allowed-tools:
  - Skill
  - Agent
---

# WF Orchestrator — A2A-Inspired Pipeline Router

## Deterministic Preflight

Claude skill은 repo 상태를 추측하지 않고 `awf`의 결정론적 결과를 먼저 읽습니다.
공통 규칙은 [reference/deterministic-preflight.md](reference/deterministic-preflight.md)를
따릅니다. 처음 도입하는 repo에서는 다음 순서를 기본 계약으로 사용합니다:

```bash
awf ready --repo-root . --json
awf scan . --no-ai
awf analyze <service> <unit> --repo-root . --dry-run --output-format json
awf wf init "작업 설명" --repo-root .
awf ready --repo-root . --gate workflow-run --json
awf wf next --repo-root . --dry-run --output-format json
```

워크플로우를 처음 시작할 때는 repo root에서 다음 gate를 실행합니다:

```bash
awf ready --gate workflow-init --repo-root . --json
```

기존 `.workflow/state.json`을 이어서 진행할 때는 다음 gate를 실행합니다:

```bash
awf ready --gate workflow-run --repo-root . --json
```

- exit code `0` (`decision: "allow"`)일 때만 `.workflow/` 생성 또는 Phase 실행으로 진행합니다.
- exit code `10` (`decision: "dry_run_only"`)이면 provider 호출 없이 `awf wf next --dry-run` 또는 상태 점검까지만 수행합니다.
- 그 외 non-zero는 오케스트레이션을 중단하고 `gate.recommended_next`의 명령만 제안합니다.
- `awf wf next --dry-run --output-format json` 결과에서 다음 phase, provider prompt, artifact 경로가 이해되지 않으면 provider-backed 실행으로 넘어가지 않습니다.

`awf wf init`과 provider-backed `awf wf next`도 같은 gate를 내부에서 다시 확인합니다. 상위 wrapper가 이미 같은 판정을 수행한 경우에만 `--no-ready-gate`를 사용합니다.

## Dispatch Surface Policy

`.workflow/state.json`과 `.workflow/artifacts/*`가 canonical state입니다. inline,
cmux-agent, Pi는 모두 실행 surface일 뿐이며 workflow state를 대체하지 않습니다.
세부 규칙은 [reference/deterministic-preflight.md](reference/deterministic-preflight.md)의
Dispatch Surface Policy를 따릅니다.

## Quick Resume (세션 컴팩션 후 우선 실행)

이 블록은 세션 재개 시 가장 먼저 실행합니다:

1. `.workflow/state.json` 로드
2. `currentPhase`와 각 Phase의 `status` 확인
3. `history` 최근 3건 읽어 컨텍스트 복원
4. 미완료 Phase부터 오케스트레이션 루프 재개
5. 상태 요약 출력: `"⏩ 워크플로우 재개: Phase {currentPhase} (retry {N}/{max})"`

**이 SKILL.md 전문을 다시 읽고 해당 Phase 섹션의 지시를 따르세요.**

## 역할

`.workflow/state.json`과 `.workflow/agent-cards/{phase}.json`을 읽고, provider-config에 따라 Phase를 **라우팅**합니다.
인라인 실행(SKILL.md)이 기본이며, 위임 모드(외부 LLM)도 지원합니다.

## Work History (K5)

`awf wf init` 시 `.work_history/YYYY-MM-DD-{slug}/` 세션 디렉토리가 자동 생성됩니다.
각 Phase 완료 시 주요 artifact가 세션 디렉토리에 복사되어 이전 세션의 맥락을 보존합니다.

```
.work_history/
├── 2026-04-06-auth-refactoring/
│   ├── concept.md          ← wf init 시 자동 생성
│   ├── plan.md             ← plan phase 결과
│   ├── review-codex.txt    ← review phase 결과
│   ├── impl-log.md         ← impl phase 결과
│   └── multi-agent/        ← cross/critical 결과
└── 2026-04-05-payment-fix/
    └── ...
```

`awf wf status`에서 최근 5개 세션 이력이 표시됩니다.

## Phase 전환 그래프

```
Phase      | Gate | 통과 시              | 실패 시 라우팅
-----------|------|---------------------|------------------------------------------
plan (P1)  | G1   | → review (자동)      | retry (max 3)
review (P2)| G2   | → approve (자동)     | CRITICAL → plan, HIGH → 사용자 선택
approve(P3)| G3   | → impl (자동)        | 수정 → plan, 거부 → 중단  [HIL]
impl (P4)  | G4   | → verify (자동)      | retry (max 5)
verify(P5) | G5   | → test (자동)        | scope → approve, bugs → impl, arch → plan
test (P6)  | G6   | → done (자동)        | test fail → impl (max 3)
done (P7)  | —    | 완료                 | —  [HIL]
```

## 오케스트레이션 루프

### 1. 상태 읽기
- `.workflow/state.json` 읽기
- `currentPhase`와 해당 phase의 `status` 확인
- 총 실행 카운터 확인 (30회 초과 시 중단 — 무한 루프 방지)

### 2. Agent Card 로드
- `.workflow/agent-cards/{currentPhase}.json` 읽기
- Agent Card가 없으면: **fallback** — 기존 방식으로 `phase-*` SKILL.md를 직접 읽고 실행
- Agent Card가 있으면: I/O 계약 기반으로 라우팅 진행

### 3. Precondition 검증 (Agent Card 기반)
Agent Card의 `input.required_state`를 확인:
- 선행 gate 통과 여부 검증
- retry budget 확인 (`agent_card.retry.max`)
- TTL 7일 경과 경고

### 4. 라우팅 결정

`.workflow/provider-config.json`을 확인하여 실행 모드 결정:

```
provider-config.json 존재?
├── Yes → phase_routing[currentPhase].mode 확인
│   ├── "inline"    → Step 5A (인라인 실행)
│   ├── "delegated" → Step 5B (위임 실행)
│   └── "dual"      → Step 5C (인라인 + 위임 병합)
│
├── codex-config.json만 존재? → review/verify만 dual, 나머지 inline (하위 호환)
└── No → 모든 Phase inline (기존 동작)
```

### 5A. 인라인 실행 (기본)

1. `▶ Phase N: <name> 시작` 출력
2. 해당 `phase-*` 스킬의 SKILL.md를 읽고 지침을 따라 실행
3. 스킬이 gate 검증까지 완료하면 state.json을 재읽기
4. gate 통과: `✓ <Gate> 통과 → <next phase>` 출력 후 루프 계속
5. gate 실패: state.json의 currentPhase가 회귀 대상으로 변경됨 → 루프 계속

### 5B. 위임 실행 (delegated)

Phase 전체를 외부 워커에게 위임:

**[Step B1: Task Message 구성]**

`~/.claude/skills/wf-orchestrator/templates/task-message.template.md`를 참조하여 self-contained 프롬프트 생성.
Agent Card의 `input.required_artifacts`를 순회하고, 프로젝트 규칙을 포함:

```
=== META ===
Project: {state.repo}
Branch: {state.branch}
Phase: {currentPhase} (attempt {retries + 1}/{max_retries})
Workflow ID: {state.id}

=== ROLE ===
You are a {currentPhase} agent for the {state.repo} project.
Your role: {agent_card.description}

=== RULES ===
{Rules 임베딩 — 아래 규칙 참조}

=== INSTRUCTION ===
{agent_card.description + skills 기반 자연어 지시}

=== OUTPUT FORMAT ===
CRITICAL: You MUST respond with ONLY a valid JSON object.
No markdown fences, no explanation text, no preamble, no trailing text.
The response must start with { and end with }.

Follow the 4-Block structure (generic multi-agent pattern):
- "conclusion": Final verdict (PASS/FAIL + summary)
- "evidence": Supporting data
- "risks": Side effects, edge cases
- "action_items": Recommended next steps

Schema:
{agent_card.output.structured_result 스키마 + 4-Block fields}

=== ARTIFACTS ===
{각 required_artifact의 전문 또는 경로}
=== END ===
```

**Rules 임베딩 규칙** (manifest.json `context_providers` 기반):
- `context_providers`에 AGENTS.md/CLAUDE.md가 있으면 RULES 섹션에 포함
- `provider.file_access: true` (Codex MCP) → 경로만: `"Read and follow: ./AGENTS.md, ./CLAUDE.md"`
- `provider.file_access: false` (Claude `--bare`) → 파일 전문 임베드
- 둘 다 없으면 RULES 섹션 생략

**아티팩트 포함 방식**:
- `provider.file_access: true` (Codex MCP) → 파일 경로만 포함, 워커가 직접 읽음
- `provider.file_access: false` (Claude `--bare`) → 아티팩트 전문 임베드

**[Step B2: 디스패치]**

| 프로바이더 | 호출 방식 | 폴백 |
|-----------|----------|------|
| `codex` | `mcp__codex__codex(prompt, sandbox:"read-only", cwd)` | `codex exec -s read-only "prompt"` (Bash) |
| `claude:<model>` | `claude --print --bare --model <model> --output-format json --max-budget-usd <budget> "prompt"` (Bash) | fallback_chain 다음 |

**모든 호출은 동기. `run_in_background: true` 사용 금지.**

**[Step B3: 응답 수신 + 파싱 + Format Retry]**

```
응답 수신 → JSON 파싱 시도
├── 성공 → structured_result 스키마 검증 → "✓ <Provider> 완료"
│
├── 파싱 실패 (1차) → Format Correction 재시도 (max 1회):
│   │
│   │  FORMAT_CORRECTION_PROMPT 구성:
│   │  "Your previous response could not be parsed as valid JSON.
│   │   Respond ONLY with a JSON object matching this schema:
│   │   {output_schema}
│   │   Previous response (first 500 chars): {truncated}"
│   │
│   ├── Codex MCP: mcp__codex__codex-reply(threadId, FORMAT_CORRECTION_PROMPT)
│   ├── Claude CLI: claude --print --bare ... "FORMAT_CORRECTION_PROMPT"
│   │
│   ├── 재시도 성공 → "✓ <Provider> 완료 (format retry)"
│   │                  provider_status: "format_retry"
│   └── 재시도 실패 → "⚠ <Provider> format retry 실패" → fallback_chain 다음 시도
│
├── 타임아웃 → "⏱ <Provider> 타임아웃" → fallback_chain 다음 시도 (재시도 없음)
│
└── 전체 실패 → "✗ 위임 실패, 인라인 실행으로 전환" → Step 5A로 fallback
```

응답 파싱:
- Claude JSON: `result.result` 필드
- Codex MCP: `content` 필드
- Codex Bash: stdout 전체
- **공통**: 응답에서 JSON 블록 추출 시도 — `{` 로 시작하는 줄 ~ 마지막 `}` 사이를 파싱

**[Step B4: Gate 평가]**

**반드시 CLI 명령으로 gate를 평가합니다** (결정론적 Python 검증기):

- plan phase: `awf wf gate plan`
- 기타 phase: `awf wf gate {phase} --result-file {result_json_path}`

**주의**: `awf wf gate`는 평가만 수행하고 state.json을 변경하지 않습니다. 상태 업데이트와 라우팅은 오케스트레이터가 별도로 수행합니다.

CLI 출력의 `G-{phase}: PASS/FAIL`을 기준으로:
- PASS → 오케스트레이터가 state.json 업데이트 (`gate.on_pass`)
- FAIL → 오케스트레이터가 `gate.on_fail` 라우팅에 따라 회귀

**[Step B5: Artifact 저장]**

워커 응답에서 마크다운 리포트를 추출하여 `agent_card.output.artifacts[].path`에 저장.

### 5C. Dual 실행 (inline + delegated)

Agent Card의 `hil` 확인 → hil이면 항상 inline only (5A로 분기).

Agent Card의 `capabilities.dual_strategy`로 실행 전략 결정:

#### 5C-A. parallel_evaluate (기본 — review/verify)

양쪽 LLM이 독립적으로 평가 후 결과 병합. generic multi-agent의 `#cross` 패턴과 동일.

> **Phase 4 자동 승격 (PR #30)**: `awf wf next` 의 `--mode` 가 미지정인 경우, review/verify phase 는 자동으로 `cross` 모드로 승격된다. 명시적 `--mode solo` 가 opt-out, `provider-config.json::wf.dual_strategy_phases` 로 비활성화 가능 (빈 리스트). 자동 승격 시 `.awf-operations/events/<date>.jsonl` 에 `dual_strategy_engaged` 이벤트가 기록된다.

1. **Primary**: Step 5A와 동일하게 인라인 실행. 단, SKILL.md의 Provider Dispatch 단계(Step 3.5/4.5)는 건너뜀.
2. **Secondary**: provider-config의 secondary 프로바이더로 Task Message 생성 + 디스패치 (Step 5B).
3. **Merge**: Primary와 Secondary 결과를 ID 기준으로 매칭:
   - 양쪽 동일 → `[Primary+Secondary]` 태그
   - 한쪽만 발견 → `[Primary]` 또는 `[Secondary]` 태그
   - 양쪽 상충 (PASS vs FAIL) → `REVIEW_CONFLICT` (gate에서 블록)
4. **Gate**: 병합된 결과로 평가. Multi-LLM Analysis 섹션을 리포트에 추가.

#### 5C-B. generate_then_validate (plan/test)

Primary가 생성 → Secondary가 사전 검증 → 피드백 반영. generic multi-agent의 `#critical` 순차 패턴과 유사.

1. **Primary (inline)**: SKILL.md 따라 산출물 생성 (spec.md, plan.md, tasks.md 등).
2. **Gate 기본 검증**: `awf wf gate plan` 실행하여 G1 조건 확인.
3. **Secondary (pre-validate)**: 생성된 산출물을 Task Message로 전달.
   - Instruction: `task-message.template.md`의 "plan pre-validate" 예시 사용
   - 산출물 + concept.md를 ARTIFACTS에 포함
   - 순차 실행: Primary 결과를 Secondary 컨텍스트에 포함 (이전 단계 결과 → 다음 단계 입력)
4. **Secondary 응답 파싱** (4-Block + findings):
   - CRITICAL/HIGH findings 발견 → `"⚠ Pre-validation: N건 발견"`
     → Primary(Claude)가 findings 기반으로 산출물 즉시 수정
     → Gate 재검증
   - MEDIUM/LOW만 → `"ℹ Pre-validation: N건 경고 (review에서 재확인)"`
     → Gate 진행 (경고만 출력)
5. **History 기록**: `{ "phase": "plan", "action": "pre-validated", "provider": "codex", "findings": N }`

#### 5C-C. implement_then_review (impl)

Primary가 구현 → Secondary가 git diff 리뷰 → 피드백 반영. Phase 4 전용.

1. **Primary (inline)**: `phase-impl` SKILL.md 따라 tasks.md 구현, lint, commit.
2. **G4 기본 검증**: 모든 task `[X]`, lint 0건 확인.
3. **Secondary (post-review)**: `git diff <base>...HEAD`를 Task Message로 전달.
   - Instruction: "Review this implementation diff for bugs, security issues, and spec compliance."
   - Artifacts: git diff + spec.md + tasks.md
   - Output: 4-Block + findings (CRITICAL/HIGH/MEDIUM/LOW)
4. **Secondary 응답 파싱**:
   - CRITICAL/HIGH findings → `"⚠ Post-review: N건 발견"` → Primary(Claude)가 즉시 수정 → G4 재검증
   - MEDIUM/LOW만 → `"ℹ Post-review: N건 경고"` → impl-log.md에 기록, G4 진행
5. **History 기록**: `{ "phase": "impl", "action": "post-reviewed", "provider": "codex", "findings": N }`

### 6. HIL Phase (approve, done)

Agent Card에 `"hil": true`인 Phase는 항상 인라인 실행.
아래 인라인 로직을 직접 실행하고 사용자 입력을 대기.

### 7. 종료 조건
- `phases.done.status == "completed"` → 워크플로우 완료
- max retries 소진 → 에러 출력 + 중단
- 총 실행 30회 초과 → "무한 루프 감지. 수동 개입 필요합니다." 중단
- 사용자가 거부 또는 중단 요청

## Phase 3: 승인 (인라인 — HIL)

### 프리앰블
1. `gates.G2.passed` true 확인. 아니면 중단.
2. `phases.approve.retries`가 1 이상이면 경고.
3. TTL 7일 경과 경고.

### 로직
1. `phases.approve.status: "in_progress"` 업데이트.
2. 승인 요약 표시:
   ```
   ══════════════════════════════════════
   워크플로우 승인 요청
   ══════════════════════════════════════

   기능: <state.id>
   브랜치: <state.branch>

   ── 스코프 ──
   파일: <allowed-files.json 파일 수>개
   <파일 목록>

   ── 리스크 ──
   CRITICAL: 0 | HIGH: <N> | MEDIUM: <N> | LOW: <N>
   Coverage: <XX>%

   ── 규모 ──
   Tasks: <N>개 (Phase <N>개)
   User Stories: <N>개

   ── 영향 ──
   <context_providers 기반, 없으면 생략>
   ══════════════════════════════════════
   ```
3. 사용자 선택 대기:
   ```
   승인하시겠습니까?
   1. 승인 — scope hash 잠금 + 구현 진행
   2. 수정요청 — 기획 수정으로 회귀
   3. 거부 — 워크플로우 폐기
   ```
4. **승인 시**:
   - `scope_hash` = spec.md + plan.md + tasks.md 해시
   - `.workflow/artifacts/approval.json` 생성
   - state.json: `phases.approve: completed`, `gates.G3.passed: true`, `gates.G3.scope_hash: <hash>`, `currentPhase: "impl"`
   - git branch 생성 (main/master/staging이면)
5. **수정요청 시**: review-feedback.md에 기록, `currentPhase: "plan"`
6. **거부 시**: `phases.approve.status: "rejected"`, 워크플로우 종료

## Phase 7: 최종확인 (인라인 — HIL)

### 프리앰블
1. `gates.G6.passed` true 확인.
2. TTL 경고.

### 로직
1. `phases.done.status: "in_progress"` 업데이트.
2. 종합 요약 생성:
   ```
   ══════════════════════════════════════
   워크플로우 최종 확인
   ══════════════════════════════════════

   기능: <state.id>
   브랜치: <state.branch>
   소요 시간: <createdAt ~ now>

   ── 7단계 타임라인 ──
   ✓ 기획    <completedAt> (retry: <N>)
   ✓ 검토    <completedAt> (retry: <N>)
   ✓ 승인    <completedAt>
   ✓ 작업    <completedAt> (retry: <N>)
   ✓ 검증    <completedAt> (retry: <N>)
   ✓ 테스트  <completedAt> (retry: <N>)
   ▶ 최종확인 진행 중

   ── 게이트 현황 ──
   G1~G6: PASS
   Bypass: <있으면 표시>
   Provider: <G2/G5에 provider 표시>

   ── 테스트 결과 ──
   회귀: <pass>/<total>
   수락: <auto + manual>/<total>

   ── 문서 갱신 체크 ──
   <context_providers 기반>
   ```
3. **문서 갱신 체크 (doc-update-check)**:
   a. `git diff <base-branch>...HEAD --name-only`로 변경 파일 목록 추출
   b. 아래 트리거 테이블과 매칭:

      | 변경 패턴 | 갱신 대상 (analysis-docs) |
      |----------|---------------------|
      | `**/routes.*`, `**/router.*` | 해당 서비스 `ROUTES.md` |
      | `**/migration*`, `**/schema*` | `02_infrastructure/databases/README.md` |
      | `package.json` 의존성 변경 | 해당 서비스 `README.md` |
      | 환경변수/시크릿 파일 | `13_operations/OPERATIONS_RUNBOOK.md` |
      | 서비스 간 HTTP 호출 변경 | `MANIFEST.md` + 관련 딥다이브 |

   c. 매칭 결과 출력:
      - 매칭 있음: `"📋 문서 갱신이 필요할 수 있습니다:"` + 대상 문서 목록
      - 매칭 없음: `"✓ 문서 갱신 트리거 없음"`
   d. analysis-docs MCP 등록 여부 확인. 등록되어 있으면 해당 문서를 읽어 현재 내용과 비교 가능
   e. 사용자에게 갱신 여부 확인: `"문서를 갱신하시겠습니까? (y/N)"`
   f. 갱신 시: analysis-docs 레포에서 브랜치 생성 → 문서 수정 → 커밋 → PR 생성

4. 수동 테스트 미서명 확인: test-report.md에 `- [ ]` 있으면 서명 요청
5. 사용자 최종 확인:
   ```
   최종 확인:
   1. 확인 + PR 생성
   2. 확인만 (PR 수동)
   3. 보류
   ```
6. **옵션 1**: 문서 갱신 → 커밋 → PR 생성 (squash merge 호환 body) → confirmation.json 기록 → state 완료
7. **옵션 2**: PR 생략, confirmation.json (pr_url: null), state 완료
8. 완료 메시지 + 후속 절차 안내

## Provider Config

### 설정 우선순위
1. `.workflow/provider-config.json` 존재 시: Phase별 라우팅
2. `.workflow/codex-config.json`만 존재 시: review/verify만 dual (하위 호환)
3. 둘 다 없으면: 모든 Phase inline (기존 동작)

### provider-config.json 스키마

```json
{
  "version": "2.0.0",
  "phase_routing": {
    "plan":    { "mode": "inline" },
    "review":  { "mode": "dual", "primary": "inline", "secondary": "codex" },
    "approve": { "mode": "inline" },
    "impl":    { "mode": "inline" },
    "verify":  { "mode": "dual", "primary": "inline", "secondary": "codex" },
    "test":    { "mode": "inline" },
    "done":    { "mode": "inline" }
  },
  "providers": {
    "codex": {
      "type": "mcp",
      "tool": "mcp__codex__codex",
      "fallback": "codex exec -s {sandbox}",
      "file_access": true,
      "timeout_seconds": 300
    },
    "claude:sonnet": {
      "type": "cli",
      "command": "claude --print --bare --model claude-sonnet-4-6 --output-format json --max-budget-usd {budget}",
      "file_access": false,
      "timeout_seconds": 180,
      "budget_usd": 0.50
    }
  },
  "fallback_chain": ["codex", "claude:sonnet"],
  "defaults": { "mode": "inline", "timeout_seconds": 300 }
}
```

### state.json gate 확장

```json
{
  "gates": {
    "G2": {
      "passed": true,
      "provider": "codex|claude:sonnet|null",
      "provider_status": "success|format_retry|fallback|timeout|parse_error|skipped",
      "format_retries": 0
    }
  }
}
```

`provider_status` 값:
- `success`: 첫 응답에서 정상 파싱
- `format_retry`: 첫 파싱 실패 후 포맷 교정 재시도로 성공
- `fallback`: fallback_chain의 다음 프로바이더로 성공
- `timeout`: 프로바이더 타임아웃
- `parse_error`: 모든 재시도 + fallback 실패
- `skipped`: 위임 없이 인라인 실행

### fallback 동작

- provider-config의 fallback_chain 순서대로 프로바이더 전환
- 전체 체인 소진 → **인라인 실행으로 fallback** (Step 5A)
- 설정 없거나 프로바이더 미설치 시: 인라인 실행

## 안전장치

- **총 실행 상한**: 전체 phase 실행 30회 초과 시 자동 중단
- **Retry budget**: plan:3, review:2, approve:1, impl:5, verify:2, test:3
- **HIL 필수**: approve/done은 절대 자동 통과 불가, 위임 불가
- **State 무결성**: 매 phase 전 state.json 읽기 + 선행 gate 확인
- **TTL**: createdAt > 7일이면 경고 표시
- **Bypass 감사**: `--force` 사용 시 history에 bypass 기록
- **동기 실행**: 모든 외부 워커 호출은 foreground. `run_in_background: true` 금지.
- **위임 fallback**: 위임 실패 시 항상 인라인 실행으로 안전하게 전환
- **Gate 실패 Protocol 제안**: gate 실패 시 적절한 `#mode`를 제안 (자동 실행 아님):
  - G2 CRITICAL → `💡 #cross로 교차 검증 가능`
  - G4 retries ≥ 3 → `💡 #precise로 코드 분석 가능`
  - G5 SCOPE_VIOLATION → `💡 #critical로 영향도 분석 가능`
  - G5 arch_issue → `💡 #cross로 아키텍처 재평가 가능`
  - G6 retries ≥ 2 → `💡 #precise로 테스트 실패 분석 가능`

## Error Classification

위임 실행 또는 gate 검증 중 발생하는 에러를 분류하여 적절한 복구 경로를 따릅니다:

| 에러 타입 | 감지 조건 | 복구 경로 |
|----------|----------|----------|
| `format_error` | JSON 파싱 실패 | format retry 1회 → fallback chain → inline |
| `timeout` | provider 응답 없음 (timeout_seconds 초과) | fallback chain → inline |
| `rate_limited` | HTTP 429 + "rate" 키워드 | 60초 대기 → 동일 provider 재시도 1회 → fallback |
| `budget_exceeded` | HTTP 429 + "billing"/"credits" 키워드 | 다음 provider로 영구 전환 (재시도 없음) |
| `auth_failure` | HTTP 401 | 사용자에게 인증 확인 요청 → provider 전환 |
| `gate_failure` | gate 조건 미충족 | 구조화된 피드백 생성 + retry (예산 내) |
| `scope_violation` | G5 스코프 벗어남 | approve Phase로 라우팅 (re-scope) |
| `max_retries` | retry 예산 소진 | 사용자 개입 요청 + 상태 저장 |

**핵심 구분**: `rate_limited`(일시적 → 대기 후 재시도)와 `budget_exceeded`(영구적 → provider 전환)는 동일 429이지만 복구 경로가 다릅니다.

## Risk-Based Routing

워크플로우 초기화 시 또는 Phase 1(plan) 완료 후, 변경 규모를 자동 판정하여 Phase별 깊이를 조절합니다.

### Change Class 판정

**반드시 아래 CLI 명령으로 판정합니다** (LLM 판단이 아닌 결정론적 Python 분류기 사용):

```bash
awf wf detect-class "concept 텍스트"
```

JSON 상세 결과 (skip/hil/risk investment 포함):
```bash
awf wf detect-class --json "concept 텍스트"
```

판정 규칙 (`detect_change_class()`):
- 고위험 키워드 매칭 (auth, payment, delete, migration, secrets, infra 등) → `high_risk`
- 30자 이하 + 고위험 키워드 없음 → `small`
- 그 외 → `standard`

### Phase별 깊이 조절

| Phase | small | standard | high_risk |
|-------|-------|----------|-----------|
| plan (P1) | concise (spec 간략) | standard | extended (상세 리서치) |
| review (P2) | gate 검증만 | gate + Codex dual | gate + Codex dual + 사용자 확인 |
| impl (P4) | Sonnet 에이전트 | Sonnet 에이전트 | Opus 에이전트 |
| verify (P5) | scope 체크만 | scope + spec 준수 | scope + spec + 코드 품질 전체 |
| test (P6) | 관련 테스트만 | regression + acceptance | full regression + acceptance + 수동 서명 |

### Phase별 모델 티어링

provider-config.json에 `phase_models` 섹션이 있으면 **awf-cli가 phase별로 다른 provider를 자동 선택**합니다:

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

- `inline_model`: phase 실행 시 사용할 모델 (기본: opus). awf-cli의 `_resolve_phase_provider()`가 읽음
- `effort`: Claude CLI `--effort` 수준 (low/medium/high/max). awf-cli가 Provider에 자동 주입
- `codex_reasoning`: Codex `-c model_reasoning_effort` 수준 (low/medium/high/xhigh). awf-cli가 Provider에 자동 주입
- impl/test Phase는 코드 작성/실행이 주이므로 Sonnet + high로 비용 절감
- plan/review/verify Phase는 추론 필요하므로 Opus + max 유지

**모델 결정 우선순위**: CLI `--provider` > `phase_models.{phase}.inline_model` > 글로벌 기본 provider

**약어 매핑**: `sonnet` → `claude:sonnet`, `opus` → `claude-code`, `haiku` → `claude-sdk`, `codex` → `codex`
