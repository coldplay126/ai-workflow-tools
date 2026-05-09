#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PYTHON_CMD=(python3)
if [[ -x "$ROOT/cli/.venv/bin/python" ]]; then
  PYTHON_CMD=("$ROOT/cli/.venv/bin/python")
elif command -v uv >/dev/null 2>&1; then
  export UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT/.tmp/uv-cache}"
  PYTHON_CMD=(uv run --project "$ROOT/cli" --no-editable python)
fi

"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_discovery_fixture.py"
"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_claude_stream_parser_fixture.py"
"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_gateway_foundation_fixture.py"
"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_gateway_event_fixture.py"
"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_gateway_state_sync_fixture.py"
"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_doctor_fixture.py"
"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_doctor_probe_fixture.py"
"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_doctor_ci_fixture.py"
"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_mcp_cli_fixture.py"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT/cli/src" "${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_judge_synthesis_fixture.py"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT/cli/src" "${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_mcp_fixture.py"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT/cli/src" "${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_mcp_http_fixture.py"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT/cli/src" "${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_provider_mcp_fixture.py"
