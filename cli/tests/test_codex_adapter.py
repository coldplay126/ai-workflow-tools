from __future__ import annotations

import json
import subprocess
from pathlib import Path

from fixture_support import (
    ROOT,
    awf_env,
    initialize_workflow_fixture,
    mark_workflow_prerequisites_passed,
    prepare_workflow_repo,
)


RUNNER = ROOT / "codex" / "run-wf.sh"


def _run_codex_runner(
    repo_root: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(RUNNER), *args],
        cwd=str(repo_root),
        env=awf_env(),
        capture_output=True,
        text=True,
    )


def _prepare_reviewable_workflow(repo_root: Path) -> None:
    prepare_workflow_repo(repo_root)
    initialized = initialize_workflow_fixture(
        repo_root,
        "Codex adapter deterministic preflight fixture",
    )
    assert initialized.returncode == 0, initialized.stderr
    mark_workflow_prerequisites_passed(repo_root)


def test_codex_runner_preflight_prints_awf_wf_next_dry_run_json(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    _prepare_reviewable_workflow(repo_root)

    completed = _run_codex_runner(repo_root, "preflight", "review", "codex")

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["phase"] == "review"
    assert payload["provider"] == "codex"
    assert payload["prompt_file"] == "(dry-run, not written)"
    assert "phase: review" in payload["prompt"]
    assert "review-report.md" in payload["prompt"]


def test_codex_runner_prompt_validates_preflight_before_writing_prompt(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    _prepare_reviewable_workflow(repo_root)

    completed = _run_codex_runner(repo_root, "prompt", "review", "codex")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("prompt: ")
    prompt_path = Path(completed.stdout.removeprefix("prompt: ").strip())
    assert prompt_path.is_file()
    assert prompt_path.parent == repo_root / ".workflow" / "tmp"
    prompt_text = prompt_path.read_text(encoding="utf-8")
    assert "=== META ===" in prompt_text
    assert "Phase: review" in prompt_text
