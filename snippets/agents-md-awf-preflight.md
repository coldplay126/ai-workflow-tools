# AWF Deterministic Preflight

Before using ai-workflow-tools from Codex, run the matching readiness gate from the repository root and obey its exit code.

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
