from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REVIEW_RESULT = ROOT / "cli" / "tests" / "fixtures" / "review-result.json"
VERIFY_RESULT = ROOT / "cli" / "tests" / "fixtures" / "verify-result.json"


def _prepare_temp_repo(temp_repo: Path) -> None:
    (temp_repo / "docs").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "docs" / "architecture" / "awf-cli-architecture.md", temp_repo / "docs" / "awf-cli-architecture.md")
    shutil.copytree(ROOT / ".workflow", temp_repo / ".workflow")
    (temp_repo / ".workflow" / "state.json").unlink(missing_ok=True)
    config_path = temp_repo / ".awf.toml"
    config_path.write_text(
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


def _run_awf(repo_root: Path, *args: str, fixture_result: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "cli" / "src")
    if fixture_result is not None:
        env["AWF_FIXTURE_RESULT_FILE"] = str(fixture_result)
    cmd = [sys.executable, "-m", "awf", *args, "--repo-root", str(repo_root)]
    return subprocess.run(
        cmd,
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir_str:
        temp_repo = Path(tmp_dir_str) / "repo"
        temp_repo.mkdir(parents=True, exist_ok=True)
        _prepare_temp_repo(temp_repo)

        initialized = _run_awf(temp_repo, "wf", "init", "Fixture lifecycle concept")
        print(initialized.stdout, end="")
        if initialized.stderr:
            print(initialized.stderr, file=sys.stderr, end="")
        if initialized.returncode != 0:
            return initialized.returncode

        status_before = _run_awf(temp_repo, "wf", "status", "--json")
        print(status_before.stdout, end="")
        if status_before.stderr:
            print(status_before.stderr, file=sys.stderr, end="")
        if status_before.returncode != 0:
            return status_before.returncode
        before_state = json.loads(status_before.stdout)
        print(f"wf_before_phase={before_state.get('currentPhase')}")
        if before_state.get("currentPhase") != "plan":
            return 1

        reviewed = _run_awf(
            temp_repo,
            "wf",
            "next",
            "--phase",
            "review",
            "--provider",
            "fixture",
            "--auto-apply",
            "--yolo",
            fixture_result=REVIEW_RESULT,
        )
        print(reviewed.stdout, end="")
        if reviewed.stderr:
            print(reviewed.stderr, file=sys.stderr, end="")
        if reviewed.returncode != 0:
            return reviewed.returncode
        if "applied_gate: PASS" not in reviewed.stdout:
            return 1

        verified = _run_awf(
            temp_repo,
            "wf",
            "next",
            "--phase",
            "verify",
            "--provider",
            "fixture",
            "--auto-apply",
            "--yolo",
            fixture_result=VERIFY_RESULT,
        )
        print(verified.stdout, end="")
        if verified.stderr:
            print(verified.stderr, file=sys.stderr, end="")
        if verified.returncode != 0:
            return verified.returncode
        if "applied_gate: PASS" not in verified.stdout:
            return 1

        status_after = _run_awf(temp_repo, "wf", "status", "--json")
        print(status_after.stdout, end="")
        if status_after.stderr:
            print(status_after.stderr, file=sys.stderr, end="")
        if status_after.returncode != 0:
            return status_after.returncode
        after_state = json.loads(status_after.stdout)
        print(f"wf_after_review_gate={after_state.get('gates', {}).get('G2', {}).get('passed')}")
        print(f"wf_after_verify_gate={after_state.get('gates', {}).get('G5', {}).get('passed')}")
        if after_state.get("gates", {}).get("G2", {}).get("passed") is not True:
            return 1
        if after_state.get("gates", {}).get("G5", {}).get("passed") is not True:
            return 1
        if after_state.get("phases", {}).get("review", {}).get("status") != "completed":
            return 1
        if after_state.get("phases", {}).get("verify", {}).get("status") != "completed":
            return 1

        reset = _run_awf(temp_repo, "wf", "reset")
        print(reset.stdout, end="")
        if reset.stderr:
            print(reset.stderr, file=sys.stderr, end="")
        if reset.returncode != 0:
            return reset.returncode

        status_reset = _run_awf(temp_repo, "wf", "status", "--json")
        print(status_reset.stdout, end="")
        if status_reset.stderr:
            print(status_reset.stderr, file=sys.stderr, end="")
        if status_reset.returncode != 0:
            return status_reset.returncode
        reset_state = json.loads(status_reset.stdout)
        print(f"wf_reset_phase={reset_state.get('currentPhase')}")
        if reset_state.get("currentPhase") != "plan":
            return 1
        if reset_state.get("gates", {}).get("G2", {}).get("passed") is not None:
            return 1
        if reset_state.get("gates", {}).get("G5", {}).get("passed") is not None:
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
