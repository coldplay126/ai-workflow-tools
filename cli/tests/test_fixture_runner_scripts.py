from __future__ import annotations

import re
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
AGGREGATE_FIXTURE_SCRIPTS = (
    TESTS_DIR / "run_core_fixture_e2e.sh",
    TESTS_DIR / "run_tooling_fixture_e2e.sh",
)
MANUAL_FIELD_RUNNERS = {
    "run_pi_field_smoke.py",
}
RUNNER_REF_RE = re.compile(r"cli/tests/(run_[A-Za-z0-9_]+\.py)")


def _referenced_runners(script_path: Path) -> set[str]:
    return set(RUNNER_REF_RE.findall(script_path.read_text(encoding="utf-8")))


def test_aggregate_fixture_scripts_include_all_python_runners() -> None:
    python_runners = {
        path.name
        for path in TESTS_DIR.glob("run_*.py")
        if path.name not in MANUAL_FIELD_RUNNERS
    }
    aggregate_runners: set[str] = set()
    for script_path in AGGREGATE_FIXTURE_SCRIPTS:
        aggregate_runners.update(_referenced_runners(script_path))

    assert sorted(python_runners - aggregate_runners) == []


def test_aggregate_fixture_scripts_reference_existing_python_runners() -> None:
    missing: list[str] = []
    for script_path in AGGREGATE_FIXTURE_SCRIPTS:
        for runner in sorted(_referenced_runners(script_path)):
            if not (TESTS_DIR / runner).is_file():
                missing.append(f"{script_path.name}: {runner}")

    assert missing == []
