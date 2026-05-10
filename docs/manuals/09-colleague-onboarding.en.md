# Colleague Onboarding Guide

Use this short guide when explaining `ai-workflow-tools` to another developer
and running a first 15-minute trial on a small repository.

Korean version: [동료 개발자 온보딩 가이드](./09-colleague-onboarding.ko.md)

## One-Sentence Explanation

`ai-workflow-tools` turns Claude Code, Codex CLI, and local CLI work into a
checked workflow: read repo readiness, discover units deterministically, preview
prompts with dry-runs, then run gated workflow phases with repo-local artifacts.

## Good Fits

- A new repository where it is unclear what the AI should inspect first
- Feature work that benefits from `plan -> review -> approve -> impl -> verify
  -> test -> done`
- Mixed Claude Code and Codex CLI usage over the same `.workflow` state
- Provider execution that should be previewed before it runs
- Persistent `.ai-context` documentation for code understanding
- Independent review/verify through another provider or runner surface

## Poor Fits

- A one-line or two-line obvious edit
- Early product discussion where human alignment comes before repo analysis
- Emergency hotfixes that must bypass workflow artifacts
- Work where the workflow overhead is larger than the task

## First 15-Minute Trial

Start on a small Python/TypeScript repository or subproject and stop before any
provider-backed execution.

```bash
awf ready --repo-root .
awf scan . --no-ai
awf analyze <service> <unit> --repo-root . --dry-run --output-format json
awf wf init "small scoped improvement" --repo-root .
awf ready --repo-root . --gate workflow-run --json
awf wf next --repo-root . --dry-run --output-format json
```

Check:

- Does `ready` explain the next safe command?
- Does `scan` find service/unit candidates that make sense?
- Do `domain_directories`, `all_directories`, and `ai_context_dir` in the
  analysis dry-run match the codebase?
- Do `phase`, `provider`, and `prompt` in the workflow dry-run look reviewable
  before execution?

If the dry-run output is unclear, do not move to provider-backed execution.

## Scenario Examples

### Example A: Unfamiliar Python Script Repository

Situation: the repository has root-level source directories such as
`collectors/`, `analyzers/`, or `importers/`.

```bash
awf ready --repo-root .
awf scan . --no-ai
awf analyze collectors naver --repo-root . --dry-run --output-format json
```

Decision:

- If `scan` identifies real source directories as units, the target is likely
  usable.
- If `domain_directories` points to the intended folder, provider execution may
  be reasonable.
- If the unit name is wrong, choose another unit from the scan output before
  calling a provider.

### Example B: Start a Small Feature Workflow

Situation: the task is narrow, such as "record the retry reason in payment
failure logs."

```bash
awf wf init "record retry reason in payment failure logs" --repo-root .
awf ready --repo-root . --gate workflow-run --json
awf wf next --repo-root . --dry-run --output-format json
```

Decision:

- If the phase is `plan`, the tool is preparing planning artifacts, not
  implementation.
- If the prompt is too broad, narrow the concept and restart.
- `.workflow/state.json` is the canonical state for this task.

### Example C: Use Codex for Review or Verify Only

Situation: Claude Code is the main host, but you want a Codex review/verify
perspective.

```bash
../ai-workflow-tools/codex/run-wf.sh preflight review codex
../ai-workflow-tools/codex/run-wf.sh prompt review codex
```

Decision:

- If `preflight` fails, follow `ready` recommendations before running Codex.
- The generated prompt should make the review goal and artifacts clear.
- This path should work without cmux-agent or Pi.

### Example D: A Task That Should Not Use AWF

Situation: fix one README typo or reorder imports.

Recommended path:

```bash
git diff
pytest <relevant tests>
```

Decision:

- If creating `.workflow` costs more than the edit, do not use it.
- Keep simple edits in the normal development flow. If the pattern repeats,
  consider only a wiki decision or docs update later.

## Example Conversation

Question: "Does this mean the AI implements the feature by itself?"

Answer:

> Not exactly. It keeps AI work from jumping straight into execution. First it
> checks repo readiness and analysis units, then previews dry-run prompts, then
> runs gated phases. It is too much for tiny edits, but useful for unfamiliar
> repo analysis and feature work that needs review or verification.

## Claude Code

Install the skills with:

```bash
./setup.sh
```

Common entrypoints:

```text
/analysis
/wf-status
/wf-orchestrator
```

Claude skills follow the same rule: read `awf ready` and dry-run JSON before
provider-backed execution.

## Codex CLI

Codex does not replicate Claude's skill UX. Use the `awf` CLI and Codex adapter.

```bash
../ai-workflow-tools/codex/run-wf.sh preflight review codex
../ai-workflow-tools/codex/run-wf.sh prompt review codex
```

`preflight` checks the same `awf ready --gate workflow-run` and
`awf wf next --dry-run --output-format json` contract.

## Avoid Saying

- "The AI just develops the feature by itself."
- "Use this for every task."
- "cmux-agent or Pi is the default path." They are optional execution surfaces.

## Better Explanation

> This tool keeps AI coding grounded in repo-local artifacts, dry-runs, and
> gates. It is too much for tiny edits, but useful for unfamiliar repo analysis,
> feature workflow, review, and verification.

## Next Docs

- [First Workflow](./08-first-workflow.en.md)
- [첫 ai-workflow-tools 작업 흐름](./08-first-workflow.ko.md)
- [AWF AI Workflow 입문 가이드](./01-getting-started.md)
- [Codex Portability Guide](./codex-portability.md)
