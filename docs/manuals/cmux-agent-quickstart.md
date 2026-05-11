# cmux-agent Quickstart

`cmux-agent` is the optional visual multi-agent runtime. It uses `cmux` for panes and records run events in `.agent/events.jsonl`, which `awf cmux` can inspect without importing the runtime package.

## Preconditions

- `cmux` is installed and available on `PATH`.
- At least one AI CLI provider used by the chosen template is installed (`claude`, `codex`, or `gemini`).
- Run commands from the repository root unless a command passes `--cwd`.

## Runtime Drift Check

Run `doctor` before a smoke test or after updating the checkout:

```bash
uv run --project cmux-agent cmux-agent doctor
```

`doctor` prints the active Python environment, the imported `cmux_agent`
module path, the active `cmux-agent` executable, and the supported command set.
If it reports `cmux-agent PATH drift`, a stale globally installed
`cmux-agent` is still on `PATH`. Prefer the `uv run --project cmux-agent ...`
form during development, or refresh the installed tool from the current
checkout:

```bash
uv tool install --force ./cmux-agent
```

## Start A Run

Default mode starts a separate orchestrator session. This is the recommended
mode for normal operation because controller, orchestrator, and workers all
have cmux surfaces and broker deliveries can be injected directly into the
right terminal.

For a full runtime smoke test after updating cmux, providers, or this checkout,
run:

```bash
uv run --project cmux-agent cmux-agent smoke
```

The smoke command creates a temporary run in attached orchestrator mode, asks
the controller watcher to spawn a dynamic worker using `agent.template`, checks
the result delivery, verifies the attached task prompt, then stops the run and
closes the cmux workspace. Use `--keep` to preserve the smoke workspace and
`.agent/` files for debugging.

```bash
uv run --project cmux-agent cmux-agent \
  --cwd . \
  --templates-dir templates/cmux \
  --template feature \
  start
```

Available built-in templates include:

| Template | When to use it | Default provider shape |
| --- | --- | --- |
| `review` | Focused review of a diff, PR, or file set | Claude orchestrator + Codex review worker |
| `bugfix` | Investigate and fix a known defect | Claude orchestrator + Codex investigate/fix workers |
| `feature` | Full gated feature workflow | Claude orchestrator + Codex plan/review/impl/verify/test workers |
| `conductor` | Dynamic model-aware orchestration for complex coding work | Claude orchestrator + Gemini planning, Claude review, Codex implementation/verification |

Template entries can define `fallbacks`. If the requested provider CLI is not
on `PATH`, `cmux-agent` uses the first installed fallback provider with that
fallback's own flags. Without explicit `fallbacks`, known providers use a
conservative built-in order: Gemini falls back to Claude then Codex, Claude
falls back to Codex then Gemini, and Codex falls back to Claude then Gemini.

The selected template is written to `.agent/template-state.json` so the separate `watch` and `spawn` processes keep using the same provider config and worker protocols.
The controller tab starts `watch` through the same Python environment that ran
`start`, so a stale globally installed `cmux-agent` binary is not used for the
active run.

If you want the current Claude Code or Codex CLI session to act as the
orchestrator, attach it instead:

```bash
uv run --project cmux-agent cmux-agent \
  --cwd . \
  --templates-dir templates/cmux \
  --template feature \
  start --attach-orchestrator
```

Attach mode still creates the controller and worker cmux sessions, but registers
`orchestrator` without a surface. The current session must read
`.agent/ORCHESTRATOR-COMMON.md` and `.agent/ORCHESTRATOR.md`, write
dispatch/control artifacts to `.agent/outbox`, and inspect
`.agent/inbox/orchestrator` or `cmux-agent messages/events/failures` for worker
results. This mode is useful when an existing Claude Code or Codex CLI session
already has the task context.

## Send Work

```bash
uv run --project cmux-agent cmux-agent --cwd . task "Implement the requested feature and verify it."
```

The orchestrator should write dispatch artifacts to `.agent/outbox`. Workers receive deliveries in `.agent/inbox/<worker-name>/` and report results by writing result artifacts back to `.agent/outbox`.

In attach mode, `task` prints the orchestrator prompt instead of injecting it
into a cmux surface.

The normal runtime loop is:

1. `start` creates the cmux workspace, controller tab, orchestrator tab, worker tabs, `.agent/` directories, protocol files, and `.agent/events.jsonl`.
2. `task` injects the user request into the orchestrator.
3. The orchestrator writes a `dispatch` JSON artifact into `.agent/outbox`.
4. `watch` validates the artifact, routes it through the broker, writes a delivery JSON into the recipient inbox, injects the prompt into the recipient terminal, and moves the artifact to `.agent/processed/`.
5. Workers write `result` JSON artifacts back to `.agent/outbox`.
6. The broker delivers worker results to the orchestrator, which either dispatches follow-up work or reports completion.

## Dynamic Workers

The orchestrator can request a new worker by writing a control artifact:

```json
{
  "type": "control",
  "sender": "orchestrator",
  "recipient": "controller",
  "message": "Need isolated API implementation worker",
  "action": "spawn_agent",
  "agent": {
    "template": "impl",
    "provider": "codex"
  }
}
```

Use `agent.template` or `agent.role` when the worker purpose is known. For
example, `template: "review"` creates `worker-review`, or `worker-review-2`
when `worker-review` already exists. Matching worker protocols such as
`WORKER-REVIEW.md` are reused for numbered workers.

You can also spawn one manually:

```bash
uv run --project cmux-agent cmux-agent --cwd . spawn worker-api --provider codex
uv run --project cmux-agent cmux-agent --cwd . spawn --worker-template review --provider codex
```

When no worker name or purpose is provided, `spawn` creates the next
`worker-auto-N` worker. Prefer purpose templates such as `impl`, `review`, or
`test` when the delegation scope is known.

## Watch And Observe

Start the artifact watcher in the controller tab, or in any shell for the same `--cwd`:

```bash
uv run --project cmux-agent cmux-agent --cwd . watch
```

Useful local runtime commands:

```bash
uv run --project cmux-agent cmux-agent --cwd . status
uv run --project cmux-agent cmux-agent --cwd . agents
uv run --project cmux-agent cmux-agent --cwd . messages
uv run --project cmux-agent cmux-agent --cwd . events -n 20
```

The `awf cmux` commands are read-only helpers for inspecting `.agent/events.jsonl` without importing the `cmux-agent` runtime package:

```bash
uv run --project cli awf cmux runs --repo-root .
uv run --project cli awf cmux tail --repo-root . --limit 20
uv run --project cli awf cmux tail --repo-root . --event message.failed
uv run --project cli awf cmux failures --repo-root . --limit 20
```

If the event log lives elsewhere, set `AWF_CMUX_LOG=/path/to/events.jsonl` or pass the path directly.

## Diagnose Failures

The watcher moves invalid or unroutable artifacts to `.agent/processed/failed/` and records failure reasons in `.agent/events.jsonl`.

Use these commands first when a run stops making progress:

```bash
uv run --project cmux-agent cmux-agent --cwd . status --failures
uv run --project cmux-agent cmux-agent --cwd . failures
uv run --project cmux-agent cmux-agent --cwd . events --failures -n 20
uv run --project cli awf cmux failures --repo-root . --limit 20
```

Common failure causes:

| Symptom | Where It Appears | Typical Fix |
| --- | --- | --- |
| malformed JSON | `artifact.validation_failed`, `.agent/processed/failed/` | Rewrite a valid JSON artifact with `type`, `sender`, `recipient`, and `message`. |
| missing required field | `artifact.validation_failed` reason starts with `필수 필드 누락` | Add the missing field and create a new artifact. |
| unknown sender or recipient | `미등록 sender` or `미등록 recipient` | Check `cmux-agent agents`; dispatch only to registered agent names. |
| inactive surface | `비활성 recipient` | Spawn or register a live worker, then dispatch again. |
| spawn failure | `spawn_agent failed` or cmux error text | Check `cmux` availability, provider command, and requested worker name. |

Do not recreate an artifact just because it disappeared from `.agent/outbox`; successful artifacts are moved to `.agent/processed/`. Recreate only after checking whether it landed in `.agent/processed/failed/` and fixing the recorded reason.

## Stop

```bash
uv run --project cmux-agent cmux-agent --cwd . stop
```

Use `--no-clean` if you want to keep `.agent/` for debugging after the run ends.

## Troubleshooting

### `ModuleNotFoundError: No module named 'cmux_agent'` when running `.venv/bin/cmux-agent`

Symptom: invoking the bare console script directly (e.g.
`/Users/me/.../cmux-agent/.venv/bin/cmux-agent watch`) crashes immediately
with a Python traceback that ends in `ModuleNotFoundError: No module named
'cmux_agent'`. Running the same command via `uv run --project cmux-agent
cmux-agent ...` works fine.

Cause: on Python 3.13 with `uv` editable installs, the editable `.pth` line
that adds the project root to `sys.path` is sometimes not processed during
script execution. The package is therefore unreachable from the binary
generated by `[project.scripts]`. The `-c` and `-m` execution modes follow a
different site init order and are unaffected.

`cmux-agent doctor` recognizes this case and labels the active CLI with
`cmux_agent import 실패 — Python 3.13 + uv editable 설치 알려진 이슈`.

Fix options, in order of preference:

1. **Use the recommended invocation form** — `uv run --project cmux-agent
   cmux-agent ...`. This bypasses the broken script path.
2. **Install the CLI as a uv tool** — `uv tool install --force ./cmux-agent`.
   Produces a non-editable copy whose console script does not depend on the
   editable `.pth`.
3. **Run via module path** — `python -m cmux_agent ...` from inside the
   activated venv. Always works because `-m` triggers full site init.
4. **Last resort** — write a `.pth` file that uses the import form so the
   path injection runs even when ordinary path entries are skipped:
   ```bash
   echo 'import sys; sys.path.insert(0, "/abs/path/to/cmux-agent")' \
     > /abs/path/to/cmux-agent/.venv/lib/python3.13/site-packages/cmux-agent-fix.pth
   ```
   This is hardcoded to one checkout location; prefer option 1 or 2 for
   anything you expect to reproduce.

The underlying upstream issue is tracked as a Python 3.13 + uv editable
install incompatibility; the doctor diagnostic and these fallbacks are
the supported path until upstream lands a fix.
