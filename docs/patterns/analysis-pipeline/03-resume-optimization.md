# 재개(Resume)와 비용 최적화

파이프라인의 실행 효율을 높이는 설계 원칙.

---

## 점진적 재개 (Incremental Resume)

파이프라인이 중간에 실패하면 처음부터 다시 실행하지 않고, 마지막으로 완료된 Stage 이후부터 재개한다.

### 재개 판정

상태 파일의 마지막 완료 Stage, `.tmp/hashes.json`의 source hash, `layers.bundle.configHash`를 비교하여 재개 지점을 결정한다.

### 보존 규칙

| 항목 | 재개 시 | 이유 |
|------|--------|------|
| Layer 1 결과 (파일 목록) | 보존 | 파일 시스템 상태 불변 |
| Layer 2 결과 (번들) | config hash 불변이면 보존 | 같은 설정에서만 번들이 동일 |
| Stage 1 결과 (observation) | 파일 단위로 보존 | 완료된 파일의 observation은 캐시 유효 |
| Stage 2 result | 같은 source/config generation에서 output이 없을 때만 복구에 재사용 | 저장 raw result가 현재 입력에 대응할 때만 신뢰 가능 |
| failed required Stage 3 artifact | 보존 | 실패 원인과 재시도 상태를 진단해야 함 |

source hash나 bundle config가 바뀌면 Stage 2 저장 result를 폐기하고 새 generation으로 분석한다. 이때 Stage 2/3의 `retryCount`는 0으로 reset한다. 성공한 Stage 2/3도 해당 Stage의 retry budget을 reset한다.

`.tmp/hashes.json`은 마지막 성공 generation의 baseline이다. 현재 source의
drift는 provider 실행 전에 계산하지만 새 해시는 final output이 `completed`된
후에만 저장한다. 실패한 재분석은 baseline을 바꾸지 않으며, transitive
invalidation은 실행 시작 시 읽은 이전 baseline을 사용한다.

required Stage 3이 failed이면 output도 failed다. resume은 `reason`, `errorMessage`, `retryCount`, `artifacts.stage3_final`을 유지한 채 Stage 3부터 재시도한다. Stage 3이 성공하거나 정책이 skipped로 표시할 때만 output으로 진행한다.

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

### `--cycles` 진단

`awf analyze {service} --cycles`는 각 unit의 `import-graph.json`에서 SCC를 검출해 import 사이클을 보고한다. 그래프가 없는 unit은 `no graph`로 분류되고 종료 코드에 영향을 주지 않는다. 사이클이 한 건이라도 있으면 종료 코드 1을 돌려준다(빌드 게이트로 활용 가능).

### `--check` / `--catalog`에서 transitive 가시화

`awf analyze {service} --check`와 `--catalog`는 직접 변경 파일 외에 import graph로 잡힌 transitive stale 후보 수도 함께 표시한다. 출력 예: `⚠ {unit}: 3 direct + 7 transitive since 2026-05-08`. exit code는 직접 변경만 기준으로 판정하므로 기존 CI 게이트 의미는 그대로 유지된다(transitive는 운영 가시성 정보).

### 간접 무효화 비활성화 (escape hatch)

다음 두 가지 방법으로 transitive 무효화만 끄고 직접 변경 파일 기준의 incremental만 유지할 수 있다. 그래프 자체는 계속 빌드되어 다음 실행에 사용 가능하다.

| 우선순위 | 방법 | 설정 |
|---------|------|------|
| 1 (응급용) | 환경변수 | `AWF_DISABLE_TRANSITIVE_INVALIDATION=1` |
| 2 | pipeline config | `analysis-pipeline.json` → `transitive_invalidation.enabled = false` |

비활성화되면 stderr에 `stage1_invalidation: transitive disabled (env:... | config:...)`가 한 줄 출력된다. 직접 변경된 파일만 재분석되므로, exports_hash 변경이 누락된 dependent는 stale 상태로 남을 수 있다.

---

## Observation 캐시

Stage 1의 파일별 observation 결과를 캐시하여 재사용한다.

### 핵심 규칙

- 캐시 키는 파일의 content_hash이다
- 파일 내용이 변하지 않으면 observation을 재활용한다
- 캐시 단위는 파일이다 (분석 단위 전체가 아님)
- Stage 1 캐시는 파일 단위로 재사용한다. Stage 2는 현재 attempt의 payload를 다시 검증하며, 저장 raw result는 같은 source/config generation에서 output 복구가 필요할 때만 재사용한다.

### 무효화 조건

| 조건 | 동작 |
|------|------|
| content_hash 불일치 | 캐시 무효, 재분석 |
| 캐시 파일 부재 | 신규 분석 |
| 캐시 파일 손상 | 폐기 후 재분석 |
| 분석 설정 변경 | bundle과 Stage 2 저장 result 무효화, 새 generation 시작 |

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
