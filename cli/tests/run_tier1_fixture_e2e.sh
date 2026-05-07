#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

python3 "$ROOT/cli/tests/run_fixture_flow.py"
python3 "$ROOT/cli/tests/run_analysis_fixture.py"
python3 "$ROOT/cli/tests/run_analysis_fanout_fixture.py"
python3 "$ROOT/cli/tests/run_chat_fixture.py"
python3 "$ROOT/cli/tests/run_router_fixture.py"
python3 "$ROOT/cli/tests/run_wf_lifecycle_fixture.py"
python3 "$ROOT/cli/tests/run_analyze_cross_fixture.py"
