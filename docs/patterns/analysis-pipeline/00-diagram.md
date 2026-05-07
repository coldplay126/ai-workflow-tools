# Analysis Pipeline 다이어그램

## 1. 4-Layer 아키텍처

입력 수집에서 산출물 생성까지의 전체 파이프라인 구조.
각 Layer는 명확한 입출력 계약을 가지며, 독립적으로 교체 가능하다.

```mermaid
graph TB
    subgraph Layer1["Layer 1: Input (탐색)"]
        L1A["소스코드 수집"] --> L1B["분석 단위 식별"]
        L1B --> L1C["규모 판정"]
        L1C --> L1D["분석 목적 결정"]
    end

    subgraph Layer2["Layer 2: Bundle (관찰 수집)"]
        L2A["파일별 번들 생성"] --> L2B["관찰 추출 (사실만)"]
        L2B --> L2C["단위별 번들 조립"]
    end

    subgraph Layer3["Layer 3: Analyze (판단)"]
        L3A["Stage 1: 파일별 분석"] --> L3B["Stage 2: 단위 합성"]
        L3B --> L3C["Stage 3: 교차 검증 (조건부)"]
    end

    subgraph Layer4["Layer 4: Output (산출물)"]
        L4A["결과 병합"] --> L4B["산출물 생성"]
        L4B --> L4C["프로젝트 지식 갱신"]
    end

    Layer1 -->|"파일 목록 + 규모 + 목적"| Layer2
    Layer2 -->|"observation 번들"| Layer3
    Layer3 -->|"종합 판단 결과"| Layer4
```

---

## 2. 분석 상태 머신

실패 시 마지막 완료 stage 이후부터 재개 가능하다.

```mermaid
stateDiagram-v2
    [*] --> pending: 분석 요청
    pending --> input: Layer 1 시작
    input --> bundle: 파일 수집 완료
    bundle --> stage1: 번들 생성 완료
    stage1 --> stage2: 파일별 분석 완료
    stage2 --> stage3: 단위 합성 완료 (조건부)
    stage2 --> output: 단위 합성 완료
    stage3 --> output: 교차 검증 완료
    output --> completed: 산출물 생성 완료

    input --> failed: 실패
    bundle --> failed: 실패
    stage1 --> failed: 실패
    stage2 --> failed: 실패

    failed --> stage1: 재개 (완료 지점부터)
    failed --> stage2: 재개
```

---

## 3. 규모 기반 라우팅

분석 대상의 규모(scale)에 따라 Provider 등급과 처리 전략을 결정한다.
규모가 Provider depth를 결정한다.

```mermaid
flowchart TD
    Start["파일 수 확인"] --> Check1{"small?"}
    Check1 -->|예| Small["저비용만"]
    Check1 -->|아니오| Check2{"standard?"}
    Check2 -->|예| Standard["저+중비용"]
    Check2 -->|아니오| Large["저+중+고비용 (Fanout)"]
```

규모 분류 임계값, Provider별 비용/품질 비교표는 reference 문서를 참조한다.

---

## 4. 전체 워크플로우 생명주기

```mermaid
flowchart TD
    INIT["분석 요청"] --> CHECK{"기존 상태?"}
    CHECK -->|없음| FRESH["처음부터 실행"]
    CHECK -->|있음| DRIFT{"파일 변경?"}
    DRIFT -->|변경 없음| RESUME["완료 지점부터 재개"]
    DRIFT -->|변경 있음| INCREMENTAL["변경분만 재분석"]
    FRESH --> L1["Layer 1-4 순차 실행"]
    RESUME --> L1
    INCREMENTAL --> L1
    L1 --> DONE["완료 + 지식 축적"]
```
