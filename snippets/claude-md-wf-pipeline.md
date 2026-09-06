## 워크플로우 파이프라인 (wf-orchestrator)

권장 진입점은 `/wf init <기능 설명>`이다. `/wf`, `/wf resume`, `/wf status`,
`/wf reset <action>`도 같은 lifecycle dispatcher를 사용한다.
phase 실행은 dispatcher가 `wf-orchestrator`에 위임한다.
스킬은 `~/.claude/skills/wf*/`, `~/.claude/skills/phase-*/`,
`~/.claude/skills/analysis/`에서 찾는다.

파이프라인은 opt-in이다. 사용자가 `/wf`, `ultracode`, `workflow`로 명시 요청한
작업에만 scope 잠금과 G1~G7 승인이 적용된다. 짧은 일반 작업(버그 수정, 작은
리팩터링, 문서 수정)에 `workflow-init`이나 7단계 승인을 강제하지 않는다.
구현 host는 부여받은 write scope 안에서 파일을 수정·검증하고 개발 branch에
일반 commit/non-force push를 수행한다. 커밋 권한은 main merge·production
deploy 권한과 분리되며, 승격·배포·branch/worktree 삭제는
`release-worktree-lifecycle` 스킬을 따른다.

### Deterministic Preflight

처음 시작: `awf ready --gate workflow-init --repo-root . --json`
중간 재개: `awf ready --gate workflow-run --repo-root . --json`

- exit code `0`: 진행
- exit code `10`: dry-run/status만 진행
- exit code `20`: 중단 후 `gate.recommended_next` 수행

### Phase 흐름
plan → review → approve → impl → verify → test → done

### Dual Mode
review/verify Phase에서 Codex MCP를 secondary worker로 사용 가능.
impl Phase에서 `implement_then_review`로 post-impl 코드 리뷰 가능.
프로젝트별 `.workflow/provider-config.json`으로 설정.

### Codex Slave 규칙 (AGENTS.md 역할)

Codex가 hashtag protocol의 직접 분석 Slave로 호출될 때:
- sandbox `read-only`
- 응답은 **반드시 valid JSON**
- 필드: `conclusion`, `findings`, `evidence`, `risks`, `action_items`
- Markdown fence, 설명 텍스트, preamble 없이 `{`로 시작하여 `}`로 끝남

`awf wf`의 phase provider sandbox는 이 direct-slave 규칙과 별도다.
review/verify는 read-only이며, 쓰기가 필요한 plan/impl/test delegated 실행은
workspace-write를 사용한다. approve/done HIL은 parent가 소유한다. read-only는
리뷰/분석 worker에만 적용되고 구현 host/implementer에는 적용되지 않는다.
