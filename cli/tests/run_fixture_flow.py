from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from fixture_support import (
    REVIEW_RESULT,
    initialize_workflow_fixture,
    mark_workflow_prerequisites_passed,
    prepare_workflow_repo,
    run_awf,
)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir_str:
        temp_repo = Path(tmp_dir_str) / "repo"
        prepare_workflow_repo(temp_repo, result_file=REVIEW_RESULT)
        initialized = initialize_workflow_fixture(
            temp_repo,
            "Fixture flow concept covering review event synchronization",
        )
        print(initialized.stdout, end="")
        if initialized.stderr:
            print(initialized.stderr, file=sys.stderr, end="")
        if initialized.returncode != 0:
            return initialized.returncode
        mark_workflow_prerequisites_passed(temp_repo)

        completed = run_awf(
            temp_repo,
            "wf",
            "next",
            "--phase",
            "review",
            "--auto-apply",
            extra_env={"AWF_FIXTURE_RESULT_FILE": str(REVIEW_RESULT)},
        )
        print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")

        state = json.loads((temp_repo / ".workflow" / "state.json").read_text(encoding="utf-8"))
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


if __name__ == "__main__":
    raise SystemExit(main())
