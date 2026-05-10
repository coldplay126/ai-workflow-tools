# Field Trial Checklist for Real Repositories

Use this checklist to try `ai-workflow-tools` on a real team repository and
decide whether it is worth continuing. The goal is not to exercise every
feature. The goal is to collect enough read-only and dry-run evidence to answer:
"Does this tool help on this repository?"

Korean version: [실제 레포 필드 트라이얼 체크리스트](./10-field-trial-checklist.ko.md)

## 0. Choose the Trial

Start with a small task that can finish in 30-60 minutes.

Good candidates:

- Identify analysis units in an unfamiliar Python or TypeScript repository
- Preview plan/review/verify for a small feature
- Use Codex only to review or verify a Claude Code workflow prompt

Avoid:

- A single README typo where workflow overhead is larger than the edit
- Product-direction work that needs human alignment before repo analysis
- Emergency hotfixes
- Repositories with no known test command

## 1. Trial Record

Fill this in before running commands.

| Item | Value |
|------|-------|
| repo / branch |  |
| task |  |
| existing test command |  |
| host | Claude Code / Codex CLI / local CLI |
| provider calls | none / dry-run only / real calls |
| cmux-agent or Pi | not used / cmux / Pi |

Start without provider calls. Pi is not the default path; run Pi field-smoke
only when Pi itself is under evaluation.

## 2. Deterministic Preflight

Run these commands from the repository root.

```bash
awf ready --repo-root .
awf scan . --no-ai
awf analyze <service> <unit> --repo-root . --dry-run --output-format json
awf wf init "small scoped improvement" --repo-root .
awf ready --repo-root . --gate workflow-run --json
awf wf next --repo-root . --dry-run --output-format json
```

Decision criteria:

| Check | Continue when | Stop when |
|-------|---------------|-----------|
| `ready` | The automation level and next command are understandable | The block reason is unclear |
| `scan` | Real source units appear as service/unit candidates | It only finds tests/docs or misses the main units |
| `analyze --dry-run` | `domain_directories` points at the intended code | It points at the wrong path or an empty path |
| `wf init` | `.workflow/state.json` matches the task description | The scope is too broad or vague |
| `ready --gate workflow-run` | The gate decision clearly says whether execution is safe | The gate cannot explain why it blocks |
| `wf next --dry-run` | Phase, provider, and prompt are reviewable before execution | The prompt does not match the task or artifacts are unclear |

If two or more checks hit the stop column, do not continue to provider-backed
execution. Fix repo structure, `.awf.toml`, the task description, or the test
command first.

## 3. Optional: Evaluate Pi or cmux-agent

Pi and cmux-agent are execution surfaces, not the default workflow path. A first
trial does not need either.

Only write fresh field-smoke evidence when evaluating Pi itself.

```bash
python3 cli/tests/run_pi_field_smoke.py --json --write-result
awf doctor --repo-root . --json
awf ready --repo-root .
```

Decision criteria:

- If quota or auth fails, stop Pi evaluation and return to the normal
  CLI/Codex/Claude path.
- If `ready` reads the latest field-smoke result, Pi dispatch can be considered.
- If Pi worsens cost, quota, latency, or debugging, keep it outside the default
  path.

## 4. Trial Result Template

Record the result in this format. When filing the result as a GitHub issue, use
the [ai-workflow-tools field trial issue form](../../.github/ISSUE_TEMPLATE/awf-field-trial.yml).

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

## 5. Completed Example

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
- reason: scan found the right unit and the dry-run prompt was reviewable before execution
- next small improvement: document the payment test command in the repo README
```

## 6. Decision Rules

- `keep using`: Scan units and dry-run prompts match the repo, and gates reduce
  execution risk.
- `use only for analysis`: `.ai-context` generation helps, but the full workflow
  is too heavy.
- `use only for review-verify`: Cross-checking is useful before or after
  implementation, but planning from AWF is unnecessary.
- `do not use here`: The task is too small, or repo structure/tests are not
  ready enough.

## Next Docs

- [Onboarding Guide for First-Time Developers](./09-colleague-onboarding.en.md)
- [First Workflow](./08-first-workflow.en.md)
- [Pi Field Validation](./pi-field-validation.md)
