# 동료 개발자 온보딩 가이드

이 문서는 `ai-workflow-tools`를 옆자리 동료에게 설명하고, 작은 repo에서
처음 15분 동안 함께 확인할 때 쓰는 짧은 안내입니다.

영어 버전: [Colleague Onboarding Guide](./09-colleague-onboarding.en.md)

## 한 문장 설명

`ai-workflow-tools`는 Claude Code, Codex CLI, local CLI 작업을 그냥 대화로
흘려보내지 않고, repo 상태 점검, deterministic scan, dry-run prompt, gated
workflow artifact로 남기면서 진행하게 하는 AI 작업 안전장치입니다.

## 언제 쓰면 좋은가

- 처음 보는 repo에서 AI에게 어느 파일과 단위를 보게 할지 정해야 할 때
- 기능 작업을 `plan -> review -> approve -> impl -> verify -> test -> done`
  단계로 쪼개고 싶을 때
- Claude Code와 Codex CLI가 같은 `.workflow` 상태를 보게 하고 싶을 때
- provider를 실제 호출하기 전에 prompt, phase, artifact 경로를 확인하고 싶을 때
- `.ai-context` 문서를 만들어 repo 이해도를 누적하고 싶을 때
- review/verify를 다른 provider나 runner surface로 교차 확인하고 싶을 때

## 굳이 안 써도 되는 경우

- 한두 줄짜리 명확한 수정
- 요구사항보다 사람 간 합의가 먼저 필요한 초기 논의
- 테스트 없이 바로 고쳐야 하는 긴급 hotfix
- workflow 산출물을 남기는 비용이 작업보다 큰 경우

## 첫 15분 실습

작은 Python/TypeScript repo 또는 subproject에서 provider 호출 없이 아래까지만
실행합니다.

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

이 단계가 이해되지 않으면 provider-backed 실행으로 넘어가지 않습니다.

## Claude Code에서 쓰기

Claude Code에서는 `setup.sh`로 skill을 설치한 뒤 skill 진입점을 사용합니다.

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

Codex에서는 Claude skill UX를 그대로 복제하지 않고, `awf` CLI와 Codex
adapter를 사용합니다.

```bash
../ai-workflow-tools/codex/run-wf.sh preflight review codex
../ai-workflow-tools/codex/run-wf.sh prompt review codex
```

`preflight`는 `awf ready --gate workflow-run`과
`awf wf next --dry-run --output-format json` 계약을 확인합니다.

## 설명할 때 피해야 할 말

- "AI가 알아서 개발해준다"라고 설명하지 않습니다.
- "모든 작업에 무조건 써야 한다"라고 말하지 않습니다.
- cmux-agent나 Pi를 기본 경로처럼 설명하지 않습니다. 둘 다 optional 실행
  surface입니다.

## 설명할 때 좋은 표현

> AI 코딩을 대화 히스토리에만 맡기지 않고, repo-local artifact와 dry-run,
> gate로 검증하면서 진행하게 해주는 도구입니다. 작은 수정에는 과하지만,
> 낯선 repo 분석, 기능 작업, review/verify에는 쓸만합니다.

## 다음 문서

- [첫 ai-workflow-tools 작업 흐름](./08-first-workflow.ko.md)
- [First Workflow](./08-first-workflow.en.md)
- [AWF AI Workflow 입문 가이드](./01-getting-started.md)
- [Codex Portability Guide](./codex-portability.md)
