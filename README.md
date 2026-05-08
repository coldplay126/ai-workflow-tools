# ai-workflow-tools

AI workflow and analysis tooling for Claude Code, Codex, and local CLI workflows.

This repository keeps the reusable workflow contracts, provider adapters, CLI, and agent prompts in one place. It is imported as a clean personal repository with no upstream Git history.

## Contents

```text
ai-workflow-tools/
├── cli/            # Python CLI: awf analyze, awf wf, awf chat, awf doctor
├── claude/         # Claude Code skills and agent definitions
├── codex/          # Codex runner and delegated worker rules
├── cmux-agent/     # cmux worker support package
├── docs/           # Architecture, specs, and operating guides
├── snippets/       # CLAUDE.md snippets
├── templates/      # cmux protocol templates
└── setup.sh        # Claude Code skill/agent symlink installer
```

## CLI

```bash
uv run --project cli --no-editable awf --help
uv run --project cli --no-editable awf doctor --repo-root . --json --ci
uv run --project cli --no-editable awf wf status --repo-root .
uv run --project cli --no-editable awf analyze sample-api health --repo-root . --dry-run
```

The Python package is `awf-cli`, and the console entrypoint is `awf`.

### Stage 1 transitive cache invalidation

`awf analyze` builds an import graph alongside its analysis output and uses it on the next run to re-analyze not just files whose content changed, but also their reverse dependents whose imported source moved. This is why a unit you did not touch can still be re-analyzed — its dependency's exported surface changed.

- `awf analyze {service} --check` flags both direct and transitive stale candidates per unit.
- `awf analyze {service} --cycles` reports import cycles using the same saved graph.
- Disable transitive invalidation in an emergency with `AWF_DISABLE_TRANSITIVE_INVALIDATION=1`, or persistently via `analysis-pipeline.json` → `transitive_invalidation.enabled = false`. Direct-change incremental still works without it.

See [docs/patterns/analysis-pipeline/03-resume-optimization.md](docs/patterns/analysis-pipeline/03-resume-optimization.md) for the full contract.

## Tests

```bash
uv run --project cli pytest cli/tests
uv run --project cmux-agent pytest cmux-agent/tests
```

## cmux-agent Runtime

`cmux-agent` provides a visual multi-agent runtime on top of cmux. It creates a controller, an orchestrator, and configured workers, then routes JSON artifacts through `.agent/outbox` and `.agent/inbox`.

Quickstart: [cmux-agent Quickstart](docs/manuals/cmux-agent-quickstart.md).

By default the orchestrator runs in its own cmux surface. Use
`start --attach-orchestrator` when the current Claude Code or Codex CLI session
should act as the orchestrator while controller and workers run in cmux.

The orchestrator can request dynamic workers by writing a `control` artifact with `action: "spawn_agent"`. You can also create one manually:

```bash
uv run --project cmux-agent cmux-agent spawn worker-api --provider codex
uv run --project cmux-agent cmux-agent spawn --worker-template review --provider codex
```

Runtime diagnostics are available from `cmux-agent` and the read-only `awf cmux` observer:

```bash
uv run --project cmux-agent cmux-agent doctor
uv run --project cmux-agent cmux-agent smoke
uv run --project cmux-agent cmux-agent status --failures
uv run --project cmux-agent cmux-agent failures
uv run --project cmux-agent cmux-agent events --failures
uv run --project cli --no-editable awf cmux failures --repo-root .
```

## Claude Code Setup

```bash
./setup.sh
```

The setup script links the skills under `claude/skills/` and agents under `claude/agents/` into `~/.claude`. It does not register any company-specific MCP server.

Optional snippets:

- `snippets/claude-md-multi-agent.md`
- `snippets/claude-md-wf-pipeline.md`

## Core Ideas

- `.workflow/` holds phase state and artifacts for gated feature work.
- `.ai-context/` holds generated analysis output.
- Provider adapters normalize Claude, Codex, OpenAI, subprocess, and fixture execution.
- The same contracts can be driven from Claude skills, Codex runner scripts, or the `awf` CLI.

## Import Notes

This repo intentionally excludes company-specific material from the source repository, including internal memory files, per-repository AI configuration backups, private documentation MCP configuration, and archived workflow snapshots.
