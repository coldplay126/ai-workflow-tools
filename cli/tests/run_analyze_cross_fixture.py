from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from fixture_support import ANALYSIS_RESULT, ROOT, prepare_analysis_docs_fixture


def _write_project_config(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "[provider]",
                'default = "fixture"',
                "",
                "[provider.fixture]",
                'result_file = ""',
                "",
                "[permissions]",
                "yolo = true",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _run_analyze(tmp_docs_root: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "cli" / "src")
    env["AWF_FIXTURE_RESULT_FILE"] = str(ANALYSIS_RESULT)
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
            "--provider",
            "fixture",
            "--mode",
            "cross",
            "--yolo",
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def main() -> int:
    config_path = ROOT / ".awf.toml"
    backup = config_path.read_text(encoding="utf-8") if config_path.exists() else None
    _write_project_config(config_path)
    try:
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            prepare_analysis_docs_fixture(tmp_dir)

            completed = _run_analyze(tmp_dir)
            print(completed.stdout, end="")
            if completed.stderr:
                print(completed.stderr, file=sys.stderr, end="")
            if completed.returncode != 0:
                return completed.returncode
            if "cross_judge: PASS" not in completed.stdout:
                return 1
            if "cross_synthesis_result: PASS" not in completed.stdout:
                return 1

            state_path = tmp_dir / "sample-api" / "quest-challenge" / ".ai-context" / ".analysis-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            cross = state["layers"]["analyze"]["stage2"].get("crossSynthesis", {})
            print(f"cross_selected_provider={cross.get('selectedProvider')}")
            print(f"cross_secondary_provider={cross.get('secondaryProvider')}")
            print(f"cross_judge_passed={cross.get('judgePassed')}")
            print(f"cross_synthesis_passed={cross.get('synthesisPassed')}")
            if cross.get("judgePassed") is not True:
                return 1
            if cross.get("synthesisPassed") is not True:
                return 1
            if cross.get("selectedProvider") != "fixture":
                return 1
            if cross.get("secondaryProvider") != "fixture":
                return 1
            if state["layers"]["output"]["status"] != "completed":
                return 1
            return 0
    finally:
        if backup is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_text(backup, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
