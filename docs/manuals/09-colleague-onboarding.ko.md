# 처음 쓰는 개발자를 위한 온보딩 가이드

이 문서는 `ai-workflow-tools`를 처음 여는 개발자를 위한 짧은 안내입니다.
먼저 도구가 맞는 상황인지 판단하고, 작은 repo에서 provider 호출 없이
15분 안에 첫 dry-run까지 확인하는 것이 목표입니다.

영어 버전: [Onboarding Guide for First-Time Developers](./09-colleague-onboarding.en.md)

## 먼저 한 문장으로 이해하기

`ai-workflow-tools`는 Claude Code, Codex CLI, local CLI 작업을 대화
히스토리에만 맡기지 않고, repo 상태 점검, deterministic scan, dry-run
prompt, gated workflow artifact로 남기면서 진행하게 해주는 AI 작업
안전장치입니다.

## 이럴 때 쓰세요

- 처음 보는 repo에서 AI에게 어느 파일과 단위를 보게 할지 정해야 할 때
- 기능 작업을 `plan -> review -> approve -> impl -> verify -> test -> done`
  단계로 쪼개고 싶을 때
- Claude Code와 Codex CLI가 같은 `.workflow` 상태를 보게 하고 싶을 때
- provider를 실제 호출하기 전에 prompt, phase, artifact 경로를 확인하고 싶을 때
- `.ai-context` 문서를 만들어 repo 이해도를 누적하고 싶을 때
- review/verify를 다른 provider나 runner surface로 교차 확인하고 싶을 때

## 이럴 때는 쓰지 마세요

- 한두 줄짜리 명확한 수정
- 요구사항보다 사람 간 합의가 먼저 필요한 초기 논의
- 테스트 없이 바로 고쳐야 하는 긴급 hotfix
- workflow 산출물을 남기는 비용이 작업보다 큰 경우

## 첫 15분에 해볼 것

작은 Python/TypeScript repo 또는 subproject에서 시작하세요. 처음에는
provider를 호출하지 말고 아래까지만 실행합니다.

```bash
awf ready --repo-root .
awf scan . --no-ai
awf analyze <service> <unit> --repo-root . --dry-run --output-format json
awf wf init "small scoped improvement" --repo-root .
awf ready --repo-root . --gate workflow-run --json
awf wf next --repo-root . --dry-run --output-format json
```

확인할 것:

- `ready`가 다음 추천 명령을 설명하는가
- `scan`이 납득 가능한 service/unit을 찾는가
- `analyze --dry-run`의 `domain_directories`, `all_directories`,
  `ai_context_dir`가 이해되는가
- `wf next --dry-run`의 `phase`, `provider`, `prompt`가 실제 실행 전에
  검토 가능한가

여기까지의 출력이 이해되지 않으면 provider-backed 실행으로 넘어가지 마세요.

## 상황별 예시

### 예시 A: 처음 보는 Python script repo

`collectors/`, `analyzers/`, `importers/` 같은 root-level 디렉토리가 있는
repo를 처음 맡았다면 먼저 read-only 명령으로 분석 단위를 확인하세요.

```bash
awf ready --repo-root .
awf scan . --no-ai
awf analyze collectors naver --repo-root . --dry-run --output-format json
```

이렇게 판단합니다:

- `scan`이 실제 소스 디렉토리를 unit으로 잡으면 분석 후보가 맞습니다.
- dry-run의 `domain_directories`가 의도한 폴더를 가리키면 provider 실행을
  고려합니다.
- unit 이름이 맞지 않으면 provider를 부르기 전에 `scan` 결과에서 다른 unit을
  고릅니다.

### 예시 B: 작은 기능 작업을 workflow로 시작

"결제 실패 로그에 retry reason을 남긴다"처럼 범위가 작은 기능은 workflow로
시작해도 좋습니다.

```bash
awf wf init "record retry reason in payment failure logs" --repo-root .
awf ready --repo-root . --gate workflow-run --json
awf wf next --repo-root . --dry-run --output-format json
```

이렇게 판단합니다:

- `phase`가 `plan`이면 아직 구현이 아니라 기획 산출물을 만드는 단계입니다.
- prompt가 너무 넓으면 concept를 더 좁혀 다시 시작합니다.
- `.workflow/state.json`은 이 작업의 canonical state입니다.

### 예시 C: 구현 전 review/verify만 Codex로 확인

Claude Code로 작업하되 review나 verify는 Codex 시각으로 한 번 더 보고
싶다면 Codex adapter를 사용하세요.

```bash
../ai-workflow-tools/codex/run-wf.sh preflight review codex
../ai-workflow-tools/codex/run-wf.sh prompt review codex
```

이렇게 판단합니다:

- `preflight`가 실패하면 Codex 실행보다 `ready`의 추천 명령을 먼저 따릅니다.
- `prompt` 파일을 읽어도 review 목적과 artifact가 분명해야 합니다.
- cmux-agent나 Pi가 없어도 이 흐름은 동작해야 합니다.

### 예시 D: 쓰지 않는 편이 나은 작업

README 오타 하나를 고치거나 import 정렬만 바꾸는 작업에는 AWF를 쓰지 않는
편이 낫습니다.

권장:

```bash
git diff
pytest <관련 테스트>
```

이렇게 판단합니다:

- `.workflow`를 만드는 비용이 수정 자체보다 크면 쓰지 않습니다.
- 단순 수정은 일반 개발 흐름으로 끝내고, 나중에 반복되는 패턴이 보이면
  `awf wiki decision`이나 문서화만 고려합니다.

## 자주 묻는 질문

질문: "이거 쓰면 AI가 알아서 구현해주는 건가요?"

답변:

> 아닙니다. AI 작업을 바로 실행하지 않고, 먼저 repo 상태와 분석 단위를
> 확인한 뒤 dry-run prompt를 보고 단계별로 진행하게 해주는 도구입니다. 작은
> 수정에는 과하지만, 낯선 repo 분석이나 review/verify가 필요한 기능 작업에
> 좋습니다.

## Claude Code에서 쓰기

Claude Code를 쓴다면 `setup.sh`로 skill을 설치한 뒤 skill 진입점을 사용합니다.

```bash
./setup.sh
```

대표 진입점:

```text
/analysis
/wf-status
/wf-orchestrator
```

Claude skill도 먼저 `awf ready`와 dry-run JSON을 읽고, 실행 가능할 때만
provider-backed 작업으로 넘어가는 방향을 따릅니다.

## Codex CLI에서 쓰기

Codex CLI를 쓴다면 Claude skill UX를 그대로 복제하지 않고, `awf` CLI와 Codex
adapter를 사용합니다.

```bash
../ai-workflow-tools/codex/run-wf.sh preflight review codex
../ai-workflow-tools/codex/run-wf.sh prompt review codex
```

`preflight`는 `awf ready --gate workflow-run`과
`awf wf next --dry-run --output-format json` 계약을 확인합니다.

## 오해하지 말아야 할 점

- AI가 알아서 개발해주는 도구가 아닙니다.
- 모든 작업에 무조건 쓰는 도구가 아닙니다.
- cmux-agent나 Pi가 기본 경로는 아닙니다. 둘 다 optional 실행 surface입니다.

## 핵심으로 기억할 말

> AI 코딩을 대화 히스토리에만 맡기지 않고, repo-local artifact와 dry-run,
> gate로 검증하면서 진행하게 해주는 도구입니다. 작은 수정에는 과하지만,
> 낯선 repo 분석, 기능 작업, review/verify에는 쓸만합니다.

## 다음 문서

- [실제 레포 필드 트라이얼 체크리스트](./10-field-trial-checklist.ko.md)
- [Field Trial Checklist for Real Repositories](./10-field-trial-checklist.en.md)
- [첫 ai-workflow-tools 작업 흐름](./08-first-workflow.ko.md)
- [First Workflow](./08-first-workflow.en.md)
- [AWF AI Workflow 입문 가이드](./01-getting-started.md)
- [Codex Portability Guide](./codex-portability.md)
