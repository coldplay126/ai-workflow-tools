from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from fixture_support import ANALYSIS_RESULT, ROOT, prepare_analysis_docs_fixture


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
            "--yolo",
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        prepare_analysis_docs_fixture(tmp_dir)
        templates_dir = tmp_dir / "_templates"

        first = _run_analyze(tmp_dir)
        print(first.stdout, end="")
        if first.stderr:
            print(first.stderr, file=sys.stderr, end="")
        if first.returncode != 0:
            return first.returncode

        ai_context_dir = tmp_dir / "sample-api" / "quest-challenge" / ".ai-context"
        state_path = ai_context_dir / ".analysis-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        print(f"first_output_status={state['layers']['output']['status']}")
        print(f"first_stage1={state['layers']['analyze']['stage1']['status']}")
        print(f"first_stage2={state['layers']['analyze']['stage2']['status']}")
        print(f"first_stage3={state['layers']['analyze']['stage3']['status']}")
        print(f"first_domain_bundle={state['artifacts']['domain_bundle']}")

        second = _run_analyze(tmp_dir)
        print(second.stdout, end="")
        if second.stderr:
            print(second.stderr, file=sys.stderr, end="")
        if second.returncode != 0:
            return second.returncode

        for file_name in ("api-spec.json", "data-model.md", "domain-overview.md", "external-integration.md"):
            (ai_context_dir / file_name).unlink()
        state["layers"]["output"]["status"] = "failed"
        state["completedAt"] = None
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        recovered = _run_analyze(tmp_dir)
        print(recovered.stdout, end="")
        if recovered.stderr:
            print(recovered.stderr, file=sys.stderr, end="")
        if recovered.returncode != 0:
            return recovered.returncode

        final_state = json.loads(state_path.read_text(encoding="utf-8"))
        print(f"recovered_output_status={final_state['layers']['output']['status']}")
        print(f"recovered_stage1={final_state['layers']['analyze']['stage1']['status']}")
        print(f"recovered_stage2={final_state['layers']['analyze']['stage2']['status']}")

        bundle_path = ai_context_dir / ".tmp" / "domain-bundle.xml"
        bundle_before = bundle_path.read_text(encoding="utf-8")
        config_hash_before = final_state["layers"]["bundle"]["configHash"]
        analysis_config_path = templates_dir / "analysis-config.json"
        analysis_config = json.loads(analysis_config_path.read_text(encoding="utf-8"))
        fallback = analysis_config.setdefault("domain_definitions", {}).setdefault("quest-challenge", {})
        dirs = fallback.setdefault("directories", {}).setdefault("sample-api", [])
        extra_dir = "src/domain/quest-extra-fixture"
        if extra_dir not in dirs:
            dirs.append(extra_dir)
        analysis_config_path.write_text(json.dumps(analysis_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        changed_seed_state = json.loads(state_path.read_text(encoding="utf-8"))
        changed_seed_state["layers"]["output"]["status"] = "failed"
        changed_seed_state["completedAt"] = None
        state_path.write_text(json.dumps(changed_seed_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        changed = _run_analyze(tmp_dir)
        print(changed.stdout, end="")
        if changed.stderr:
            print(changed.stderr, file=sys.stderr, end="")
        if changed.returncode != 0:
            return changed.returncode

        changed_state = json.loads(state_path.read_text(encoding="utf-8"))
        bundle_after = bundle_path.read_text(encoding="utf-8")
        print(f"changed_config_hash={changed_state['layers']['bundle']['configHash']}")
        print(f"bundle_hash_changed={config_hash_before != changed_state['layers']['bundle']['configHash']}")
        print(f"bundle_content_changed={bundle_before != bundle_after}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
