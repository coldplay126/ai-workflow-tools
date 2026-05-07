#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"

uv run --project cli --no-editable awf wf next --repo-root . --phase review --provider codex --dry-run
