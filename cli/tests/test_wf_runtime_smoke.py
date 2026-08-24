from __future__ import annotations

import getpass
import json
import shlex
import subprocess

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


def test_wf_reset_runtime_smoke_resets_state_and_preserves_runtime_files(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    prepare_workflow_repo(repo_root)
    concept = "fix typo in docs"

    initialized = initialize_workflow_fixture(repo_root, concept)
    _assert_success(initialized)
    mark_workflow_prerequisites_passed(repo_root)

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

    concept_path = repo_root / ".workflow" / "concept.md"
    concept_before = concept_path.read_text(encoding="utf-8")
    review_report = repo_root / ".workflow" / "artifacts" / "review-report.md"
    assert review_report.is_file()

    reset = run_awf(repo_root, "wf", "reset")
    _assert_success(reset)
    assert "workflow reset" in reset.stdout

    state = _workflow_state(repo_root)
    assert state["currentPhase"] == "plan"
    assert state["changeClass"] == "small"
    assert state["totalExecutions"] == 0
    assert state["history"] == []
    assert state["loop"]["replanCount"] == 0
    assert all(phase["status"] == "pending" for phase in state["phases"].values())
    assert all(gate["passed"] is None for gate in state["gates"].values())

    assert concept_path.read_text(encoding="utf-8") == concept_before
    assert (repo_root / ".workflow" / "provider-config.json").is_file()
    assert (repo_root / ".workflow" / "agent-cards" / "review.json").is_file()
    assert review_report.is_file()

    status = run_awf(repo_root, "wf", "status", "--json")
    _assert_success(status)
    assert json.loads(status.stdout)["currentPhase"] == "plan"


def test_wf_reset_runtime_smoke_replaces_explicit_concept_consistently(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    prepare_workflow_repo(repo_root)

    initialized = initialize_workflow_fixture(repo_root, "fix typo in docs")
    _assert_success(initialized)

    replacement = "payment production migration"
    reset = run_awf(repo_root, "wf", "reset", "--concept", replacement)
    _assert_success(reset)
    assert "workflow reset" in reset.stdout

    state = _workflow_state(repo_root)
    assert state["currentPhase"] == "plan"
    assert state["changeClass"] == "high_risk"
    assert "payment-production-migration" in state["id"]
    assert all(phase["status"] == "pending" for phase in state["phases"].values())
    assert all(gate["passed"] is None for gate in state["gates"].values())

    concept_text = (repo_root / ".workflow" / "concept.md").read_text(
        encoding="utf-8"
    )
    assert "fix typo in docs" not in concept_text
    assert replacement in concept_text
    assert "## 요구사항" in concept_text

    status = run_awf(repo_root, "wf", "status", "--json")
    _assert_success(status)
    status_payload = json.loads(status.stdout)
    assert status_payload["id"] == state["id"]
    assert status_payload["changeClass"] == "high_risk"


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


def test_wf_next_auto_apply_failure_applies_gate_once(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    prepare_workflow_repo(repo_root)

    initialized = initialize_workflow_fixture(
        repo_root,
        "Fixture runtime smoke concept covering failed review decision routing",
    )
    _assert_success(initialized)
    mark_workflow_prerequisites_passed(repo_root)

    failing_review = repo_root / "failing-review.json"
    failing_review.write_text(
        json.dumps(
            {
                "status": "completed",
                "phase": "review",
                "provider": "fixture",
                "result": {
                    "conclusion": "FAIL - unresolved high review finding",
                    "findings": [
                        {
                            "id": "F-HIGH",
                            "category": "scope",
                            "severity": "HIGH",
                            "locations": ["artifacts/tasks.md:T001"],
                            "summary": "High severity finding needs user decision",
                        }
                    ],
                    "coverage": {
                        "total_requirements": 1,
                        "mapped_requirements": 1,
                        "percentage": 100,
                        "gaps": [],
                    },
                    "evidence": [],
                    "risks": [],
                    "action_items": [],
                },
                "escape": None,
                "meta": {"format_version": 1},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

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
        extra_env={"AWF_FIXTURE_RESULT_FILE": str(failing_review)},
    )

    assert reviewed.returncode == 3
    assert "applied_gate: FAIL" in reviewed.stdout
    state = _workflow_state(repo_root)
    review_state = state["phases"]["review"]
    assert review_state["status"] == "deciding"
    assert review_state["retries"] == 1
    assert review_state["decision"] == "escalate_user"
    assert state["loop"]["pendingDecision"]["phase"] == "review"
    assert state["loop"]["pendingDecision"]["decision"] == "escalate_user"
    assert [entry["action"] for entry in state["history"]].count("deciding") == 1


def test_wf_next_verify_scope_violation_replans_to_approve(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    prepare_workflow_repo(repo_root)

    initialized = initialize_workflow_fixture(
        repo_root,
        "Fixture runtime smoke concept covering verify scope violation routing",
    )
    _assert_success(initialized)
    mark_workflow_prerequisites_passed(repo_root)

    failing_verify = repo_root / "failing-verify.json"
    failing_verify.write_text(
        json.dumps(
            {
                "status": "completed",
                "phase": "verify",
                "provider": "fixture",
                "result": {
                    "conclusion": "FAIL - scope violation",
                    "scope": {
                        "changed_files": 2,
                        "planned_files": 1,
                        "violations": 1,
                        "violation_files": ["src/unplanned.py"],
                    },
                    "compliance": {
                        "total_requirements": 1,
                        "pass": 1,
                        "warn": 0,
                        "fail": 0,
                        "percentage": 100,
                        "failed_requirements": [],
                    },
                    "quality": {
                        "critical": 0,
                        "high": 0,
                        "medium": 0,
                        "low": 0,
                        "issues": [],
                    },
                    "evidence": [],
                    "risks": [],
                    "action_items": [],
                },
                "escape": None,
                "meta": {"format_version": 1},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

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
        extra_env={"AWF_FIXTURE_RESULT_FILE": str(failing_verify)},
    )

    assert verified.returncode == 3
    assert "applied_gate: FAIL" in verified.stdout
    state = _workflow_state(repo_root)
    assert state["currentPhase"] == "approve"
    assert state["phases"]["approve"]["status"] == "pending"
    assert state["phases"]["impl"]["status"] == "pending"
    assert state["phases"]["verify"]["status"] == "pending"
    assert state["gates"]["G3"]["passed"] is None
    assert state["gates"]["G4"]["passed"] is None
    assert state["gates"]["G5"]["passed"] is None
    assert state["loop"]["replanCount"] == 1
    assert state["history"][-1]["action"] == "replanned"


def test_wf_next_verify_compliance_failure_replans_to_impl(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    prepare_workflow_repo(repo_root)

    initialized = initialize_workflow_fixture(
        repo_root,
        "Fixture runtime smoke concept covering verify implementation bug routing",
    )
    _assert_success(initialized)
    mark_workflow_prerequisites_passed(repo_root)

    failing_verify = repo_root / "failing-verify-impl.json"
    failing_verify.write_text(
        json.dumps(
            {
                "status": "completed",
                "phase": "verify",
                "provider": "fixture",
                "result": {
                    "conclusion": "FAIL - compliance failure",
                    "scope": {
                        "changed_files": 1,
                        "planned_files": 1,
                        "violations": 0,
                        "violation_files": [],
                    },
                    "compliance": {
                        "total_requirements": 2,
                        "pass": 1,
                        "warn": 0,
                        "fail": 1,
                        "percentage": 50,
                        "failed_requirements": ["FR-002"],
                    },
                    "quality": {
                        "critical": 0,
                        "high": 0,
                        "medium": 0,
                        "low": 0,
                        "issues": [],
                    },
                    "evidence": [],
                    "risks": [],
                    "action_items": [],
                },
                "escape": None,
                "meta": {"format_version": 1},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

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
        extra_env={"AWF_FIXTURE_RESULT_FILE": str(failing_verify)},
    )

    assert verified.returncode == 3
    assert "applied_gate: FAIL" in verified.stdout
    state = _workflow_state(repo_root)
    assert state["currentPhase"] == "impl"
    assert state["phases"]["impl"]["status"] == "pending"
    assert state["phases"]["verify"]["status"] == "pending"
    assert state["gates"]["G4"]["passed"] is None
    assert state["gates"]["G5"]["passed"] is None
    assert state["loop"]["replanCount"] == 1
    assert state["history"][-1]["action"] == "replanned"


def test_wf_next_plan_user_decision_escape_prints_safe_selection_commands(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    prepare_workflow_repo(repo_root)
    initialized = initialize_workflow_fixture(
        repo_root,
        "Fixture runtime smoke concept requiring a material plan selection",
    )
    _assert_success(initialized)

    (repo_root / ".workflow" / "artifacts" / "planning-options.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "selection_required",
                "no_decision_reason": None,
                "decisions": [
                    {
                        "id": "D-001",
                        "question": "Which rollout should apply?",
                        "materiality_axes": [
                            "compatibility_migration",
                            "security_slo",
                        ],
                        "options": [
                            {
                                "id": "O-001",
                                "summary": "Use the guarded rollout.",
                                "affected_work": ["service", "tests"],
                                "acceptance_delta": "Acceptance evidence changes with the rollout.",
                                "work_risks": ["Implementation work differs."],
                                "transition_risks": ["Transition behavior differs."],
                                "rollback_or_exit": "Restore the prior release before reopening traffic.",
                            },
                            {
                                "id": "O-002",
                                "summary": "Use the direct cutover.",
                                "affected_work": ["service", "tests"],
                                "acceptance_delta": "Acceptance evidence changes after direct cutover.",
                                "work_risks": ["Implementation work changes differently."],
                                "transition_risks": ["Transition behavior changes differently."],
                                "rollback_or_exit": "Restore the prior release before reopening traffic.",
                            },
                        ],
                        "recommended_option_id": "O-001",
                        "recommendation_rationale": "The guarded rollout preserves an explicit exit path.",
                        "selected_option_id": None,
                        "selected_by": None,
                        "selected_at": None,
                    }
                ],
                "selection_history": [],
            }
        ),
        encoding="utf-8",
    )
    escaped_plan = repo_root / "escaped-plan.json"
    escaped_plan.write_text(
        json.dumps(
            {
                "status": "escaped",
                "phase": "plan",
                "provider": "fixture",
                "result": {},
                "escape": {
                    "severity": "blocking",
                    "reason": "decision_selection_required",
                    "summary": "Worker summary that must never appear.",
                    "recommended_action": "user_decision",
                },
                "meta": {"format_version": 1},
            }
        ),
        encoding="utf-8",
    )

    escaped = run_awf(
        repo_root,
        "wf",
        "next",
        "--phase",
        "plan",
        "--provider",
        "fixture",
        "--mode",
        "solo",
        "--auto-apply",
        "--yolo",
        extra_env={"AWF_FIXTURE_RESULT_FILE": str(escaped_plan)},
    )

    assert escaped.returncode == 5
    actor = shlex.quote(getpass.getuser())
    recommended_command = (
        "awf wf select-option --decision-id D-001 --option-id O-001 "
        f"--actor {actor} --repo-root . --json"
    )
    alternative_command = (
        "awf wf select-option --decision-id D-001 --option-id O-002 "
        f"--actor {actor} --repo-root . --json"
    )
    assert "decision_id: D-001" in escaped.stderr
    assert "recommended_option_id: O-001" in escaped.stderr
    assert "option_ids: O-001, O-002" in escaped.stderr
    assert recommended_command in escaped.stderr
    assert alternative_command in escaped.stderr
    assert escaped.stderr.index(recommended_command) < escaped.stderr.index(
        alternative_command
    )
    assert "review wf status and decide" not in escaped.stderr
    assert "Worker summary" not in escaped.stderr
    assert "guarded rollout" not in escaped.stderr
    assert "Which rollout" not in escaped.stderr

    state = _workflow_state(repo_root)
    assert state["phases"]["plan"]["status"] == "deciding"
    assert state["phases"]["plan"]["decision"] == "user_decision"
    assert state["phases"]["plan"]["escapeSummary"] == ""
    assert state["loop"]["lastEscape"]["summary"] == ""
    assert "Worker summary" not in json.dumps(state)
