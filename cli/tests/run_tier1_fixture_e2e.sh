#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "warning: run_tier1_fixture_e2e.sh is deprecated; use run_core_fixture_e2e.sh" >&2
bash "$ROOT/cli/tests/run_core_fixture_e2e.sh"
