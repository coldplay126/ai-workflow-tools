from __future__ import annotations

import json
import subprocess
from pathlib import Path

from fixture_support import (
    REVIEW_RESULT,
    VERIFY_RESULT,
    initialize_workflow_fixture,
    mark_workflow_prerequisites_passed,
    prepare_workflow_repo,
    run_awf,
)


def _assert_success(completed: subprocess.CompletedProcess[str]) -> None:
    assert completed.returncode == 0, (
        f"command failed with exit={completed.returncode}\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )


def _workflow_state(repo_root: Path) -> dict:
    state_path = repo_root / ".workflow" / "state.json"
    return json.loads(state_path.read_text(encoding="utf-8"))


def test_wf_next_runtime_smoke_blocks_phase_when_required_gate_missing(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    prepare_workflow_repo(repo_root)

    initialized = initialize_workflow_fixture(
        repo_root,
        "Fixture negative runtime smoke concept covering missing gate preconditions",
    )
    _assert_success(initialized)

    blocked = run_awf(
        repo_root,
        "wf",
        "next",
        "--phase",
        "review",
        "--provider",
        "fixture",
        "--mode",
        "solo",
        "--dry-run",
    )
    assert blocked.returncode == 2
    assert "Precondition failed for phase 'review'" in blocked.stderr
    assert "gate G1 must pass first" in blocked.stderr

    state = _workflow_state(repo_root)
    assert state["currentPhase"] == "plan"
    assert state["phases"]["review"]["status"] == "pending"
    assert state["gates"]["G1"]["passed"] is None
    assert state["gates"]["G2"]["passed"] is None


def test_wf_next_runtime_smoke_blocks_phase_when_required_artifact_missing(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    prepare_workflow_repo(repo_root)

    initialized = initialize_workflow_fixture(
        repo_root,
        "Fixture negative runtime smoke concept covering missing artifacts",
    )
    _assert_success(initialized)
    mark_workflow_prerequisites_passed(repo_root)

    missing_spec = repo_root / ".workflow" / "artifacts" / "spec.md"
    missing_spec.unlink()

    blocked = run_awf(
        repo_root,
        "wf",
        "next",
        "--phase",
        "review",
        "--provider",
        "fixture",
        "--mode",
        "solo",
        "--dry-run",
    )
    assert blocked.returncode == 2
    assert "Missing required workflow artifact" in blocked.stderr
    assert ".workflow/artifacts/spec.md" in blocked.stderr

    state = _workflow_state(repo_root)
    assert state["currentPhase"] == "plan"
    assert state["phases"]["plan"]["status"] == "completed"
    assert state["phases"]["review"]["status"] == "pending"
    assert state["gates"]["G1"]["passed"] is True
    assert state["gates"]["G2"]["passed"] is None


def test_wf_next_runtime_smoke_uses_agent_cards_and_updates_status(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    prepare_workflow_repo(repo_root)

    initialized = initialize_workflow_fixture(
        repo_root,
        "Fixture runtime smoke concept covering workflow review and verify gates",
    )
    _assert_success(initialized)

    status_before = run_awf(repo_root, "wf", "status", "--json")
    _assert_success(status_before)
    before = json.loads(status_before.stdout)
    assert before["currentPhase"] == "plan"
    assert before["gates"]["G2"]["passed"] is None
    assert (repo_root / ".workflow" / "agent-cards" / "review.json").is_file()

    mark_workflow_prerequisites_passed(repo_root)

    dry_run = run_awf(
        repo_root,
        "wf",
        "next",
        "--phase",
        "review",
        "--provider",
        "fixture",
        "--mode",
        "solo",
        "--dry-run",
        "--print-prompt",
    )
    _assert_success(dry_run)
    assert "phase: review" in dry_run.stdout
    assert "--- spec (artifacts/spec.md) ---" in dry_run.stdout
    assert (
        "- review_report: .workflow/artifacts/review-report.md (markdown)"
        in dry_run.stdout
    )
    assert "id: G2" in dry_run.stdout

    after_dry_run = _workflow_state(repo_root)
    assert after_dry_run["phases"]["review"]["status"] == "pending"
    assert after_dry_run["gates"]["G2"]["passed"] is None

    reviewed = run_awf(
        repo_root,
        "wf",
        "next",
        "--phase",
        "review",
        "--provider",
        "fixture",
        "--mode",
        "solo",
        "--auto-apply",
        "--yolo",
        extra_env={"AWF_FIXTURE_RESULT_FILE": str(REVIEW_RESULT)},
    )
    _assert_success(reviewed)
    assert "applied_gate: PASS" in reviewed.stdout

    after_review = _workflow_state(repo_root)
    assert after_review["phases"]["review"]["status"] == "completed"
    assert after_review["gates"]["G2"]["passed"] is True
    assert after_review["currentPhase"] == "approve"
    assert (repo_root / ".workflow" / "artifacts" / "review-report.md").is_file()

    verified = run_awf(
        repo_root,
        "wf",
        "next",
        "--phase",
        "verify",
        "--provider",
        "fixture",
        "--mode",
        "solo",
        "--auto-apply",
        "--yolo",
        extra_env={"AWF_FIXTURE_RESULT_FILE": str(VERIFY_RESULT)},
    )
    _assert_success(verified)
    assert "applied_gate: PASS" in verified.stdout

    status_after = run_awf(repo_root, "wf", "status", "--json")
    _assert_success(status_after)
    after = json.loads(status_after.stdout)
    assert after["phases"]["verify"]["status"] == "completed"
    assert after["gates"]["G5"]["passed"] is True
    assert after["currentPhase"] == "test"
    assert (repo_root / ".workflow" / "artifacts" / "verification-report.md").is_file()
