# 실제 레포 필드 트라이얼 체크리스트

이 문서는 `ai-workflow-tools`를 실제 팀 repo에 한 번 적용해 보고 계속 쓸 만한지
판정하기 위한 짧은 체크리스트입니다. 목표는 기능을 많이 실행하는 것이 아니라,
read-only와 dry-run evidence만으로 "이 도구가 이 repo에서 도움이 되는가"를
판단하는 것입니다.

영어 버전: [Field Trial Checklist for Real Repositories](./10-field-trial-checklist.en.md)

## 0. 먼저 정할 것

필드 트라이얼은 30-60분 안에 끝나는 작은 작업으로만 시작하세요.

좋은 후보:

- 처음 맡은 Python/TypeScript repo에서 분석 단위를 파악하는 작업
- 작은 기능 작업의 plan/review/verify 흐름을 먼저 확인하는 작업
- Claude Code 작업 전에 Codex로 review/verify prompt만 확인하는 작업

피해야 할 후보:

- README 오타 하나처럼 workflow 비용이 더 큰 작업
- 제품 방향 합의가 아직 안 된 작업
- 바로 고쳐야 하는 긴급 hotfix
- 테스트 명령이 전혀 정해지지 않은 repo

## 1. 트라이얼 기록

아래 항목을 먼저 채웁니다.

| 항목 | 값 |
|------|----|
| repo / branch |  |
| 작업 설명 |  |
| 기존 테스트 명령 |  |
| 사용할 host | Claude Code / Codex CLI / local CLI |
| provider 호출 여부 | 없음 / dry-run만 / 실제 호출 |
| cmux-agent 또는 Pi 사용 여부 | 사용 안 함 / cmux / Pi |

처음에는 provider 호출 없이 진행하는 것이 좋습니다. Pi는 기본 경로가 아니므로,
Pi를 평가할 때만 별도 field-smoke를 실행합니다.

## 2. 결정론적 preflight

repo 루트에서 아래 명령을 실행합니다.

```bash
awf ready --repo-root .
awf scan . --no-ai
awf analyze <service> <unit> --repo-root . --dry-run --output-format json
awf wf init "small scoped improvement" --repo-root .
awf ready --repo-root . --gate workflow-run --json
awf wf next --repo-root . --dry-run --output-format json
```

판정 기준:

| 확인 | 계속 진행 가능 | 멈출 신호 |
|------|----------------|-----------|
| `ready` | automation level과 다음 명령이 이해된다 | block 이유가 불명확하다 |
| `scan` | 실제 소스 단위를 service/unit으로 잡는다 | 테스트/문서 폴더만 잡거나 핵심 단위를 놓친다 |
| `analyze --dry-run` | `domain_directories`가 의도한 코드 위치다 | 엉뚱한 경로나 빈 경로를 가리킨다 |
| `wf init` | `.workflow/state.json`이 작업 설명과 맞다 | scope가 너무 넓거나 모호하다 |
| `ready --gate workflow-run` | gate decision이 실행 가능 여부를 분명히 말한다 | gate가 왜 막는지 설명하지 못한다 |
| `wf next --dry-run` | phase, provider, prompt가 실행 전 검토 가능하다 | prompt가 작업과 맞지 않거나 artifact가 불분명하다 |

여기서 두 개 이상 멈출 신호가 나오면 provider-backed 실행으로 넘어가지 마세요.
먼저 repo 구조, `.awf.toml`, 작업 설명, 테스트 명령을 정리합니다.

## 3. 선택 사항: Pi 또는 cmux-agent 평가

Pi와 cmux-agent는 기본 사용 경로가 아니라 실행 surface입니다. 일반적인 첫
트라이얼에서는 없어도 됩니다.

Pi를 실제로 평가할 때만 최신 field-smoke evidence를 남깁니다.

```bash
python3 cli/tests/run_pi_field_smoke.py --json --write-result
awf doctor --repo-root . --json
awf ready --repo-root .
```

판정 기준:

- quota/auth 문제로 실패하면 Pi 평가를 중단하고 기본 CLI/Codex/Claude 흐름으로
  돌아갑니다.
- `ready`가 최신 field-smoke 결과를 읽으면 Pi dispatch 가능성을 검토합니다.
- Pi가 비용, quota, 지연시간, 디버깅 난이도 중 하나라도 악화시키면 기본 경로로
  두는 편이 낫습니다.

## 4. 트라이얼 결과 템플릿

트라이얼이 끝나면 아래 형식으로 판단을 남깁니다.

```markdown
## ai-workflow-tools field trial

- repo / branch:
- task:
- host:
- provider calls:
- test command:

### Commands run

- [ ] awf ready --repo-root .
- [ ] awf scan . --no-ai
- [ ] awf analyze <service> <unit> --repo-root . --dry-run --output-format json
- [ ] awf wf init "..."
- [ ] awf ready --repo-root . --gate workflow-run --json
- [ ] awf wf next --repo-root . --dry-run --output-format json

### Evidence

- ready recommendation:
- scan service/unit:
- analysis dry-run paths:
- workflow dry-run phase/provider:
- confusing output:
- missing guard:

### Decision

- keep using / use only for analysis / use only for review-verify / do not use here:
- reason:
- next small improvement:
```

## 5. 완료 예시

```markdown
## ai-workflow-tools field trial

- repo / branch: billing-api / trial-awf-readiness
- task: record retry reason in failed payment logs
- host: Codex CLI
- provider calls: dry-run only
- test command: pytest tests/payments -q

### Commands run

- [x] awf ready --repo-root .
- [x] awf scan . --no-ai
- [x] awf analyze billing-api payments --repo-root . --dry-run --output-format json
- [x] awf wf init "record retry reason in failed payment logs"
- [x] awf ready --repo-root . --gate workflow-run --json
- [x] awf wf next --repo-root . --dry-run --output-format json

### Evidence

- ready recommendation: workflow-run gate allowed after init
- scan service/unit: billing-api / payments
- analysis dry-run paths: src/payments
- workflow dry-run phase/provider: plan / fixture
- confusing output: none
- missing guard: test command is not documented in repo

### Decision

- keep using: yes, for plan/review/verify on payment changes
- reason: scan found the right unit and dry-run prompt was reviewable before execution
- next small improvement: document payment test command in repo README
```

## 6. 판정 규칙

- `keep using`: scan 단위와 dry-run prompt가 맞고, gate가 실행 전 위험을 줄였다.
- `use only for analysis`: `.ai-context` 생성은 유용하지만 workflow는 과하다.
- `use only for review-verify`: 구현 전후 교차 검증에는 유용하지만 plan부터
  시작할 필요는 없다.
- `do not use here`: 작업이 너무 작거나 repo 구조/테스트가 아직 정리되지 않았다.

## 다음 문서

- [처음 쓰는 개발자를 위한 온보딩 가이드](./09-colleague-onboarding.ko.md)
- [첫 ai-workflow-tools 작업 흐름](./08-first-workflow.ko.md)
- [Pi Field Validation](./pi-field-validation.md)
