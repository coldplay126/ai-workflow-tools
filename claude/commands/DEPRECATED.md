# Deprecated: Commands → Skills

이 디렉토리의 command 파일들은 `claude/skills/`로 마이그레이션되었습니다.
Claude Code에서 commands는 deprecated되었으며, skills가 권장 대체입니다.

## 매핑

| Command | Skill |
|---------|-------|
| `analysis.md` | `skills/analysis/` |
| `wf.md` | `skills/wf/` |
| `wf.approve.md` | `skills/phase-approve/` |
| `wf.discover.md` | `skills/wf-discovery/` |
| `wf.done.md` | `skills/phase-done/` |
| `wf.impl.md` | `skills/phase-impl/` |
| `wf.plan.md` | `skills/phase-plan/` |
| `wf.reset.md` | `skills/wf-reset/` |
| `wf.review.md` | `skills/phase-review/` |
| `wf.status.md` | `skills/wf-status/` |
| `wf.test.md` | `skills/phase-test/` |
| `wf.verify.md` | `skills/phase-verify/` |

## 정리

기존 command 심링크를 삭제하려면:
```bash
rm ~/.claude/commands/wf*.md ~/.claude/commands/analysis.md
```
