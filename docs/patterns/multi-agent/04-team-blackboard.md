# Team Blackboard Pattern

3레이어 에이전트 팀 패턴. 워커가 공유 작업 공간(Blackboard)을 통해 간접 통신하며, Python이 흐름을 제어하고, Leader가 AI 판단을 담당한다.

---

## 1. 3레이어 구조

```
┌─ Layer 1: Python (TeamRunner) ──────────────────┐
│  결정론적 흐름 제어                                │
│  - 턴 순서, 종료 조건, Gate, 상태 저장, 타임아웃   │
│                                                   │
│  ┌─ Layer 2: Leader ─────────────────────────┐    │
│  │  AI 판단                                   │    │
│  │  - 업무 분석, 워커 배정, Stop/Go            │    │
│  └────────────────────────────────────────────┘    │
│                                                   │
│  ┌─ Layer 3: Workers ────────────────────────┐    │
│  │  자율 작업 + Blackboard 통신               │    │
│  │  Claude ←→ board/ + discussion/ ←→ Codex  │    │
│  └────────────────────────────────────────────┘    │
└───────────────────────────────────────────────────┘
```

### 레이어 분리 원칙

| 레이어 | 담당 | 이유 |
|--------|------|------|
| Python | 턴 순서, 종료 조건, Gate, 상태 | LLM은 비결정론적. 코드만 100% 보장 |
| Leader | 업무 분석, 워커 배정, Stop/Go | AI가 맥락 기반 판단에 강함 |
| Worker | 탐색, 생성, 검증, 대화 | AI가 코드 이해와 창의적 작업에 강함 |

**경계 규칙**: Gate 평가를 Leader에 맡기지 않는다. 상태 전이를 Worker에 맡기지 않는다. 코드 탐색을 Python에 맡기지 않는다.

---

## 2. Blackboard 통신

### 원리

워커끼리 직접 메시지를 주고받지 않는다. **공유 파일 시스템**을 통해 간접 통신한다.

```
Writer ──► board/spec.md ◄── Reviewer가 읽음
Writer ──► discussion/turn-1-writer.md ◄── Reviewer가 읽음
Reviewer ──► discussion/turn-1-reviewer.json ◄── Writer가 읽음 (다음 턴)
```

### 디렉토리 구조

```
.workflow/team/{phase}/
├── mission.md              ← Leader가 작성 (목표, 팀 구성)
├── board/                  ← 공유 artifact
│   ├── spec.md
│   ├── plan.md
│   └── (phase별 산출물)
└── discussion/             ← 워커 간 대화
    ├── turn-1-{role}.md    ← 질문, 설명, 변경 근거
    ├── turn-1-{role}.json  ← findings, 답변
    ├── turn-2-{role}.md
    └── turn-2-{role}.json
```

### 쓰기 범위 (Write Scope)

각 워커는 정해진 범위에만 쓸 수 있다.

| 워커 | 읽기 | 쓰기 |
|------|------|------|
| Writer (Claude) | board/, discussion/ 전체 | board/\*\*, discussion/turn-N-writer.\* |
| Reviewer (Codex) | board/, discussion/ 전체 | discussion/turn-N-reviewer.\* |

Reviewer가 board/를 직접 수정하지 않으므로 artifact 오염이 없다.

---

## 3. 실행 흐름

### 전 Phase 동일 루프

plan, impl, test 모두 같은 패턴을 탄다.

```
Python: workspace 생성 (.workflow/team/{phase}/)
Python: Leader에게 phase 정보 전달
  │
  Leader: 업무 분석 → mission.md 작성 → 워커 배정
  │
  ├─ Turn 1:
  │   Python: Worker A 실행 (run_agent / mcp__codex__codex)
  │     Worker A: board/에 artifact, discussion/에 질문/설명
  │   Python: Worker B 실행
  │     Worker B: board/ 읽음, discussion/에 findings/답변
  │   Python: 종료 조건 평가 (discussion/ 파싱, 결정론적)
  │   Leader: 결과 확인 → Stop/Go
  │
  ├─ Turn 2:
  │   Worker A: discussion/ 읽고 피드백 반영, board/ 수정
  │   Worker B: 수정본 재검토
  │   Python: 종료 조건 → CRITICAL 0건 → 종료
  │
  Python: Gate 평가
  Python: 상태 저장 → 다음 phase
```

### 순차 vs 병렬

| 실행 방식 | Phase | 이유 |
|-----------|-------|------|
| 순차 | plan, impl | Writer 산출물이 있어야 Reviewer가 검토 |
| 병렬 | test | happy-path와 adversarial이 독립 관점 |

---

## 4. 워커 프로토콜

### 원칙: 목표 + 도구 + 범위. 방법은 자율.

프로토콜은 **WHAT**을 정의하고 **HOW**는 워커에 맡긴다. AI의 탐색/추론/생성 능력을 최대한 활용하기 위함.

### 프로토콜 구성 요소

| 항목 | 정의 여부 | 설명 |
|------|----------|------|
| Mission | ✅ 정의 | 달성할 목표 |
| Workspace | ✅ 정의 | board/, discussion/ 경로, write scope |
| Tools | ✅ 정의 | 사용 가능한 도구 목록 |
| Output Contract | ✅ 정의 | 반환 형식 (JSON schema, 파일 위치) |
| 탐색 순서 | ❌ 자율 | 어떤 파일을 먼저 읽을지 |
| 생성 순서 | ❌ 자율 | 어떤 artifact를 먼저 만들지 |
| 자체 검증 횟수 | ❌ 자율 | mcp__codex__codex를 몇 번 돌릴지 |
| 접근 전략 | ❌ 자율 | 어떤 패턴으로 코드를 분석할지 |

### 예시

```markdown
# spec_writer.md

## Mission
5개 artifact를 생성하라: spec.md, plan.md, tasks.md, test-criteria.md, constitution.md

## Workspace
- Board: .workflow/team/plan/board/ (읽기+쓰기)
- Discussion: .workflow/team/plan/discussion/turn-{N}-writer.md (쓰기)
- 상대 discussion: turn-{N}-reviewer.json (읽기)

## Tools
Read, Glob, Grep, Bash, Write, mcp__codex__codex (선택)

## Output Contract
- board/에 각 artifact 파일 저장
- discussion/에 설계 판단 근거, 질문, 변경 사항 기록
```

---

## 5. Leader의 역할

Leader는 AI다. 업무를 분석하고 워커에게 무엇을 시킬지 판단한다.

### Leader가 하는 일

| 시점 | 판단 |
|------|------|
| Phase 시작 | 업무 분석, 팀 구성, mission.md 작성 |
| 턴 종료 후 | discussion/ 결과 확인, 피드백 내용 결정 |
| Stop/Go | 다음 턴 필요 여부 판단 |

### Leader가 하지 않는 일

| 항목 | 담당 |
|------|------|
| 종료 조건 평가 | Python (결정론적) |
| Gate 실행 | Python (결정론적) |
| 상태 저장 | Python (결정론적) |
| 타임아웃 강제 | Python (결정론적) |

### Leader 선정

```
가용한 Provider 중 선택:
1순위: Claude Code (분석/판단 강점)
2순위: Codex (Claude 불가 시)
```

---

## 6. 기존 패턴과의 관계

| 기존 패턴 | Blackboard에서의 위치 |
|-----------|---------------------|
| 5-Mode (solo/quick/precise/cross/critical) | `pattern: "subagent"` 전용. team에서는 사용하지 않음 |
| Judge Rules | 턴 종료 후 findings 판정에 재사용 가능 |
| Escalation Chain | team 실패 시 subagent fallback에 재사용 |
| Provider Routing | Leader와 Worker 모두에 적용 |

### pattern 필드로 전환

```json
{ "pattern": "subagent", "mode": "cross" }    ← 기존 5-mode
{ "pattern": "team", "team": { ... } }        ← Blackboard 팀
```

`pattern` 필드 absent → `"subagent"` (기본값). 기존 config 호환.

---

## 7. State Corruption 안전성

Blackboard 패턴에서 동시 쓰기가 발생하지 않는 이유:

1. **Python이 턴 순서 제어**: Worker A 완료 → Worker B 시작. 동시 실행 없음 (순차 phase)
2. **Write scope 분리**: Worker별로 쓰기 경로가 다름
3. **병렬 phase (test)**: ThreadPoolExecutor 사용, discussion/ 파일명으로 분리

기존 `threading.Lock` + atomic write로 충분. `filelock` 불필요.

([State Corruption 분석 참조](https://github.com/kimchanhyung98/agentic-workflows/pull/28/files#r3058914494))
