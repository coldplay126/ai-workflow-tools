from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from fixture_support import ROOT, prepare_analysis_docs_fixture


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        prepare_analysis_docs_fixture(tmp_dir)
        templates_dir = tmp_dir / "_templates"

        analysis_config_path = templates_dir / "analysis-config.json"
        analysis_config = json.loads(analysis_config_path.read_text(encoding="utf-8"))
        analysis_config.setdefault("service_map", {})["fixture-empty"] = str(tmp_dir / "fixture-empty-repo")
        analysis_config.setdefault("domain_definitions", {})["empty-domain"] = {
            "directories": {"fixture-empty": ["src/domain/empty-domain", "src/empty-domain"]},
            "related_domains": [],
            "existing_docs": [],
        }
        analysis_config_path.write_text(json.dumps(analysis_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        (tmp_dir / "fixture-empty-repo").mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "cli" / "src")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "awf",
                "analyze",
                "fixture-empty",
                "empty-domain",
                "--repo-root",
                str(ROOT),
                "--docs-root",
                str(tmp_dir),
                "--github-root",
                str(tmp_dir),
                "--provider",
                "fixture",
                "--yolo",
            ],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")

        if completed.returncode == 0:
            raise SystemExit("zero-input analyze should fail")

        ai_context_dir = tmp_dir / "fixture-empty" / "empty-domain" / ".ai-context"
        state = json.loads((ai_context_dir / ".analysis-state.json").read_text(encoding="utf-8"))
        if state["layers"]["bundle"]["status"] != "failed":
            raise SystemExit("bundle status should be failed for zero-input case")
        if state["layers"]["bundle"].get("errorMessage") != "source_discovery_empty":
            raise SystemExit("bundle errorMessage should record source_discovery_empty")
        if state["layers"]["output"]["status"] != "failed":
            raise SystemExit("output status should be failed for zero-input case")
        if "input_quality_failed: no target source files collected" not in completed.stderr:
            raise SystemExit("stderr should explain zero-input failure")

        memo = (ai_context_dir / ".tmp" / "stage1-analysis.md").read_text(encoding="utf-8")
        if "Target file count: 0" not in memo:
            raise SystemExit("stage1 memo should record zero target file count")

        print("analysis_zero_input_ok=true")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
