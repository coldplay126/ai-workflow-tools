# 재개(Resume)와 비용 최적화

파이프라인의 실행 효율을 높이는 설계 원칙.

---

## 점진적 재개 (Incremental Resume)

파이프라인이 중간에 실패하면 처음부터 다시 실행하지 않고, 마지막으로 완료된 Stage 이후부터 재개한다.

### 재개 판정

상태 파일에 기록된 마지막 완료 Stage와 현재 파일 해시를 비교하여 재개 지점을 결정한다.

### 보존 규칙

| 항목 | 재개 시 | 이유 |
|------|--------|------|
| Layer 1 결과 (파일 목록) | 보존 | 파일 시스템 상태 불변 |
| Layer 2 결과 (번들) | 보존 | 해시 불변이면 번들도 동일 |
| Stage 1 결과 (observation) | 파일 단위로 보존 | 완료된 파일의 observation은 캐시 유효 |
| Stage 2 결과 (Writer/Judge) | 재실행 | 부분 완료 상태를 신뢰할 수 없음 |

상태 파일 JSON 구조는 reference 문서를 참조한다.

---

## Drift 감지

기존 분석 결과가 현재 코드와 얼마나 다른지(drift) 감지한다.
전체 재분석 대신 변경된 파일만 선별하여 비용을 절감한다.

### 해시 기반 변경 감지

저장된 파일 해시와 현재 해시를 비교하여 변경/신규/삭제/무변경을 구분한다.

| 유형 | 감지 방법 | 처리 |
|------|----------|------|
| 변경된 파일 | 해시 불일치 | Stage 1 재분석, Stage 2 재합성 |
| 신규 파일 | 해시 기록에 없음 | Stage 1 분석, Stage 2 재합성 |
| 삭제된 파일 | 파일 부재 + 해시 기록 존재 | 해시에서 제거, Stage 2 재합성 |
| 무변경 파일 | 해시 일치 | 건너뜀 (캐시된 observation 사용) |

### Import graph 기반 간접 무효화

Stage 1 완료 후 `.ai-context/.tmp/import-graph.json`을 저장한다. 다음 실행에서 직접 변경 파일의 exported surface hash가 바뀌었거나, baseline extractor가 surface hash를 만들 수 없어 content hash fallback이 필요한 경우, 이전 import graph의 reverse dependents도 Stage 1 재분석 대상에 포함한다.

| 조건 | 추가 대상 | 캐시 정책 |
|------|----------|----------|
| 변경 파일의 `exports_hash` 변경 | 이전 graph에서 해당 파일을 import하던 모든 transitive dependent | observation cache 우회 |
| `exports_hash` 없음 + content hash 변경 | 이전 graph의 모든 transitive dependent | observation cache 우회 |
| 삭제 파일 | 이전 graph에서 삭제 파일을 import하던 모든 transitive dependent | observation cache 우회 |
| content hash 변경 + `exports_hash` 동일 | 직접 변경 파일만 재분석 | dependent는 기존 observation 재사용 |

Type-only edge도 analysis stale 방지를 위해 포함한다. Runtime-only invalidation은 별도 소비자가 생길 때 graph query 옵션으로 분리한다.

---

## Observation 캐시

Stage 1의 파일별 observation 결과를 캐시하여 재사용한다.

### 핵심 규칙

- 캐시 키는 파일의 content_hash이다
- 파일 내용이 변하지 않으면 observation을 재활용한다
- 캐시 단위는 파일이다 (분석 단위 전체가 아님)
- Stage 1 캐시만 재사용 가능하다. Stage 2는 반드시 재실행한다 (입력이 달라지므로)

### 무효화 조건

| 조건 | 동작 |
|------|------|
| content_hash 불일치 | 캐시 무효, 재분석 |
| 캐시 파일 부재 | 신규 분석 |
| 캐시 파일 손상 | 폐기 후 재분석 |
| 분석 설정 변경 | 전체 캐시 무효화 |

캐시 저장 형식 및 경로 상세는 reference 문서를 참조한다.

---

## 지식 축적 (Knowledge Accumulation)

개별 분석의 결과를 프로젝트 수준의 지식으로 축적한다.
축적된 지식은 이후 분석에서 참조 맥락으로 활용된다.

### 핵심 원칙

- 매 분석이 프로젝트 지식을 개선하고, 축적된 지식이 다음 분석의 품질을 높인다.
- Stage 2/3에서 참조할 때 우선순위를 따른다.
- 참조 토큰 상한을 초과하면 우선순위 낮은 것부터 제거한다.

### 축적되는 지식 유형

| 유형 | 활용 |
|------|------|
| 데이터 모델 맵 | 교차 서비스 의존성 파악 |
| API 엔드포인트 카탈로그 | 호출 체인 추적 |
| 외부 연동 현황 | 장애 영향 범위 예측 |
| 관찰된 패턴 | 프로젝트 전체 경향 파악 |

참조 우선순위 및 상한 수치는 reference 문서를 참조한다.

---

## 원자적 상태 관리

### 불변식

- 상태 파일 갱신 시 중간 상태가 노출되지 않아야 한다.
- 여러 분석이 동시에 실행될 때 같은 파일에 동시 쓰기를 방지해야 한다.

### 원칙

- **원자적 쓰기**: 임시 파일에 쓴 뒤 원자적 rename으로 교체
- **경로별 잠금**: 분석 단위별 독립 잠금, 서로 다른 단위는 병렬 가능
- **무결성 검증**: 쓰기 후 파싱 가능 여부를 확인

구현 방식(파일명 패턴, lock 경로 등)은 status 문서를 참조한다.
