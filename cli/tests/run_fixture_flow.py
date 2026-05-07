from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    config_path = ROOT / ".awf.toml"
    backup = config_path.read_text(encoding="utf-8") if config_path.exists() else None
    state_path = ROOT / ".workflow" / "state.json"
    state_backup = state_path.read_text(encoding="utf-8")
    review_report_path = ROOT / ".workflow" / "artifacts" / "review-report.md"
    review_report_backup = review_report_path.read_text(encoding="utf-8")
    config_path.write_text(
        "\n".join(
            [
                "[provider]",
                'default = "fixture"',
                "",
                "[provider.fixture]",
                'result_file = "cli/tests/fixtures/review-result.json"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    try:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "cli" / "src")
        env["AWF_FIXTURE_RESULT_FILE"] = str(ROOT / "cli" / "tests" / "fixtures" / "review-result.json")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "awf",
                "wf",
                "next",
                "--repo-root",
                str(ROOT),
                "--phase",
                "review",
                "--auto-apply",
            ],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")

        state = json.loads((ROOT / ".workflow" / "state.json").read_text(encoding="utf-8"))
        stages = ((state.get("eventSync") or {}).get("stages") or {})
        if stages.get("prepare", {}).get("status") != "completed":
            raise SystemExit("missing prepare stage sync")
        if stages.get("execute", {}).get("status") != "completed":
            raise SystemExit("missing execute stage sync")
        if stages.get("apply", {}).get("status") != "completed":
            raise SystemExit("missing apply stage sync")
        print(f"currentPhase={state.get('currentPhase')}")
        print(f"G2={state.get('gates', {}).get('G2')}")
        return completed.returncode
    finally:
        if backup is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_text(backup, encoding="utf-8")
        state_path.write_text(state_backup, encoding="utf-8")
        review_report_path.write_text(review_report_backup, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
