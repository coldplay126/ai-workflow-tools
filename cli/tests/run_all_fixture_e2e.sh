#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

bash "$ROOT/cli/tests/run_core_fixture_e2e.sh"
bash "$ROOT/cli/tests/run_tooling_fixture_e2e.sh"
