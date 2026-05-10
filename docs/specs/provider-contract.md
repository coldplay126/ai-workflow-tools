# Provider Contract Specification

> **Status**: Draft v0.1 (2026-04-17)
> **Owner**: awf-cli host parity 이니셔티브
> **목표**: LLM을 순수 provider 레이어로 고립시켜, host 레이어(awf-cli, Claude Code, 향후 확장)가 provider 교체(Claude ↔ Codex ↔ OpenAI) 시 **상위 워크플로우 코드 변경 없이** 작동하게 한다.

---

## 1. Purpose & Scope

### 1.1 왜 이 문서가 필요한가

현재 ai-workflow-tools는 Claude Code에 깊게 결합되어 있다:
- `WF_NATIVE`, `ANALYZE_NATIVE` 같은 capability는 사실상 **Claude Code 한정 기능**으로 선언됨
- `claude/skills/phase-*`는 Claude Code가 skill 로딩/실행을 책임진다는 전제
- `codex/AGENTS.md`는 review/verify 보조 경로로만 정의
- 7-phase 파이프라인 전체를 Claude 없이 실행하는 경로가 미완성

이 문서는 **provider**(LLM 호출 레이어)와 **host**(오케스트레이션 레이어)의 경계를 정의하여, 새로운 provider 추가나 기존 provider 교체가 **host 코드에 변경을 일으키지 않도록** 한다.

### 1.2 Non-goals

- 특정 LLM 공급자의 독점 기능(extended thinking, native web search 등) 1급 지원. 이런 기능은 optional capability로 취급하되, 워크플로우 필수 경로에서 배제된다.
- UX(chat REPL, TUI) 레이어. 본 문서는 backend contract만 다룬다.
- prompt 템플릿의 LLM-specific 최적화. 템플릿은 host의 책임이며, provider는 바이트 그대로 전달한다.

### 1.3 대상 독자

- 새 provider를 추가하려는 구현자 (예: Gemini, Mistral 추가)
- host 레이어를 수정하려는 구현자 (awf-cli, Claude Code skill, Codex adapter)
- Conformance test 작성자

---

## 2. Host vs Provider 책임 경계

본 계약의 핵심 원칙: **provider는 LLM 호출의 얇은 어댑터. workflow semantics는 host에 있다.**

| 책임 | Host | Provider |
|------|:----:|:--------:|
| prompt 생성 (phase-specific, agent-card 기반) | ✅ | ❌ |
| agent-card 로딩 / artifact 읽기·쓰기 | ✅ | ❌ |
| gate 판정, state 전이, 재시도 루프 | ✅ | ❌ |
| provider 선택(fallback chain) | ✅ | ❌ |
| prompt budget 관리, 섹션 절단 | ✅ | ❌ |
| `.workflow/` `.ai-context/` 파일 계약 | ✅ | ❌ |
| MCP server registry / routing | ✅ | ❌ |
| **LLM API 호출 (text in, text out)** | ❌ | ✅ |
| token usage 리포트 | ❌ | ✅ |
| tool call 중계 (provider가 tool loop를 네이티브 지원 시) | optional | ✅ if supported |
| streaming event 방출 (가능 시) | ❌ | ✅ if supported |
| 인증, API key 관리 | ❌ | ✅ |

**핵심 위반 사례** (현재 코드에서 수정 필요):
- `ClaudeCodeProvider`가 `ANALYZE_NATIVE` capability를 가진다는 것은 "Claude Code가 /analysis 오케스트레이션 전체를 안다"는 뜻. 이 책임은 host로 이전해야 한다.
- `claude/skills/phase-*`에 phase 로직이 내장된 것은 Claude Code host 특수성. awf-cli host로 이전하면 provider는 `complete()`만 제공하면 된다.

---

## 3. Provider Interface

### 3.1 Tier 구조

provider는 세 단계의 인터페이스를 구현한다. 각 tier는 상위 tier의 전제 조건이다.

```
Tier 0: Minimum  (필수)            → prompt in, text out
Tier 1: Streaming (권장)           → 중간 결과를 event로 방출
Tier 2: Tool Loop (optional)       → provider 네이티브 tool use/MCP 지원
```

host는 **각 phase가 요구하는 최소 tier**만 선언하고, provider가 그 tier를 만족하는지 capability로 검증한다.

### 3.2 Tier 0 — Minimum Interface (MUST)

```python
class Provider(Protocol):
    name: str
    capabilities: set[ProviderCapability]

    def complete(
        self,
        prompt: str,
        *,
        cwd: str | None = None,
        add_dirs: list[str] | None = None,
        timeout_sec: int | None = None,
    ) -> ProviderResult: ...
```

**계약**:
- `prompt`는 UTF-8 텍스트. provider는 이를 LLM에 그대로 전달한다.
- `cwd`, `add_dirs`는 provider가 파일 시스템 접근을 네이티브로 지원할 때 의미 있다(예: `claude --add-dir`). 미지원 provider는 무시하되 경고하지 않는다.
- `timeout_sec`은 provider 측 wall-clock timeout. 초과 시 `ProviderResult.returncode != 0`와 표준화된 오류 메시지를 반환한다.
- 함수는 **동기**. 스트리밍/비동기는 Tier 1 이상에서 제공.

> **현재 구현과의 diff**: 2026-04-17 기준 실제 `Provider.complete()` 시그니처에는 `timeout_sec` 파라미터가 **없다**. provider 초기화 시점 (`__init__`)에 환경변수로 설정될 뿐이다. 본 contract는 per-call timeout 지정이 필요함을 규정하므로, §10.2 Step 1 직후 "Step 0: `complete()` 시그니처에 `timeout_sec` 추가"를 선행해야 한다.

> **추가 패턴**: `ClaudeCodeProvider`는 `GatewayProvider` Protocol(`base.py:47-49`)도 구현하며 `execute(task) -> EventStream`을 제공한다. 이는 Tier 1의 `complete_stream()`과 다른 인터페이스로, workflow_loop에서 phase 단위 실행에 사용된다. v0.2에서 `GatewayProvider`와 Tier 1 관계를 재정의할 예정(§12 Open Question).

**ProviderResult 구조**:

```python
@dataclass
class ProviderResult:
    returncode: int           # 0 = success, non-zero = failure
    stdout: str               # LLM 응답 본문 (text). Markdown/JSON 여부는 prompt에 의해 결정
    stderr: str               # 진단 메시지 (토큰 리포트, 경고 등)
    usage: TokenUsage | None  # 가능하면 반드시 채울 것
    provider_name: str        # 응답한 provider 이름 (fallback chain 추적용)
    model: str | None         # 실제 사용된 모델 ID (있으면)
    elapsed_sec: float        # wall-clock 실행 시간
```

**오류 시맨틱**:
- `returncode != 0`이면 stderr에 사람이 읽을 수 있는 원인(인증 실패, timeout, rate limit, ...) 기록
- 예외를 **던지지 않는다**. 모든 실패는 `ProviderResult`로 구조화되어 반환. 이는 host의 retry/fallback 로직을 단순화하기 위함.
- 단, **설정 오류**(API key 누락, command not found 등)는 `ProviderConfigError`를 raise해도 무방.

### 3.3 Tier 1 — Streaming Interface (SHOULD)

```python
class StreamingProvider(Provider, Protocol):
    def complete_stream(
        self,
        prompt: str,
        *,
        cwd: str | None = None,
        add_dirs: list[str] | None = None,
        timeout_sec: int | None = None,
    ) -> Iterator[ProviderEvent]: ...
```

**ProviderEvent 분류** (최소 집합):

| 이벤트 | 페이로드 | 의미 |
|--------|----------|------|
| `text_delta` | `{chunk: str}` | 본문 토큰 증분 |
| `thinking_delta` | `{chunk: str}` | extended thinking 내부 추론 (Anthropic thinking tokens 등). optional. |
| `tool_use` | `{tool: str, args: dict, id: str}` | provider 네이티브 tool 호출 요청 (Tier 2) |
| `tool_result` | `{id: str, output: str, is_error: bool}` | tool 결과 반영 (Tier 2) |
| `usage` | `{input: int, output: int}` | 중간/최종 토큰 리포트 |
| `done` | `{returncode: int, final_text: str}` | 정상 종료 |
| `error` | `{message: str, fatal: bool}` | 오류 (fatal=true면 스트림 종료) |

**계약**:
- 스트림 소비자는 `done` 또는 `error(fatal=true)`를 받을 때까지 iterator를 소진한다.
- 중간에 `close()`를 호출해 조기 종료 가능. provider는 백엔드 취소 신호를 전송해야 한다(best effort).
- Tier 0의 `complete()`는 Tier 1이 있으면 `complete_stream()`을 내부적으로 소비해 구현 가능(레퍼런스 adapter 제공).

### 3.4 Tier 2 — Tool Loop Interface (OPTIONAL)

provider가 LLM tool use를 네이티브로 관리하는 경우 (Anthropic SDK, OpenAI SDK 등) 적용:

```python
class ToolLoopProvider(StreamingProvider, Protocol):
    def complete_with_tools(
        self,
        prompt: str,
        *,
        tools: list[ToolDefinition],
        tool_executor: Callable[[str, dict], ToolResult],
        cwd: str | None = None,
        timeout_sec: int | None = None,
        max_iterations: int = 10,
    ) -> ProviderResult: ...
```

**계약**:
- `tools`: host가 제공하는 tool 스키마(read/write/glob/grep/git/MCP 등). JSON Schema 또는 provider-특화 포맷.
- `tool_executor`: host가 제공하는 실행자. provider가 tool call 요청을 받으면 이 콜백으로 디스패치하고 결과를 LLM에 반영한다.
- `max_iterations`를 초과하면 loop를 종료하고 `returncode=2`, stderr에 `"tool_loop_exceeded"` 기록.

**미지원 provider**:
- Tier 2를 지원하지 않는 provider에서 tool이 필요한 phase는 **host가 tool loop를 외부에서 구현**한다(더 느리지만 동등한 결과).
- 구체적으로: host가 tool call 마커(`<tool_call>...</tool_call>`)를 포함한 prompt를 보내고, 응답을 파싱해 tool 실행 후 다시 `complete()` 호출. 이는 Tier 0만으로도 tool 워크플로우가 가능함을 보장.

---

## 4. Capability Matrix

### 4.1 Capability 재정의 (target)

현재 `ProviderCapability`를 다음과 같이 정비한다:

| 현재 | 조치 | 새 이름 / 의미 |
|------|------|---------------|
| `COMPLETE` | KEEP | Tier 0 지원. 모든 provider MUST. |
| `EVENT_STREAM` | KEEP, 이름 유지 | Tier 1 지원. `complete_stream()` 제공. |
| `TOOL_LOOP` | KEEP | Tier 2 지원. provider 네이티브 tool loop. |
| `ANALYZE_NATIVE` | **DEPRECATE** | Claude Code 특수성. 삭제 대상. host가 analyze 오케스트레이션을 전담한다. |
| `WF_NATIVE` | **DEPRECATE** | 위와 동일. WF도 host 전담. |
| 신규: `THINKING` | ADD | Anthropic extended thinking 등 내부 추론 토큰 지원 |
| 신규: `CITATIONS` | ADD (optional) | 응답에 출처 인용 포함 능력 |
| 신규: `SESSION` | ADD | provider가 multi-turn 세션을 네이티브 관리(OpenAI threads 등). 미지원 시 host가 대화 이력을 prompt에 포함 |
| 신규: `ADD_DIR` | ADD | `cwd` 외 추가 디렉토리 접근 지원 (`claude --add-dir`) |

### 4.2 Phase 요구 capability

각 phase는 **최소 tier**와 **선호 capability**를 선언한다. host는 이에 맞는 provider를 fallback chain에서 선택한다.

| Phase | 최소 tier | 선호 capability | 비고 |
|-------|:---------:|----------------|------|
| `/analysis` Stage 1 | Tier 0 | `ADD_DIR` | 파일별 분석. codex/sonnet 선호 |
| `/analysis` Stage 2 | Tier 0 | — | 도메인 합성. sonnet/opus |
| `/analysis` Stage 3 | Tier 0 | `THINKING` | 크로스서비스 검증. opus 선호 |
| WF plan | Tier 0 | `THINKING` | 설계 판단 집약 |
| WF review | Tier 0 | — | Codex(read-only) 우선 |
| WF approve | host 내부 | — | provider 호출 없음 |
| WF impl | Tier 0, 선호 Tier 2 | `TOOL_LOOP`, `ADD_DIR` | 파일 편집 필요. Tier 2 없으면 host가 외부 tool loop |
| WF verify | Tier 0 | — | |
| WF test | Tier 0, 선호 Tier 2 | `TOOL_LOOP` | 테스트 실행 후 결과 판정 |
| WF done | host 내부 | — | |
| `awf chat` | Tier 1, 선호 `SESSION` | — | 대화 세션 |

### 4.3 Provider별 capability

**현재 구현 (2026-04-17)** — 실제 코드에서 선언된 capability:

| Provider | Tier | Capabilities (현재) |
|----------|:----:|---------------------|
| `claude-code` | 2 | `COMPLETE`, `ANALYZE_NATIVE`*, `WF_NATIVE`*, `TOOL_LOOP`, `EVENT_STREAM`, `ADD_DIR` |
| `claude-sdk` | 2 | `COMPLETE`, `TOOL_LOOP`, `EVENT_STREAM` |
| `codex` | 0 | `COMPLETE`, `ADD_DIR` (sandbox 기반 subprocess) |
| `openai` | 2 | `COMPLETE`, `TOOL_LOOP`, `EVENT_STREAM` |
| `fixture` | 0 | `COMPLETE` (테스트 전용) |

*`ANALYZE_NATIVE`/`WF_NATIVE`는 deprecate 예정.

**목표 상태 (v0.2 이후)** — contract 정비 완료 후:

| Provider | Tier | Capabilities (목표) |
|----------|:----:|---------------------|
| `claude-code` | 2 | `COMPLETE`, `EVENT_STREAM`, `TOOL_LOOP`, `THINKING`, `ADD_DIR` |
| `claude-sdk` | 2 | `COMPLETE`, `EVENT_STREAM`, `TOOL_LOOP`, `THINKING`, `CITATIONS` |
| `codex` | 0 | `COMPLETE`, `ADD_DIR` (향후 `EVENT_STREAM` 추가 검토) |
| `openai` | 2 | `COMPLETE`, `EVENT_STREAM`, `TOOL_LOOP`, `SESSION` |
| `fixture` | 0 | `COMPLETE` |

> **enum 상태**: `THINKING`, `CITATIONS`, `SESSION`, `ADD_DIR`은 현재 `ProviderCapability` enum에 존재한다. `claude-code`와 `codex`는 CLI `--add-dir` 지원을 `ADD_DIR` capability로 노출한다.

**`ANALYZE_NATIVE` / `WF_NATIVE` 제거 후 파급**:
- `claude-code` provider는 더 이상 "파이프라인을 안다"는 역할을 하지 않는다.
- 현재 `claude/skills/phase-*`의 로직은 점진적으로 **host 코드**(`cli/src/awf/core/`)로 이전.
- 전환 기간 동안 `claude/skills/*`는 "얇은 alias"가 되어 `awf wf next --phase X`를 호출.

---

## 5. Lifecycle & Error Semantics

### 5.1 Provider 생명주기

```
create() [registry factory] → ready → complete()/complete_stream()  → done
                               ↓                    ↑
                          [auth check]            [retry n times]
                               ↓
                           ProviderConfigError (init 단계)
```

- **init 단계** (registry factory 호출 시): API key 존재, command PATH, 설정 유효성 검사. 실패 시 `ProviderConfigError` raise.
- **실행 단계** (`complete()` 내부): 모든 실패를 `ProviderResult(returncode!=0)`로 반환. raise 금지.
- **정리**: provider는 세션/연결을 들고 있어도 되지만, 각 `complete()` 호출이 독립적이어야 한다(idempotent).

### 5.2 Timeout

- provider는 `timeout_sec`을 받으면 반드시 wall-clock 기준으로 강제한다.
- `timeout_sec=None`이면 provider 기본값 사용:
  - `claude-code`: `AWF_CLAUDE_TIMEOUT_SEC` 기본 900초
  - `codex`: `AWF_CODEX_TIMEOUT_SEC` 기본 300초 (`cli/src/awf/providers/codex.py:30` 검증)
  - SDK 계열: provider별 기본 (120-600초)
- Timeout 시 `returncode=124`, stderr에 `"provider_timeout elapsed={}s"`.

### 5.3 재시도는 host 책임

provider는 자체적으로 재시도하지 않는다. 예외:
- rate limit (HTTP 429)은 provider 내부에서 exponential backoff 1회 허용 (사용자 경험)
- 그 외 모든 재시도는 host가 결정 (retry count를 state에 기록해 무한 루프 방지)

**fallback chain 정의 위치**: fallback chain 자체는 `provider-config.json`의 `phase_models.{phase}` 또는 루트 `fallback_chain` 필드에 host가 선언하며, provider는 chain에 참여하는 후보 중 하나일 뿐이다. chain traversal은 `cli/src/awf/core/workflow_loop.py`에서 host 로직으로 수행된다.

### 5.4 오류 분류

stderr 메시지는 다음 prefix 중 하나로 시작하는 것이 권장된다 (host의 오류 분류 편의):

| Prefix | 의미 | host 대응 |
|--------|------|----------|
| `provider_auth:` | 인증 실패 | 재시도 금지, 사용자에게 설정 안내 |
| `provider_timeout:` | timeout 초과 | fallback chain 이동 |
| `provider_rate_limit:` | rate limit | 지수 백오프 재시도 |
| `provider_unavailable:` | 서비스/네트워크 | fallback chain 이동 |
| `provider_bad_input:` | prompt가 너무 크거나 잘못된 형식 | host가 prompt 재작성/절단 |
| `provider_tool_error:` | tool loop 중 오류 | Tier 2에서 의미 있음 |

---

## 6. Prompt Contract

### 6.1 Prompt는 provider-neutral

host가 생성하는 prompt는 **어떤 provider가 받아도 동일하게 이해 가능**해야 한다. 이를 위해:

- Anthropic 특유의 `<thinking>` 태그, OpenAI 특유의 `system`/`user`/`assistant` role 구분을 prompt 본문에 넣지 않는다.
- provider가 role 분리를 원하면 어댑터 내부에서 `"\n\n[SYSTEM]: ..."` 같은 마커를 파싱해 분리한다.
- 본 문서 v0.1은 **single-prompt** 전제. 향후 v0.2에서 role-aware prompt 포맷을 추가할 수 있다.

### 6.2 Prompt 템플릿 위치

```
ai-workflow-tools/
├── core/prompts/             ← provider-neutral 원본 (target)
│   ├── wf/plan.md.j2
│   ├── wf/review.md.j2
│   └── analysis/stage2.md.j2
├── claude/skills/.../prompts/ ← Claude Code skill 포장 (점진적 얇은 래퍼로 전환)
└── codex/templates/           ← Codex AGENTS.md 프롬프트 포장
```

현재는 claude/ codex/ 하위에 분산되어 있다. 리팩터링 후 **단일 원본**(core/prompts/)을 두고 host가 provider에 맞게 경량 포장만 수행.

### 6.3 Prompt budget

- prompt 크기 제한은 provider별로 다르다(claude-sdk 200K 등).
- host는 **bundle 섹션부터** 절단하며, 필수 지시사항(system prompt, output format)은 마지막에 유지한다.
- provider는 절단을 수행하지 않는다 (의도된 prompt를 그대로 전송).

---

## 7. Tool / MCP 계약

### 7.1 Tool 스키마 (JSON Schema 기반)

host가 provider에게 전달하는 tool 정의:

```json
{
  "name": "file.read",
  "description": "Read a file from the project",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string"},
      "max_bytes": {"type": "integer", "default": 1048576}
    },
    "required": ["path"]
  }
}
```

provider는 이를 자체 포맷으로 변환하되, **tool 실행 결과 포맷은 통일**:

```json
{
  "tool_use_id": "...",
  "output": "<string>",
  "is_error": false,
  "metadata": {"bytes_read": 1234}
}
```

### 7.2 MCP

- MCP server registry는 host가 관리 (`mcp/` + config).
- Tier 2 provider는 MCP를 **tool의 subset**으로 처리한다 (host가 MCP tool을 일반 tool 스키마로 변환해 넘김).
- `tool_executor` 콜백 내부에서 host가 MCP 클라이언트를 호출.
- provider는 MCP transport(stdio/http/sse)를 알 필요 없다.

---

## 8. Versioning

### 8.1 Contract version

- 본 문서: `v0.1` (draft)
- provider 구현은 자신이 지원하는 contract version을 노출:
  ```python
  class Provider(Protocol):
      contract_version: str = "0.1"
  ```
- host는 contract version 호환성을 검사하고, mismatch 시 경고.

### 8.2 Breaking change 정책

- **Stable 규칙 (major version)**: major version 증가 시에만 breaking change 허용.
- **Draft 규칙 (`v0.x`)**: minor 내 변경 가능하되, 아래 **Tier 0 안정성 제약**을 준수한다(constitution C2 준수):
  - `Provider.complete()`의 필수 파라미터(`prompt`)와 `ProviderResult`의 필수 필드(`returncode`, `stdout`, `stderr`)는 v0.x 기간에도 breaking change 금지
  - 필수 필드 **추가**는 허용되나, **삭제/의미 변경**은 major version 증가 시에만 허용
  - 변경 시 §13 Change Log에 반드시 기록하고 기존 provider 구현자에게 사전 공지

---

## 9. Conformance Tests

본 계약을 만족하는지 검증하는 테스트 슈트 (`cli/tests/conformance/`) 요구사항:

### 9.1 Tier 0 (MUST pass)

> **구현 상태**: Step 4 완료 (2026-04-17). `fixture` provider는 baseline suite(`cli/tests/conformance/test_provider_contract_baseline.py`)에서 PASS, 4개 real provider(`claude-code`, `claude-sdk`, `codex`, `openai`)는 `@pytest.mark.live`로 격리되어 기본 실행에서 deselected. subprocess timeout 경로는 `unittest.mock`로 deterministic하게 검증한다.

- `test_complete_returns_result`: prompt 입력 시 `ProviderResult` 반환
- `test_error_is_structured`: 실패 시 예외 대신 `returncode!=0` 반환
- `test_timeout_enforced`: `timeout_sec=1` + 긴 prompt → `returncode=124`
- `test_usage_reported`: `usage.input_tokens > 0` (가능한 provider 한정)
- `test_unicode_roundtrip`: 한글/이모지 prompt 안전 처리

### 9.2 Tier 1 (SHOULD pass)

- `test_stream_emits_text_delta`: 스트림에서 텍스트 델타 수신
- `test_stream_ends_with_done_or_error`: 종결 이벤트 보장
- `test_stream_cancellation`: 조기 close 후 자원 해제

### 9.3 Tier 2 (optional)

- `test_tool_use_invokes_executor`: tool call 요청 시 executor 호출
- `test_max_iterations_enforced`
- `test_tool_error_propagates`

### 9.4 Phase-level conformance

각 phase를 동일 입력으로 두 provider(claude-code, codex)에서 실행하고 산출물의 **필수 필드 동등성**을 비교:

- `/analysis` Stage 2: 4개 필수 파일 생성 여부, 필수 섹션 존재 여부
- WF review: `findings` 배열 schema 일치, gate 판정 가능 여부
- WF impl: 편집된 파일 목록 동등성 (내용 완전 일치는 요구 안 함)

conformance test는 **provider 추상화 성공 기준**이며, [4] awf-cli host parity 작업의 졸업 판정 도구다.

---

## 10. Migration Path (Claude → Codex Parity)

현재 상태에서 목표 상태로 가는 단계:

### 10.1 현재 (2026-04-17)

- `claude-code`: 모든 phase 지원
- `codex`: review / verify phase만 실제 지원
- `claude/skills/phase-*`에 phase 로직 내장

### 10.2 Step 1 — Capability 재정의 (코드 변경 최소)

- `ANALYZE_NATIVE`, `WF_NATIVE` deprecated 표시
- `ProviderCapability` enum에 `THINKING`, `SESSION`, `ADD_DIR` 추가
- 문서/테스트만 변경. 기존 동작 유지.

### 10.3 Step 2 — Prompt 원본을 core/prompts/로 통합

- 현재 claude/skills/phase-*의 prompt 섹션을 추출해 `core/prompts/wf/*.md.j2`로 이전
- claude/ skills는 `awf wf next --phase X`를 호출하는 얇은 래퍼로 전환
- codex/ templates도 동일 원본을 소비

### 10.4 Step 3 — host 구현을 awf-cli로 이전

- phase 실행 로직(state 전이, gate 판정)을 `cli/src/awf/core/workflow_loop.py`로 이전
- `awf wf next --phase impl --provider codex`가 실제 작동
- 이 시점에서 claude-code는 "Tier 2 provider 중 하나"가 되고, 여전히 편의상 UX host 역할은 유지

### 10.5 Step 4 — Conformance gate

- `cli/tests/conformance/` 모든 테스트가 claude-code와 codex에서 녹색
- workflow `plan → done`이 `--provider codex` 단일 옵션으로 완주

### 10.6 Step 5 — Opt-in provider 확장

- OpenAI를 Tier 2 일등 시민으로 추가 (`awf wf`에서 `--provider openai` 지원)
- 향후 Gemini, Mistral 등은 본 contract만 구현하면 host 수정 0

---

## 11. 부록: 현재 구현과의 Diff

### 11.1 `cli/src/awf/providers/base.py` 권장 변경

현재:
```python
class ProviderCapability(str, Enum):
    COMPLETE = "complete"
    ANALYZE_NATIVE = "analyze_native"
    WF_NATIVE = "wf_native"
    TOOL_LOOP = "tool_loop"
    EVENT_STREAM = "event_stream"
```

목표:
```python
class ProviderCapability(str, Enum):
    # Tier 0 (required)
    COMPLETE = "complete"
    # Tier 1
    EVENT_STREAM = "event_stream"
    # Tier 2
    TOOL_LOOP = "tool_loop"
    # Optional
    THINKING = "thinking"
    CITATIONS = "citations"
    SESSION = "session"
    ADD_DIR = "add_dir"

    # Deprecated (kept for back-compat, host는 무시)
    ANALYZE_NATIVE = "analyze_native"   # DEPRECATED v0.1
    WF_NATIVE = "wf_native"              # DEPRECATED v0.1
```

### 11.2 `ProviderResult` 확장

현재 4개 필드 → `provider_name`, `model`, `elapsed_sec` 추가. 기존 코드는 호환성 유지.

### 11.3 conformance test 디렉토리 신설

`cli/tests/conformance/` + `cli/tests/conformance/fixtures/` 생성. 각 Tier 테스트는 파일 단위로 분리.

**Step 4에서 실제 생성된 파일**:
- `cli/tests/conformance/__init__.py` — 패키지 마커
- `cli/tests/conformance/conftest.py` — `baseline_fixture_path`, `fixture_provider`, parametrized `live_provider_name` fixture
- `cli/tests/conformance/test_protocol_contract.py` — `ProviderResult` 7필드 / `ProviderCapability` 9 enum / keyword-only 시그니처 / deprecated 마커 정적 검증
- `cli/tests/conformance/test_provider_contract_baseline.py` — FixtureProvider 대상 Tier 0 MUST 5종 (returns_result, unicode, error, timeout, usage reported/skip)
- `cli/tests/conformance/test_provider_contract_subprocess.py` — SubprocessProvider timeout(124)/timeout=None/structured error를 mock으로 검증 + `claude-code`/`codex` 대상 `@pytest.mark.live` 스켈레톤
- `cli/tests/conformance/test_provider_contract_sdk.py` — `claude-sdk`/`openai` 대상 `@pytest.mark.live` 스켈레톤 (timeout은 `xfail(strict=False)`)
- `cli/tests/conformance/fixtures/fixture_echo.txt` — baseline 응답 본문 (한글/이모지 포함)
- `cli/tests/conformance/fixtures/fixture_error.txt` — 빈 파일 (오류 유도는 `AWF_FIXTURE_RETURNCODE` 환경변수로)
- `cli/tests/conformance/fixtures/slow_cmd.py` — 장기 실행 레퍼런스 (실 실행은 mock으로 차단)
- `cli/tests/conformance/fixtures/.gitkeep` — 빈 fixture 디렉토리 보존용

---

## 12. Open Questions

본 draft에서 확정하지 않고 후속 논의 대상:

1. **Role-aware prompt 포맷 도입 여부** — 현재 single-prompt 전제. OpenAI system/user 분리를 1st-class로 지원할지.
2. **Async interface** — `async def complete()` 병행 제공 여부. 현재는 sync만.
3. **Session state 위치** — awf chat 세션을 provider가 관리 vs host SQLite. 현재 host SQLite 전제.
4. **Prompt template engine** — jinja2 / mustache / 단순 f-string 중 표준화 필요.
5. **Tool schema 표준** — JSON Schema vs Anthropic format vs OpenAI format. 내부 표준은 JSON Schema, 어댑터에서 변환하는 쪽으로 제안.

---

## 13. Change Log

| 버전 | 날짜 | 요약 |
|------|------|------|
| v0.1 | 2026-04-17 | 초안. Tier 구조, capability 재정의, migration path 정의 |
| v0.1.1 | 2026-04-17 | 정합성 리뷰 반영: codex timeout 300초 수정, 현재/목표 capability 분리, Tier 0 breaking change 제약 추가, fallback chain 정의 위치 명시, GatewayProvider 언급, timeout_sec 파라미터 현재 구현 diff 명시 |
| v0.1.2 | 2026-04-17 | Step 0+1 구현 완료. ProviderResult 3필드 추가(provider_name/model/elapsed_sec, 모두 default 값), complete() timeout_sec 파라미터 추가(5개 provider + SubprocessProvider), capability enum 4개 확장(THINKING/CITATIONS/SESSION/ADD_DIR) |
| v0.1.3 | 2026-04-17 | Step 4 구현: `cli/tests/conformance/` 신설. Tier 0 MUST 5종 + 프로토콜 정합성 4종 테스트 추가 (총 +12 PASS, +1 fixture skip). subprocess timeout은 `unittest.mock.patch`로 deterministic 검증. 4개 real provider(claude-code/claude-sdk/codex/openai)는 `@pytest.mark.live`로 격리, `pyproject.toml` `addopts = "-m 'not live'"`로 기본 실행에서 deselected (총 10개). pytest `live` marker 등록 |
