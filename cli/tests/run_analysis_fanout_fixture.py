from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AWF_DOCS_TEMPLATES = ROOT.parent / "analysis-docs" / "_templates"
FANOUT_FIXTURE_DIR = ROOT / "cli" / "tests" / "fixtures" / "fanout"


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
                "[analysis]",
                "fanout_large_file_threshold = 1",
                "stage2_token_threshold = 1",
                "stage2_line_threshold = 1",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _run_analyze(tmp_docs_root: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "cli" / "src")
    env["AWF_FIXTURE_RESULT_FILE"] = str(ROOT / "cli" / "tests" / "fixtures" / "analysis-stage2-result.txt")
    env["AWF_FIXTURE_FANOUT_DIR"] = str(FANOUT_FIXTURE_DIR)
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
            "--provider",
            "fixture",
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
            templates_dir = tmp_dir / "_templates"
            templates_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(AWF_DOCS_TEMPLATES / "analysis-config.json", templates_dir / "analysis-config.json")
            shutil.copy2(AWF_DOCS_TEMPLATES / "analysis-pipeline.json", templates_dir / "analysis-pipeline.json")

            completed = _run_analyze(tmp_dir)
            print(completed.stdout, end="")
            if completed.stderr:
                print(completed.stderr, file=sys.stderr, end="")
            if completed.returncode != 0:
                return completed.returncode

            state_path = tmp_dir / "sample-api" / "quest-challenge" / ".ai-context" / ".analysis-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            fanout = state["layers"]["analyze"]["stage2"].get("fanout", {})
            print(f"fanout_state_selected={fanout.get('selected')}")
            print(f"fanout_used={fanout.get('used')}")
            print(f"fanout_status={fanout.get('status')}")
            print(f"fanout_execution_mode={fanout.get('executionMode')}")
            print(f"fanout_consistency_passed={fanout.get('consistencyPassed')}")
            print(f"fanout_consistency_issues={fanout.get('consistencyIssues')}")
            print(f"fanout_scale={fanout.get('scale')}")
            print(f"fanout_bundle_lines={fanout.get('bundleLines')}")
            print(f"fanout_bundle_tokens={fanout.get('bundleTokens')}")
            writer_prompts = state["artifacts"].get("fanout_writer_prompts", {})
            print(f"fanout_writer_prompt_count={len(writer_prompts)}")
            if not fanout.get("selected"):
                return 1
            if not fanout.get("used"):
                return 1
            if fanout.get("executionMode") not in {"parallel", "parallel_v2"}:
                return 1
            if fanout.get("consistencyPassed") is not True:
                return 1
            # v1: 4 writer prompts, v2: 4 writer prompts (legacy compat in state)
            if len(writer_prompts) not in {2, 4}:
                return 1
            return 0
    finally:
        if backup is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_text(backup, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
