# Onboarding Guide for First-Time Developers

Use this short guide when opening `ai-workflow-tools` for the first time. The
goal is to decide whether the tool fits your task, then reach the first dry-run
on a small repository within 15 minutes without calling a provider.

Korean version: [처음 쓰는 개발자를 위한 온보딩 가이드](./09-colleague-onboarding.ko.md)

## Understand It in One Sentence

`ai-workflow-tools` turns Claude Code, Codex CLI, and local CLI work into a
checked workflow: read repo readiness, discover units deterministically, preview
prompts with dry-runs, then run gated workflow phases with repo-local artifacts.

## Use It When

- A new repository where it is unclear what the AI should inspect first
- Feature work that benefits from `plan -> review -> approve -> impl -> verify
  -> test -> done`
- Mixed Claude Code and Codex CLI usage over the same `.workflow` state
- Provider execution that should be previewed before it runs
- Persistent `.ai-context` documentation for code understanding
- Independent review/verify through another provider or runner surface

## Do Not Use It When

- A one-line or two-line obvious edit
- Early product discussion where human alignment comes before repo analysis
- Emergency hotfixes that must bypass workflow artifacts
- Work where the workflow overhead is larger than the task

## Your First 15 Minutes

Start on a small Python/TypeScript repository or subproject. Do not call a
provider yet. Stop after the dry-run commands below.

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

If the repository has root-level source directories such as `collectors/`,
`analyzers/`, or `importers/`, first use read-only commands to identify the
analysis units.

```bash
awf ready --repo-root .
awf scan . --no-ai
awf analyze collectors naver --repo-root . --dry-run --output-format json
```

How to decide:

- If `scan` identifies real source directories as units, the target is likely
  usable.
- If `domain_directories` points to the intended folder, provider execution may
  be reasonable.
- If the unit name is wrong, choose another unit from the scan output before
  calling a provider.

### Example B: Start a Small Feature Workflow

For a narrow task such as "record the retry reason in payment failure logs,"
starting a workflow can be useful.

```bash
awf wf init "record retry reason in payment failure logs" --repo-root .
awf ready --repo-root . --gate workflow-run --json
awf wf next --repo-root . --dry-run --output-format json
```

How to decide:

- If the phase is `plan`, the tool is preparing planning artifacts, not
  implementation.
- If the prompt is too broad, narrow the concept and restart.
- `.workflow/state.json` is the canonical state for this task.

### Example C: Use Codex for Review or Verify Only

If Claude Code is your main host but you want a Codex review/verify
perspective, use the Codex adapter.

```bash
../ai-workflow-tools/codex/run-wf.sh preflight review codex
../ai-workflow-tools/codex/run-wf.sh prompt review codex
```

How to decide:

- If `preflight` fails, follow `ready` recommendations before running Codex.
- The generated prompt should make the review goal and artifacts clear.
- This path should work without cmux-agent or Pi.

### Example D: A Task That Should Not Use AWF

If the task is one README typo or import ordering, AWF is probably too much.

Recommended path:

```bash
git diff
pytest <relevant tests>
```

How to decide:

- If creating `.workflow` costs more than the edit, do not use it.
- Keep simple edits in the normal development flow. If the pattern repeats,
  consider only a wiki decision or docs update later.

## FAQ

Question: "Does this mean the AI implements the feature by itself?"

Answer:

> Not exactly. It keeps AI work from jumping straight into execution. First it
> checks repo readiness and analysis units, then previews dry-run prompts, then
> runs gated phases. It is too much for tiny edits, but useful for unfamiliar
> repo analysis and feature work that needs review or verification.

## Using Claude Code

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

## Using Codex CLI

Codex does not replicate Claude's skill UX. Use the `awf` CLI and Codex adapter.

```bash
../ai-workflow-tools/codex/run-wf.sh preflight review codex
../ai-workflow-tools/codex/run-wf.sh prompt review codex
```

`preflight` checks the same `awf ready --gate workflow-run` and
`awf wf next --dry-run --output-format json` contract.

## Avoid These Misunderstandings

- The AI does not just develop the feature by itself.
- You should not use this for every task.
- cmux-agent and Pi are not the default path. They are optional execution
  surfaces.

## The Main Point

> This tool keeps AI coding grounded in repo-local artifacts, dry-runs, and
> gates. It is too much for tiny edits, but useful for unfamiliar repo analysis,
> feature workflow, review, and verification.

## Next Docs

- [First Workflow](./08-first-workflow.en.md)
- [첫 ai-workflow-tools 작업 흐름](./08-first-workflow.ko.md)
- [AWF AI Workflow 입문 가이드](./01-getting-started.md)
- [Codex Portability Guide](./codex-portability.md)
