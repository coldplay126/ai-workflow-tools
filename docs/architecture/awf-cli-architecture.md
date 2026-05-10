# awf-cli 아키텍처 설계 — 3분할 차용 전략

> **문서 상태**: 이 문서는 2026-04-06 기준 **초기 설계 의사결정**을 기록한 design document입니다. 현재 구현은 [cli/README.md](../../cli/README.md)를 기준으로 삼아야 합니다. 2026-04-08 이후 아래 항목이 변경되었습니다:
> - `--deep` CLI 플래그 제거 (커밋 a1ec135). Stage 3은 `related_domains`/`stage3_force` 기반 자동 승격으로 전환
> - Stage 2 실제 fan-out 구현 (synthesizer + 4 writer)
> - gateway migration 완료 (2026-03-31)
>
> opencode 참고 내용은 host/runtime 구조 초안을 정리한 것으로, 현재 구현은 이보다 단순한 형태로 귀결되었습니다.

## Context

ai-workflow-tools의 `/wf`, `/analysis`, `.workflow` 계약을 독립 CLI로 제품화한다.
opencode/crush를 **채택**하지 않고, 각 프로젝트의 강한 패턴만 **차용**하여 AWF 전용 코어를 구축한다.

**결정**: awf-cli 직접 구축 (Python + uv)
**참고 원천**: opencode(UX/runtime), crush(config/registry/permissions), ai-workflow-tools(도메인 코어)

### 왜 직접 구축인가

| 비교 대상 | 역할 | awf-cli와의 관계 |
|-----------|------|-------------------|
| **opencode** (anomalyco) | 완성형 범용 코딩 에이전트 호스트 | 실행 UX, host/runtime 분리, built-in agent 경험 **참고** |
| **crush** (charmbracelet) | 범용 agent runtime/terminal app | config 계층, MCP transport, permissions, skills, provider 관리 **참고** |
| **awf-cli** | AWF 전용 orchestration core | `.workflow`, `/analysis`, gate/state 계약을 **직접 제품화** |

두 프로젝트 모두 `.workflow`와 `/analysis`를 그대로 이해하지 못하므로, 채택 시 결국 AWF 전용 레이어를 다시 얹어야 한다. 따라서 직접 구축하되 검증된 패턴만 차용한다.

### 참고 프로젝트 링크

- opencode: https://github.com/anomalyco/opencode
- crush: https://github.com/charmbracelet/crush

---

## 1. opencode에서 차용할 항목

### 1.1 Host/Runtime 분리 구조 + 실행 모델 이분화

opencode는 Hono HTTP 서버(host)와 Agent(runtime)를 분리한다. awf-cli에서는 경량화하되, **실행 모델을 용도별로 이분화**한다:

```
awf-cli (host)
  ├─ CLI entry (typer)          ← 사용자 인터페이스
  ├─ Session Manager            ← SQLite 세션 (이력/토큰 추적용)
  └─ Step Dispatcher            ← 실행 모델 선택 + 디스패치
       │
       ├─ [chat] Session-based agent loop
       │   └─ LLM → tool calls → validate → execute → repeat
       │       (장기 대화, compaction 적용)
       │
       └─ [wf/analysis] Stateless step invocation
           └─ artifact 읽기 → provider 호출 → artifact 쓰기 → 종료
               (phase/stage 단위, 대화 이력 없음)
```

**실행 모델 이분화 원칙**:

| 용도 | 실행 모델 | 컨텍스트 관리 | 이유 |
|------|----------|--------------|------|
| `awf chat` / REPL | session-based loop | SQLite + compaction | 대화형, 이력 필요 |
| `awf wf next` | stateless invocation | artifact 파일 읽기/쓰기 | phase별 컨텍스트 격리, `.workflow/artifacts`가 상태 |
| `awf analyze` | stateless invocation | `.analysis-state.json` + `.tmp/` | stage별 컨텍스트 격리, 산출물이 상태 |

**왜 이분화하는가**: ai-workflow-tools의 강점은 `.workflow/artifacts/`, `agent-cards`, `.analysis-state.json`으로 컨텍스트를 **파일에 외부화**하는 것이다. wf/analysis를 session loop로 실행하면 같은 정보를 대화 이력과 artifact 파일에 이중 저장하게 되고, phase가 바뀔수록 불필요한 이력이 누적되어 compaction 비용이 발생한다.

**차용 핵심**: opencode의 agent processor loop는 `awf chat`에만 적용. wf/analysis는 기존 artifact-driven 계약을 유지하는 stateless step invocation이 기본이다.

### 1.2 Agent 구조: Router + Evaluator Slot

opencode의 agent 정의 구조를 참고하되, ai-workflow-tools의 기존 인프라(phase_models, dual_strategy)에 맞게 **router + evaluator slot** 구조로 설계한다.

```python
@dataclass
class AgentInfo:
    name: str
    description: str
    mode: Literal["router", "worker", "session"]
    model: str | None                  # 기본 모델 (없으면 phase_models/config에 의해 결정)
    prompt_template: str               # 시스템 프롬프트 템플릿
    tools: list[str]                   # 사용 가능한 tool 이름
    permission: PermissionRuleset      # tool별 allow/deny/ask
```

내장 agent:

| Agent | Mode | 역할 | 모델 결정 |
|-------|------|------|----------|
| `chat` | session | 자연어 REPL, 장기 대화 | config default |
| `wf-router` | router | `/wf` 명령을 phase/provider로 디스패치 | phase/state/provider 해석 중심의 제한적 추론 |
| `analysis-synthesizer` | worker | Stage 2 도메인 합성, Stage 3 크로스서비스 | phase_models에 의해 결정 |
| `analysis-writer-*` | worker | .ai-context 파일 개별 생성 (fan-out 시) | sonnet |
| `review-evaluator` | worker | review phase evaluator slot | dual_strategy + provider-config에 의해 결정 |
| `verify-evaluator` | worker | verify phase evaluator slot | dual_strategy + provider-config에 의해 결정 |
| `judge` | worker | findings 종합, gate 판정 | opus (추론 집중) |

**핵심 구조 원칙**:
- **router**는 얇다 — 긴 컨텍스트를 직접 들고 있지 않고, 어떤 worker/provider를 호출할지만 결정
- **worker**는 짧다 — artifact 읽기 → provider 호출 → artifact 쓰기 → 종료 (stateless)
- **session**은 길다 — chat에서만 사용, compaction 적용
- **evaluator slot**은 provider를 꽂는 자리 — `dual_strategy: parallel_evaluate`일 때 2개 slot에 Codex + Sonnet을 병렬 배치

**WF 모델 결정 우선순위**: CLI `--provider` > `provider-config.json`의 `phase_models.{phase}.inline_model` > 글로벌 기본 provider
**Analysis 모델 결정 우선순위**: CLI `--provider` > `analysis-pipeline.json`의 `stage_routing.{scale}.{stage}` > 글로벌 기본 provider

`provider-config.default.json`의 phase_models (WF용):
```json
{
  "phase_models": {
    "plan":   { "inline_thinking": "high" },
    "review": { "inline_thinking": "high" },
    "impl":   { "inline_model": "sonnet", "inline_thinking": "medium" },
    "verify": { "inline_thinking": "high" },
    "test":   { "inline_model": "sonnet", "inline_thinking": "medium" }
  }
}
```

### 1.3 Install/Run UX

opencode의 다중 설치 경로를 참고하여:

```bash
# Primary (Python 생태계)
uv tool install awf-cli          # 추천
pip install awf-cli              # 대안

# 명령 구조
awf --help                       # CLI help
awf analyze sample-api health    # 분석 실행
awf wf init '기능 설명'           # 워크플로우 시작
awf wf next                      # 다음 phase
awf wf status                    # 현재 상태
awf config show                  # 설정 확인
awf mcp list                     # MCP 서버 목록
```

### 1.4 세션/상태 관리 (SQLite)

opencode의 SQLite 스키마에서 핵심만 차용:

```sql
-- ~/.local/share/awf/awf.db
CREATE TABLE session (
    id TEXT PRIMARY KEY,
    project_dir TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    title TEXT,
    model TEXT,
    token_input INTEGER DEFAULT 0,
    token_output INTEGER DEFAULT 0,
    time_created TEXT NOT NULL,
    time_updated TEXT NOT NULL
);

CREATE TABLE message (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES session(id),
    role TEXT NOT NULL,            -- user | assistant | tool
    content TEXT NOT NULL,         -- JSON
    time_created TEXT NOT NULL
);
```

**차용 핵심**: 세션 재개, 토큰 추적, 이력 조회
**AI workflow**: `.workflow/state.json`과 `.analysis-state.json`은 기존 파일 기반 유지 (호환성)

### 1.5 Compaction 전략 (chat 세션 전용)

opencode의 컨텍스트 압축:
- `tokens_used > context_limit - reserved_buffer` → 오래된 메시지 요약
- 최근 N개 메시지 유지, tool output 정리

awf-cli 적용: **`awf chat` (session mode)에서만 적용**. wf/analysis는 stateless step invocation이므로 대화 이력이 누적되지 않아 compaction이 불필요하다.

---

## 2. crush에서 차용할 항목

### 2.1 Config 계층 (3-level merge)

crush의 config 우선순위:
```
.crush.json (프로젝트) > crush.json (프로젝트) > ~/.config/crush/crush.json (사용자) > defaults
```

awf-cli 적용:
```
.awf.toml (프로젝트, git tracked)
  > ~/.config/awf/config.toml (사용자)
  > awf-cli 내장 defaults
```

```toml
# .awf.toml (프로젝트 루트)
[provider]
default = "claude-sdk"
model = "claude-sonnet-4-6"
fallback = ["openai:gpt-4.1", "codex"]

[provider.claude-sdk]
api_key_env = "ANTHROPIC_API_KEY"
max_tokens = 8192

[provider.claude-code]
command = "claude"
flags = ["--print", "--bare"]

[paths]
analysis_docs = "~/Documents/GitHub/analysis-docs"
awf_github = "~/Documents/GitHub"

[analysis]
default_mode = "standard"    # standard | deep

[permissions]
allowed_tools = ["read", "glob", "grep"]

[mcp.analysis-docs]
type = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "${AWF_DOCS_ROOT}"]
```

`[paths]` override는 `~`를 확장한 명시 경로로 해석한다. 환경변수 기반 경로를 쓰려면
`[paths]` 항목을 생략하고 셸 환경에서 `AWF_DOCS_ROOT` / `AWF_GITHUB_ROOT`를
설정한다. MCP `command` / `args`는 환경변수 expansion을 거치므로
`${AWF_DOCS_ROOT}` 같은 placeholder를 사용할 수 있다.

Config loader 구현:
```python
def load_config() -> AwfConfig:
    """crush 방식의 3-level merge"""
    defaults = AwfConfig.defaults()
    user_config = load_toml(Path.home() / ".config/awf/config.toml")
    project_config = load_toml(find_project_root() / ".awf.toml")

    return defaults.merge(user_config).merge(project_config)
```

### 2.2 MCP Transport (3종)

crush의 MCP 설정 형식을 그대로 차용:

```toml
# stdio (로컬 프로세스)
[mcp.analysis-docs]
type = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/docs"]
timeout = 120

# http (원격)
[mcp.remote-api]
type = "http"
url = "https://api.example.com/mcp/"
headers = { Authorization = "Bearer ${API_TOKEN}" }
timeout = 30

# sse (스트리밍)
[mcp.streaming]
type = "sse"
url = "https://example.com/mcp/sse"
timeout = 120
```

**차용 핵심**: transport 추상화 + 환경변수 치환 (`${VAR}` syntax)
**구현**: `mcp/client.py`에서 transport type별 팩토리

### 2.3 Permissions 모델

crush의 3단계 권한:

```python
@dataclass
class PermissionRuleset:
    allowed_tools: list[str]          # 자동 허용 목록
    disabled_tools: list[str]         # 완전 비활성화 (agent에게 안 보임)
    yolo: bool = False                # 모든 권한 자동 승인

# tool 실행 시
async def check_permission(tool_name: str, action: str) -> bool:
    if ruleset.yolo:
        return True
    if tool_name in ruleset.disabled_tools:
        raise ToolDisabledError(tool_name)
    if tool_name in ruleset.allowed_tools:
        return True
    return await prompt_user(f"Allow {tool_name}:{action}?")
```

awf-cli 적용:
```toml
# .awf.toml
[permissions]
allowed_tools = ["read", "glob", "grep", "analysis_docs"]
disabled_tools = ["bash"]      # analyzer agent에서 bash 비활성화

# CLI 플래그
# awf analyze --yolo quest-challenge    → 모든 tool 자동 승인
# awf wf next --non-interactive         → CI/CD용, 자동 승인 + 프롬프트 없음
```

### 2.4 Skills Discovery

crush의 다중 경로 탐색:
```
$CRUSH_SKILLS_DIR > ~/.config/crush/skills > .crush/skills > .claude/skills
```

awf-cli 적용:
```python
SKILL_SEARCH_PATHS = [
    *( [Path(os.environ["AWF_SKILLS_DIR"])] if os.environ.get("AWF_SKILLS_DIR") else [] ),
    Path.home() / ".config/awf/skills",                 # 사용자
    project_root / ".awf/skills",                       # 프로젝트
    project_root / ".claude/skills",                     # Claude Code 호환
]
```

Skill 파일 형식 (crush의 SKILL.md 차용):
```markdown
---
name: custom-analyzer
description: "프로젝트 특화 분석 로직"
---

분석 시 다음 규칙을 적용하세요:
- API 엔드포인트는 반드시 인증 미들웨어를 포함해야 합니다
- ...
```

### 2.5 Provider Registry

crush의 3-layer provider:
```
embedded (내장) < remote registry (업데이트) < custom (사용자 정의)
```

awf-cli 적용:
```python
class ProviderRegistry:
    """crush 방식의 layered provider registry"""

    def __init__(self):
        self._builtin = {
            "claude-sdk": ClaudeSdkProvider,
            "claude-code": ClaudeCodeProvider,
            "openai": OpenAiProvider,
            "codex": CodexProvider,
        }
        self._custom: dict[str, ProviderFactory] = {}

    def get(self, name: str) -> Provider:
        if name in self._custom:
            return self._custom[name]()
        if name in self._builtin:
            return self._builtin[name]()
        raise UnknownProviderError(name)

    def register(self, name: str, factory: ProviderFactory):
        """사용자 정의 provider 등록"""
        self._custom[name] = factory
```

```toml
# 사용자 정의 provider
[provider.custom-deepseek]
type = "openai-compat"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"
models = ["deepseek-chat", "deepseek-coder"]
```

---

## 3. ai-workflow-tools에서 유지할 항목 (직접 구현)

### 3.1 .workflow 계약

기존 `.workflow/` 구조 100% 호환:
```
.workflow/
├── state.json              ← 7-phase 상태 (awf-cli가 직접 읽기/쓰기)
├── provider-config.json    ← dual mode 설정
├── agent-cards/            ← phase별 I/O 계약
│   ├── review.json
│   ├── verify.json
│   └── ...
└── artifacts/
    ├── concept.md
    ├── spec.md
    ├── tasks.md
    ├── allowed-files.json
    └── impl-log.md
```

**핵심**: Claude Code `/wf`와 awf-cli `awf wf`가 같은 state.json을 공유. 사용자가 두 도구를 혼용 가능.

### 3.2 agent-cards 기반 Gate 시스템

```
agent-cards/
├── plan.json       → G1: concept.md 존재 + 5-field 완성도
├── review.json     → G2: critical finding 0건
├── impl.json       → G4: allowed-files (planned ∪ expanded) 준수 + 컴파일
├── verify.json     → G5: `awf wf scope-check` 위반 0건 (결정론적, .workflow/ 자동 제외)
└── test.json       → G6: 테스트 통과율 >= 80%
```

```python
class GateEvaluator:
    """agent-cards 기반 결정론적 gate 평가"""

    def evaluate(self, phase: str, artifacts: dict) -> GateResult:
        card = load_agent_card(phase)
        checks = []

        for rule in card["gate"]["pass_conditions"]:
            result = self._check_rule(rule, artifacts, phase=phase, card=card)
            checks.append(result)

        passed = all(c.passed for c in checks)
        next_phase = card["gate"]["on_pass"]["next_phase"] if passed else None
        return GateResult(passed=passed, checks=checks, gate_id=card["gate"]["id"], next_phase=next_phase)
```

**주의**: `agent-cards`는 현재 `gate_rules` 필드가 아니라 `gate.pass_conditions` 구조를 사용한다.
`awf-cli`는 이 계약을 그대로 해석해야 Claude/Codex/WF runner와 호환된다.

### 3.3 /analysis 파이프라인

4-Layer, 3-Stage 구조를 Python으로 직접 포팅:

```
Layer 1 (Input)  → service/unit 해석, 경로 탐색 (heuristic + AI auto-discovery), include_patterns 적용
Layer 2 (Bundle) → scale 판정 (small/standard/large), 파일 수집 (언어별 자동 결정)
Layer 3 (Analyze) → Stage 1 (파일별 XML 번들 + import context, 저비용) → Stage 2 (단위 합성, 중간) → Stage 3 (프로젝트 정제, 고비용)
Layer 4 (Output) → .ai-context/ 4개 파일 생성
```

Stage별 provider는 `analysis-pipeline.json`의 `stage_routing.{scale}`에서 결정:
- small: stage1=codex, stage2=sonnet, stage3=skip
- standard: stage1=codex, stage2=sonnet, stage3=opus
- large: stage1=codex, stage2=opus, stage3=opus

모드별 라우팅:

| | standard | deep |
|---|---|---|
| Stage 2 provider | sonnet | scale별 (sonnet/opus) |
| Stage 2 실행 방식 | 단일 agent (4파일 통합) | 조건부 fan-out (아래 참조) |
| Stage 3 | skip | scale별 (skip/execute) |
| Stage 3 provider | - | opus (기본), sonnet (opt-in, 아래 참조) |
| Evaluator | lightweight | full |

#### Stage 2 조건부 Writer Fan-out

기본: 단일 agent가 4개 파일(api-spec.json, data-model.md, domain-overview.md, external-integration.md)을 한 번에 생성한다. 이 방식은 파일 간 일관성(엔드포인트 ↔ 엔티티 ↔ 흐름 ↔ 의존성)을 자연스럽게 보장한다.

**조건부 fan-out**: 다음 조건을 **모두** 충족할 때, synthesizer + 4 writer 구조로 분리한다:

```
scale == "large"
AND (bundle_tokens > STAGE2_TOKEN_THRESHOLD OR bundle_lines > STAGE2_LINE_THRESHOLD)
```

- `scale`만으로는 부족하다 — 같은 large라도 작은 DTO 50개(번들 작음)와 거대한 서비스 15개(번들 큼)는 실제 문맥 밀도가 다르다
- threshold 값은 구현 시 실측으로 결정 (config에서 오버라이드 가능)

fan-out 시 실행 구조:
```
analysis-synthesizer (sonnet/opus)
  → Stage 1 결과 기반 도메인 관점 정리 + 파일별 지시서 생성
  → fan-out:
      analysis-writer-api (sonnet)         → api-spec.json
      analysis-writer-data (sonnet)        → data-model.md
      analysis-writer-domain (sonnet)      → domain-overview.md
      analysis-writer-integration (sonnet) → external-integration.md
  → synthesizer가 4개 결과의 상호 일관성 최종 검증
```

장점: 부분 재시도 가능, 컨텍스트 분산. 단점: 파일 간 일관성 검증 비용 추가.

**기존 fallback과의 관계**: analysis.md의 Stage 2 파싱 실패 fallback(재시도 → 개별 분할)은 그대로 유지. fan-out은 "실패 시 분할"이 아니라 "사전에 분할하는 것"이 다르다.

#### Stage 3 모델 선택

**기본: Opus** (현행 유지). Stage 3은 deep 모드에서만 실행되고 빈도가 낮으므로, 안전성을 우선한다.

**Sonnet 다운그레이드: config flag로 opt-in** (`stage3_model_override: "sonnet"`).

Sonnet 사용 가능 조건 (3가지 모두 충족):
- 기존 문서와 신규 산출물의 누락/불일치 **표면 비교**만 수행
- 크로스서비스 dependency graph **재해석 없음**
- 아키텍처 권고/재분류 **없음**

Opus가 필요한 경우 (하나라도 해당):
- dependency graph 재해석이 필요 (새 서비스 추가, 의존 방향 변경)
- 아키텍처 패턴 재분류가 필요 (CQRS→Event Sourcing 등)
- 3개 이상 서비스의 계약 불일치 해소가 필요

**Future: 에스컬레이션 메커니즘** (장기 최적화, 현재 미구현)

Sonnet으로 시작했으나 실행 중 Opus가 필요하다고 판명될 때의 상태 전이:
1. `stage3.provider = "sonnet"` → 실행 시작
2. escalation 조건 충족 시 → `stage3.status = "escalated"`
3. Sonnet 결과는 `.tmp/`에만 보존, **최종 산출물에 반영하지 않음**
4. `stage3.provider = "opus"` → 재실행
5. **최종 승인은 Opus 결과만 사용 — 부분 혼합 금지**

escalation 조건 예시:
- findings에서 `CRITICAL` severity 발견
- cross-service 의존이 예상보다 확대 (3개 이상)
- 기존 문서와의 차이가 변경 항목 threshold 초과

### 3.4 State Persistence (하이브리드)

| 상태 유형 | 저장소 | 이유 |
|----------|--------|------|
| WF state | `.workflow/state.json` | Claude Code 호환, git tracking |
| Analysis state | `.analysis-state.json` | 기존 resume protocol 호환 |
| CLI 세션 | SQLite (`~/.local/share/awf/awf.db`) | 이력, 토큰, 재개 (opencode 차용) |

### 3.5 Multi-Agent Protocol

기존 `#precise`/`#cross`/`#critical` 모드를 CLI에서 직접 지원:

```bash
awf analyze sample-api quest-challenge --mode precise  # Codex + Claude 검증
awf analyze sample-api quest-challenge --mode cross    # Codex + Sonnet 병렬
awf wf next --mode critical                    # Codex → Sonnet → Primary 체인
```

다중 에이전트 실행은 `awf.core.dispatch.MultiAgentDispatch` 인터페이스 뒤에 추상화된다:
- `run(workers, *, cwd, strategy)` — 고정 리스트의 워커를 parallel/sequential 로 실행. cross / agent team (Phase 5) 가 사용.
- `run_chained(steps, *, cwd)` — 각 step의 prompt 가 이전 `AgentResult` 리스트에 의존하는 체인. critical 이 사용. agent team 은 prior threading 이 blackboard 기반(파일 side-effect)이라 `run` 으로 회귀.

백엔드는 `InlineDispatch` (ThreadPoolExecutor), `CmuxDispatch` (cmux-agent `.agent/` artifact 프로토콜), `PiDispatch` (Pi print-mode terminal harness)가 있고, `provider-config.json` 의 `dispatch.surface_preference` 로 선택한다 (`auto`/`inline`/`cmux`/`pi`). `auto` 는 기존처럼 inline/cmux만 선택하며, Pi는 명시적 opt-in일 때만 사용한다. cmux 라우팅에서 `run_chained` 는 step 별 worker 를 role 로 고정해 같은 터미널 컨텍스트가 chain 동안 누적되도록 한다.

Judge Rules (결정론적):
1. `CRITICAL` finding 1건 이상 → FAIL
2. `HIGH` finding 존재 시 phase별 gate 규칙에 따라 FAIL 또는 사용자 확인 필요
3. Slave 불일치 → FAIL (보수적)
4. 전부 PASS → PASS

**주의**: severity 체계는 `CRITICAL|HIGH|MEDIUM|LOW`로 고정한다.
`major/minor/info` 같은 별도 체계를 도입하지 않고, 기존 `review.json`, `verify.json`, `codex/AGENTS.md`와 동일하게 유지한다.

### 3.6 Operations data layer

awf 가 내보내는 운영 이벤트(transitive invalidation 요약, scope-check 판정, 향후 dispatch 결과 등)는 `<repo_root>/.awf-operations/` 아래에 프로젝트 단위로 누적된다. service 단위 분석 산출물(`.ai-context/`)과 분리되어 있고 git ignore 대상이다 (운영 텔레메트리는 로컬 누적용).

```
.awf-operations/
├── events/<YYYY-MM-DD>.jsonl    # 원본 이벤트 스트림 (불변, append-only)
├── log.md                        # ## [ts] type | summary 한 줄/이벤트
├── index.md                      # 자동 재생성되는 wiki 페이지 카탈로그
└── wiki/
    ├── operations/<topic>.md     # LLM이 누적 컴파일하는 운영 synthesis 페이지
    └── concepts/<topic>.md       # 차후: concept 페이지
```

각 wiki 페이지는 YAML frontmatter(`title` / `last_compiled_at` / `source_runs` / `source_commits` / `confidence` / `related`)를 가지며 `awf wiki lint` 가 orphan / stale / missing-provenance / malformed-frontmatter 를 검출한다. 컨셉은 Karpathy 의 LLM Wiki 패턴(2026-04 gist) — RAG 대신 컴파일된 LLM-friendly markdown 으로 지식을 누적. `wiki/operations/` 페이지는 `awf wiki compile` 이 event 스트림을 결정적으로 합성해 생성한다 (LLM 호출 없음). service 단위 `.ai-context/wiki/` Stage 4.5 자동 컴파일은 추후 별도 PR.

CLI:
```bash
awf wiki init --profile self_improvement          # starter dirs + .profile marker
awf wiki decision "Adopt operations wiki" --from-pr 37  # ADR-style page under wiki/decisions/
awf wiki log                                       # 시간순 history
awf wiki events --type stage1_invalidation         # 원본 JSONL 스트림 필터
awf wiki lint                                      # orphan/stale/missing-provenance 검출
awf wiki regenerate-index                          # wiki/ 변경 후 index.md 갱신
awf wiki compile --since 30 --topic scope-check --dry-run  # events → operations/<topic>.md 합성
```

**Profile 두 가지** — 같은 storage 위에서 starter set 과 decision 템플릿 variant 만 다름:
- `self_improvement`: awf 자체 개선용 (개념: dispatch architecture, transitive invalidation 매커니즘 등). starter dirs: `decisions/ concepts/ operations/`.
- `consumer` (기본): awf 를 사용하는 프로젝트용 (개념: per-service domain summary, awf config 튜닝 결정). starter dirs: `decisions/ services/ operations/`.

Profile 선택은 `.awf-operations/.profile` 한 줄 marker 에 저장되며, 모든 wiki 명령에 `--profile` 플래그로 1회 override 가능. `awf wiki init` 이 awf 자체 레포 같은 환경(`cli/pyproject.toml::name == "awf-cli"`)에선 `self_improvement` 추천 hint 만 표시 — auto-detect 는 안 한다 (false-positive 회피).

**Events** — 현재 5 종이 자동 기록된다:
- `stage1_invalidation`: `awf analyze` 의 import-graph transitive invalidation 결과 카운트
- `scope_check`: `awf wf scope-check` 의 위반/계획/변경 카운트
- `analysis_complete`: `awf analyze` 성공 종료 시 (service/domain/mode/total_seconds/source_file_count/bundle_line_count/bundle_token_estimate/output_file_count)
- `dispatch_complete`: cross/critical 끝 시 (backend/strategy/worker_count/success_count/total_seconds)
- `dual_strategy_engaged`: `awf wf next` 가 review/verify phase 에서 solo→cross 자동 승격 시 (phase/promoted_from/promoted_to)

**Gitignore 분리**: `.awf-operations/events/`, `log.md`, `.profile`, `wiki/operations/` 는 ignore. `wiki/decisions/`, `wiki/concepts/`, `wiki/services/`, `index.md` 는 commit 대상 — ADR 와 LLM 합성 페이지는 PR 에 박혀야 협업이 된다.

**`awf wiki compile`** — `.awf-operations/events/*.jsonl` → `wiki/operations/<topic>.md` 결정적 합성. 정책:
- LLM 호출 없음 (stdlib only). 합성 결과는 100% 재현 가능하며 ADR 에 evidence 로 인용 가능. LLM 소비는 *reader* 가 페이지를 읽을 때 일어난다.
- Topic 4종 (event type 1:1 매핑, `analysis_complete` 제외): `stage1-invalidation`, `scope-check`, `dispatch-performance`, `dual-strategy-promotions`.
- 추가 frontmatter 키: `event_window: [start_date, end_date]`, `event_count: N`, `event_types: [...]`, `metric_method: deterministic_v1`. lint 의 provenance keys 에 `event_window` 추가.
- Confidence 자동 산정: `high` (≥50 events ∧ ≥7d), `medium` (≥10 ∧ ≥3d), `low` (그 외). `contested` 는 사람이 ADR 에서만 명시.
- Idempotent overwrite + 자동 `regenerate_index`. 0-event topic 은 페이지 생성 안 함.

**`awf wiki compile` (English)** — Deterministic synthesis from `.awf-operations/events/*.jsonl` to `wiki/operations/<topic>.md`. Stdlib only, no LLM call: results are 100% reproducible and ADR-citable; LLM consumption belongs to a reader opening the page later. Four topics (1:1 with event types, excluding `analysis_complete`): `stage1-invalidation`, `scope-check`, `dispatch-performance`, `dual-strategy-promotions`. Extra frontmatter keys: `event_window`, `event_count`, `event_types`, `metric_method: deterministic_v1`; `event_window` is recognized as provenance by `awf wiki lint`. Confidence is computed (high ≥50 ∧ ≥7d, medium ≥10 ∧ ≥3d, else low; `contested` reserved for human ADR use). Compile is idempotent, auto-regenerates the index, and skips topics with zero events in window.

---

## 4. 프로젝트 구조

```
ai-workflow-tools/
└── cli/                          ← 신규 (awf-cli 소스)
    ├── pyproject.toml
    ├── src/awf/
    │   ├── __init__.py
    │   ├── cli.py                # argparse 진입점
    │   │
    │   ├── commands/             # CLI subcommands
    │   │   ├── analyze.py
    │   │   ├── wf.py
    │   │   ├── wf_apply.py
    │   │   ├── config.py
    │   │   ├── skills.py
    │   │   └── mcp.py
    │   │
    │   ├── providers/            # crush registry 차용
    │   │   ├── base.py           # Provider Protocol
    │   │   ├── registry.py       # ProviderRegistry (layered)
    │   │   ├── claude_sdk.py     # anthropic SDK
    │   │   ├── claude_code.py    # subprocess
    │   │   ├── openai.py         # openai SDK
    │   │   └── codex.py          # subprocess
    │   │
    │   ├── core/
    │   │   ├── config.py         # crush 3-level merge
    │   │   ├── state.py          # .workflow prompt/context helpers
    │   │   ├── analysis_state.py # .analysis-state.json
    │   │   ├── gates.py          # agent-cards 기반 gate (gate.pass_conditions)
    │   │   ├── permissions.py    # crush permission model
    │   │   ├── workflow_results.py
    │   │   ├── analysis_prompt.py
    │   │   ├── judge.py
    │   │   ├── mcp.py            # stdio/http/sse check + stdio invoke/read
    │   │   └── skills.py         # 다중 경로 skill 탐색
    │
    └── tests/
```

---

## 5. 구현 Phase

### Phase 1: 기반 (2주)
- [x] `pyproject.toml` + uv 프로젝트 초기화
- [x] Config loader (3-level merge, TOML) — crush 패턴
- [x] Provider Protocol + ClaudeCodeAdapter (subprocess)
- [x] `awf analyze <service> <domain>` — claude --print로 위임
- [ ] 설치: `uv tool install awf-cli`

**검증**: `awf analyze sample-api quest` → claude subprocess → .ai-context/ 생성

### Phase 2: SDK 직접 호출 (2주)
- [x] ClaudeSdkAdapter (anthropic Python SDK)
- [x] Tool 시스템: file read/write/glob/grep Python 네이티브
- [x] Stateless step invoker (artifact 읽기 → provider 호출 → artifact 쓰기)
- [x] `--deep` 모드: Stage 1→2→3 오케스트레이션 (Stage 2는 단일 agent 기본)
- [x] Evaluator gate + State persistence (.analysis-state.json)

**검증**: Claude Code 없이 `awf analyze --deep` 단독 실행

### Phase 3: WF + Permissions (2주)
- [x] `awf wf init/next/status/reset`
- [x] Gate evaluator (agent-cards 기반)
- [x] Permissions 모델 (crush 차용) — allowed/disabled/yolo
- [x] Provider fallback chain
- [ ] SQLite 세션 관리 (opencode 차용)

**검증**: `awf wf '로그인 기능'` → 7-phase 파이프라인 실행, gate 통과

### Phase 4: 멀티프로바이더 + UX (2주)
- [~] OpenAI adapter, Codex adapter
- [~] MCP client (stdio/http/sse — crush 차용)
- [~] Skills discovery (crush 차용)
- [~] large-scale Stage 2 fan-out (threshold 기반 opt-in/자동 분기)
- [x] `--non-interactive` 모드 (CI/CD)
- [~] `#precise`/`#cross`/`#critical` 모드

**검증**: `awf wf next --mode critical --dry-run`, `awf analyze --mode cross --dry-run`, `awf mcp check`, `awf mcp invoke`, `awf mcp read`

### Phase 5: 극단 UX (이후)
- [~] `awf "해줘"` 자연어 라우팅
- [~] Interactive REPL
- [~] 토큰 사용량 + 비용 리포트
- [ ] VS Code Extension (subprocess)

진행 표기:
- `[x]` 완료
- `[~]` 부분 구현 또는 핵심 골격 구현
- `[ ]` 미착수

현재 구현 메모:
- Phase 2는 `claude-sdk`, tool system, `.analysis-state.json`, Stage 1/2/3 상태 전이, bundle 저장/configHash invalidation, resume/retry, deep mode live Stage 3까지 반영되어 완료 상태로 본다
- Phase 3의 workflow는 `init`, `reset`, `status`, `next`, `apply-result`, gate/state 갱신, permissions, fallback chain까지 반영됐고 SQLite만 미구현이다
- Phase 4 전반부는 `codex` 운영 경로, `openai` skeleton, provider timeout/progress UX, `analyze --mode precise|cross`, `wf next --mode critical`, `--non-interactive`, `skills list`, `mcp list`, `mcp check`, `mcp invoke`, `mcp read` 최소 경로까지 반영되어 완료로 본다
- Phase 4 전반부 마감 기준은 "mode UX + non-interactive + skills discovery + MCP check/invoke/read + MCP provider integration"까지 확보된 상태다
- Phase 4는 현재 전반부 완료를 넘어 후반부 핵심도 상당수 반영된 상태이며, practical하게는 "대부분 완료"로 본다
- 따라서 현재 문서 기준으로는 "Phase 4 closeout, Phase 5 checkpoint active" 상태로 보는 것이 가장 정확하다
- Phase 4 후반부에서는 deep/large 조건의 Stage 2 fan-out이 scaffold-only가 아니라 실제 실행 경로까지 들어갔다
- 구조 정리로 `core/analysis_prompt.py`와 `core/judge.py`가 추가되어 prompt 조립과 judge 규칙이 `analyze.py`/`wf.py`에서 분리됐다
- 추가 구조 정리로 `core/analysis_fanout.py`가 분리되어 fan-out decision/execution/consistency/fallback이 `analyze.py`에서 분리됐다
- `mcp check`는 stdio transport에 한해 실제 MCP `initialize` handshake와 optional `tools/list`/`resources/list`까지 수행한다
- `mcp invoke`는 stdio transport에 한해 실제 MCP `tools/call`을 수행한다
- `mcp read`는 stdio transport에 한해 실제 MCP `resources/read`를 수행한다
- `http`는 `initialize` POST 요청과 JSON-RPC `tools/call`, `resources/read`까지 지원한다
- `sse`는 event-stream 연결 확인까지만 지원한다
- `claude-sdk`와 `openai`의 Python tool loop는 `mcp_call_tool`, `mcp_read_resource`를 통해 configured MCP server를 직접 호출할 수 있다
- provider MCP tool 호출은 explicit `server`를 우선 사용하고, 없으면 `[mcp_defaults]`의 `invoke`/`read`/`default` 순으로 기본 서버를 해석한다
- MCP-backed tool guidance는 "prompt/repo 우선, 외부 참조가 필요할 때만 MCP 사용" 원칙을 따른다
- 안정적인 reference는 `mcp_read_resource`, 능동 조회/계산은 `mcp_call_tool`을 우선 사용한다
- MCP fixture와 수동 검증 흐름은 [cmux-agent quickstart](../manuals/cmux-agent-quickstart.md)와 CLI fixture runner를 함께 참조한다
- 아직 sse 상호작용은 하지 않는다
- `critical`의 최소 2-provider judge와 `cross`의 Stage 2 secondary check까지 들어갔고, large-scale Stage 2 fan-out은 synthesizer 선행 + writer 병렬 실행 + post-writer consistency pass + single-agent fallback 수준까지 반영되었다
- synthesis는 아직 full weighted merge는 아니지만 deterministic selection 규칙을 가진다:
  - review: PASS 결과끼리는 coverage가 더 높은 쪽 우선
  - verify: PASS 결과끼리는 compliance percentage가 더 높은 쪽 우선
  - cross: required output set이 모두 complete면 extra output이 더 적은 쪽 우선
- synthesis 결과는 stdout뿐 아니라 workflow state/history, analysis state, workflow report에 selection basis까지 남긴다
- Phase 4 잔여 항목은 주로 고도화 영역이다:
  - full MCP transport abstraction
  - weighted synthesis / merge
  - stronger fan-out semantic scoring

운영 경로 매핑:
- Claude Code `/analysis` = 기본 운영 경로 (도메인 규모 무관)
- `awf analyze` = 보조 운영 경로 (`small + standard` 권장)
- `awf analyze --dry-run` = prompt/bundle 준비 도구 (대규모 도메인 포함)

small domain 임시 기준:
- `layers.bundle.fileCount <= 30`
- 이 기준을 넘으면 `awf analyze`보다 Claude Code `/analysis` 또는 `--dry-run` 조합을 우선 권장한다

Phase 5 진입 기준:
- Phase 4의 mode UX, MCP 최소 사용 경로, synthesis, fan-out이 모두 작동하고 회귀 검증이 있는 상태
- 남은 Phase 4 항목이 운영 필수 기능보다 고도화 성격이 강한 상태

Phase 5 첫 범위 권장:
- SQLite session scaffold
- interactive REPL 최소 루프
- session resume / compaction scaffold
- 자연어 task routing / richer compaction은 그 이후 단계적 확장

현재 Phase 5 핵심 구현 상태:
- `awf chat`은 SQLite session 저장, 최소 REPL, `--session-id`, `--latest`, `--list-sessions`, `--show-session`, `--show-latest`까지 지원한다
- `--compact-session`, `--compact-latest`는 이전 턴 일부를 summary system message로 접을 수 있고, 가능하면 provider-assisted summary를 사용한다
- chat turn 시작 시 message-count threshold를 넘는 세션은 auto-compaction으로 먼저 접어 prompt 폭발을 막는다
- provider-assisted summary가 실패하면 기존 heuristic truncation summary로 fallback한다
- chat turn 결과와 session 조회에는 estimated input/output token, session 누적 token, estimated session cost가 포함된다
- `awf ready`는 repo별 config/provider/skill/heuristic scan/workflow/operations 상태를 read-only로 합쳐 automation level과 다음 안전 명령을 출력한다. 처음 쓰는 repo에서는 `doctor`, `scan`, `skills list`를 따로 추측하기 전에 이 명령을 먼저 본다
- `awf ready --gate inspect|analysis|workflow-init|workflow-run|operations --json`은 Claude/Codex entrypoint용 deterministic preflight다. JSON에 `decision: allow|dry_run_only|block`을 포함하고, `allow` 외 decision은 non-zero exit로 provider 호출이나 workflow 진행을 막는다
- `awf analyze`, `awf wf init`, `awf wf next`, `awf wiki decision`, `awf wiki regenerate-index`, `awf wiki compile`은 내부에서도 해당 ready gate를 기본 실행한다. 조회성 경로(`--dry-run`, `--check`, `--catalog`, `status`, `log`, `events`, `lint`)는 gate로 막지 않으며, 명시적 escape hatch는 `--no-ready-gate`다
- `awf doctor`는 provider readiness를 installed/configured 수준으로 점검하고, `--probe`로 subprocess provider 접근성, `--ci`로 default provider readiness에 대한 CI/CD exit code 게이트를 제공한다
- `awf doctor --json`은 `pi_readiness`로 Pi command/path/version, auth env 존재 여부, opt-in surface, Anthropic Extra Usage 과금 주의를 provider 호출 없이 노출한다
- provider 실행 실패 시에는 `hint: run awf doctor`를 통해 readiness 진단 경로를 안내한다
- top-level 자연어 라우팅은 안전한 조회 의도(`wf status`/`config show`/`skills list`/`mcp list`/session list-show)를 직접 보낸다
- 명시적인 `service + domain + 분석` 의도와 `review`/`verify` 의도는 기본적으로 `analyze --dry-run`, `wf next --phase ... --dry-run`으로 보낸다
- known alias와 `analysis-docs/_templates/analysis-config.json` catalog를 사용해 service가 생략된 analyze 요청도 기본 service로 추론할 수 있다
- 일부 analyze/service/domain 오타는 fuzzy alias matching으로 보수적으로 보정한다
- 입력에 `실행` 또는 `run`이 포함되면 같은 intent를 실제 `analyze`, `wf next --phase ...` 실행으로 승격한다
- `provider 상태 확인해줘`, `환경 진단`, `provider 상태 probe 확인해줘` 같은 readiness intent는 `awf doctor` / `awf doctor --probe`로 직접 라우팅한다
- 실제 실행으로 승격된 자연어 라우팅은 interactive TTY에서 실행 전 확인을 한 번 더 요구하고, 간단한 위험도(`low|medium|high`)도 함께 표시한다. non-interactive/non-TTY에서는 확인을 생략한다
- 그 외 자연어 입력은 기본적으로 `chat --message` fallback으로 보낸다
- 현재 token/cost report는 provider-native usage가 있으면 그 값을 우선 사용하고, 없으면 heuristic estimate(`len(text) // 4`)와 optional provider pricing 설정으로 fallback한다
- 아직 자연어 라우팅은 full task planning이나 provider-assisted intent parsing까지는 하지 않는다
- compaction은 provider-assisted summary까지 들어갔지만, summary 품질은 아직 richer summarization policy나 model-tuned rubric까지는 가지 않았다

Phase 5 checkpoint 이후 다음 우선순위:
- VS Code Extension: 마지막 독립 deliverable

---

## 6. 차용 출처 요약

| 항목 | 출처 | 차용 이유 |
|------|------|----------|
| Agent processor loop | opencode | chat 세션의 tool call 반복 실행 모델 (wf/analysis에는 적용하지 않음) |
| Agent 정의 구조 | opencode | name/mode/permission/model 구조를 router+worker+session으로 확장 |
| Install UX | opencode | 다중 설치 경로 (uv/pip/brew) |
| SQLite 세션 | opencode | chat 이력/토큰 추적/재개용 (wf/analysis 실행 상태는 artifact 파일) |
| Compaction | opencode | chat 장기 세션 컨텍스트 관리 (chat 전용) |
| Config 3-level merge | crush | 우선순위가 명확하고 실전 검증됨 |
| MCP 3-transport | crush | stdio/http/sse 추상화가 깔끔 |
| Permissions 모델 | crush | allowed/disabled/yolo 3단계가 직관적 |
| Skills discovery | crush | 다중 경로 탐색 + frontmatter 형식 |
| Provider registry | crush | layered (builtin + custom) 구조 |
| .workflow 계약 | ai-workflow-tools | 기존 호환성 필수 |
| agent-cards/gate | ai-workflow-tools | 결정론적 검증 시스템 |
| /analysis pipeline | ai-workflow-tools | 4-Layer 3-Stage 도메인 로직 |
| Multi-Agent Protocol | ai-workflow-tools | #precise/#cross/#critical |
| State persistence | ai-workflow-tools | state.json 호환성 |

---

## 7. 검증 계획

각 Phase 완료 시:
1. `awf analyze sample-api quest-challenge` → `.ai-context/` 4개 파일 확인
2. 기존 `/analysis`와 동일한 `.analysis-state.json` 생성 확인 (호환성)
3. `.awf.toml` 없는 프로젝트에서도 default config로 동작 확인
4. `awf wf status` → 기존 `.workflow/state.json` 읽기 확인 (Claude Code와 호환)
5. Provider fallback: primary 실패 → secondary 자동 전환 확인

## 8. 극단적 UX 달성 경로

```
Phase 1: awf analyze sample-api quest-challenge
Phase 2: awf analyze quest-challenge     (service 자동 감지)
Phase 3: awf wf '기능 설명'
Phase 4: awf analyze --mode cross quest-challenge
Phase 5: awf "quest 분석해줘"            (자연어)
극단:    awf "해줘"                       (컨텍스트 기반 추론)
```
