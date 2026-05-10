# AWF AI Workflow 입문 가이드

이 문서는 `ai-workflow-tools`를 처음 사용하는 팀원을 위한 30분 입문 가이드입니다.

목표:
- `wf-*` skills, `/analysis`, `awf-cli`가 어떤 역할을 하는지 이해한다.
- 상태 파일과 산출물 위치를 직접 확인한다.
- "자동화가 실제로 어떤 파일을 읽고 쓰는지"를 감으로 익힌다.

관련 문서:
- [첫 ai-workflow-tools 작업 흐름](./08-first-workflow.ko.md)
- [First Workflow](./08-first-workflow.en.md)
- [WF 빠른 시작](./wf-quickstart.md)
- [WF 아키텍처](../architecture/02-wf-pipeline.md)
- [.ai-context 사양](../specs/ai-context-specification.md)
- [awf-cli README](../../cli/README.md)

> **참고**: `claude/commands/`는 2026-02 폐기되어 `claude/commands/DEPRECATED.md`만 남아 있습니다. 모든 기능이 `claude/skills/` (phase-*, wf-*, analysis, multi-agent, analysis-docs)로 이전됐습니다.

## 1. 핵심 개념

이 레포의 자동화는 대화 히스토리보다 파일 계약을 중심으로 동작합니다.

- `.workflow/`: 기능 개발 workflow 상태와 산출물
- `.workflow/agent-cards/`: phase별 입력, 출력, gate 조건
- `.ai-context/`: 도메인 분석 산출물
- `.analysis-state.json`: 분석 파이프라인 재개 상태

즉 "에이전트가 뭔가 기억해서 이어간다"보다 "파일에 남은 상태를 읽고 이어간다"가 더 정확합니다.

## 2. 준비

레포 루트:

```bash
cd ~/Documents/GitHub/ai-workflow-tools
```

Claude 중심 설치가 되어 있다면:

```text
/wf-status
```

Python CLI를 같이 보고 싶다면:

```bash
uv run --project cli awf ready --repo-root .
uv run --project cli awf ready --repo-root . --gate workflow-init --json
```

`ready`는 설정, provider, skill, scan, workflow, operations wiki 상태를 읽기 전용으로 모아 현재 repo에서 어느 자동화 레벨까지 안전한지 보여줍니다.
`--gate`는 Claude/Codex가 첫 실행 전에 따를 수 있는 결정론적 preflight이며, `allow`가 아니면 non-zero exit로 진행을 멈춥니다.

## 3. 실습 A: workflow 상태 읽기

먼저 현재 workflow 상태를 봅니다.

Claude:

```text
/wf-status
```

CLI:

```bash
uv run --project cli awf wf status --repo-root .
```

확인할 것:
- 현재 phase
- gate 상태
- 최근 history

직접 열어볼 파일:
- `.workflow/state.json`

체크 질문:
- `currentPhase`는 무엇인가?
- `gates`는 어떤 시점에 채워지는가?
- `history`는 어떤 실행 흔적을 남기는가?

## 4. 실습 B: `awf wf next`가 만드는 것 보기

실제 provider를 부르지 않고 prompt만 확인합니다.

```bash
uv run --project cli awf wf next --repo-root . --phase review --provider codex --dry-run
```

실행 후 확인할 파일:
- `.workflow/tmp`

핵심 포인트:
- `wf next`는 phase를 정하고
- agent-card를 읽고
- self-contained prompt를 만들고
- 그 prompt를 provider에 넘깁니다.

즉 `wf-*` skill과 `awf wf`는 "마법 명령"이 아니라 "상태 + 계약 + 프롬프트 생성기"입니다.

## 5. 실습 C: `/analysis` 드라이런 보기

도메인 분석 prompt가 어떤 입력을 받는지 확인합니다.

```bash
uv run --project cli awf analyze sample-api quest-challenge --repo-root . --dry-run
```

확인할 것:
- `domain_directories`
- `all_directories`
- `related_domains`
- `.ai-context` 대상 경로

체크 질문:
- 왜 `domain_directories`와 `all_directories`가 둘 다 필요한가?
- 왜 `.analysis-state.json`이 필요한가?

## 6. 산출물 위치 감각 익히기

Workflow 관련:
- `.workflow/state.json`
- `.workflow/artifacts`
- `.workflow/agent-cards`

Analysis 관련:
- [analysis skill](../../claude/skills/analysis/)
- [ai-context-specification.md](../specs/ai-context-specification.md)

## 7. 기대 결과

이 문서를 끝내면 다음 설명이 가능해야 합니다.

- "`wf-*` skill과 `awf wf`는 `.workflow`를 읽고 phase별 prompt를 만드는 흐름이다."
- "`/analysis`는 `.ai-context`와 `.analysis-state.json`을 남기는 분석 파이프라인이다."
- "이 자동화는 세션 메모리보다 파일 상태를 더 신뢰한다."

## 8. 다음 단계

다음 문서로 이어서 보세요.

- [두 세션 운영 실습](./02-two-session-workflow.md)
- [첫 ai-workflow-tools 작업 흐름](./08-first-workflow.ko.md)
- [First Workflow](./08-first-workflow.en.md)
- [커맨드 없이 재현하기](./03-manual-prompt-reproduction.md)
- [내부 원리서](./04-internal-principles.md)
- [실환경 검증 체크리스트](./05-live-validation.md)
