# AWF AI Workflow 커맨드 없이 재현하기

이 문서는 `wf-*` skills, `/analysis` 같은 진입점 없이도 같은 흐름을 프롬프트로 재현하는 방법을 설명합니다.

목표:
- slash command/skill이 실제로 무엇을 하는지 이해한다.
- 자동화가 깨져도 수동으로 복구할 수 있다.
- 내부 계약을 이해한 상태에서 provider를 바꿔도 대응할 수 있다.

## 1. 기본 원리

커맨드는 보통 아래 일을 대신 합니다.

- 관련 상태 파일 찾기
- 입력 artifact 모으기
- phase에 맞는 prompt 만들기
- 결과를 정해진 파일에 반영하기

즉 수동 재현은 "같은 입력과 출력 계약"만 지키면 됩니다.

## 2. workflow 계획 단계 재현

읽을 파일:
- `.workflow/concept.md`
- `.workflow/manifest.json`

예시 프롬프트:

```text
`.workflow/concept.md`와 `.workflow/manifest.json`을 읽고 다음 산출물을 작성하세요.

1. `.workflow/artifacts/spec.md`
2. `.workflow/artifacts/plan.md`
3. `.workflow/artifacts/tasks.md`
4. `.workflow/artifacts/allowed-files.json`

요구사항:
- spec에는 user story, requirements, acceptance scenarios 포함
- plan은 구현 전략과 검증 방향 포함
- tasks는 파일 경로와 task ID 포함
- allowed-files의 `planned_files`는 tasks에서 추출. plan 직후 `awf wf expand-scope --direction dependents`를 실행해 `expanded_files`/`graph_expansion`을 기본으로 채운다. 분석된 import graph가 없으면 정상 no-op이며, graph가 있으면 verify 단계의 G5 false positive를 줄인다.
- 기존 `.workflow` 계약과 호환되게 작성
```

## 3. review 단계 재현

읽을 파일:
- `.workflow/artifacts/spec.md`
- `.workflow/artifacts/plan.md`
- `.workflow/artifacts/tasks.md`
- `.workflow/agent-cards/review.json`

예시 프롬프트:

```text
다음 파일을 읽고 review를 수행하세요.
- `.workflow/artifacts/spec.md`
- `.workflow/artifacts/plan.md`
- `.workflow/artifacts/tasks.md`
- `.workflow/agent-cards/review.json`

목표:
- 요구사항 누락
- 모호한 수용 조건
- task coverage gap
- 구현 범위 불명확성

출력:
1. `.workflow/artifacts/review-report.md`
2. findings JSON 요약

severity는 `CRITICAL|HIGH|MEDIUM|LOW`를 사용하세요.
gate 판단은 `review.json`의 `gate.pass_conditions`를 따르세요.
```

## 4. verify 단계 재현

읽을 파일:
- `.workflow/artifacts/spec.md`
- `.workflow/artifacts/tasks.md`
- `.workflow/artifacts/allowed-files.json`
- 현재 git diff
- `.workflow/agent-cards/verify.json`

예시 프롬프트:

```text
다음 기준으로 구현을 검증하세요.
- `.workflow/artifacts/spec.md`
- `.workflow/artifacts/tasks.md`
- `.workflow/artifacts/allowed-files.json`
- 현재 git diff
- `.workflow/agent-cards/verify.json`

검증 항목:
- scope violation — `awf wf scope-check --json`을 먼저 실행해 결정론적 분류(planned/expanded/violation)를 받고, 위반된 path는 명령의 `reason` 필드를 그대로 인용한다. 수동으로 git diff와 allowed-files를 비교하지 않는다.
- requirement 미구현
- compliance fail
- quality critical issue

출력:
1. `.workflow/artifacts/verification-report.md`
2. findings JSON
3. G5 pass/fail 판단
```

## 5. `/analysis` 재현

읽을 파일:
- `claude/skills/analysis/SKILL.md`
- `docs/specs/ai-context-specification.md`
- `analysis-docs/_templates/analysis-config.json`
- `analysis-docs/_templates/analysis-pipeline.json`

예시 프롬프트:

```text
다음 도메인 소스코드를 분석해 `.ai-context` 4개 파일을 생성하세요.

입력:
- service: sample-api
- domain: quest-challenge
- mode: standard
- target: `analysis-docs/sample-api/quest-challenge/.ai-context`

출력 파일:
- `api-spec.json`
- `data-model.md`
- `domain-overview.md`
- `external-integration.md`

제약:
- `.analysis-state.json` resume 규칙을 따른다
- source of truth는 `analysis.md`와 `ai-context-specification.md`
- 결과가 크면 `===FILE: <name>===` 구분자로 반환한다
```

## 6. 수동 재현에서 꼭 지켜야 할 것

- 결과를 대화에만 두지 말고 파일에 반영한다.
- gate 판단 근거를 남긴다.
- `.tmp/`와 최종 산출물을 구분한다.
- state 파일 업데이트를 빼먹지 않는다.

## 7. 이 문서를 끝내면 가능한 설명

- "`/phase-review`는 사실 spec/plan/tasks와 review agent-card를 모아 검토 prompt를 만드는 일이다."
- "`/analysis`는 입력 문서와 소스 디렉토리 맵을 읽고 `.ai-context` 계약에 맞춰 출력하는 파이프라인이다."
