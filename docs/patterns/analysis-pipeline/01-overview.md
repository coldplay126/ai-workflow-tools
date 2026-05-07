# Analysis Pipeline 개요

AI 기반 코드 분석 파이프라인의 핵심 설계 원리.

---

## 4-Layer 아키텍처

파이프라인은 4개의 독립된 Layer로 구성된다. 각 Layer는 명확한 입출력 계약을 가지며, Layer 간 데이터는 파일 또는 구조화된 데이터로 전달된다.

### Layer 1: Input (탐색)

소스코드를 수집하고, 분석 대상의 범위를 결정한다.

- 분석 대상 파일 탐색 및 목록 생성
- 분석 단위(unit) 식별 — 기능 모듈, 패키지, 컴포넌트 등
- 규모(scale) 판정: 파일 수 기준으로 small / standard / large 분류
- 분석 목적(mode) 결정: 문서화, 리뷰, 조사 등

**권한**: 파일 시스템 읽기, 설정 파일 해석
**금지**: LLM 호출, 파일 수정

### Layer 2: Bundle (관찰 수집)

각 파일을 구조화된 번들로 포장하고, AI Provider를 사용하여 관찰(observation)을 추출한다.

- 파일별 번들 생성 (target 파일 + context 파일)
- import 그래프 기반 context 파일 수집
- AI Provider를 통한 관찰 추출 (사실만, 판단 없음)

**권한**: LLM 호출 (사실 추출 전용, 파일별 독립 세션)
**금지**: 심각도 판정, 결론 도출

### Layer 3: Analyze (판단)

observation을 기반으로 판단(judgment)을 수행한다. 규모에 따라 1~3단계의 처리를 거친다.

- Stage 1: 파일별 관찰 데이터 처리 (저비용 Provider)
- Stage 2: 단위 수준 합성 — Writer/Judge 패턴 (중비용 Provider)
- Stage 3: 교차 서비스 검증 (고비용 Provider, 조건부 실행)

**권한**: LLM 호출 (판단), 참조 확장 (규칙 기반)
**금지**: 코드 직접 읽기 (fallback 제외), 파일 수정

### Layer 4: Output (산출물)

종합 판단 결과를 목적에 맞는 최종 산출물로 변환한다.

- 결과 병합 및 산출물 파일 생성
- 프로젝트 수준 지식 갱신 (knowledge accumulation)
- 해시 기록 및 상태 저장

**권한**: 파일 쓰기, 프로젝트 지식 갱신
**금지**: LLM 호출

### Layer별 권한 경계

| Layer | LLM 호출 | 파일 읽기 | 파일 쓰기 | 판단 |
|-------|---------|----------|----------|------|
| Layer 1 (Input) | - | O | - | - |
| Layer 2 (Bundle) | O (사실 추출) | O | - | - |
| Layer 3 (Analyze) | O (판단) | - (fallback 제외) | - | O |
| Layer 4 (Output) | - | - | O | - |

---

## 관찰과 판단의 분리

파이프라인의 가장 중요한 설계 원칙이다.

관찰과 판단이 섞이면 확증 편향이 발생한다. "이슈가 있다"고 먼저 판단하면 그에 맞는 증거만 선택적으로 수집하게 된다.

### 규칙

- Layer 2는 사실만 기록한다. 심각도, 결론, 권장사항을 포함하면 안 된다.
- Layer 3은 Layer 2의 관찰을 기반으로만 판단한다.

### 위반 신호

Layer 2의 observation에 다음이 포함되면 원칙 위반이다:
- 심각도 태그: `[High]`, `[Critical]`, `[Warning]`
- 판단 동사: "~해야 한다", "~가 문제다", "~를 권장한다"
- 결론: "이 패턴은 위험하다"

---

## 번들 구조

코드를 AI Provider에 전달할 때 target 파일과 context 파일을 구조화된 번들로 묶는다.

### 핵심 구조

| 속성 | 설명 |
|------|------|
| `target` | 분석 대상 파일. 코드 전문을 포함 |
| `context` | 참조 파일. import 관계로 연결, 시그니처만 포함 |

- context 파일은 target의 import 문을 파싱하여 자동 수집한다.
- context 파일은 시그니처 모드로 포함: export/class/function 선언만 추출.
- 토큰 사용량을 절감하면서 파일 간 관계를 유지한다.

번들 형식 상세(XML 스키마, 예시)는 reference 문서를 참조한다.

---

## 분석 목적과 산출물 계약

Layer 1-2는 모든 목적에 공통이고, 목적(mode)에 따라 Layer 3의 구성과 Layer 4의 산출물만 교체한다.

### 산출물 규칙

- 각 mode는 고정된 **required output files** 집합을 정의한다.
- Layer 3 Writer 구성과 Layer 4 산출물은 mode에 의해 **쌍으로 결정**된다.
- 새 mode 추가 시 최소한 Writer 구성 + required output files를 정의한다. 추가로 mode별 prompt, Judge 출력 형식, Layer 4 생성 규칙이 필요할 수 있다.

| 목적 | Layer 3 구성 | Layer 4 산출물 |
|------|-------------|--------------|
| 문서화 | Writer(구조) + Writer(행위) → Analysis Judge | required output files (mode별 정의) |
| 리뷰 | Writer(보안) + Writer(품질) + Writer(성능) → Analysis Judge | required output files (mode별 정의) |
| 조사 | 단일 Writer (코드 접근 가능) | required output files (mode별 정의) |

mode별 구체적인 required output files 목록은 reference 문서를 참조한다.

### 설계 장점

- Layer 2 개선이 모든 목적에 자동 전파
- 새 목적 추가 시 Layer 3-4를 정의한다 (최소: Writer 구성 + 산출물 집합. 추가로 mode별 prompt, Judge 출력 형식, Layer 4 생성 규칙이 필요할 수 있다)

---

## 분석 단위(Unit)

분석 단위는 파이프라인이 한 번에 분석하는 코드의 논리적 묶음이다.
"기능적으로 응집된 파일 그룹"이면 충분하며, 특정 아키텍처 패턴을 전제하지 않는다.
