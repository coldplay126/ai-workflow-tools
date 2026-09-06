# AWF Deterministic Preflight

Before using an ai-workflow-tools workflow or analysis feature from Codex (`awf analyze`, `awf wf init`, `awf wf next`, operations wiki writes), run the matching readiness gate from the repository root and obey its exit code. The gate is a precondition for those features only; it is not required before ordinary file edits, local verification, or a normal commit on a development branch.

```bash
awf ready --gate inspect --repo-root . --json
awf ready --gate analysis --repo-root . --json
awf ready --gate workflow-init --repo-root . --json
awf ready --gate workflow-run --repo-root . --json
awf ready --gate operations --repo-root . --json
```

Exit code contract:

- `0`: `decision: "allow"`; continue with the requested automation.
- `10`: `decision: "dry_run_only"`; do not call a provider or perform delegated execution. Only run dry-run/status commands named in `gate.recommended_next`.
- `20`: `decision: "block"`; stop and follow `gate.recommended_next`.

Use `analysis` before provider-backed `awf analyze`, `workflow-init` before creating `.workflow/`, `workflow-run` before continuing a workflow, and `operations` before writing operations wiki decisions.

The main write/provider commands enforce the same gate internally. Use `--no-ready-gate` only when a higher-level wrapper has already performed the equivalent check.

## Roles

- Implementation host (Codex working directly in the user's repository, or an `awf wf` plan/impl/test delegated run) uses `workspace-write`: edit within the granted write scope, verify, and make ordinary `git add`/`git commit`/non-force `git push` on the development branch without a separate approval per commit. Commit permission is separate from merge-to-production and deployment permission, which follow the `release-worktree-lifecycle` skill.
- Review/analysis worker (`awf wf` review/verify, or a hashtag-protocol `#precise`/`#cross`/`#critical` slave) uses `read-only`: read and analyze only, respond in JSON.
- Scope locks and phase approvals apply only to the opt-in workflow contract (an active `.workflow/` pipeline that the task was routed through). Do not impose `workflow-init` or the seven-phase approval flow on an ordinary short task.
