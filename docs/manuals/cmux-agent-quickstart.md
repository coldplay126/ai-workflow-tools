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

## Observe Runs

```bash
uv run --project cli --no-editable awf cmux runs --repo-root .
uv run --project cli --no-editable awf cmux tail --repo-root . --limit 20
uv run --project cli --no-editable awf cmux tail --repo-root . --event message.failed
```

If the event log lives elsewhere, set `AWF_CMUX_LOG=/path/to/events.jsonl` or pass the path directly.

## Stop

```bash
uv run --project cmux-agent cmux-agent --cwd . stop
```

Use `--no-clean` if you want to keep `.agent/` for debugging after the run ends.
