# 3-Stage 처리 상세

Layer 3 (Analyze)의 내부 구조. 규모에 따라 1~3개의 Stage를 거치며, 각 Stage는 서로 다른 Provider 등급을 사용한다.

---

## Stage 개요

| 항목 | Stage 1 | Stage 2 | Stage 3 |
|------|---------|---------|---------|
| 단위 | 파일별 | 분석 단위(unit)별 | 서비스/프로젝트별 |
| Provider | 저비용 | 중비용 | 고비용 |
| 목적 | 사실 추출 | 합성 + 판단 | 교차 검증 |
| 실행 조건 | 항상 | 항상 | 조건부 (기본: large + deep, 예외 허용) |
| 병렬화 | 파일 단위 | Writer 단위 | - |
| 협업 패턴 | 서브에이전트 | 서브에이전트 | 서브에이전트 |

모든 Stage는 **서브에이전트** 패턴을 사용한다. Writer 간 토론이 불필요하고, 결과만 수집하여 Analysis Judge가 통합하기 때문이다.

---

## Stage 1: 파일별 분석 (저비용 Provider)

각 파일의 observation에서 구조화된 정보를 추출한다.

### 추출 규칙

1. **사실만 기록**: 심각도, 결론, 권장 사항을 포함하지 않는다
2. **단일 파일 범위**: 다른 파일의 내용을 추측하지 않는다
3. **인터페이스 기준**: 다른 파일과의 연관은 입출력 인터페이스만 기록한다
4. **위치 참조**: 코드 스니펫 대신 파일명:행번호로 위치를 표시한다

### 병렬 실행

Stage 1은 파일 간 의존성이 없으므로 모든 파일을 병렬로 처리할 수 있다.
동시 실행 수(concurrency)는 Provider의 rate limit과 비용을 고려하여 설정한다.

Stage 1 출력 스키마는 reference 문서를 참조한다.

### 부수 효과: import graph 갱신

Stage 1이 추출한 import 정보를 사용해 Layer 4가 `.ai-context/.tmp/import-graph.json`을 갱신한다. 그래프 빌드는 LLM 호출이 없는 결정론적 후처리이며, 다음 실행에서 transitive cache invalidation의 입력으로 쓰인다 — 변경 파일의 exports hash가 바뀌면 reverse-dependents도 stage1 재분석 대상으로 자동 포함된다. 비활성화는 `AWF_DISABLE_TRANSITIVE_INVALIDATION=1` 또는 `analysis-pipeline.json` → `transitive_invalidation.enabled = false`.

### 부수 효과: 운영 텔레메트리 기록

`Stage1GraphInvalidation` 결과는 `<repo_root>/.awf-operations/events/<YYYY-MM-DD>.jsonl` 에 한 줄로 추가되고 동시에 `.awf-operations/log.md` 에 시간순 entry 가 append 된다. 기록 실패는 분석 자체를 막지 않는다 (telemetry 실패가 게이트가 되지 않도록 try/swallow). 누적된 데이터는 over-invalidation 비율 측정 + AST adapter 도입 결정의 근거로 사용된다. 자세한 layout 은 [`docs/architecture/awf-cli-architecture.md` §3.6](../../architecture/awf-cli-architecture.md) 참조.

---

## Stage 2: 단위 합성 — Writer/Judge 패턴

여러 파일의 observation을 종합하여 단위 수준의 판단을 생성한다.
핵심 패턴은 **Writer/Judge 분리**이다.

### Writer

- 각 Writer는 서로 다른 관점으로 동일한 observation을 분석한다
- Writer는 병렬로 실행된다 (서로 독립적)
- 각 Writer는 claim(주장)과 산출물 초안을 출력한다

### Claim

모든 Writer 출력과 Judge 입력의 공통 계약.

| 필드 | 설명 |
|------|------|
| `id` | claim 고유 식별자 |
| `type` | endpoint, data_access, external_call, logic_flow, signal 등 |
| `claim` | 주장 내용 |
| `evidence` | observation에서의 근거 |
| `source_files` | 근거가 되는 소스 파일 목록 |
| `confidence` | high / medium / low |

Claim JSON 스키마 예시는 reference 문서를 참조한다.

### Analysis Judge

Analysis Judge는 모든 Writer의 출력을 수신하여 최종 산출물을 생성한다. (Multi-Agent Judge와 역할이 다름 — glossary 참조)

**Analysis Judge가 하는 것**:
- 중복 claim 제거 (같은 사실을 여러 Writer가 보고한 경우)
- Writer 간 모순 발견 시 confidence 높은 쪽 채택 + 모순 기록
- 산출물 간 일관성 검증

**Analysis Judge가 하지 않는 것**:
- Writer의 evidence chain을 수정하지 않는다
- 새로운 claim을 생성하지 않는다
- 원본 데이터를 변조하지 않는다


### Generation integrity

Stage 2와 Stage 3은 다음 불변식을 따른다.

required Stage 3은 현재 routing 정책이 실행 대상으로 결정한 Stage 3이다. `stage3_force`, `related_domains >= 3`, scale routing으로 실행된 경우가 여기에 해당한다. policy skip된 Stage 3은 required가 아니다.

1. Stage 2 payload는 현재 attempt가 mode의 모든 required output을 공급할 때만 complete다. 이전 attempt에서 남은 파일은 완료 판정에 사용하지 않는다.
2. 저장된 Stage 2 result는 같은 source/config generation에서만 재사용한다.
3. required Stage 3이 failed이면 이후 Stage 3 attempt가 성공하거나 정책상 skipped가 될 때까지 failed다. 진단 artifact와 실패 상태를 보존한다.
4. Stage 2/3 성공 또는 새 source/config generation은 해당 retry budget을 reset한다.

상태 필드와 artifact 경로는 reference 문서를 참조한다.

---

## Stage 3: 교차 검증 (고비용 Provider)

단일 분석 단위를 넘어 서비스 또는 프로젝트 수준의 교차 검증을 수행한다.

### 실행 조건

deep 모드에서만 실행 가능하다. deep 모드 내에서의 활성화 규칙:

| 조건 | 우선순위 | 설명 |
|------|---------|------|
| `stage3_force` | 1 (최고) | 강제 플래그. 설정되면 무조건 실행 |
| `related_domains >= 3` | 2 | 관련 도메인이 3개 이상이면 scale routing skip을 override하여 활성화 |
| scale routing | 3 (기본) | pipeline config의 `stage_routing.{scale}.stage3` 값. large=실행, 그 외=skip |

기본 정책은 large 규모이면서 deep 모드일 때 실행한다.
관련 도메인이 많으면 standard/small 규모에서도 교차 검증의 가치가 있으므로 자동 활성화한다.

### 교차 검증 항목

- 데이터 참조 교차: 같은 테이블을 읽고/쓰는 다른 서비스
- API 호출 체인: 이 서비스의 API를 호출하는 다른 서비스 패턴
- 공유 모듈 의존성: 공통 모듈 변경의 영향 범위
- 프로젝트 수준 지식: 축적된 지식과의 정합성

### 참조 확장 규칙

Stage 3이 참조하는 외부 문서에는 우선순위와 상한이 있다.

| 우선순위 | 참조 대상 |
|---------|----------|
| 1 | 현재 단위의 observation |
| 2 | 관련 단위의 기존 산출물 |
| 3 | 프로젝트 수준 지식 |

상한 수치는 reference 문서를 참조한다.

---

## Fanout 병렬화 (large 규모)

대규모 분석 단위는 단일 Provider 호출로 처리할 수 없다.
Fanout 패턴으로 분할하여 병렬 처리한 후 병합한다.

### 핵심 원칙

- 파일 간 의존성이 높은 것끼리 같은 그룹에 배치
- 각 하위 그룹은 standard 규모 이내
- import 그래프 기반 응집도 클러스터 식별
- 하위 그룹별 Writer/Analysis Judge 실행 후 최종 Analysis Judge가 전체 병합

### 병합 시 규칙

| 상황 | 처리 |
|------|------|
| 하위 그룹 간 중복 claim | 최종 Analysis Judge가 중복 제거 |
| 하위 그룹 간 모순 | 최종 Analysis Judge가 confidence 기준으로 해결 |
| 하위 그룹 경계의 파일 의존성 | context 파일로 인접 그룹의 시그니처 제공 |
