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

"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_fixture_flow.py"
"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_analysis_fixture.py"
"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_analysis_fanout_fixture.py"
"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_analysis_zero_input_fixture.py"
"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_analyze_status_fixture.py"
"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_chat_fixture.py"
"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_router_fixture.py"
"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_wf_status_fixture.py"
"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_wf_decide_fixture.py"
"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_wf_lifecycle_fixture.py"
"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_workflow_envelope_fixture.py"
"${PYTHON_CMD[@]}" "$ROOT/cli/tests/run_analyze_cross_fixture.py"
