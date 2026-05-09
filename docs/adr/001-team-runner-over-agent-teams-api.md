# ADR-001: 3레이어 에이전트 팀 아키텍처

## Status

**Accepted** (2026-04-10)

## Context

WF-005/MA-002 에이전트 팀 구현이 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` API 안정화를 기다리며 보류 상태였다.

### 문제

1. **API 의존**: Agent Teams는 Claude Code의 실험 기능으로, 변경/제거 가능성이 있음
2. **프로세스 모델 차이**: Agent Teams는 teammate를 **별도 프로세스**로 spawn하여 공유 파일시스템에 동시 접근 → state corruption 위험
3. **제어 한계**: Agent Teams의 턴 관리, 종료 조건 평가가 Claude Code 내부에서 발생하여 awf이 직접 제어 불가
4. **결정론성**: LLM은 비결정론적이므로 Gate 평가, 상태 전이, 종료 조건 같은 흐름 제어를 LLM에 맡기면 100% 보장이 안 됨

### 기존 인프라 현황

awf의 `multi_agent.py`에 이미 팀 패턴의 빌딩 블록이 존재한다:

| 요소 | 기존 구현 |
|------|----------|
| 병렬 실행 | `_run_cross()` — `ThreadPoolExecutor` |
| 순차 체인 | `_run_critical()` — step 결과를 다음 step에 전달 |
| Provider 추상화 | `ProviderRegistry` — claude, codex, sonnet 교체 가능 |
| 프로토콜 로딩 | `protocols/*.md` 동적 로딩 |
| 결과 판정 | `judge()` — severity 기반 결정론적 규칙 |
| 결과 파싱 | `AgentResult.parsed`, `.findings`, `.has_critical` |
| 장애 대응 | `_ESCALATION_FALLBACK` 체인 |

## Decision

3레이어 아키텍처를 채택한다. 각 레이어는 자기가 잘하는 것만 담당한다.

### 3레이어 구조

```
┌─ Layer 1: Python (TeamRunner) ─ 항상 떠있음 ─────────┐
│  결정론적 흐름 제어                                     │
│  - 턴 순서, 종료 조건, Gate 평가, 상태 저장, 타임아웃   │
│  - LLM이 빠뜨릴 수 있는 것을 코드로 100% 보장          │
│                                                        │
│  ┌─ Layer 2: Leader (Claude Code / Codex) ──────────┐  │
│  │  AI 판단                                          │  │
│  │  - 업무 분석, 워커 배정, 피드백 내용, Stop/Go 결정 │  │
│  │  - Provider 가용성에 따라 선정                     │  │
│  └───────────────────────────────────────────────────┘  │
│                                                        │
│  ┌─ Layer 3: Workers ────────────────────────────────┐  │
│  │  자율 작업 (AI 강점 극대화)                        │  │
│  │                                                   │  │
│  │  Claude subagent        Codex MCP                 │  │
│  │  - 코드 탐색, 생성, 수정  - 정밀 분석, 검증        │  │
│  │  - HOW는 스스로 결정      - read-only, structured  │  │
│  │                                                   │  │
│  │  워커 간 통신: 파일 기반 Blackboard                │  │
│  │  - board/: 공유 artifact                          │  │
│  │  - discussion/: 턴별 대화 (질문, 답변, findings)   │  │
│  └───────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────┘
```

### 레이어별 책임

| 레이어 | 책임 | 왜 이 레이어인가 |
|--------|------|-----------------|
| **Python** | 턴 순서, 종료 조건, Gate, 상태 저장 | 결정론적 — 코드는 빠뜨리지 않음 |
| **Leader** | 업무 분석, 워커 배정, Stop/Go | AI 판단 — 맥락 이해, 유연한 결정 |
| **Worker** | 탐색, 생성, 검증, 워커 간 대화 | AI 강점 — 깊이 있는 자율 작업 |

### 워커 간 통신: Blackboard + Discussion

```
.workflow/team/{phase}/
├── board/                    ← 공유 artifact (spec.md, code diff 등)
└── discussion/               ← 워커 간 대화
    ├── turn-1-writer.md      ← Writer 질문/설명
    ├── turn-1-reviewer.json  ← Reviewer 답변/findings
    └── turn-2-writer.md      ← Writer 응답/수정 설명
```

- Worker A가 `board/`에 artifact를 쓰고, `discussion/`에 질문/설명을 남김
- Worker B가 `board/`와 `discussion/`을 읽고, 자신의 결과/답변을 씀
- Python TeamRunner는 **턴 순서만 제어** — 내용은 워커가 자율적으로 결정

### 전 Phase 동일 패턴

plan, impl, test 모두 같은 루프를 탄다:

```
Python: Leader에게 phase 정보 전달
Leader: 업무 분석 → 워커 배정
  └─ 반복:
       Python: 워커 실행 (run_agent / mcp__codex__codex)
       Worker: 자율 작업 + board/discussion 활용
       Python: 종료 조건 평가
       Leader: Stop/Go 판단
Python: Gate 평가 → 다음 phase
```

### 프로세스/스레드 안전성

| 구성 요소 | 동시성 모델 | 잠금 방식 |
|-----------|-----------|----------|
| Turn 내 병렬 역할 (test phase) | `ThreadPoolExecutor` | `threading.Lock` |
| 자식 프로세스 (Claude/Codex CLI) | `subprocess.Popen` | 상태 파일 미접근 |
| 상태 파일 갱신 | 메인 프로세스 스레드 | `threading.Lock` + atomic write |

`filelock`/`fcntl` 불필요. 기존 `threading.Lock` + atomic write로 충분. ([State Corruption 분석 계기](https://github.com/kimchanhyung98/agentic-workflows/pull/28/files#r3058914494))

## Consequences

### Positive

- **즉시 구현 가능**: 외부 API 안정화 대기 불필요
- **결정론적 제어**: Gate, 종료 조건, 상태 전이를 Python이 보장
- **워커 자율성**: HOW는 워커가 결정, 프로토콜은 목표+도구+범위만 정의
- **Provider 자유도**: Leader, 워커 모두 가용성 기반으로 Claude/Codex/Sonnet 배치
- **워커 간 통신**: Blackboard 파일로 간접 대화, 별도 IPC 불필요
- **State corruption 안전**: 단일 프로세스 → `threading.Lock` 충분

### Negative

- **턴 기반 대화**: 워커 간 실시간 대화 불가, 턴 단위로 파일 교환
- **컨텍스트 비누적**: 각 `run_agent()`가 one-shot이므로 이전 턴 context를 프롬프트 또는 파일로 전달
- **프롬프트 증가**: discussion/ 파일이 커지면 워커 프롬프트도 증가

### 마이그레이션 경로

Agent Teams API가 안정화되면 Layer 3(Worker) 내부 구현만 교체 가능. Layer 1(Python 흐름 제어)과 Layer 2(Leader 판단)는 변경 없음.

## References

- [Team Blackboard Pattern](../patterns/multi-agent/04-team-blackboard.md) — agent team worker coordination pattern
- [State Corruption 분석](https://github.com/kimchanhyung98/agentic-workflows/pull/28) — threading.Lock vs filelock 검토 계기
- `cli/src/awf/core/multi_agent.py` — 기존 5-mode 멀티에이전트
- `cli/src/awf/core/agent_runner.py` — `run_agent()`, `AgentResult`
