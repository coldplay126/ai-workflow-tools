# awf-cli

`awf-cli`는 `ai-workflow-tools`의 도구 중립 코어 계약을 Python 진입점으로 노출하는 실험적 CLI입니다. 현재 운영 우선 대상은 `claude-code`, `codex`, `gemini`, `fixture`이며 `claude-sdk`와 `openai`는 optional provider입니다.

현재 상태 요약:
- `ready`/`doctor`: repo-level deterministic preflight, runtime diagnostics, dispatch surface/Pi readiness 요약을 제공
- `analyze`: document-mode `.ai-context` 생성, resume/incremental, transitive invalidation, Stage 2 fan-out, Stage 3 conditional routing 지원
- `wf`: gated init/status/next/apply-result/reset, manual decide, ready gates, scope expansion/check, deterministic gate helpers 지원
- `wiki`/`cmux`: operations telemetry/wiki compile, cmux observability, Pi opt-in field-smoke evidence 소비 지원

최근 문서:
- [Gateway Migration Checklist](../docs/manuals/06-gateway-migration-checklist.md)
- [awf CLI Architecture](../docs/architecture/awf-cli-architecture.md)
- [Workflow Pipeline Architecture](../docs/architecture/02-wf-pipeline.md)
- [Provider Contract](../docs/specs/provider-contract.md)

현재 CLI 표면:
- `awf chat [--message ...] [--session-id ...] [--latest]`: Phase 5 chat session mode. SQLite에 세션/메시지를 저장하고 provider를 단일 턴 또는 최소 REPL로 호출하며, message-count threshold를 넘으면 turn 전 auto-compaction을 수행한다. compaction은 가능하면 provider-assisted summary를 사용하고, 실패하면 heuristic summary로 fallback한다. 각 turn에는 estimated input/output token과 session 누적 estimated cost를 함께 남긴다
- `awf chat --list-sessions | --show-session <id> | --show-latest | --compact-session <id> | --compact-latest`: session 조회/재개/수동 요약 압축 경로. session별 estimated token/cost usage도 함께 확인할 수 있고, compaction 결과에는 `summary_mode`가 포함된다
- `awf "<자연어 요청>"`: Phase 5 자연어 라우팅의 현재 버전. 안전한 조회 의도(`wf status`, `config show`, `skills list`, `mcp list`, session list/show)는 직접 디스패치하고, 명시적 analyze/review/verify 의도는 기본적으로 `--dry-run`으로 보낸다. `실행`/`run`이 포함되면 실제 `analyze`/`wf next` 실행으로 라우팅한다. 서비스명이 생략된 analyze 요청은 알려진 alias와 `analysis-docs/_templates/analysis-config.json`의 domain/service catalog를 기준으로 기본 service를 추론할 수 있고, 일부 service/domain/analyze keyword 오타도 보수적으로 보정한다. 그 외는 기본적으로 `chat --message`로 보낸다
- `awf analyze <service> <domain> [--mode solo|quick|precise|cross|critical] [--non-interactive] [--no-ready-gate]`: provider 위임 분석. 이전 분석이 있고 소스가 변경되었으면 변경 파일만 Stage 1 재분석 (incremental). Provider-backed 실행은 기본적으로 `awf ready --gate analysis`를 먼저 통과해야 한다
- `awf analyze <service> <domain> --dry-run --output-format json`: provider 호출 없이 deterministic discovery로 prompt와 경로를 구조화 JSON으로 출력한다. 설정이 비어 있는 repo에서도 dry-run은 AI unit discovery를 호출하지 않는다
- `awf analyze <service> --check`: drift detection. 소스 파일 해시와 `.tmp/hashes.json` 비교로 stale 단위 탐지
- `awf analyze <service> --catalog`: 서비스 전체 분석 현황. config의 단위 정의(분모) + .ai-context(분자) join
- `awf analyze <service> --cycles`: 저장된 import graph 기준 순환 의존성 리포트
- `awf wf init <concept> [--no-ready-gate]`: `.workflow` 초기화 + `.work_history/` 세션 자동 생성. 기본적으로 `awf ready --gate workflow-init`를 먼저 통과해야 한다
- `awf wf status`: `.workflow/state.json` 요약 출력 + 최근 work_history 세션 표시
- `awf wf next [--phase <name>] [--mode solo|quick|precise|cross|critical] [--auto-apply] [--non-interactive] [--no-ready-gate]`: 다음 phase 해석, delegated prompt 생성, `.workflow/tmp/`에 prompt/result 저장, fallback chain 시도, phase를 `in_progress`로 표시. Provider-backed 실행은 기본적으로 `awf ready --gate workflow-run`를 먼저 통과해야 한다. `--dry-run --output-format json`은 prompt preview를 구조화 JSON으로 출력하고 state/prompt 파일을 쓰지 않는다
- `awf wf decide <continue|replan|abort> [--phase <name>] [--target <phase>]`: deciding 상태의 closed-loop workflow phase에 수동 결정을 반영
- `awf wf apply-result <phase> <result-file>`: review/verify/impl/test JSON 결과를 artifact markdown으로 반영하고 gate/state를 갱신
- `awf wf gate <phase>`: plan/review/verify/impl/test deterministic gate 평가
- `awf wf pr [--base main] [--draft] [--dry-run]`: 현재 cycle의 state.json + concept.md를 PR title/body로 합성해 `gh pr create` 호출
- `awf wf detect-class <concept>`: concept text 기반 change class 판정
- `awf wf expand-scope`: 저장된 import graph의 reverse dependents/imports로 allowed-files 확장
- `awf wf scope-check`: `git diff`와 allowed-files를 비교하는 deterministic G5 scope check. `.workflow/manifest.json`의 `sibling_repos: [{name, path, branch}]`이 선언되면 각 sibling repo의 `git diff`도 합산한다. allowed-files 경로는 `@<name>/...` prefix로 sibling repo의 파일을 가리킨다 (docs/specs/multi-repo-scope.md). exit 코드는 violation 있으면 1, repo-level config 오류(missing path, branch 미해석 등)는 2
- `awf wf reset`: workflow state를 다시 `plan` phase로 초기화
- `awf config show`: 3-level merge 결과와 resolved path 확인
- `awf skills list`: supported search paths에서 `SKILL.md`를 탐색해 skill 목록 출력
- `awf mcp list`: merged config의 MCP 서버 registry 출력
- `awf mcp check <name>`: transport별 최소 연결 확인
  - `stdio`: 실제 `initialize` handshake + optional `tools/list`, `resources/list`
  - `http`: `initialize` POST 요청
  - `sse`: event-stream 연결 확인
- `awf mcp invoke <name> <tool> --input '{"key":"value"}'`: MCP tool 호출. 현재는 `stdio`, `http` transport 지원
- `awf mcp read <name> <uri>`: MCP resource 읽기. 현재는 `stdio`, `http` transport 지원
- `awf doctor [--probe] [--ci]`: provider readiness MVP. 기본은 installed/configured 상태와 default provider, session DB, MCP server count, dispatch surface preference, Pi opt-in runner readiness를 보여주고, `--probe`는 가능한 provider에 대해 lightweight subprocess probe를 추가한다. `--ci`는 default provider readiness가 충분하지 않으면 non-zero exit를 반환한다
- `awf ready [--probe] [--gate inspect|analysis|workflow-init|workflow-run|operations]`: repo별 자동화 준비 상태를 read-only로 요약한다. `doctor`/heuristic `scan`/skill discovery/workflow/operations 상태를 한 보고서로 모아 automation level(L0 inspect → L3 workflow)과 다음 추천 명령을 출력한다. `--gate`는 `decision: allow|dry_run_only|block`을 JSON에 포함하고 `allow` 외에는 non-zero exit로 Claude/Codex entrypoint와 내부 실행 명령을 중단시킨다. `.workflow/`가 target repo의 `.gitignore`에 있으면 workflow state가 local-only라는 경고를 함께 표시한다
- `awf init [--repo-root <path>] [--force]`: 대상 프로젝트에 `.awf.toml`을 초기화
- Gemini CLI provider: `.awf.toml`에서 `provider.default = "gemini"`로 선택한다. `provider.gemini.model = ""` 또는 `AWF_GEMINI_MODEL` 미설정은 Gemini CLI Auto를 의미하며, `gemini-3.1-pro` 같은 값을 넣으면 특정 모델로 고정한다
- `awf scan [repo_path] [--all] [--merge] [--dry-run] [--no-ai]`: 프로젝트 구조를 휴리스틱/AI fallback으로 탐색해 analysis config 후보를 생성한다. `--no-ai` 경로는 Python marker로 `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements.txt`, `Pipfile`, `poetry.lock`를 인식하며, `src/` 없는 script repo의 root-level source directory도 unit으로 잡는다
- `awf cmux tail [path] [-f] [--run-id ...] [--event ...] [--limit N] [--json]`: cmux-agent `.agent/events.jsonl`을 구조화된 4컬럼(`ts / run_id-prefix / event / summary`)으로 출력한다. `-f/--follow`는 폴링 기반 tail이며 `Ctrl-C`로 정상 종료한다. cmux-agent 패키지를 import하지 않는 read-only consumer다
- `awf cmux runs [path] [--json] [--limit N]`: 로그를 1회 스캔해 run_id별 `STARTED / STATUS / EVENTS / DURATION`을 요약한다. 마지막 `run.status_changed.new`가 `completed/failed/aborted`면 해당 값, 아니면 `running`으로 표시한다
- `awf cmux failures [path] [--run-id ...] [--limit N] [--json]`: `artifact.validation_failed`와 `message.failed`를 한 번에 필터링해 timestamp, run_id, target, reason을 보여준다. JSON 모드는 structured array를 출력한다
- `awf wiki init [--profile self_improvement|consumer]`: `.awf-operations/` 운영 텔레메트리 + LLM Wiki 레이아웃 초기화 (`.profile` marker + starter 디렉토리)
- `awf wiki decision "<title>" [--from-pr N] [--no-ready-gate]`: ADR-style 결정 페이지를 `wiki/decisions/<YYYY-MM-DD>-<slug>.md` 로 생성. `--from-pr` 은 `gh pr view` JSON 으로 context_prs/body 자동 prefill (gh 미설치 시 graceful fallback)
- `awf wiki log [--tail N]`: 시간순 운영 로그(`log.md`) 출력
- `awf wiki events [--type ...] [--limit N] [--json]`: 원본 JSONL 이벤트 스트림 필터 출력
- `awf wiki lint [--stale-days N] [--json]`: orphan / stale / missing-provenance / malformed-frontmatter 검출
- `awf wiki regenerate-index`: `wiki/` 변경 후 `index.md` 재생성
- `awf wiki compile [--since N] [--topic ...] [--dry-run] [--show-body] [--json] [--no-ready-gate]`: `events/*.jsonl` 을 결정적으로 합성해 `wiki/operations/<topic>.md` 4 페이지(stage1-invalidation/scope-check/dispatch-performance/dual-strategy-promotions) 갱신. LLM 호출 없음, idempotent overwrite, 자동 `regenerate-index`
- `~/.config/awf/config.toml`, `.awf.toml`: 기본 provider/경로 override 읽기
- `permissions.allowed_tools` / `disabled_tools` / `yolo`: provider 실행 전 최소 권한 검사
- `tools/` 모듈: `read/write/glob/grep/git diff/log` 기본 계층 추가 (Phase 2 groundwork)

### Operations wiki / `awf wiki` (English summary)

`awf wiki` manages the project-scoped knowledge layer under `.awf-operations/`:
- `init` seeds starter dirs and a profile marker (`self_improvement` for awf itself, `consumer` for projects using awf).
- `decision` creates ADR-style pages, with optional `--from-pr` prefill via `gh pr view`.
- `log` / `events` print the append-only log and raw JSONL event stream.
- `lint` flags orphan / stale / missing-provenance / malformed-frontmatter pages.
- `regenerate-index` rebuilds `index.md` from `wiki/` contents.
- `compile` deterministically synthesizes four `operations/<topic>.md` pages (stage1-invalidation, scope-check, dispatch-performance, dual-strategy-promotions) from the event log. Stdlib only — no LLM call. Idempotent overwrite, auto-runs `regenerate-index`. Confidence is computed from sample size and time span; output is reproducible and citable from ADRs.

빠른 실행:

```bash
uv run --project cli awf config show --repo-root .
uv run --project cli awf ready --repo-root .
uv run --project cli awf ready --repo-root . --json
uv run --project cli awf ready --repo-root . --gate analysis --json
uv run --project cli awf chat --repo-root . --provider fixture --message "hello" --json --yolo
uv run --project cli awf chat --repo-root . --latest --message "continue" --json --yolo
uv run --project cli awf chat --repo-root . --show-latest --json
uv run --project cli awf chat --repo-root . --compact-latest --json
uv run --project cli awf "workflow status 보여줘"
uv run --project cli awf "간단히 도와줘"
uv run --project cli awf "세션 목록 보여줘"
uv run --project cli awf "최근 세션 보여줘"
uv run --project cli awf "provider 상태 확인해줘"
uv run --project cli awf "provider 상태 probe 확인해줘"
uv run --project cli awf "sample-api quest-challenge 분석해줘"
uv run --project cli awf "quest challenge 분석해줘"
uv run --project cli awf "sample-api quest-challenge 분석 실행"
uv run --project cli awf "review 해줘"
uv run --project cli awf "review 실행"
uv run --project cli awf wf init "README 개편" --repo-root .
uv run --project cli awf wf status --repo-root .
uv run --project cli awf wf next --repo-root . --phase review --dry-run
uv run --project cli awf wf next --repo-root . --phase review --dry-run --output-format json
uv run --project cli awf wf next --repo-root . --phase review --provider codex --auto-apply
uv run --project cli awf wf apply-result review .workflow/tmp/result-review-claude_sonnet.json --repo-root .
uv run --project cli awf wf reset --repo-root .
uv run --project cli awf analyze sample-api quest-challenge --repo-root . --dry-run
uv run --project cli awf analyze sample-api quest-challenge --repo-root . --dry-run --output-format json
uv run --project cli awf analyze sample-api --check --repo-root .
uv run --project cli awf analyze sample-api --catalog --repo-root .
uv run --project cli awf skills list --repo-root .
uv run --project cli awf mcp list --repo-root .
uv run --project cli awf mcp check analysis-docs --repo-root .
uv run --project cli awf mcp invoke fixture-mcp echo --input '{"text":"hello"}' --repo-root .
uv run --project cli awf mcp read fixture-mcp fixture://resource --repo-root .
uv run --project cli awf doctor --repo-root .
uv run --project cli awf doctor --repo-root . --probe
uv run --project cli awf doctor --repo-root . --ci
uv run --project cli awf doctor --repo-root . --json --ci
uv run --project cli awf cmux tail --repo-root .
uv run --project cli awf cmux tail --repo-root . -f --run-id <run-id>
uv run --project cli awf cmux tail --repo-root . --event run.status_changed --limit 20 --json
uv run --project cli awf cmux runs --repo-root .
uv run --project cli awf cmux failures --repo-root . --limit 20
uv run --project cli awf cmux failures --repo-root . --json
AWF_CMUX_LOG=/path/to/events.jsonl uv run --project cli awf cmux tail
NO_COLOR=1 uv run --project cli awf cmux tail --repo-root . | cat
```

패키지 wheel 자체를 검증할 때만 editable checkout 대신 재설치된 wheel을 사용합니다:

```bash
uv run --project cli --no-editable --reinstall-package awf-cli awf --help
```

## awf cmux (observability)

`awf cmux`는 cmux-agent가 기록한 `.agent/events.jsonl`(4필드: `ts / event / run_id / data`)을 read-only로 소비해 구조화된 타임라인과 run 요약을 노출한다. cmux-agent 패키지를 import하지 않고 자체 JSONL parser로 동작하므로 cmux-agent 설치 여부와 무관하게 사용 가능하다.

- `awf cmux tail` — 고정폭 4컬럼(`ts / run_id-prefix / event / summary`) 출력. `-f/--follow`는 폴링 기반이며 `Ctrl-C`로 exit 0. `--run-id`, `--event`, `--limit N`, `--json` 필터/포맷 지원
- `awf cmux runs` — 전체 스캔 후 run_id별 `STARTED / STATUS / EVENTS / DURATION` 요약. `--json`으로 구조화 출력
- `awf cmux failures` — `artifact.validation_failed`와 `message.failed`만 출력. `--run-id`, `--limit N`, `--json` 지원
- 컬러는 stdout이 tty이고 `NO_COLOR`가 설정되지 않은 경우에만 ANSI escape 사용 (`rich`/`colorama` 등 외부 라이브러리 미사용)
- `--follow`에서 파일 rotation(`st_ino` 변경 또는 size 축소)이 감지되면 defensive하게 재오픈한다

Path 해석 우선순위:

| 순위 | 소스 | 비고 |
|------|------|------|
| 1 | `$AWF_CMUX_LOG` | 환경변수. 빈 문자열이면 무시 |
| 2 | positional `path` 인자 | 파일 또는 디렉토리 모두 허용. 디렉토리면 `<dir>/.agent/events.jsonl` |
| 3 | `<repo-root or cwd>/.agent/events.jsonl` | 존재하면 선택 |
| 4 | `<repo-root or cwd>/cmux-agent/.agent/events.jsonl` | 3번 fallback |

파일이 존재하지 않으면 `error: cmux events log not found at <path>. Set AWF_CMUX_LOG or pass a path.`와 함께 exit code 2로 종료한다.

권장 운영 패턴:
- 작성/오케스트레이션: `claude-code`
- `awf analyze`: small domain + standard mode 자동화 경로
- 큰 분석/Stage 3-heavy case: Claude Code `/analysis` 또는 `awf analyze --dry-run` 조합
- 분석 단독 실행 실험: `claude-sdk`
- review/verify 교차검증: `codex`

Phase 4 mode UX의 현재 매핑:
- `awf analyze --mode precise`: 동일 실행 경로를 유지하되, 더 보수적이고 근거 중심의 분석을 요청
- `awf analyze --mode cross`: 가능한 경우 secondary provider를 한 번 더 실행해 Stage 2 필수 산출물 세트를 보수적으로 교차 검증
- Stage 3은 CLI flag가 아니라 내부 라우팅 결과다. `related_domains`나 `stage3_force`가 있으면 internal deep context를 켜고, `should_run_stage3`가 retry block, `stage3_force`, `related_domains >= 3`, `stage_routing.{scale}.stage3` 순으로 실행 여부를 결정한다
- Stage 2 fan-out은 `[analysis.layer3] fanout_enabled=false` 또는 writer 목록이 비어 있을 때만 꺼지며, 켜져 있으면 synthesizer + writer prompt를 `.tmp/`에 준비하고 state에 기록한다
- fan-out이 실제로 선택되면 현재는 synthesizer 1회 → writer 4개 병렬 실행 → post-writer consistency pass 순으로 진행하고, 실패 시 single-agent Stage 2로 fallback
- `awf wf next --mode critical`: `codex` 우선, `claude:sonnet` fallback을 우선순위로 올리고 더 엄격한 gate 관점을 prompt에 추가
- synthesis는 현재 deterministic selection 규칙을 포함한다:
  - workflow review: PASS 결과끼리는 coverage가 더 높은 쪽 우선
  - workflow verify: PASS 결과끼리는 compliance percentage가 더 높은 쪽 우선
  - analyze cross: required set이 모두 complete면 extra file이 더 적은 쪽 우선

`--non-interactive`의 현재 의미:
- `awf wf next --non-interactive`: `review`/`verify` phase에서는 `--auto-apply`를 자동 활성화하고, 안내성 `next_step` 문구를 줄여 CI 로그를 더 간결하게 유지
- `awf analyze --non-interactive`: long-running 안내/후속 제안 문구를 줄이고 exit code + state/artifact 중심으로 동작

Skills discovery의 현재 의미:
- 검색 순서: `AWF_SKILLS_DIR` → `~/.config/awf/skills` → `.awf/skills` → `.claude/skills`
- 각 skill 디렉토리의 `SKILL.md`를 읽고 frontmatter의 `name`, `description`을 노출

MCP registry의 현재 의미:
- merged config의 `[mcp.*]`를 읽어 stdio/http/sse transport 메타를 보여주는 최소 registry
- `mcp check`는 현재 transport별 최소 연결을 확인
- `stdio`는 `initialize` + optional `tools/list`/`resources/list`까지 확인
- `http`는 `initialize` POST 요청까지만 확인
- `sse`는 event-stream 응답 확인까지만 수행
- `mcp invoke`는 현재 `stdio` transport에 대해 `tools/call`만 지원
- `mcp invoke`는 현재 `http` transport에 대해 JSON-RPC `tools/call`도 지원
- `mcp read`는 현재 `stdio`, `http` transport에 대해 `resources/read`를 지원
- sse 상호작용은 아직 미구현

개발 중 직접 모듈 실행이 필요하면:

```bash
cd cli
PYTHONPATH=src python3 -m awf wf status --repo-root ..
PYTHONPATH=src python3 -m awf chat --repo-root .. --provider fixture --message "hello" --json --yolo
```

실제 분석 실행은 로컬 `claude` CLI 인증 상태에 의존합니다.
`wf next`도 현재는 Claude CLI subprocess 위임 경로를 사용합니다.
생성된 prompt는 `.workflow/tmp/prompt-<phase>-<provider>.txt`에 저장됩니다.
provider stdout/stderr는 `.workflow/tmp/result-<phase>-<provider>.txt`에 저장됩니다.
`wf next`는 `provider-config.json`의 `fallback_chain`을 참고해 지원 가능한 provider를 순차 시도합니다.
`wf apply-result`는 현재 `review`, `verify` 두 phase만 지원합니다.

`awf analyze`는 실행 시 다음을 남깁니다.
- `.ai-context/.analysis-state.json`
- `.ai-context/.tmp/domain-bundle.xml`
- `.ai-context/.tmp/project-bundle.xml` (Stage 3 실행 시)
- `.ai-context/.tmp/stage1-analysis.md`
- `.ai-context/.tmp/prompt-stage2-<provider>.txt`
- `.ai-context/.tmp/result-stage2-<provider>.txt`
- `.ai-context/.tmp/stage2-draft.md`
- `.ai-context/.tmp/stage3-final.md` (Stage 3 실행 시)

Stage 2 결과가 `===FILE: ...===` 형식을 따르면 필수 산출물 4종을 실제 `.ai-context/` 파일로 분리 저장합니다.
`stage1/stage2/stage3` 상태와 bundle `configHash`도 `.analysis-state.json`에 기록됩니다.
Stage 3은 provider에 따라 두 경로로 동작합니다.
- `claude-code`, `claude-sdk`: Stage 3 cross-service validation을 한 번 더 실행하고 `stage3.status = completed`
- 그 외 provider: scaffold 메모만 남기고 `stage3.status = scaffold`
필수 산출물 4종이 모두 생성되면 `layers.output.status = completed`로 마감하고, 아니면 failed로 남겨 resume 가능한 흔적을 유지합니다.
이미 완료된 `.ai-context`가 있으면 provider를 다시 호출하지 않고 종료합니다.
이전에 저장된 stage2 결과가 있고 출력물만 비어 있으면, 저장된 결과를 다시 파싱해 산출물 복구를 먼저 시도합니다.
bundle 관련 설정이 바뀌면 기존 bundle은 무효화 후 재생성합니다.
`stage2.retryCount >= 2`인 failed 상태는 자동 재시도를 막고 비정상 종료합니다.

설정 예시:

```toml
[provider]
default = "claude-code"

[provider.claude-code]
command = "claude"
flags = ["--print", "--permission-mode", "default"]

[provider.claude-sdk]
api_key_env = "ANTHROPIC_API_KEY"
model = "claude-sonnet-4-6"
max_tokens = 8192

[provider.openai]
api_key_env = "OPENAI_API_KEY"
model = "gpt-5-mini"
max_output_tokens = 8192

[provider.codex]
command = "codex"
flags = ["exec", "--sandbox", "workspace-write"]

[provider.gemini]
command = "gemini"
flags = ["--output-format", "text"]
model = ""

[paths]
analysis_docs = "~/Documents/GitHub/analysis-docs"
awf_github = "~/Documents/GitHub"

[permissions]
allowed_tools = ["provider:claude-code", "provider:codex", "provider:gemini", "provider:fixture", "provider:claude-sdk", "provider:openai", "tool:file.read", "tool:file.glob", "tool:file.grep", "tool:git.diff", "tool:git.log"]
disabled_tools = []
yolo = false
```

현재 내장 provider:
- `claude-code`
- `claude-sdk`
- `openai`
- `codex`
- `gemini`
- `fixture` (테스트용)

`claude-sdk`는 optional provider입니다. 사용하려면 `anthropic` 패키지와 API key가 필요합니다.
`claude-sdk`로 `analyze`를 실행하면 stage1 memo, bundle XML, 기존 문서 일부를 prompt에 임베딩해 self-contained 실행 경로를 사용합니다.
이 경로는 prompt budget 경고를 출력하며, 예산을 넘기면 bundle 섹션부터 잘라서 한 번만 축소 시도합니다.
`claude-sdk`와 `openai`의 Python tool loop는 현재 `mcp_call_tool`, `mcp_read_resource`도 지원합니다. 즉 MCP server가 설정돼 있으면 provider가 MCP-backed tool을 직접 사용할 수 있습니다.
현재 guidance는 다음 원칙을 따릅니다:
- prompt와 repo에 이미 있는 정보는 MCP보다 먼저 사용
- repo-local 근거는 file/git tool을 우선 사용
- MCP는 외부 참조나 provider-configured lookup이 실제로 필요할 때만 사용
- 안정적인 reference는 `mcp_read_resource`, 능동 조회/계산은 `mcp_call_tool` 우선
- `server`는 명시 가능하지만, `[mcp_defaults]`가 설정돼 있으면 provider tool 호출에서 생략할 수 있음
MCP-backed tool guidance는 [awf CLI architecture](../docs/architecture/awf-cli-architecture.md)의 구현 메모와 위 원칙을 기준으로 유지합니다.
`codex`는 현재 `wf next --provider codex` 경로와 review/verify 검증에 맞춰져 있습니다.
`gemini`는 Gemini CLI 기반 provider입니다. 모델을 비워두면 Gemini CLI Auto에 맡기고, 특정 모델을 고정할 때만 `provider.gemini.model` 또는 `AWF_GEMINI_MODEL`을 설정합니다.
`openai`도 optional provider이지만 현재는 실운영 우선순위 밖의 experimental provider입니다.
`analyze`와 `wf next`는 provider 실행 전에 `permissions`를 검사합니다. `claude-sdk`와 `openai`의 tool loop는 `tool:file.read`, `tool:file.glob`, `tool:file.grep`, `tool:git.diff`, `tool:git.log` 권한도 함께 검사합니다. 필요하면 `--yolo`로 일시 우회할 수 있습니다.
`claude-code`는 기본적으로 Claude Code의 `default` permission mode를 사용합니다. 자동화 환경에서 권한 확인을 우회해야 할 때만 `--yolo`를 사용하면 `bypassPermissions`로 전환됩니다.
`--add-dir`는 provider 시작 시간이 길어질 수 있어 기본값이 `off`입니다. 분석 대상이 prompt에 충분히 임베딩되지 않았고 외부 sibling repo 접근이 필요할 때만 `awf analyze ... --provider-add-dirs minimal` 또는 `full`을 사용합니다. `minimal`은 외부 project root만 제한적으로 추가하고, `AWF_PROVIDER_ADD_DIRS_MAX`로 개수를 제한할 수 있습니다.
현재 실환경 기준으로 `claude-code` analyze는 small domain + standard mode에 더 적합하며, large/deep 분석은 timeout 가능성이 있습니다.
현재 chat usage 리포트는 provider-native usage가 있으면 그 값을 우선 사용하고, 없으면 `len(text) // 4` 기반 estimated token으로 fallback합니다. cost는 optional provider pricing 설정 기반 estimated cost입니다.
자연어 라우팅도 같은 원칙을 따른다:
- `분석해줘`/`review 해줘`/`verify 해줘` 같은 요청은 기본적으로 dry-run으로 보낸다
- `세션 목록`, `최근 세션 보여줘`, `최근 세션 압축해줘` 같은 chat/session intent도 직접 라우팅한다
- `provider 상태 확인해줘`, `환경 진단`, `provider 상태 probe 확인해줘` 같은 readiness intent는 `awf doctor` / `awf doctor --probe`로 직접 라우팅한다
- `quest challenge 분석해줘`, `health 분석해줘`처럼 service가 생략된 일부 analyze 요청은 known alias와 `analysis-config.json` catalog 기준으로 기본 service를 추론한다
- `healt analyss` 같은 일부 analyze/service/domain 오타도 fuzzy alias matching으로 보수적으로 보정한다
- `분석 실행`/`review 실행`/`verify 실행`처럼 명시적인 실행 의도가 있을 때만 실제 실행으로 보낸다
- 실제 실행으로 승격된 자연어 라우팅은 interactive TTY에서 한 번 더 실행 확인을 요청하고, 간단한 위험도(`low|medium|high`)를 함께 표시한다. `--non-interactive`, non-TTY, `AWF_AUTO_CONFIRM=1`에서는 확인을 생략한다
chat compaction은 현재 다음 순서로 동작한다:
- provider-assisted summary 우선
- 실패 시 heuristic truncation summary fallback
- manual compaction JSON 응답에는 `summary_mode = provider|heuristic|none`가 포함된다
`codex`는 `AWF_CODEX_TIMEOUT_SEC`(기본 300초)로 제어하며, `wf next`와 `analyze`는 provider 실행 시작/종료 시 timeout과 경과 시간을 출력합니다. `wf next`의 plan/review/verify/test phase와 multi-agent quick/secondary Codex 실행은 read-only sandbox로 낮추고, 결과 파일 작성은 AWF CLI 호스트가 수행합니다.

실운영 검증 순서:

```bash
uv run --project cli awf analyze sample-api health --repo-root . --provider claude-code --yolo
uv run --project cli awf wf next --repo-root . --phase review --provider codex --auto-apply
```

첫 번째 명령은 small domain 기준의 `claude-code` analyze 확인용입니다. 두 번째 명령은 Codex CLI 인증과 실행 가능 상태가 필요합니다.

테스트용 fixture provider 예시:

```toml
[provider]
default = "fixture"

[provider.fixture]
result_file = "cli/tests/fixtures/review-result.json"
```

이 설정으로 `awf wf next --phase review --auto-apply`를 live provider 없이 재현할 수 있습니다.

빠른 fixture 검증:

```bash
python3 cli/tests/run_fixture_flow.py
python3 cli/tests/run_analysis_fixture.py
python3 cli/tests/run_mcp_fixture.py
PYTHONPATH=cli/src python3 cli/tests/run_mcp_http_fixture.py
PYTHONPATH=cli/src python3 cli/tests/run_provider_mcp_fixture.py
PYTHONPATH=cli/src python3 cli/tests/run_judge_synthesis_fixture.py
python3 cli/tests/run_analysis_fanout_fixture.py
```

Fixture E2E runners:

```bash
bash cli/tests/run_core_fixture_e2e.sh
bash cli/tests/run_tooling_fixture_e2e.sh
bash cli/tests/run_all_fixture_e2e.sh
```

Optional live smoke:

```bash
bash cli/tests/smoke/smoke_doctor_ci.sh
bash cli/tests/smoke/smoke_analyze_dryrun.sh
bash cli/tests/smoke/smoke_session_list.sh
bash cli/tests/smoke/smoke_wf_review_codex_dryrun.sh
bash cli/tests/smoke/smoke_codex_ping.sh
bash cli/tests/smoke/smoke_claude_ping.sh
```

provider-side MCP 기본 서버 매핑 예시:

```toml
[mcp."fixture-mcp"]
type = "stdio"
command = "python3"
args = ["-u", "cli/tests/fixtures/fixture_mcp_server.py"]

[mcp_defaults]
default = "fixture-mcp"
invoke = "fixture-mcp"
read = "fixture-mcp"
```
