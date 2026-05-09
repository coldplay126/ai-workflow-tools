## 워크플로우 파이프라인 (wf-orchestrator)

7단계 게이트 파이프라인. `/wf-orchestrator '기능 설명'`으로 시작 (과거 `/wf` alias는 2026-02 commands 폐기 시 제거됨).
스킬: `~/.claude/skills/wf-*/`, `~/.claude/skills/phase-*/`, `~/.claude/skills/analysis/`

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

Codex가 Slave로 호출될 때:
- sandbox `read-only` 환경에서 실행
- 응답은 **반드시 valid JSON**으로 반환
- 4-Block 구조: `conclusion`, `evidence`, `risks`, `action_items`
- Markdown fence, 설명 텍스트, preamble 없이 `{`로 시작하여 `}`로 끝남
