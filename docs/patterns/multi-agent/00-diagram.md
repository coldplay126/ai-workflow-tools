# Multi-Agent Orchestration Diagrams

---

## 1. 5-Mode 실행 의사결정

입력된 작업의 정책, 위험도, 명시적 지정에 따라 실행 모드를 결정한다.

```mermaid
flowchart LR
    START(["작업 입력"]) --> EXPLICIT{"명시적 모드 지정?"}
    EXPLICIT -->|Yes| USE_MODE["지정된 모드 사용"]
    EXPLICIT -->|No| POLICY["Policy 기반 모드 선택"]
    POLICY --> EXEC(["모드 실행"])
    USE_MODE --> EXEC
```

모드 선택에 사용되는 키워드 목록과 매핑은 reference 문서를 참조한다.

---

## 2. Cross Mode — 병렬 실행 흐름

두 에이전트가 독립적으로 병렬 분석하고, Judge가 결과를 종합 판정한다.

```mermaid
graph TB
    PROMPT["프롬프트"]
    subgraph parallel["병렬 실행"]
        direction LR
        AGENT_A["Agent A"]
        AGENT_B["Agent B"]
    end
    PROMPT --> AGENT_A
    PROMPT --> AGENT_B
    AGENT_A --> COLLECT["결과 수집 + 정렬"]
    AGENT_B --> COLLECT
    COLLECT --> DEDUP["Finding 중복 제거"]
    DEDUP --> JUDGE{"Judge Rules"}
    JUDGE -->|PASS| PASS_RESULT["PASS"]
    JUDGE -->|FAIL| FAIL_RESULT["FAIL + 피드백"]

    style parallel fill:#fff3bf,stroke:#fcc419
    style PASS_RESULT fill:#69db7c,color:#fff
    style FAIL_RESULT fill:#ff6b6b,color:#fff
```

---

## 3. Critical Mode — 순차 체인 흐름

각 단계의 결과가 다음 단계의 입력에 누적되는 순차 실행.

```mermaid
graph TB
    PROMPT["프롬프트"]
    PROMPT --> STEP1["Step 1: Agent A"]
    STEP1 -->|result_A| STEP2["Step 2: Agent B\n(prompt + result_A)"]
    STEP2 -->|result_B| STEP3["Step 3: Primary\n(prompt + result_A + result_B)"]
    STEP3 --> JUDGE{"Judge Rules"}
    JUDGE -->|PASS| DONE["PASS"]
    JUDGE -->|FAIL| RETRY["FAIL + 강등/재시도"]
```

---

## 4. 자동 승격/강등 상태

Policy 기반으로 승격(escalation)하고, 에이전트 실패 시 강등(de-escalation)한다.

```mermaid
stateDiagram-v2
    [*] --> solo: 기본 모드

    solo --> cross: 보안 관련 policy 트리거
    solo --> critical: 프로덕션 관련 policy 트리거
    cross --> critical: 추가 위험 요소 감지

    state "Fallback Chain" as fallback {
        critical --> cross: Agent 실패
        cross --> precise: 병렬 Agent 실패
        precise --> solo: 보조 Agent 실패
    }

    solo --> [*]: 작업 완료
```

승격/강등 키워드 목록 및 구체 경로는 reference 문서를 참조한다.

---

## 5. 전체 생명주기

```mermaid
graph TB
    INPUT["작업 입력"] --> MODE["모드 선택\n(policy/명시적)"]
    MODE --> ROUTE["Provider 라우팅\n(Registry + Protocol)"]
    ROUTE --> EXEC["에이전트 실행"]
    EXEC --> JUDGE["Judge Rules"]
    JUDGE -->|PASS| DONE["완료"]
    JUDGE -->|FAIL 재시도| EXEC
    JUDGE -->|FAIL 강등| MODE

    style DONE fill:#69db7c,color:#fff
```

---

## 6. Team Blackboard — 3레이어 팀 패턴

`pattern: "team"` 사용 시의 실행 흐름. 상세는 [04-team-blackboard.md](04-team-blackboard.md) 참조.

```mermaid
graph TB
    INPUT["작업 입력"] --> PATTERN{"pattern?"}
    PATTERN -->|subagent| MODE["기존 5-Mode"]
    PATTERN -->|team| TEAM["Team Blackboard"]

    subgraph TEAM_FLOW["Team Blackboard"]
        LEADER["Leader\n업무 분석 + 워커 배정"]
        LEADER --> WORKSPACE["Workspace 생성\nboard/ + discussion/"]
        WORKSPACE --> TURN["Turn Loop"]

        subgraph TURN["Python 턴 제어"]
            WORKER_A["Worker A\n자율 작업"]
            WORKER_B["Worker B\n자율 작업"]
            WORKER_A --> BB["board/ + discussion/\n(Blackboard)"]
            WORKER_B --> BB
            BB --> TERM{"종료 조건?"}
            TERM -->|No| STOP{"Leader Stop/Go"}
            STOP -->|Go| WORKER_A
            TERM -->|Yes| GATE
        end

        GATE["Gate 평가\n(Python, 결정론적)"]
    end

    GATE --> DONE["완료"]
    style DONE fill:#69db7c,color:#fff
```
