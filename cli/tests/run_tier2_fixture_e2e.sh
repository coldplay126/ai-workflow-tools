#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

python3 "$ROOT/cli/tests/run_discovery_fixture.py"
python3 "$ROOT/cli/tests/run_doctor_fixture.py"
python3 "$ROOT/cli/tests/run_doctor_probe_fixture.py"
python3 "$ROOT/cli/tests/run_doctor_ci_fixture.py"
python3 "$ROOT/cli/tests/run_mcp_cli_fixture.py"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT/cli/src" python3 "$ROOT/cli/tests/run_judge_synthesis_fixture.py"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT/cli/src" python3 "$ROOT/cli/tests/run_mcp_fixture.py"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT/cli/src" python3 "$ROOT/cli/tests/run_mcp_http_fixture.py"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT/cli/src" python3 "$ROOT/cli/tests/run_provider_mcp_fixture.py"
