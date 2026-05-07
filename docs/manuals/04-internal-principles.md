# AWF AI Workflow 내부 원리서

이 문서는 사용법보다 원리를 설명합니다. 목표는 "왜 이렇게 설계됐는가"를 팀원이 설명할 수 있게 만드는 것입니다.

## 1. 왜 파일 중심인가

이 시스템은 긴 세션 메모리보다 파일 상태를 더 신뢰합니다.

이유:
- provider가 바뀌어도 계약을 유지할 수 있음
- 재개와 검증이 쉬움
- 대화 이력 없이도 상태를 복원할 수 있음

핵심 파일:
- `.workflow/state.json`
- `.workflow/artifacts/*`
- `.workflow/agent-cards/*.json`
- `.ai-context/.analysis-state.json`

## 2. 왜 review와 verify를 나누나

둘은 보는 대상이 다릅니다.

- `review`: 계획의 정합성
- `verify`: 구현의 정합성

즉 review는 "하기 전에 맞는지", verify는 "한 뒤에 맞는지"를 본다고 생각하면 됩니다.

## 3. 왜 agent-card가 필요한가

agent-card는 phase 계약입니다.

들어있는 것:
- 어떤 artifact를 읽을지
- 어떤 출력 형식을 기대하는지
- gate를 어떻게 통과시키는지
- dual strategy를 쓸지

이게 있어야 Claude, Codex, 향후 `awf-cli`가 같은 phase를 같은 뜻으로 실행할 수 있습니다.

## 4. 왜 `.analysis-state.json`이 필요한가

분석은 한 번에 끝나지 않을 수 있습니다.

그래서 다음이 필요합니다.
- 현재 layer/stage
- 실패 여부
- retryCount
- 재사용 가능한 중간 결과

이 파일이 있어야:
- 완료된 분석은 다시 돌리지 않고
- 저장된 Stage 2 결과로 산출물을 복구하고
- retry 한도를 넘긴 실패는 자동 재실행하지 않을 수 있습니다.

## 5. 왜 `.tmp/`가 필요한가

`.tmp/`는 중간 산출물 보관소입니다.

예:
- prompt 파일
- provider raw result
- stage2 draft
- bundle xml

이 파일들은 디버깅, resume, provider 교체 검증에는 유용하지만 최종 산출물은 아닙니다.

## 6. 왜 두 세션이 유리한가

두 세션을 쓰면 책임을 나눌 수 있습니다.

- 세션 A: 판단
- 세션 B: 실행

이렇게 하면:
- 한 세션에 과도한 문맥이 쌓이지 않고
- approve와 impl이 섞이지 않고
- review/verify를 더 독립적으로 볼 수 있습니다.

## 7. 왜 커맨드 없이도 재현 가능해야 하나

도구는 바뀔 수 있지만 코어 계약은 남아야 하기 때문입니다.

그래서 팀원은 다음을 설명할 수 있어야 합니다.
- `/wf`는 어떤 파일을 읽고 어떤 파일을 쓰는가
- `/analysis`는 어떤 입력을 받아 어떤 출력을 만드는가
- gate는 무엇을 기준으로 pass/fail을 판단하는가

이 수준까지 이해해야 자동화가 깨져도 복구할 수 있습니다.

## 8. 팀 내 설명용 한 문장 요약

`ai-workflow-tools`는 "Claude용 명령 모음"이 아니라, workflow와 analysis를 파일 계약으로 외부화하고 Claude/Codex/CLI가 그 계약을 실행하는 구조입니다.
