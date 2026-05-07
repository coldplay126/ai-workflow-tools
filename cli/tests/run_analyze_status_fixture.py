from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from fixture_support import ANALYSIS_RESULT, ROOT, prepare_analysis_docs_fixture


def _run(tmp_docs_root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    env = {"PYTHONPATH": str(ROOT / "cli" / "src")}
    env.update(**dict())  # keep explicit local env shape
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "awf",
            "analyze",
            "sample-api",
            "quest-challenge",
            "--repo-root",
            str(ROOT),
            "--docs-root",
            str(tmp_docs_root),
            "--github-root",
            str(tmp_docs_root),
            *extra,
        ],
        cwd=str(ROOT),
        env={**env, **{"AWF_FIXTURE_RESULT_FILE": str(ANALYSIS_RESULT)}},
        capture_output=True,
        text=True,
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        prepare_analysis_docs_fixture(tmp_dir)

        first = _run(tmp_dir, "--provider", "fixture", "--yolo")
        if first.returncode != 0:
            print(first.stdout, end="")
            if first.stderr:
                print(first.stderr, file=sys.stderr, end="")
            return first.returncode

        status = _run(tmp_dir, "--status")
        print(status.stdout, end="")
        if status.stderr:
            print(status.stderr, file=sys.stderr, end="")
        if status.returncode != 0:
            return status.returncode
        if "event_summary:" not in status.stdout:
            raise SystemExit("missing event_summary")
        if "analyze_stages:" not in status.stdout:
            raise SystemExit("missing analyze_stages")
        if "event_stages:" not in status.stdout:
            raise SystemExit("missing event_stages")
        if "event_tasks:" not in status.stdout:
            raise SystemExit("missing event_tasks")

        status_json = _run(tmp_dir, "--status", "--json")
        if status_json.returncode != 0:
            print(status_json.stdout, end="")
            if status_json.stderr:
                print(status_json.stderr, file=sys.stderr, end="")
            return status_json.returncode
        payload = json.loads(status_json.stdout)
        print(f"statusOutput={payload['layers']['output']['status']}")
        print(f"statusEventTasks={len((payload.get('eventSync') or {}).get('tasks', {}))}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
