# Codex Adapter

## 개요

이 디렉토리는 Claude 중심으로 설계된 WF 파이프라인과 멀티에이전트 프로토콜을 Codex에서 실행하기 위한 어댑터 계층입니다.

공통 코어는 `.workflow/`에 있고, Codex 쪽에서는 아래만 담당합니다.

- 현재 phase 읽기
- provider-config 기반 실행 모드 결정
- Claude CLI fallback 호출
- delegated JSON 응답 정규화

## 파일

| 파일 | 역할 |
|------|------|
| `AGENTS.md` | Codex delegated worker 규칙 |
| `run-wf.sh` | Codex host용 최소 runner |
| `templates/provider-config.codex-primary.json` | Codex host 기본 provider 예시 |

## 사용 예시

```bash
cd ~/Documents/GitHub/<repo>
../ai-workflow-tools/codex/run-wf.sh status
../ai-workflow-tools/codex/run-wf.sh dispatch
../ai-workflow-tools/codex/run-wf.sh phase review
../ai-workflow-tools/codex/run-wf.sh prompt review claude:sonnet
../ai-workflow-tools/codex/run-wf.sh run-secondary review claude:sonnet
```

## 설계 원칙

- `inline`은 Codex host local execution을 뜻한다.
- `delegated`는 외부 provider 호출을 뜻한다.
- `dual`은 Codex local 결과와 secondary provider 결과를 병합한다.
- approve/done은 자동 처리하지 않는다.

## Claude fallback

Codex runner는 `provider-config.json`의 `fallback_chain`을 보고 다음 provider를 선택할 수 있습니다.

예:

```json
{
  "fallback_chain": ["claude:sonnet", "codex"]
}
```

이 경우 Codex host에서 Claude CLI를 먼저 secondary/fallback으로 사용합니다.

현재 `run-wf.sh`는 다음까지 지원합니다.

- `awf ready --gate workflow-run` preflight
- `.workflow` 상태 읽기
- phase별 secondary provider 해석
- delegated self-contained prompt 생성
- Claude CLI provider 실행 결과 저장

아직 지원하지 않는 것:

- `verification-report.md` 같은 artifact 자동 반영
- dual merge / format retry 자동화
