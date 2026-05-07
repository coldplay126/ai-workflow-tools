# 아키텍처 다이어그램

---

## 1. 시스템 전체 구조

3개 파이프라인과 공유 인프라 계층의 관계.

```mermaid
graph TB
    subgraph pipelines["파이프라인"]
        AP["Analysis Pipeline\n코드 분석 → 문서 생성"]
        WP["Workflow Pipeline\n기능 구현 자동화"]
        MA["Multi-Agent Pipeline\n교차 검증"]
    end

    subgraph infra["공유 인프라"]
        PL["Provider Layer"]
        CFG["Config"]
        ST["State"]
        EV["Event System"]
        SK["Skill System"]
        PM["Permission Model"]
    end

    AP --> PL & CFG & ST & EV & SK
    WP --> PL & CFG & ST & EV & SK
    MA --> PL & CFG & EV & SK
    PL --> PM
```

### 파이프라인 역할

| 파이프라인 | 목적 | 핵심 패턴 |
|-----------|------|----------|
| Analysis | 코드 분석 → 구조화된 문서 생성 | N-Layer, M-Stage, Writer/Judge |
| Workflow | 기능 구현의 구조화된 자동화 | N-Phase, Gate, Closed-Loop |
| Multi-Agent | 다중 에이전트 교차 검증 | N-Mode, Judge Rules, Synthesis |

---

## 2. 파이프라인 간 데이터 흐름

```mermaid
graph LR
    AP["Analysis\n결과"] -->|분석 산출물| WP["Workflow"]
    WP -->|검증 요청| MA["Multi-Agent"]
    MA -->|판정 결과| WP
    WP -->|상태| FS["파일 시스템\n(외부화)"]
    AP -->|상태| FS
```

---

## 3. 역할 분리

시스템의 5가지 역할과 책임 경계.

| 역할 | 책임 | 금지 |
|------|------|------|
| Orchestrator | 상태 관리, Phase/Stage 전환, Provider 라우팅 | AI 판단, 파일 직접 수정 |
| Executor | 코드 실행, 파일 수정 (Primary만) | 상태 직접 수정, Gate 우회 |
| Reviewer | 분석, 검증, 관찰 추출 (Secondary) | 파일 수정, 상태 수정 |
| Judge (추상 역할) | 결과 종합, 판정. analysis judge (synthesis), multi-agent judge (verdict) | 새 evidence 생성, 원본 변조 |
| Gate | 통과 조건 평가, 라우팅 결정 | 실행, 판단 |

---

## 4. 상위 상태 전이 모델

모든 파이프라인이 공유하는 실행 상태 전이.

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> in_progress: 실행 시작
    in_progress --> completed: 성공
    in_progress --> failed: 실패
    in_progress --> escaped: Worker escape
    escaped --> deciding: 규칙 평가
    deciding --> in_progress: continue
    deciding --> pending: replan
    deciding --> aborted: abort
    deciding --> escalated: escalate_user
    failed --> in_progress: retry
    failed --> aborted: budget 소진
    completed --> [*]
    aborted --> [*]
    escalated --> [*]
```

---

## 5. Provider 계층

```mermaid
graph TB
    subgraph protocol["Provider Protocol"]
        IFACE["인터페이스\n(capabilities, complete, execute)"]
    end
    subgraph registry["Registry"]
        REG["이름 기반 조회\n(builtin + custom)"]
    end
    protocol --> registry
    registry --> IMPL["구현체"]
```

Provider는 Protocol 추상화 뒤에 위치하며, Registry를 통해 이름으로 조회한다.
구현체 목록 및 capability 비교는 reference 문서를 참조한다.

---

## 6. Skill 탐색

Skill은 우선순위 기반 탐색으로 발견되며, 같은 이름의 skill은 먼저 발견된 것이 우선한다.

탐색 경로 및 캐시 메커니즘은 reference 문서를 참조한다.
