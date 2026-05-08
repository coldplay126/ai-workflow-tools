# cmux-agent Quickstart

`cmux-agent` is the optional visual multi-agent runtime. It uses `cmux` for panes and records run events in `.agent/events.jsonl`, which `awf cmux` can inspect without importing the runtime package.

## Preconditions

- `cmux` is installed and available on `PATH`.
- At least one AI CLI provider used by the chosen template is installed (`claude`, `codex`, or `gemini`).
- Run commands from the repository root unless a command passes `--cwd`.

## Start A Run

```bash
uv run --project cmux-agent cmux-agent \
  --cwd . \
  --templates-dir templates/cmux \
  --template feature \
  start
```

The selected template is written to `.agent/template-state.json` so the separate `watch` and `spawn` processes keep using the same provider config and worker protocols.

## Send Work

```bash
uv run --project cmux-agent cmux-agent --cwd . task "Implement the requested feature and verify it."
```

The orchestrator should write dispatch artifacts to `.agent/outbox`. Workers receive deliveries in `.agent/inbox/<worker-name>/` and report results by writing result artifacts back to `.agent/outbox`.

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
    "name": "worker-api",
    "provider": "codex"
  }
}
```

You can also spawn one manually:

```bash
uv run --project cmux-agent cmux-agent --cwd . spawn worker-api --provider codex
```

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
uv run --project cli --no-editable awf cmux runs --repo-root .
uv run --project cli --no-editable awf cmux tail --repo-root . --limit 20
uv run --project cli --no-editable awf cmux tail --repo-root . --event message.failed
```

If the event log lives elsewhere, set `AWF_CMUX_LOG=/path/to/events.jsonl` or pass the path directly.

## Diagnose Failures

The watcher moves invalid or unroutable artifacts to `.agent/processed/failed/` and records failure reasons in `.agent/events.jsonl`.

Use these commands first when a run stops making progress:

```bash
uv run --project cmux-agent cmux-agent --cwd . status --failures
uv run --project cmux-agent cmux-agent --cwd . failures
uv run --project cmux-agent cmux-agent --cwd . events --failures -n 20
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
