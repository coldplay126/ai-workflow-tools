from __future__ import annotations

import json
from pathlib import Path

from awf.core.workflow_envelope import normalize_worker_result
from awf.core.workflow_prompt import build_workflow_prompt


def test_workflow_prompt_includes_redacted_omp_followup_evidence(tmp_path: Path):
    workflow = tmp_path / ".workflow"
    cards = workflow / "agent-cards"
    dispatch = workflow / "artifacts" / "dispatch"
    cards.mkdir(parents=True)
    dispatch.mkdir(parents=True)
    (cards / "plan.json").write_text(
        json.dumps(
            {
                "description": "Plan",
                "capabilities": {},
                "input": {"required_artifacts": []},
                "output": {},
            }
        ),
        encoding="utf-8",
    )
    (dispatch / "followup.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "backend": "omp",
                "mode": "agents:followup-omp",
                "run_id": "omp-child",
                "created_at": "2026-07-29T00:00:00+00:00",
                "status": "completed",
                "parent_run_id": "omp-parent",
                "parent_task_id": "task-parent",
                "message_sha256": "a" * 64,
                "agents": [
                    {
                        "status": "completed",
                        "task_id": "task-successor",
                        "agent_uri": "agent://task-successor",
                        "history_uri": "history://task-successor",
                        "output_sha256": "b" * 64,
                        "lineage": {
                            "followup_kind": "successor",
                            "parent_task_id": "task-parent",
                            "successor_task_id": "task-successor",
                        },
                        "followup_evidence": {
                            "hub": [
                                {
                                    "target_task_id": "task-parent",
                                    "outcome": "failed",
                                    "reason_code": "registry_unavailable",
                                }
                            ]
                        },
                        "stdout": "sensitive response body",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    prompt = build_workflow_prompt(
        str(tmp_path),
        {"repo": "fixture", "branch": "main"},
        {},
        "plan",
    )

    assert "=== OMP FOLLOW-UP EVIDENCE ===" in prompt
    assert "task-successor" in prompt
    assert "registry_unavailable" in prompt
    assert "sensitive response body" not in prompt


def test_workflow_prompt_keeps_review_provider_read_only(tmp_path: Path):
    workflow = tmp_path / ".workflow"
    cards = workflow / "agent-cards"
    cards.mkdir(parents=True)
    (cards / "review.json").write_text(
        json.dumps(
            {
                "description": "Review",
                "capabilities": {},
                "input": {"required_artifacts": []},
                "output": {},
            }
        ),
        encoding="utf-8",
    )

    prompt = build_workflow_prompt(
        str(tmp_path),
        {"repo": "fixture", "branch": "main"},
        {},
        "review",
    )

    assert "Return the required structured result only." in prompt
    assert "Do not write workflow artifacts" in prompt
    assert "write any required outputs to the documented paths" not in prompt


def test_plan_prompt_declares_selection_escape_and_normalizes_it(
    tmp_path: Path,
) -> None:
    cards = tmp_path / ".workflow" / "agent-cards"
    cards.mkdir(parents=True)
    (cards / "plan.json").write_text(
        json.dumps(
            {
                "description": "Plan",
                "capabilities": {},
                "input": {"required_artifacts": []},
                "output": {"structured_result": {"plan_artifact": "artifact path"}},
            }
        ),
        encoding="utf-8",
    )

    prompt = build_workflow_prompt(
        str(tmp_path),
        {"repo": "fixture", "branch": "main"},
        {},
        "plan",
    )
    normalized = normalize_worker_result(
        {
            "status": "escaped",
            "phase": "plan",
            "provider": "fixture",
            "result": {},
            "escape": {
                "severity": "blocking",
                "reason": "decision_selection_required",
                "summary": "A material plan decision needs an option selection.",
                "recommended_action": "user_decision",
            },
            "meta": {"format_version": 1},
        },
        phase="plan",
        provider="fixture",
    )

    assert "decision_selection_required" in prompt
    assert normalized["escape"]["reason"] == "decision_selection_required"
    assert normalized["escape"]["recommended_action"] == "user_decision"


def _selected_planning_options() -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "selected",
        "no_decision_reason": None,
        "decisions": [
            {
                "id": "D-001",
                "question": "Which rollout should be used?",
                "materiality_axes": ["compatibility_migration"],
                "options": [
                    {
                        "id": "O-001",
                        "summary": "Dual-read rollout",
                        "affected_work": ["service"],
                        "acceptance_delta": "Prove parity",
                        "work_risks": ["Extra paths"],
                        "transition_risks": ["Temporary reconciliation"],
                        "rollback_or_exit": "Restore legacy reads",
                    },
                    {
                        "id": "O-002",
                        "summary": "Single cutover",
                        "affected_work": ["service", "runbook"],
                        "acceptance_delta": "Rehearse cutover",
                        "work_risks": ["Less coverage"],
                        "transition_risks": ["Pause traffic"],
                        "rollback_or_exit": "Restore prior deployment",
                    },
                ],
                "recommended_option_id": "O-001",
                "recommendation_rationale": "Protect rollback.",
                "selected_option_id": "O-002",
                "selected_by": "operator",
                "selected_at": "2026-08-24T10:00:00.000000Z",
            }
        ],
        "selection_history": [
            {
                "decision_id": "D-001",
                "previous_option_id": None,
                "selected_option_id": "O-002",
                "selected_by": "operator",
                "selected_at": "2026-08-24T10:00:00.000000Z",
                "source": "cli",
            }
        ],
    }


def _write_planning_prompt_fixture(
    root: Path, planning_options: object, *, required: bool = True
) -> None:
    workflow = root / ".workflow"
    cards = workflow / "agent-cards"
    artifacts = workflow / "artifacts"
    cards.mkdir(parents=True)
    artifacts.mkdir()
    (workflow / "manifest.json").write_text(
        json.dumps({"planning_options": {"required": required}}),
        encoding="utf-8",
    )
    (artifacts / "planning-options.json").write_text(
        json.dumps(planning_options), encoding="utf-8"
    )
    (cards / "plan.json").write_text(
        json.dumps(
            {
                "description": "Plan",
                "capabilities": {},
                "input": {
                    "required_artifacts": [],
                    "planning_options": {
                        "policy": "manifest.planning_options.required",
                        "artifact": "artifacts/planning-options.json",
                    },
                },
                "output": {},
            }
        ),
        encoding="utf-8",
    )


def test_plan_prompt_embeds_the_validated_canonical_selected_options(
    tmp_path: Path,
) -> None:
    _write_planning_prompt_fixture(tmp_path, _selected_planning_options())

    prompt = build_workflow_prompt(
        str(tmp_path),
        {"repo": "fixture", "branch": "main"},
        {},
        "plan",
    )

    assert "=== PLANNING OPTIONS ===" in prompt
    assert '"status":"selected"' in prompt
    assert '"selected_option_id":"O-002"' in prompt
    assert "Which rollout should be used?" in prompt


def test_plan_prompt_uses_static_safe_instruction_for_invalid_options(
    tmp_path: Path,
) -> None:
    _write_planning_prompt_fixture(
        tmp_path,
        {
            "schema_version": 1,
            "password": "top-secret",
        },
    )

    prompt = build_workflow_prompt(
        str(tmp_path),
        {"repo": "fixture", "branch": "main"},
        {},
        "plan",
    )

    assert "Planning Options input is unavailable or invalid." in prompt
    assert "top-secret" not in prompt
