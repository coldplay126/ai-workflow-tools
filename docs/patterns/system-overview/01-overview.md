# 핵심 설계 원칙

4가지 핵심 설계 원칙. 특정 AI 도구나 프레임워크에 종속되지 않는 일반 패턴.

---

## 원칙 요약

| 원칙 | 핵심 질문 | 위반 시 증상 |
|------|----------|------------|
| Spec-as-Truth | 동작을 바꾸려면 어디를 수정하는가? | 코드를 수정해야 프롬프트가 바뀜 |
| State Externalization | 세션이 끊기면 작업을 이어갈 수 있는가? | LLM 컨텍스트 소실로 재시작 필요 |
| Tool Neutrality | 다른 AI 도구로 교체할 수 있는가? | 특정 도구의 전용 문법에 종속 |
| Provider Pluggability | 더 저렴한 모델로 위임할 수 있는가? | 모든 작업이 단일 모델에 고정 |

---

## 1. Spec-as-Truth (명세가 진실 공급원)

**명세 파일이 시스템 동작의 유일한 진실 공급원이다.**
코드는 오케스트레이션만 담당하고, AI의 동작을 결정하는 것은 명세 파일이다.

### Spec과 Code의 경계

- 명세(spec): 프롬프트, 프로토콜, agent card — AI가 **무엇을** 하는지 정의
- 코드(code): loader, orchestrator, runner — **어떻게** 실행하는지 정의
- 명세를 편집하면 코드 수정 없이 동작이 바뀐다

### 변경 유형별 수정 대상

| 변경 유형 | 수정 대상 |
|----------|----------|
| 오케스트레이션 로직 (Phase 순서, 재시도) | 코드 |
| AI 프롬프트 내용/형식 | 명세 (prompts/) |
| Gate 조건, artifact 목록 | 계약 (agent-cards/) |

### 위반 신호

- 프롬프트가 코드 파일에 하드코딩
- 동작 변경에 코드 배포 필요
- 프롬프트 수정 시 파싱 로직도 함께 수정 필요

---

## 2. State Externalization (상태 외부화)

**모든 실행 상태와 단계 간 전달 데이터는 파일 시스템에 외부화한다.**
LLM 대화 이력이나 메모리에 상태를 보관하지 않는다.

### 외부화가 가능하게 하는 것

| 능력 | LLM 컨텍스트 의존 시 |
|------|------------------|
| Provider 전환 | 불가능 (대화 이력 소실) |
| 세션 재개 | 불가능 (컨텍스트 리셋) |
| 감사 추적 | 제한적 |
| 병렬 실행 | 불가능 (세션 격리) |

### 상태 전이 원자성

상태 파일 쓰기는 원자적 rename 패턴을 사용한다.
임시 파일에 먼저 쓰고, 완료 후 원본을 교체한다.

### 위반 신호

- Phase 결과를 LLM 대화 이력에서 참조
- 세션을 끊으면 작업을 이어갈 수 없음
- 상태 파일 없이 파이프라인 실행

---

## 3. Tool Neutrality (도구 중립)

**같은 명세가 어떤 AI CLI 도구에서든 동작한다.**
특정 도구의 전용 문법, API, 호출 규약에 종속되지 않는다.

### 중립성 계층

- 중립: 명세, 상태, 계약 — 도구 전용 문법 없음
- 흡수: Provider 계층이 도구별 차이를 흡수

### 달성 전략

1. 프롬프트에 도구 전용 문법을 사용하지 않는다
2. 상태 형식은 JSON/markdown만 사용한다
3. 도구별 차이는 Provider 계층에서 흡수한다
4. 새 도구 지원은 Provider 구현 추가만으로 가능하다

### 위반 신호

- 프롬프트에 특정 도구의 전용 태그 포함
- 특정 도구에서만 파이프라인 실행 가능
- 새 도구 지원 시 프롬프트 전면 재작성 필요

---

## 4. Provider Pluggability (Provider 교체 가능)

**Provider Protocol 추상화를 통해 AI 모델을 작업 특성에 따라 동적 선택한다.**

### 핵심 구조

- Protocol: 모든 Provider가 구현하는 인터페이스
- Registry: 이름 기반 Provider 조회
- Capability: Provider가 노출하는 기능 집합

### 비용 인식 라우팅

작업 특성에 따라 저/중/고비용 Provider를 자동 라우팅한다.
새 Provider 추가 시 파이프라인 코드 수정이 불필요하다.

### 위반 신호

- 모든 작업이 단일 Provider에 고정
- Provider 교체 시 오케스트레이션 코드 수정 필요
- 새 Provider 추가 시 기존 파이프라인 수정 필요

---

## 원칙 간 상호작용

4개 원칙은 상호 강화한다.

- Spec-as-Truth → Tool Neutrality: 명세를 markdown으로 작성하면 도구 중립 달성
- State Externalization → Tool Neutrality: 상태를 파일로 외부화하면 어떤 도구든 읽을 수 있음
- Tool Neutrality → Provider Pluggability: 도구에 독립적이면 Provider를 자유롭게 교체
- Provider Pluggability → Spec-as-Truth: Provider가 바뀌어도 명세는 유지

### 충돌 시 우선순위

1. **State Externalization** — 상태 소실 시 다른 원칙이 무의미
2. **Spec-as-Truth** — 명세 훼손 시 동작 제어 불가
3. **Tool Neutrality** — 도구 종속은 장기 유지보수 저해
4. **Provider Pluggability** — 비용 최적화는 기능 정확성 이후
