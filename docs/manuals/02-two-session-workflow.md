# AWF AI Workflow 두 세션 운영 실습

이 문서는 "기획 세션"과 "실행 세션"을 나눠 자동화를 운영하는 방법을 설명합니다.

권장 역할:
- 세션 A: 기획/판단
- 세션 B: 실행/산출물 생성

선택 역할:
- 세션 C: 검증/Codex

핵심 원칙:
- 한 세션이 모든 걸 다 하지 않는다.
- 결정과 실행을 나누면 컨텍스트가 단순해진다.
- 두 세션은 대화가 아니라 파일 상태를 통해 협업한다.

## 1. 언제 두 세션으로 나누나

이 패턴이 특히 유용한 경우:
- 요구사항 정리가 필요한 기능 작업
- review/verify를 분리하고 싶은 경우
- Claude host와 Codex 검증을 병행하는 경우
- `/analysis` 결과를 바탕으로 문서 반영을 따로 하는 경우

## 2. 시나리오 A: 기능 workflow

### 세션 A: 기획

목표:
- 요구사항 정리
- spec/plan/tasks 확인
- approve 판단

예시:

```text
/wf 'README 개편'
```

확인할 파일:
- `.workflow/concept.md`
- `.workflow/artifacts/spec.md`
- `.workflow/artifacts/plan.md`
- `.workflow/artifacts/tasks.md`

세션 A가 하는 질문:
- 요구사항이 빠졌나?
- task가 spec을 충분히 덮나?
- approve해도 되는가?

### 세션 B: 실행

목표:
- 현재 phase 확인
- impl/verify/test 실행
- 결과 산출물 반영

CLI 예시:

```bash
uv run --project cli --no-editable awf wf status --repo-root .
uv run --project cli --no-editable awf wf next --repo-root . --phase review --provider codex --dry-run
```

세션 B가 확인할 것:
- 현재 phase
- delegated prompt
- report artifact

## 3. 시나리오 B: 분석과 문서화 분리

### 세션 A: 분석

```text
/analysis sample-api quest-challenge
```

또는:

```bash
uv run --project cli --no-editable awf analyze sample-api quest-challenge --repo-root . --dry-run
```

세션 A 산출물:
- `.ai-context/` 4개 파일
- `.analysis-state.json`

### 세션 B: 문서 반영

세션 B는 `.ai-context`를 읽고:
- 딥다이브 문서 갱신
- 중복 문서 통합
- 참조 링크 정리

즉 세션 A는 "분석", 세션 B는 "편집/정리"를 담당합니다.

## 4. 시나리오 C: Claude / Codex 분업

세션 A:
- Claude Code에서 `/wf`, `/analysis`
- 필요하면 선택적으로 `awf analyze ... --provider claude-sdk --yolo`

세션 B:
- Codex에서 review/verify
- 또는 `awf wf next --provider codex`

이 구조의 장점:
- 같은 결과를 다른 provider로 검증 가능
- 한쪽 세션이 길어져도 다른 쪽은 짧은 read-only 검토에 집중 가능

권장 순서:
1. 세션 A에서 Claude Code로 spec/plan 또는 analysis 초안 생성
2. 세션 B에서 Codex로 review/verify prompt 실행
3. 세션 A가 결과를 보고 승인 또는 수정 방향 결정

CLI 예시:

```bash
uv run --project cli --no-editable awf wf next --repo-root . --phase review --provider codex --auto-apply
```

선택 실험 경로:

```bash
uv run --project cli --no-editable awf analyze sample-api quest-challenge --repo-root . --provider claude-sdk --yolo
```

## 5. 체크포인트

두 세션 운영이 잘 되고 있다면:
- 세션 A는 "무엇을 할지"에 집중한다.
- 세션 B는 "어떻게 반영할지"에 집중한다.
- 둘 다 같은 `.workflow`와 `.ai-context`를 본다.
- 구두 공유보다 파일 상태 공유가 중심이다.

## 6. 흔한 실수

- 세션 A와 B가 같은 파일을 동시에 임의 수정
- approve 전에 impl부터 진행
- review/verify 결과를 state에 반영하지 않고 대화로만 소비
- `.tmp/` 파일을 최종 산출물처럼 취급

## 7. 다음 단계

두 세션 운영에 익숙해지면, 다음 문서에서 같은 흐름을 커맨드 없이 직접 재현해 볼 수 있습니다.

- [커맨드 없이 재현하기](./03-manual-prompt-reproduction.md)
