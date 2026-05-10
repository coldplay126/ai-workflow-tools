# System Overview Reference

`docs/patterns/system-overview/`에서 분리된 구현 세부와 운영값.

---

## 1. 상태 저장 경로

| 영역 | 경로 | 용도 |
|------|------|------|
| Workflow 상태 | `.workflow/state.json` | Phase 진행, Gate 결과 |
| Workflow 산출물 | `.workflow/artifacts/` | spec, plan, review-report 등 |
| Workflow 계약 | `.workflow/agent-cards/` | Phase별 런타임 계약 |
| Analysis 상태 | `.analysis-state.json` | Stage 진행 상태 |
| Analysis 산출물 | `.ai-context/` | 분석 산출물 4파일 |
| Analysis 임시 | `.tmp/` | Stage 1 XML, hashes |
| 작업 이력 | `.work_history/` | 누적 이력 |
| 프로젝트 지식 | `project-context.md` | 도메인 지식 |

`.workflow/`가 target repo의 `.gitignore`에 포함되어 있으면 workflow state는
local-only 운영 상태로 취급한다. `awf ready`는 이 경우 warning을 표시하지만,
workflow 실행 자체를 막지는 않는다.

---

## 2. Config 설정 키

| 영역 | 주요 키 |
|------|--------|
| Provider | `provider.default`, `provider.aliases` |
| Paths | `paths.skills_dir`, `paths.workflow_dir` |
| Analysis | `analysis.fanout_large_file_threshold`, `analysis.stage_routing` |
| Permissions | `permissions.default_policy`, `permissions.disabled_tools` |

### Config 파일 경로

| 계층 | 경로 |
|------|------|
| System | 코드 내 defaults |
| User | `~/.config/awf/config.toml` |
| Project | `{repo_root}/.awf.toml` |

## 2.1 Readiness / scan 구현값

| 항목 | 값 |
|------|----|
| Python project markers | `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements.txt`, `Pipfile`, `poetry.lock` |
| deterministic unit patterns | `src/domains/{unit}`, `src/domain/{unit}`, `src/modules/{unit}`, `src/features/{unit}`, `src/{unit}`, root-level `{unit}` |
| root-level source unit 예시 | `collectors`, `analyzers`, `importers`, `exporters`, `monitors`, `matchers` |
| dry-run JSON | `awf analyze ... --dry-run --output-format json`, `awf wf next ... --dry-run --output-format json` |

---

## 3. Provider 구현체

| 유형 | 설명 | Capability |
|------|------|-----------|
| SubprocessProvider | 외부 CLI를 subprocess로 실행 | COMPLETE |
| SDKProvider | API SDK 직접 호출 + Tool Loop | COMPLETE, TOOL_LOOP, EVENT_STREAM |
| FixtureProvider | 테스트용 고정 응답 | COMPLETE |

### Provider 실제 이름

| 이름 | 유형 |
|------|------|
| `claude-code` | SubprocessProvider |
| `claude-sdk` | SDKProvider |
| `claude:sonnet` | SubprocessProvider (alias) |
| `codex` | SubprocessProvider |
| `openai` | SubprocessProvider |
| `fixture` | FixtureProvider |

---

## 4. Skill 탐색 경로

| 우선순위 | 경로 |
|---------|------|
| 1 | `~/.claude/skills/` |
| 2 | `./claude/skills/` |

---

## 5. EventType 목록

### 카테고리

| 카테고리 | 이벤트 |
|---------|--------|
| Lifecycle | PIPELINE_STARTED, PIPELINE_COMPLETED, PIPELINE_FAILED |
| Pipeline | STAGE_STARTED, STAGE_COMPLETED, PHASE_STARTED, PHASE_COMPLETED |
| Worker | WORKER_STARTED, WORKER_COMPLETED, WORKER_FAILED |
| Artifact | ARTIFACT_CREATED |
| Provider | PROVIDER_SELECTED |
| Decision | GATE_EVALUATED, DECISION_MADE |
| Multi-Agent | MULTI_AGENT_STARTED, MULTI_AGENT_COMPLETED |
| Infra | HEARTBEAT |

### Heartbeat

| 항목 | 값 |
|------|------|
| 주기 | 15초 |
| 용도 | 장시간 실행 감지 |

---

## 6. Permission 기본 정책

### 검사 순서

1. `yolo` 모드 → 전체 허용
2. `disabled_tools` → 명시적 차단
3. `allowed_tools` → 명시적 허용

### Provider/Tool 이름 매핑

| 대상 | 이름 형식 |
|------|----------|
| Provider | `provider:{name}` (e.g. `provider:claude-code`) |
| Tool | `tool:{name}` (e.g. `tool:file_write`) |
| Alias | `claude:sonnet` → `claude-code` + sonnet flags |

---

## 7. Stage/Phase별 Provider 라우팅 예시

### Analysis Pipeline

| Scale | Stage 1 | Stage 2 | Stage 3 |
|-------|---------|---------|---------|
| small | 저비용 | 중비용 | skip |
| standard | 저비용 | 중비용 | 고비용 |
| large | 저비용 | 고비용 | 고비용 |

### Workflow Pipeline

| Phase | Provider 등급 | 이유 |
|-------|-------------|------|
| plan | 고비용 | 설계 판단 |
| review | 고비용 | 교차 검증 |
| impl | 중비용 | 코드 작성 |
| verify | 고비용 | 범위 검증 |
| test | 중비용 | 테스트 분석 |
