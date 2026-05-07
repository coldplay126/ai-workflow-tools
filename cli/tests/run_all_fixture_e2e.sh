#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

bash "$ROOT/cli/tests/run_tier1_fixture_e2e.sh"
bash "$ROOT/cli/tests/run_tier2_fixture_e2e.sh"
