# Analysis Pipeline Reference

운영값, 스키마 상세, 임계값 등 `docs/patterns/analysis-pipeline/`에서 분리된 구현 세부와 운영값.

---

## 0. Mode별 Required Output Files

### 문서화 모드 (document) — 구현됨

| 파일 | 형식 | 역할 |
|------|------|------|
| `api-spec.json` | JSON | API 엔드포인트 명세 (컨트롤러, 엔드포인트, S2S 호출, 미들웨어) |
| `data-model.md` | Markdown | DB 스키마, 엔티티, 관계도, Redis 패턴 |
| `domain-overview.md` | Markdown | 도메인 책임, 비즈니스 규칙, 기술 부채, 서비스 간 의존성 |
| `external-integration.md` | Markdown | 외부 API, 메시지 큐(SQS), Webhook, S2S 호출, 타임아웃/재시도 |

산출물 디렉토리: `.ai-context/`

자동 생성 부가 파일:
- `ANALYSIS_REPORT.md` — 분석 요약 리포트 (엔드포인트 수, 이슈, 환경변수 등)

### 리뷰 모드 (review) — 계획중

| 파일 | 형식 | 역할 |
|------|------|------|
| `review-report.md` | Markdown | 보안/품질/성능 리뷰 리포트 (severity 분류) |

Status: v3 로드맵 Phase 4에서 구현 예정. Writer 구성: 보안 + 품질 + 성능.

### 조사 모드 (investigate) — 계획중

| 파일 | 형식 | 역할 |
|------|------|------|
| `investigation-report.md` | Markdown | 코드 경로 추적 결과 |

Status: 미정.

---

## 1. 규모 분류 임계값

| 규모 | 파일 수 | 전략 |
|------|--------|------|
| small | 1-10 | 코드 전문 포함, 최소 Stage |
| standard | 11-30 | observation 기반, Writer/Judge 합성 |
| large | 30+ | observation 기반, Fanout 병렬, 교차 검증 |

설정 기본값: `fanout_large_file_threshold = 80` (config.py 기본값, 코드 상수는 30)

---

## 2. 규모별 비교표

| 항목 | small (1-10) | standard (11-30) | large (30+) |
|------|-------------|------------------|-------------|
| Stage 1 Provider | 저비용 | 저비용 | 저비용 |
| Stage 2 Provider | 중비용 | 중비용 | 중비용 |
| Stage 3 Provider | -† | -† | 고비용 |
| 번들 내용 | observation + 코드 전문 | observation만 | observation만 |
| 코드 fallback | 항상 포함 | confidence 낮을 때 | confidence 낮을 때 |
| 병렬화 | 불필요 | 선택적 | Fanout 필수 |
| 예상 비용 비율 | 1x | 2-3x | 5-10x |

† `related_domains >= 3`이면 auto-enable (skip override). `stage3_force`로도 강제 활성화 가능.

---

## 3. Stage 1 출력 스키마

```json
{
  "path": "src/notification/notification.service.ts",
  "role": "service",
  "language": "typescript",
  "lines": 245,
  "endpoints": [],
  "tables": [
    {"name": "USER_NOTIFICATIONS", "access": "read", "usage": "미읽음 집계"}
  ],
  "external_calls": [
    {"service": "PushNotification", "method": "enqueue", "target": "메시지 큐"}
  ],
  "imports": [
    {"name": "PushNotification", "source": "./push.notification"}
  ],
  "business_logic": [
    {"method": "sendNotification", "steps": ["수신자 중복 제거", "분할 처리", "큐 적재"]}
  ],
  "signals": [
    "context 미존재 시 undefined 속성 접근 경로 존재 (파일:30행)",
    "조회에 정렬 조건 없음 (파일:42행)"
  ]
}
```

---

## 4. Claim 스키마

```json
{
  "writer": "structure",
  "claims": [
    {
      "id": "C1",
      "type": "endpoint",
      "claim": "GET /notification/:unitId/count 엔드포인트가 존재",
      "evidence": "notification.controller.ts:15 — 데코레이터 선언",
      "source_files": ["notification.controller.ts", "notification.service.ts"],
      "confidence": "high"
    }
  ],
  "output_files": {
    "api-spec.json": "...",
    "data-model.md": "..."
  }
}
```

---

## 5. XML 번들 예시

```xml
<review target="src/notification/notification.service.ts">
  <structure>
    <path>src/notification/notification.service.ts</path>
    <path>src/notification/push.notification.ts</path>
  </structure>

  <file path="src/notification/notification.service.ts"
        role="target" language="typescript">
    <content encoding="xml-escaped">...</content>
  </file>

  <file path="src/notification/push.notification.ts"
        role="context" language="typescript">
    <content encoding="xml-escaped" mode="signatures">...</content>
  </file>
</review>
```

---

## 6. Writer 구성 (목적별)

### 문서화 모드

| Writer | 관점 | 담당 산출물 |
|--------|------|-----------|
| Writer A | 구조 (API + 데이터) | API 명세, 데이터 모델 |
| Writer B | 행위 (흐름 + 연동) | 단위 개요, 외부 연동 |

### 리뷰 모드

| Writer | 관점 | 담당 산출물 |
|--------|------|-----------|
| Writer A | 보안 | 보안 취약점 분석 |
| Writer B | 품질 | 코드 품질 분석 |
| Writer C | 성능 | 성능 병목 분석 |

---

## 7. Stage 3 참조 확장 상한

| 항목 | 값 |
|------|------|
| 참조 문서 최대 수 | 5개 |
| 총 토큰 상한 | 8,000 토큰 |
| 관련 단위 최대 수 | 3개 |

---

## 8. 코드 fallback 상한

| 항목 | 값 |
|------|------|
| 최대 fallback 파일 수 | 3파일 |
| 트리거 조건 | Writer가 confidence: low 표시 시 |

---

## 9. 상태 파일 예시

```json
{
  "unit": "notification",
  "scale": "standard",
  "mode": "document",
  "last_completed_stage": "stage1",
  "started_at": "2026-04-07T10:00:00Z",
  "updated_at": "2026-04-07T10:05:00Z",
  "stage_results": {
    "input": { "status": "completed", "file_count": 13 },
    "bundle": { "status": "completed", "bundle_count": 13 },
    "stage1": { "status": "completed", "observation_count": 13 },
    "stage2": { "status": "failed", "error": "provider_timeout" }
  }
}
```

---

## 10. Observation 캐시 저장 형식

```json
{
  "src/notification/notification.service.ts": {
    "content_hash": "sha256:a1b2c3d4...",
    "observation_hash": "sha256:e5f6g7h8...",
    "created_at": "2026-04-07T10:00:00Z",
    "observation": {
      "role": "service",
      "language": "typescript",
      "lines": 245,
      "signals": ["..."],
      "business_logic": ["..."]
    }
  }
}
```

---

## 11. 비용 절감 추정

14개 파일 중 2개만 변경된 경우:
- Stage 1 비용: 2/14 = 14% (86% 절감)
- Stage 2 비용: 기존 문서 + 변경분 업데이트
- 총 예상 절감: 약 70-85%

---

## 12. Transitive invalidation 설정

`analysis-pipeline.json` 최상위에 다음 절을 두면 graph 기반 transitive 무효화를 영구히 끌 수 있다. 비활성화 시 직접 변경 파일만 재분석되며 graph 자체는 계속 빌드된다.

```json
{
  "transitive_invalidation": {
    "enabled": true
  }
}
```

| 키 | 타입 | 기본값 | 의미 |
|----|------|------|------|
| `transitive_invalidation.enabled` | bool | `true` | `false`로 두면 graph reverse-dependent invalidation을 건너뛴다. 누락/잘못된 타입은 default-on으로 처리. |

응급 상황에선 환경변수가 우선한다(설정 파일 수정 없이 즉시 비활성화):

```bash
AWF_DISABLE_TRANSITIVE_INVALIDATION=1 awf analyze sample-api health
```

env 우선순위: `1`/`true`/`yes`/`on` (대소문자 무관) → 비활성화. 빈 문자열이나 그 외 값은 unset과 동일하게 처리.

## 13. 운영 텔레메트리

`awf analyze` 호출 시 `stage1_invalidation` 이벤트가 `.awf-operations/events/` 에 누적된다 (페이로드: `direct_count`, `indirect_count`, `invalidating_count`, `unchanged_count`, `deleted_count`, `transitive_enabled`). 추세는 `awf wiki compile` 로 합성된 `wiki/operations/stage1-invalidation.md` 에서 확인할 수 있고, **change density** (`invalidating / (invalidating+unchanged)`) 가 낮으면 regex import extractor → AST adapter 업그레이드의 ROI 신호다. 자세한 정책은 [awf-cli-architecture §3.6](../architecture/awf-cli-architecture.md).

### 13. Operational telemetry (English)

Each `awf analyze` invocation appends a `stage1_invalidation` event to `.awf-operations/events/` with direct/indirect/invalidating/unchanged/deleted counts. Aggregate trends are visible in `wiki/operations/stage1-invalidation.md` (compiled by `awf wiki compile`); a low **change density** (`invalidating / (invalidating+unchanged)`) is the empirical signal for upgrading the regex-based import extractor to an AST adapter.
