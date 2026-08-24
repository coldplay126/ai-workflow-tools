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
