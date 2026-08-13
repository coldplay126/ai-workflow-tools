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

Markdown 산출물(`data-model.md`, `domain-overview.md`, `external-integration.md`, `ANALYSIS_REPORT.md`)은 `ai_context_markdown_v1` frontmatter를 가진다. `api-spec.json`은 JSON 소비자 호환성을 위해 frontmatter 대상에서 제외한다.

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

## 0.1 Unit Discovery

`awf ready`와 `awf scan --no-ai`는 provider 호출 없이 deterministic scanner를
사용한다.

### Python project markers

| Marker | 의미 |
|--------|------|
| `pyproject.toml` | 표준 Python project metadata |
| `setup.py` / `setup.cfg` | setuptools 기반 project |
| `requirements.txt` | script-style Python repo 또는 legacy dependency manifest |
| `Pipfile` | Pipenv project |
| `poetry.lock` | Poetry-managed dependency lock |

### Unit directory patterns

| 구조 | unit pattern | 예시 |
|------|--------------|------|
| conventional source tree | `src/{unit}`, `src/domains/{unit}`, `src/modules/{unit}` 등 | `src/orders`, `src/features/payment` |
| root-level script repo | `{unit}` | `collectors`, `analyzers`, `importers`, `exporters`, `monitors`, `matchers` |

`--dry-run --output-format json`은 이 deterministic discovery 결과를 기반으로
prompt와 경로를 구조화 출력한다. 설정이 비어 있어도 dry-run 단계에서는 AI
unit discovery를 호출하지 않는다.

---

## 1. 규모 분류 임계값

| 규모 | 파일 수 | 전략 |
|------|--------|------|
| small | ≤ 10 | 코드 전문 포함, 최소 Stage |
| standard | 11부터 configured large threshold까지 | observation 기반, Writer/Judge 합성 |
| large | configured large threshold 초과 | observation 기반, Fanout 병렬, 교차 검증 |

`analysis.fanout_large_file_threshold` 기본값은 `80`이므로 기본 설정에서는
11-80개가 standard, 81개부터 large다. 코드 상수 `30`은 설정 키를 읽을 수
없는 경우의 fallback이며 운영 기본값이 아니다.

---

## 2. 규모별 비교표

| 항목 | small (≤10) | standard (기본 11-80) | large (기본 81+) |
|------|-------------|------------------------|------------------|
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
  "id": "analysis-sample-api-notification-20260813",
  "service": "sample-api",
  "domain": "notification",
  "mode": "deep",
  "scale": "standard",
  "startedAt": "2026-08-13T10:00:00+00:00",
  "completedAt": null,
  "currentLayer": "analyze",
  "currentStage": 3,
  "layers": {
    "input": { "status": "completed" },
    "bundle": { "status": "completed", "fileCount": 13, "lineCount": 720, "tokenEstimate": 5400, "configHash": "sha256:..." },
    "analyze": {
      "stage1": { "status": "completed", "provider": "codex", "errorMessage": "", "retryCount": 0 },
      "stage2": { "status": "completed", "provider": "sonnet", "errorMessage": "", "retryCount": 0 },
      "stage3": { "status": "failed", "provider": "opus", "reason": "reference expansion live stage3 validation failed", "errorMessage": "provider_timeout", "retryCount": 1 }
    },
    "output": { "status": "failed", "errorMessage": "provider_timeout" }
  },
  "artifacts": {
    "result_file": ".tmp/result-stage2-sonnet.txt",
    "stage3_final": ".tmp/stage3-final.md"
  }
}
```

## 9.1 Generation 상태와 재개

| 상태/파일 | 재개에서의 용도 |
|-----------|----------------|
| `.tmp/hashes.json` | 현재 source generation과 비교할 파일 해시 |
| `layers.bundle.configHash` | 현재 bundle 설정 generation과 비교할 해시 |
| `artifacts.result_file` | 출력 복구에 사용할 저장된 Stage 2 raw result |
| `layers.analyze.stage2.status`, `retryCount`, `errorMessage` | Stage 2 재시도와 `missing_required_outputs:` 진단 |
| `layers.analyze.stage3.status`, `reason`, `errorMessage`, `retryCount` | required Stage 3 실패·정책 skip·재시도 상태 |
| `artifacts.stage3_final` | 보존되는 Stage 3 진단 artifact 경로 |

저장된 Stage 2 result는 Stage 1이 completed이고 Stage 2 상태가 `in_progress` 또는 `completed`이며 output이 없을 때만 복구 후보가 된다. `.tmp/hashes.json`의 source와 `layers.bundle.configHash`가 현재 generation과 모두 일치해야 재사용하며, 어느 하나라도 달라지면 raw result를 폐기한다.

Stage 2 finalization은 현재 payload의 `missing_files`를 사용한다. 누락된 required output이 있으면 이전 실행의 파일이 남아 있어도 Stage 2와 output을 failed로 두고 `missing_required_outputs:` 진단을 기록한다.

required Stage 3이 failed이면 `layers.output.status`도 failed로 유지한다. `errorMessage`, `reason`, `retryCount`, `artifacts.stage3_final`은 보존하며, 이후 Stage 3 성공 또는 정책상 skip에서만 진행할 수 있다. Stage 2/3 성공과 source 또는 bundle config 변경으로 시작한 새 generation은 각 retry budget을 0으로 재설정한다.

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

`awf analyze` 호출 시 운영 이벤트가 `.awf-operations/events/` 에 누적된다.

- `stage1_invalidation`: `direct_count`, `indirect_count`, `invalidating_count`, `unchanged_count`, `deleted_count`, `transitive_enabled`
- `analysis_complete`: `service`, `domain`, `mode`, `total_seconds`, `source_file_count`, `bundle_line_count`, `bundle_token_estimate`, `output_file_count`

`stage1_invalidation` 추세는 `awf wiki compile` 로 합성된 `wiki/operations/stage1-invalidation.md` 에서 확인할 수 있고, **change density** (`invalidating / (invalidating+unchanged)`) 가 낮으면 regex import extractor → AST adapter 업그레이드의 ROI 신호다. `analysis_complete`는 single-run telemetry로 남기며 compile topic은 만들지 않는다. 자세한 정책은 [awf-cli-architecture §3.6](../architecture/awf-cli-architecture.md).

### 13. Operational telemetry (English)

Each `awf analyze` invocation appends operational events to `.awf-operations/events/`. `stage1_invalidation` carries direct/indirect/invalidating/unchanged/deleted counts, and `analysis_complete` carries service/domain/mode/elapsed time plus source file count, bundle line count, bundle token estimate, and output file count. Aggregate `stage1_invalidation` trends are visible in `wiki/operations/stage1-invalidation.md` (compiled by `awf wiki compile`); a low **change density** (`invalidating / (invalidating+unchanged)`) is the empirical signal for upgrading the regex-based import extractor to an AST adapter. `analysis_complete` remains single-run telemetry and is not compiled into a wiki topic.
