# /analysis — 상세 파이프라인 레퍼런스

이 문서는 `analysis` 스킬의 상세 실행 지침입니다. `SKILL.md`에서 참조합니다.

---

## .ai-context 폴더 구조

분석 결과는 `analysis-docs/{service}/{domain}/.ai-context/`에 저장됩니다:

```
analysis-docs/{service}/{domain}/.ai-context/
├── api-spec.json              ← API 엔드포인트, Guard, DTO 명세
├── data-model.md              ← Entity, 테이블, Redis 패턴
├── domain-overview.md         ← 도메인 개요, 핵심 기능, 비즈니스 규칙
├── external-integration.md    ← 외부 서비스 호출, SQS 이벤트
├── ANALYSIS_REPORT.md         ← 분석 리포트 (통계, 변경점, 기술 부채)
├── analysis-config.json       ← 도메인별 오버라이드 설정 (선택)
├── .analysis-state.json       ← 파이프라인 상태 (재개용)
└── .tmp/                      ← 중간 산출물 (완료 시 삭제)
    ├── stage1-analysis.md     ← Stage 1 결과
    ├── stage2-draft.md        ← Stage 2 초안
    ├── stage3-final.md        ← Stage 3 결과 (조건부 실행 시)
    └── hashes.json            ← 증분 분석용 SHA-256 (삭제 안 됨)
```

---

## 경로 해석 규칙

이 커맨드는 특정 사용자 홈 경로에 고정되지 않도록 아래 순서로 루트를 해석합니다.

1. `AWF_DOCS_ROOT`가 설정돼 있으면 해당 경로를 사용
2. 아니면 현재 작업공간 기준 `../analysis-docs` 존재 여부 확인
3. 둘 다 없으면 `$HOME/Documents/GitHub/analysis-docs`를 마지막 fallback으로 사용

서비스 소스코드 루트는 `analysis-config.json`의 `service_map`에서 읽습니다.
이 값에 `${AWF_GITHUB_ROOT}` 같은 플레이스홀더가 있으면 다음 순서로 해석합니다.

1. 환경변수 값
2. 미설정 시 `$HOME/Documents/GitHub`

즉 기본 가정은 유지하되, 팀원이 다른 경로에 클론한 경우 환경변수만 바꾸면 동작해야 합니다.

---

## Resume Protocol (반드시 최우선 실행)

**이 섹션은 모든 실행에서 가장 먼저 수행합니다.**

1. `$ARGUMENTS`에서 service, domain, mode 관련 옵션을 파싱합니다
2. `{AWF_DOCS_ROOT}/{service}/{domain}/.ai-context/.analysis-state.json` 존재 여부 확인
3. **존재하고** `layers.output.status != "completed"`이면:
   - state 파일에서 `currentLayer`, `currentStage`, `mode` 확인
   - `summaries` 필드를 읽어 이전 단계 컨텍스트를 복원
   - `.ai-context/.tmp/` 디렉토리의 중간 산출물 확인
   - **stale artifact 검증** (`.tmp/` 정리):
     - `.tmp/` 내 각 artifact와 state의 단계별 status 대조
     - status가 `"failed"`인 단계의 artifact → 삭제 (재생성 대상)
     - status가 `"completed"`인 단계의 artifact → 유지
     - artifact가 존재하지만 state에 기록이 없는 파일 → 삭제 (orphan)
   - **failed 상태 처리**:
     - `retryCount < 2`인 failed 단계 → 해당 단계부터 재시도, `retryCount` 증가
     - `retryCount >= 2`인 failed 단계 → 스킵하고 다음 단계로 진행, 사용자에게 경고
   - **미완료 단계부터 재개** (Layer 1부터 재시작하지 않음)
   - 사용자에게 재개 상태를 알림: `"⏩ 이전 실행 감지: {currentLayer} Stage {currentStage}부터 재개합니다."`
   - failed 상태가 있으면 추가 알림: `"⚠️ {stage}가 실패 상태 (retryCount: {N}). 재시도합니다."`
4. **존재하지 않으면** Layer 1부터 새로 시작

---

## 실행 흐름

### Layer 1: Input (분석 설정 로드)

1. `{AWF_DOCS_ROOT}/_templates/analysis-config.json` 로드
2. `{AWF_DOCS_ROOT}/_templates/analysis-pipeline.json` 로드
3. `$ARGUMENTS`에서 service, domain, mode 파싱
4. `domain_definitions`에서 대상 디렉토리, 관련 도메인, 기존 문서 경로 확인
5. `service_map`에서 소스코드 절대 경로 해석
   - `${AWF_GITHUB_ROOT}` 플레이스홀더가 있으면 환경변수 또는 `$HOME/Documents/GitHub`로 확장
6. 도메인별 오버라이드 설정 확인: `analysis-docs/{service}/{domain}/.ai-context/analysis-config.json`

**오버라이드 가능 필드**:
- `exclude_patterns`: 도메인별 추가 제외 패턴 (기본 패턴에 병합)
- `include_tests`: `true`이면 `*.spec.ts`, `*.test.ts`도 분석 대상에 포함
- `scale_override`: `"small"` | `"standard"` | `"large"` — 자동 규모 판정을 무시
- `stage3_force`: `true`이면 internal deep context를 켜고 규모와 `related_domains` 수에 무관하게 Stage 3 실행

설정이 없는 분석 단위는 heuristic(`src/domain/`, `src/domains/`, `src/`, `app/`) 탐색 후, 실패 시 AI 기반 구조 분석으로 자동 발견.

**파일 수집 패턴 (include_patterns)**:
- `analysis-config.json`의 서비스별 또는 단위별 `include_patterns`로 수집 확장자를 지정할 수 있음
- 미설정 시 프로젝트 언어에 따라 자동 결정 (TS, PHP, Python, Go, Terraform, YAML 등)
- 우선순위: 단위 설정 > 서비스 설정 > 글로벌 설정 > 언어 기본값

**상태 전이**: `.analysis-state.json` 생성, `layers.input.status = "completed"`, `mode` 기록

### Layer 2: Bundle (XML Bundling)

소스코드를 LLM이 이해할 수 있는 구조화된 XML로 변환합니다.
`<content>` 내부의 소스코드는 반드시 XML-escaped 처리합니다 (`<` → `&lt;`, `>` → `&gt;`, `&` → `&amp;`).

#### Step 2-1: 파일 수집

1. 대상 디렉토리에서 파일 수집 (Glob 사용)
   - `analysis-config.json`의 `include_patterns` 설정이 있으면 해당 패턴 사용
   - 없으면 프로젝트 언어에 따라 자동 결정 (TS/JS, PHP, Python, Go, Terraform, YAML 등)
2. 제외 패턴 적용: `exclude_patterns` + `.gitignore`
3. 바이너리, 자동 생성 파일 제외

#### Step 2-2: import 그래프 분석

각 파일의 import 문을 분석하여 context 파일을 식별합니다:
- `import { X } from '../common/...'` → common 폴더 파일을 context로 포함
- `import { X } from '../../other-domain/...'` → 다른 도메인 파일을 context로 포함
- `node_modules` import는 제외

#### Step 2-3: XML 번들 생성

**도메인 번들**: 도메인 내 모든 파일을 `role="target"`으로 포함
```xml
<review domain="{domain}">
  <structure><!-- 전체 도메인 트리 --></structure>
  <file path="quest.controller.ts" role="target" language="typescript">
    <content encoding="xml-escaped">...</content>
  </file>
  <file path="quest.service.ts" role="target" language="typescript">
    <content encoding="xml-escaped">...</content>
  </file>
  <!-- context 파일은 시그니처만 포함 -->
  <file path="../common/base.service.ts" role="context" mode="signatures" language="typescript">
    <content encoding="xml-escaped">
      export class BaseService { findOne(id: number): Promise&lt;T&gt; }
    </content>
  </file>
</review>
```

**프로젝트 번들** (Stage 3 실행 시): Stage 3에서 사용
```xml
<review scope="project" name="{service}">
  <structure><!-- 전체 프로젝트 트리 (요약) --></structure>
  <config path="nest-cli.json" language="json">
    <content encoding="xml-escaped">...</content>
  </config>
  <previous-reviews>
    <stage1-summary><!-- Stage 1 분석 결과 요약 --></stage1-summary>
    <stage2-summary><!-- Stage 2 분석 결과 요약 --></stage2-summary>
  </previous-reviews>
</review>
```

**상태 전이**: `layers.bundle.status = "completed"`, `fileCount` 기록

#### Deterministic Gate (비용 0)

Layer 3 진입 전, 다음 게이트를 순서대로 실행:

1. **규모 판정**: `analysis-pipeline.json`의 `scale_thresholds` 기준으로 파일 수 분류
   - `< 10개` → **small**
   - `10~30개` → **standard**
   - `> 30개` → **large**

2. **모드별 라우팅**:

   **standard context** (`mode = "standard"`):
   - Stage 1: Codex
   - Stage 2: Sonnet (규모 무관)
   - Stage 3: 항상 생략

   **internal deep context** (`related_domains` 또는 `stage3_force`가 있는 경우):
   - `stage3_force: true` → scale routing이 `skip`이어도 Stage 3 실행
   - `related_domains.length >= 3` → scale routing이 `skip`이어도 Stage 3 실행
   - 그 외에는 `analysis-pipeline.json`의 `stage_routing.{scale}.stage3`이 `skip`이 아닐 때만 Stage 3 실행
   - Stage 3 실행 provider는 기본 Opus

3. **증분 해시 체크**: `.ai-context/.tmp/hashes.json` 존재 시, 각 파일의 SHA-256 비교
   - 변경된 파일만 Stage 1 재분석 대상으로 마킹
   - 전체 변경 없으면 Stage 1 스킵 → Stage 2로 직행

4. **번들 크기 검증**: 도메인 번들이 2000줄 초과 시 배치 분할

**상태 전이**: `scale` 필드 기록, 게이트 결과 기록

### Layer 3: Analyze (점진적 분석)

`analysis-pipeline.json`의 `stage_routing`에 따라 모델을 선택합니다.
각 스테이지 완료 시 반드시 중간 산출물을 `.tmp/`에 저장하고 state를 업데이트합니다.

#### Stage 1: 파일별 분석 — 저비용 프로바이더

**프로바이더 선택**:
- `analysis-pipeline.json`의 `stage_routing.{scale}.stage1`에 따름 (기본: `codex`)
- `awf-cli`에서는 `_resolve_stage_provider()`가 scale별 provider를 자동 선택
- Claude Code에서는 cmux-agent 활성 시 `cmux-agent send <worker>`로 broker 경로(권장, 2026-05-13 §12.5), 미활성 시 `mcp__codex__codex` (read-only sandbox, file_access: true) fallback

**awf-cli 실행 시**: stage1 provider가 글로벌 기본 provider와 다를 때만 파일별 분석 실행.
파일별로 역할/import/export/summary를 JSON으로 추출하여 `.tmp/stage1-file-analyses.json`에 저장.
결과가 Stage 1 memo의 `## File Analyses` 섹션으로 Stage 2에 전달됨.

**Claude Code 실행 시**: Codex는 파일을 직접 읽을 수 있으므로 XML 번들 임베딩이 불필요합니다.
파일 경로 목록과 분석 지시를 Task Message로 전달합니다:

```
다음 TypeScript 파일들을 분석하세요.

대상 파일:
- {service_root}/src/domain/{domain}/quest.controller.ts
- {service_root}/src/domain/{domain}/quest.service.ts
- ...

컨텍스트 파일 (참조용):
- {service_root}/src/domain/{domain}/entities/quest.entity.ts
- ...

다음 3개 섹션으로 파일별 분석 결과를 JSON으로 출력하세요:

{
  "conclusion": "전체 요약",
  "evidence": {
    "api_structure": [
      { "file": "파일명", "endpoints": [{ "method": "GET", "path": "/quest", "guard": "...", "dto": "...", "response": "..." }] }
    ],
    "data_model": [
      { "file": "파일명", "entities": [{ "table": "...", "columns": [...], "relations": [...] }], "redis_keys": [...] }
    ],
    "business_logic": [
      { "file": "파일명", "rules": [...], "error_handling": [...], "external_calls": [...] }
    ]
  },
  "risks": ["발견된 기술 부채나 잠재적 이슈"],
  "action_items": ["문서화 시 특별히 주의할 사항"]
}
```

**Codex 실패 시 fallback**:
1. JSON 파싱 실패 → broker 경로면 `cmux-agent send <worker> "응답을 valid JSON으로 다시 출력하세요. { 로 시작하여 } 로 끝나야 합니다."`, MCP 경로면 `mcp__codex__codex-reply(threadId, ...)` (1회)
2. 재실패 또는 타임아웃 → Sonnet 에이전트로 fallback (Agent tool, model: sonnet), 도메인 XML 번들을 전달하여 동일 분석 수행

**종합**: Codex 4-Block 결과의 `evidence` 섹션에서 3관점 데이터 추출 → 📄 `.tmp/stage1-analysis.md`

**상태 전이**: `analyze.stage1.status = "completed"`, `summaries.stage1` 기록

#### Stage 2: 도메인 분석 — Sonnet/Opus

**프로바이더**:
- standard context → Sonnet (규모 무관)
- internal deep context → `analysis-pipeline.json`의 `stage_routing.{scale}.stage2`에 따름

1개 에이전트를 실행합니다 (Agent tool, model: sonnet 또는 opus):

```
도메인 '{domain}'의 전체 분석 결과를 통합하여 .ai-context 4개 파일을 생성하세요.

## Stage 1 분석 메모
[.tmp/stage1-analysis.md 내용]

## 도메인 XML 번들
[도메인 번들 내용]

## 기존 딥다이브 문서 (있는 경우)
[16_learning/ 해당 문서 내용]

## 출력 형식

다음 4개 파일을 순서대로 생성하세요. 각 파일은 `===FILE: {filename}===` 구분자로 분리합니다:

===FILE: api-spec.json===
{ "domain": "...", "endpoints": [...], "guards": [...], "dtos": [...] }

===FILE: data-model.md===
# Data Model
## Entities
...
## Redis Patterns
...

===FILE: domain-overview.md===
# {domain} 도메인 개요
## 핵심 기능
...
## 비즈니스 규칙
...

===FILE: external-integration.md===
# 외부 연동
## 서비스 호출
...
## SQS 이벤트
...

각 파일에서:
- Stage 1 메모와 불일치하는 내용이 있으면 소스코드 기준으로 판단
- 기존 딥다이브 문서 대비 누락된 내용이 있으면 명시적으로 기록
```

**종합**: 에이전트 결과를 4개 파일로 파싱 → 📄 `.tmp/stage2-draft.md`

**Stage 2 파싱 실패 시 fallback**:
1. `===FILE:===` 구분자 파싱 실패 → 동일 에이전트에 `"===FILE: {filename}=== 구분자를 정확히 사용하여 4개 파일을 다시 출력하세요."` 재요청 (1회)
2. 재실패 → 4개 파일(`api-spec.json`, `data-model.md`, `domain-overview.md`, `external-integration.md`)을 개별 프롬프트로 분할 실행
3. 개별 실행도 실패 → `analyze.stage2.status = "failed"`, `errorMessage` 기록

**상태 전이**: `analyze.stage2.status = "completed"`, `summaries.stage2` 기록

#### Stage 3: 크로스서비스 분석 — Opus (조건부 실행)

**실행 조건**: internal deep context AND retry block 없음 AND (`stage3_force: true` 또는 `related_domains.length >= 3` 또는 `stage_routing.{scale}.stage3 != "skip"`)

> 기본 모드에서는 항상 생략됩니다.

**프로바이더**: Opus (Agent tool, model: opus)

**입력**: 프로젝트 XML 번들 + Stage 1-2 결과 + 기존 딥다이브

이 단계에서는:
1. Stage 2 초안과 기존 딥다이브를 비교하여 누락/불일치 최종 확인
2. 다른 서비스의 동일 도메인 `.ai-context`가 있으면 크로스서비스 의존성 검증
3. Stage 1-2에서 반복적으로 나타난 패턴을 프로젝트 수준으로 진단

**산출물**: 최종 `.ai-context` 4개 파일 수정사항 + 크로스서비스 의존성 맵 → 📄 `.tmp/stage3-final.md`

**상태 전이**: `analyze.stage3.status = "completed"`, `summaries.stage3` 기록

### Layer 4: Output (문서 생성)

1. **4개 파일 확정**: Stage 2 (또는 Stage 3) 결과를 최종 확정
   - `analysis-docs/{service}/{domain}/.ai-context/api-spec.json`
   - `analysis-docs/{service}/{domain}/.ai-context/data-model.md`
   - `analysis-docs/{service}/{domain}/.ai-context/domain-overview.md`
   - `analysis-docs/{service}/{domain}/.ai-context/external-integration.md`

2. **분석 리포트 생성**: `ANALYSIS_REPORT.md`

   **기본 모드**: 간략 요약
   - API 수, 테이블 수, 비즈니스 규칙 수
   - 사용된 모델 요약

   **Stage 3 실행 시**: 전체 리포트
   - 요약: API 수, 테이블 수, 비즈니스 규칙 수
   - 기존 문서 대비 변경점
   - 크로스서비스 의존성
   - 기술 부채 / 알려진 이슈
   - 사용된 모델 및 토큰 비용 요약

3. **기존 문서 연결**: 해당 딥다이브에 `.ai-context/` 참조 링크 추가

4. **해시 저장**: `.ai-context/.tmp/hashes.json`에 분석된 파일들의 SHA-256 저장 (증분 분석용)

5. **Evaluator 게이트** (`evaluator.enabled`이 true일 때):

   **기본 모드** — lightweight만 실행:
   - `endpoint_exists`: `api-spec.json`의 각 endpoint path가 도메인 XML 번들의 `@Controller`/`@Get`/`@Post` 등에 존재하는지 확인
   - `table_exists`: `data-model.md`의 각 테이블명이 entity 파일의 `@Entity()` 또는 `@Table()` 데코레이터에 존재하는지 확인
   - 하나라도 불일치 → FAIL, 불일치 항목 목록과 함께 Stage 2 1회 재실행 (재프롬프트에 불일치 목록 포함)
   - 모두 일치 → PASS

   **Stage 3 실행 시** — lightweight + full 순차 실행:
   - 먼저 lightweight 검증 실행 (위와 동일)
   - lightweight PASS 후 → Codex(read-only)에게 최종 4개 파일과 기존 딥다이브를 비교 요청
   - PASS → 완료, FAIL → 피드백과 함께 Stage 2 1회 재실행

6. **정리**: `.tmp/` 디렉토리 삭제 (hashes.json 제외)

7. **결과 표시**: 생성된 파일 목록과 요약 통계 출력

**상태 전이**: `layers.output.status = "completed"`, `completedAt` 기록

---

## 상태 파일 스키마

`analysis-docs/{service}/{domain}/.ai-context/.analysis-state.json`:

```json
{
  "id": "analysis-{service}-{domain}-{YYYYMMDD}",
  "service": "{service}",
  "domain": "{domain}",
  "mode": "standard|deep",
  "scale": "small|standard|large",
  "startedAt": "ISO-8601",
  "completedAt": "ISO-8601 (완료 시)",
  "currentLayer": "input|bundle|analyze|output",
  "currentStage": 1,
  "layers": {
    "input":   { "status": "pending|completed" },
    "bundle":  { "status": "pending|completed", "fileCount": 0 },
    "analyze": {
      "stage1": { "status": "pending|in_progress|completed|skipped|failed", "provider": "codex|sonnet", "errorMessage": "", "retryCount": 0 },
      "stage2": { "status": "pending|in_progress|completed|failed", "provider": "sonnet|opus", "errorMessage": "", "retryCount": 0 },
      "stage3": { "status": "pending|in_progress|completed|skipped|failed", "provider": "opus", "reason": "", "errorMessage": "", "retryCount": 0 }
    },
    "output":  { "status": "pending|in_progress|completed|failed", "errorMessage": "" }
  },
  "summaries": {
    "stage1": "파일 수, API 수, 테이블 수, 규칙 수 등 한 줄 요약",
    "stage2": "도메인 합성 결과 한 줄 요약",
    "stage3": "크로스서비스 검증 결과 한 줄 요약 (해당 시)"
  },
  "artifacts": {
    "stage1_memo": ".tmp/stage1-analysis.md",
    "stage2_draft": ".tmp/stage2-draft.md",
    "stage3_final": ".tmp/stage3-final.md"
  }
}
```

---

## 주의사항

- analysis-docs MCP 서버를 통해 문서를 읽고 쓸 수 있습니다
- 소스코드는 로컬 파일시스템에서 직접 읽습니다 (service_map 경로)
- XML Bundling 시 `<content>` 내부는 반드시 XML-escape 처리합니다
- Context 파일은 시그니처 모드 (`mode="signatures"`)로 포함하여 번들 크기를 줄입니다
- Stage 1에서 저비용 프로바이더(기본: Codex)를 사용할 때는 XML 번들 대신 파일 경로를 전달합니다 (Claude Code 경로). awf-cli 경로에서는 파일별 분석 프롬프트를 개별 전송합니다
- 대규모 도메인 (파일 30+)은 Stage 1을 배치로 나누어 실행합니다
- 기존 .ai-context 파일이 있으면 덮어쓰기 전 확인합니다
- **각 스테이지 완료 시 반드시 `.analysis-state.json`을 업데이트**합니다 — 이것이 세션 컴팩션 후 재개를 가능하게 합니다
