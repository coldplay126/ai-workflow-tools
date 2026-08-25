from __future__ import annotations

import hashlib
import json
from pathlib import Path

from awf.cli import main


_PLAN_ARTIFACTS = {
    "constitution.md": "# Constitution\n",
    "spec.md": "# Spec\n",
    "plan.md": "# Plan\n",
    "tasks.md": "- [ ] T01 Deliver\n",
    "test-criteria.md": "# Test criteria\n",
    "allowed-files.json": "{\"allowed_files\":[]}\n",
}


def _write_plan_root(root: Path, planning_options: dict[str, object]) -> None:
    workflow = root / ".workflow"
    artifacts = workflow / "artifacts"
    artifacts.mkdir(parents=True)
    (workflow / "manifest.json").write_text(
        json.dumps({"planning_options": {"required": True}}), encoding="utf-8"
    )
    (artifacts / "planning-options.json").write_text(
        json.dumps(planning_options), encoding="utf-8"
    )
    for name, content in _PLAN_ARTIFACTS.items():
        (artifacts / name).write_text(content, encoding="utf-8")


def _no_decision_options() -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "no_decision_required",
        "no_decision_reason": "One safe delivery path is already determined.",
        "decisions": [],
        "selection_history": [],
    }


def _selection_required_options() -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "selection_required",
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
                "selected_option_id": None,
                "selected_by": None,
                "selected_at": None,
            }
        ],
        "selection_history": [],
    }


def test_seal_plan_writes_strict_six_artifact_marker_as_json(
    tmp_path: Path, capsys
):
    _write_plan_root(tmp_path, _no_decision_options())

    rc = main(["wf", "seal-plan", "--repo-root", str(tmp_path), "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    marker_path = tmp_path / ".workflow" / "artifacts" / "planning-provenance.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    expected_hashes = {
        name: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for name, content in _PLAN_ARTIFACTS.items()
    }
    assert marker == {
        "schema_version": 1,
        "planning_options_hash": payload["planning_options_hash"],
        "artifacts": expected_hashes,
    }
    assert payload == {
        "status": "sealed",
        "planning_options_hash": marker["planning_options_hash"],
        "artifacts": expected_hashes,
    }


def test_seal_plan_blocks_unselected_material_options(tmp_path: Path, capsys) -> None:
    _write_plan_root(tmp_path, _selection_required_options())

    rc = main(["wf", "seal-plan", "--repo-root", str(tmp_path), "--json"])

    assert rc == 1
    assert json.loads(capsys.readouterr().out) == {"status": "blocked", "reason": "selection_required"}
    assert not (tmp_path / ".workflow" / "artifacts" / "planning-provenance.json").exists()
