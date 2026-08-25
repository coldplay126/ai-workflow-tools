"""Durable CLI selection and workflow-reconciliation tests."""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from awf.cli import build_parser, main
from awf.commands import wf as wf_commands
from awf.commands.wf import run_wf_select_option
from awf.core.planning_options import load_planning_options
from awf.core import planning_options, state as workflow_state


_PHASES = ("plan", "review", "approve", "impl", "verify", "test", "done")


def _option(option_id: str, summary: str) -> dict[str, object]:
    return {
        "id": option_id,
        "summary": summary,
        "affected_work": ["service", "tests"],
        "acceptance_delta": "Acceptance evidence changes with the rollout.",
        "work_risks": ["Implementation work differs."],
        "transition_risks": ["Transition behavior differs."],
        "rollback_or_exit": "Restore the prior release before reopening traffic.",
    }


def _decision(
    decision_id: str,
    *,
    selected_option_id: str | None = None,
    selected_by: str | None = None,
    selected_at: str | None = None,
) -> dict[str, object]:
    return {
        "id": decision_id,
        "question": f"Which rollout should apply for {decision_id}?",
        "materiality_axes": ["compatibility_migration", "security_slo"],
        "options": [
            _option("O-001", "Use the guarded rollout."),
            _option("O-002", "Use the direct cutover."),
        ],
        "recommended_option_id": "O-001",
        "recommendation_rationale": "The guarded rollout preserves an explicit exit path.",
        "selected_option_id": selected_option_id,
        "selected_by": selected_by,
        "selected_at": selected_at,
    }


def _artifact(*, decision_count: int = 1, selected: bool = False) -> dict[str, object]:
    selected_at = "2026-08-24T10:00:00Z" if selected else None
    decisions = [
        _decision(
            f"D-{index:03d}",
            selected_option_id="O-001" if selected else None,
            selected_by="prior-operator" if selected else None,
            selected_at=selected_at,
        )
        for index in range(1, decision_count + 1)
    ]
    history = [
        {
            "decision_id": decision["id"],
            "previous_option_id": None,
            "selected_option_id": "O-001",
            "selected_by": "prior-operator",
            "selected_at": selected_at,
            "source": "cli",
        }
        for decision in decisions
    ] if selected else []
    return {
        "schema_version": 1,
        "status": "selected" if selected else "selection_required",
        "no_decision_reason": None,
        "decisions": decisions,
        "selection_history": history,
    }


def _write_artifact(root: Path, artifact: dict[str, object]) -> None:
    path = root / ".workflow" / "artifacts" / "planning-options.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact), encoding="utf-8")


def _write_derived_plan_artifacts(root: Path) -> None:
    artifacts = root / ".workflow" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    contents = {
        "constitution.md": "# Constitution\n",
        "spec.md": "# Spec\n\n- FR-001: Persist the chosen rollout.\n",
        "plan.md": "# Plan\n\n- [FR-001] Persist the chosen rollout.\n",
        "tasks.md": "# Tasks\n\n- [ ] T001 [FR-001] Exercise the selection lifecycle.\n",
        "test-criteria.md": "# Test Criteria\n\n- [FR-001] Selection resumes planning.\n",
        "allowed-files.json": "{\"allowed_files\":[]}\n",
    }
    for name, text in contents.items():
        (artifacts / name).write_text(text, encoding="utf-8")


def _state(*, current_phase: str, plan_status: str, g1_passed: bool | None) -> dict:
    phases = {phase: {"status": "pending", "retries": 0} for phase in _PHASES}
    phases["plan"]["status"] = plan_status
    if current_phase != "plan":
        for phase in _PHASES[1:_PHASES.index(current_phase)]:
            phases[phase]["status"] = "completed"
    return {
        "id": "planning-option-selection",
        "repo": "selection-repo",
        "branch": "main",
        "currentPhase": current_phase,
        "phases": phases,
        "gates": {
            "G1": {"passed": g1_passed},
            "G2": {"passed": True, "provider": "fixture", "provider_status": "PASS"},
            "G3": {"passed": True, "scope_hash": "prior-scope"},
            "G4": {"passed": True},
            "G5": {"passed": True, "provider": "fixture", "provider_status": "PASS"},
            "G6": {"passed": True},
        },
        "totalExecutions": 0,
        "loop": {"replanCount": 0, "maxReplans": 3},
        "history": [],
    }


def _write_state(root: Path, state: dict) -> None:
    path = root / ".workflow" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def _load_state(root: Path) -> dict:
    return json.loads((root / ".workflow" / "state.json").read_text(encoding="utf-8"))

def _reconciliation_journal(root: Path) -> dict[str, object]:
    artifact = load_planning_options(root)
    return {
        "schema_version": 1,
        "current_hash": artifact.artifact_hash,
        "previous_hash": "f" * 64,
        "decision_id": "D-001",
        "option_id": "O-001",
        "artifact_status": artifact.status,
        "source": "cli",
    }


def _write_reconciliation_marker(root: Path, journal: dict[str, object]) -> None:
    with planning_options.planning_option_selection_transaction(
        root
    ) as (_, directory_fd):
        planning_options._write_reconciliation_marker(directory_fd, journal)




def _args(
    root: Path,
    decision_id: str,
    option_id: str,
    actor: str = "operator",
    *,
    json_output: bool = False,
) -> Namespace:
    return Namespace(
        repo_root=str(root),
        decision_id=decision_id,
        option_id=option_id,
        actor=actor,
        json=json_output,
    )


def test_selection_guidance_uses_only_unselected_ids_and_a_quoted_local_actor(
    tmp_path: Path, monkeypatch
) -> None:
    artifact = _artifact(decision_count=2)
    decisions = artifact["decisions"]
    assert isinstance(decisions, list)
    first = decisions[0]
    assert isinstance(first, dict)
    first["selected_option_id"] = "O-001"
    first["selected_by"] = "prior-operator"
    first["selected_at"] = "2026-08-24T10:00:00Z"
    artifact["selection_history"] = [
        {
            "decision_id": "D-001",
            "previous_option_id": None,
            "selected_option_id": "O-001",
            "selected_by": "prior-operator",
            "selected_at": "2026-08-24T10:00:00Z",
            "source": "cli",
        }
    ]
    _write_artifact(tmp_path, artifact)
    monkeypatch.setattr("getpass.getuser", lambda: "local operator")

    guidance = wf_commands._planning_option_selection_guidance(tmp_path)

    assert guidance == (
        "decision_id: D-002",
        "recommended_option_id: O-001",
        "option_ids: O-001, O-002",
        "select_option: awf wf select-option --decision-id D-002 --option-id O-001 --actor 'local operator' --repo-root . --json",
        "select_option: awf wf select-option --decision-id D-002 --option-id O-002 --actor 'local operator' --repo-root . --json",
    )
    rendered = "\n".join(guidance)
    assert "D-001" not in rendered
    assert "guarded rollout" not in rendered
    assert "Which rollout" not in rendered


def test_selection_guidance_returns_fixed_generic_error_for_malformed_artifact(
    tmp_path: Path,
) -> None:
    artifact_path = tmp_path / ".workflow" / "artifacts" / "planning-options.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(
        '{"unknown": "sensitive option body that must not appear"}',
        encoding="utf-8",
    )

    guidance = wf_commands._planning_option_selection_guidance(tmp_path)

    assert guidance == ("error: planning option selection is unavailable",)


def test_select_option_parser_requires_exact_flags_and_supports_json() -> None:
    parsed = build_parser().parse_args(
        [
            "wf",
            "select-option",
            "--decision-id",
            "D-001",
            "--option-id",
            "O-002",
            "--actor",
            "operator",
            "--json",
        ]
    )

    assert parsed.decision_id == "D-001"
    assert parsed.option_id == "O-002"
    assert parsed.actor == "operator"
    assert parsed.json is True
    assert parsed.handler is run_wf_select_option
    with pytest.raises(SystemExit):
        build_parser().parse_args(["wf", "select-option", "--decision-id", "D-001"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["wf", "select-option", "D-001", "O-002", "--selected-by", "operator"]
        )


def test_initial_deciding_phase_waits_for_every_selection_before_continuing(tmp_path: Path) -> None:
    _write_artifact(tmp_path, _artifact(decision_count=2))
    _write_state(tmp_path, _state(current_phase="plan", plan_status="deciding", g1_passed=None))

    first = run_wf_select_option(_args(tmp_path, "D-001", "O-002"))
    partial_state = _load_state(tmp_path)
    second = run_wf_select_option(_args(tmp_path, "D-002", "O-002"))
    completed_state = _load_state(tmp_path)

    assert first == 0
    assert partial_state["currentPhase"] == "plan"
    assert partial_state["phases"]["plan"]["status"] == "deciding"
    assert partial_state["planningOptions"]["action"] == "selected_pending"
    assert second == 0
    assert completed_state["phases"]["plan"]["status"] == "in_progress"
    assert completed_state["planningOptions"]["action"] == "continued"
    assert [item["action"] for item in completed_state["history"]] == ["continued"]


def test_post_g1_selection_replans_and_resets_every_downstream_gate(tmp_path: Path) -> None:
    _write_artifact(tmp_path, _artifact(selected=True))
    _write_state(tmp_path, _state(current_phase="impl", plan_status="completed", g1_passed=True))

    rc = run_wf_select_option(_args(tmp_path, "D-001", "O-002"))
    state = _load_state(tmp_path)

    assert rc == 0
    assert state["currentPhase"] == "plan"
    assert {phase["status"] for phase in state["phases"].values()} == {"pending"}
    assert state["gates"] == {
        "G1": {"passed": None},
        "G2": {"passed": None, "provider": None, "provider_status": None},
        "G3": {"passed": None, "scope_hash": None},
        "G4": {"passed": None},
        "G5": {"passed": None, "provider": None, "provider_status": None},
        "G6": {"passed": None},
    }
    assert state["loop"]["replanCount"] == 1
    assert [entry["action"] for entry in state["history"]].count("replanned") == 1


def test_post_g1_same_option_reuse_preserves_gates_and_replan_count(tmp_path: Path) -> None:
    _write_artifact(tmp_path, _artifact(selected=True))
    state = _state(current_phase="review", plan_status="completed", g1_passed=True)
    state["planningOptions"] = {
        "artifactHash": load_planning_options(tmp_path).artifact_hash,
        "decision": "D-001",
        "option": "O-001",
        "appliedAt": "2026-08-24T10:00:00Z",
        "action": "replanned",
    }
    _write_state(tmp_path, state)

    before = _load_state(tmp_path)
    assert run_wf_select_option(_args(tmp_path, "D-001", "O-001")) == 0
    after_reuse = _load_state(tmp_path)

    assert after_reuse["gates"] == before["gates"]
    assert after_reuse["loop"]["replanCount"] == 0
    assert after_reuse["history"] == before["history"]


def test_marker_absent_same_option_reuse_preserves_post_g1_state(tmp_path: Path) -> None:
    _write_artifact(tmp_path, _artifact(selected=True))
    _write_state(tmp_path, _state(current_phase="review", plan_status="completed", g1_passed=True))

    assert run_wf_select_option(_args(tmp_path, "D-001", "O-001")) == 0
    reused = _load_state(tmp_path)

    assert reused["loop"]["replanCount"] == 0
    assert reused["gates"]["G3"]["scope_hash"] == "prior-scope"




def test_empty_reconciliation_journal_blocks_retry_without_state_transition(
    tmp_path: Path
) -> None:
    _write_artifact(tmp_path, _artifact(selected=True))
    _write_state(tmp_path, _state(current_phase="review", plan_status="completed", g1_passed=True))
    marker = tmp_path / ".workflow" / "artifacts" / ".planning-options-reconcile"
    marker.write_text("", encoding="utf-8")

    assert run_wf_select_option(_args(tmp_path, "D-001", "O-001")) == 1

    assert _load_state(tmp_path)["loop"]["replanCount"] == 0

@pytest.mark.parametrize(
    "case",
    (
        "boolean_schema_version",
        "invalid_previous_hash",
        "equal_previous_hash",
        "unexpected_source",
        "unexpected_status",
        "partial_artifact_claims_selected",
        "unknown_decision",
        "wrong_option",
        "history_mismatch",
    ),
)
def test_forged_reconciliation_journal_never_transitions_state(
    tmp_path: Path, case: str
) -> None:
    artifact = _artifact(
        decision_count=2
        if case in {"partial_artifact_claims_selected", "history_mismatch"}
        else 1,
        selected=True,
    )
    if case == "partial_artifact_claims_selected":
        artifact["status"] = "selection_required"
        partial_decision = artifact["decisions"][1]
        assert isinstance(partial_decision, dict)
        partial_decision.update(
            {
                "selected_option_id": None,
                "selected_by": None,
                "selected_at": None,
            }
        )
        artifact["selection_history"] = artifact["selection_history"][:1]
    _write_artifact(tmp_path, artifact)
    _write_state(
        tmp_path,
        _state(current_phase="plan", plan_status="deciding", g1_passed=None),
    )

    journal = _reconciliation_journal(tmp_path)
    if case == "boolean_schema_version":
        journal["schema_version"] = True
    elif case == "invalid_previous_hash":
        journal["previous_hash"] = "not-a-sha256"
    elif case == "equal_previous_hash":
        journal["previous_hash"] = journal["current_hash"]
    elif case == "unexpected_source":
        journal["source"] = "agent"
    elif case == "unexpected_status":
        journal["artifact_status"] = "selection_required"
    elif case == "partial_artifact_claims_selected":
        journal["artifact_status"] = "selected"
    elif case == "unknown_decision":
        journal["decision_id"] = "D-999"
    elif case == "wrong_option":
        journal["option_id"] = "O-002"
    _write_reconciliation_marker(tmp_path, journal)

    state_path = tmp_path / ".workflow" / "state.json"
    artifact_path = tmp_path / ".workflow" / "artifacts" / "planning-options.json"
    state_before = state_path.read_bytes()
    state_before_payload = json.loads(state_before)
    artifact_before = artifact_path.read_bytes()
    artifact_before_hash = load_planning_options(tmp_path).artifact_hash
    artifact_before_history_count = len(load_planning_options(tmp_path).selection_history)

    assert run_wf_select_option(_args(tmp_path, "D-001", "O-001")) == 1

    assert state_path.read_bytes() == state_before
    state_after = _load_state(tmp_path)
    assert state_after["currentPhase"] == "plan"
    assert state_after["phases"]["plan"]["status"] == "deciding"
    assert state_after["gates"] == state_before_payload["gates"]
    assert artifact_path.read_bytes() == artifact_before
    artifact_after = load_planning_options(tmp_path)
    assert artifact_after.artifact_hash == artifact_before_hash
    assert len(artifact_after.selection_history) == artifact_before_history_count


def test_retry_after_state_failure_reconciles_without_duplicate_replan_history(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _write_artifact(tmp_path, _artifact(selected=True))
    _write_derived_plan_artifacts(tmp_path)
    before_selection = load_planning_options(tmp_path)
    _write_state(tmp_path, _state(current_phase="review", plan_status="completed", g1_passed=True))

    with monkeypatch.context() as patch:
        patch.setattr(
            wf_commands,
            "apply_planning_option_selection",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("state blocked")),
        )
        assert run_wf_select_option(_args(tmp_path, "D-001", "O-002")) == 1

    failed_output = capsys.readouterr().err
    persisted_artifact = json.loads(
        (tmp_path / ".workflow" / "artifacts" / "planning-options.json").read_text(encoding="utf-8")
    )
    assert "top-secret" not in failed_output
    assert persisted_artifact["decisions"][0]["selected_option_id"] == "O-002"
    assert len(persisted_artifact["selection_history"]) == 2
    artifacts = tmp_path / ".workflow" / "artifacts"
    for name in (
        "constitution.md",
        "spec.md",
        "plan.md",
        "tasks.md",
        "test-criteria.md",
        "allowed-files.json",
    ):
        assert not (artifacts / name).exists()
        assert (
            artifacts / f".stale.{before_selection.artifact_hash[:12]}.{name}"
        ).exists()
    _write_derived_plan_artifacts(tmp_path)


    assert run_wf_select_option(_args(tmp_path, "D-001", "O-002")) == 0
    assert not any(
        (artifacts / name).exists()
        for name in (
            "constitution.md",
            "spec.md",
            "plan.md",
            "tasks.md",
            "test-criteria.md",
            "allowed-files.json",
        )
    )
    reconciled = _load_state(tmp_path)
    assert reconciled["loop"]["replanCount"] == 1
    assert [entry["action"] for entry in reconciled["history"]].count("replanned") == 1


def test_select_option_rejects_invalid_input_without_echoing_sensitive_values(
    tmp_path: Path, capsys
) -> None:
    _write_artifact(tmp_path, _artifact())
    _write_state(tmp_path, _state(current_phase="plan", plan_status="deciding", g1_passed=None))

    rc = run_wf_select_option(_args(tmp_path, "D-001", "O-099", "password=top-secret"))

    assert rc == 1
    assert "top-secret" not in capsys.readouterr().err


def test_plan_in_progress_selection_change_replans_instead_of_leaving_stale_plan(
    tmp_path: Path,
) -> None:
    _write_artifact(tmp_path, _artifact(selected=True))
    _write_state(tmp_path, _state(current_phase="plan", plan_status="in_progress", g1_passed=None))

    assert run_wf_select_option(_args(tmp_path, "D-001", "O-002")) == 0
    state = _load_state(tmp_path)

    assert state["currentPhase"] == "plan"
    assert state["phases"]["plan"]["status"] == "pending"
    assert state["loop"]["replanCount"] == 1


def test_select_option_json_exposes_only_ids_status_actions_and_hashes(
    tmp_path: Path, capsys
) -> None:
    _write_artifact(tmp_path, _artifact())
    _write_state(tmp_path, _state(current_phase="plan", plan_status="deciding", g1_passed=None))

    assert run_wf_select_option(
        _args(tmp_path, "D-001", "O-002", json_output=True)
    ) == 0
    payload = json.loads(capsys.readouterr().out)

    assert set(payload) == {
        "decision_id",
        "option_id",
        "status",
        "selection_action",
        "workflow_action",
        "previous_hash",
        "current_hash",
    }
    assert payload["decision_id"] == "D-001"
    assert payload["option_id"] == "O-002"
    assert payload["status"] == "selected"
    assert payload["selection_action"] == "selected"
    assert payload["workflow_action"] == "continued"
    assert "guarded rollout" not in json.dumps(payload)


def test_state_marker_makes_reconciliation_idempotent_after_a_success(tmp_path: Path) -> None:
    _write_artifact(tmp_path, _artifact(selected=True))
    _write_state(tmp_path, _state(current_phase="review", plan_status="completed", g1_passed=True))

    assert run_wf_select_option(_args(tmp_path, "D-001", "O-002")) == 0
    state = _load_state(tmp_path)

    assert state["planningOptions"]["artifactHash"]
    assert state["planningOptions"]["decision"] == "D-001"
    assert state["planningOptions"]["option"] == "O-002"
    assert state["planningOptions"]["action"] == "replanned"


def test_aborted_state_rejects_a_change_before_artifact_publication(
    tmp_path: Path, capsys
) -> None:
    _write_artifact(tmp_path, _artifact(selected=True))
    state = _state(current_phase="plan", plan_status="completed", g1_passed=True)
    state["currentPhase"] = "aborted"
    _write_state(tmp_path, state)
    original = (
        tmp_path / ".workflow" / "artifacts" / "planning-options.json"
    ).read_text(encoding="utf-8")

    assert run_wf_select_option(_args(tmp_path, "D-001", "O-002")) == 1

    assert "rejected" in capsys.readouterr().err
    assert (
        tmp_path / ".workflow" / "artifacts" / "planning-options.json"
    ).read_text(encoding="utf-8") == original


def test_select_option_returns_operational_exit_without_exposing_a_bad_root(
    tmp_path: Path, capsys
) -> None:
    missing_root = tmp_path / "missing-root"

    assert run_wf_select_option(_args(missing_root, "D-001", "O-002")) == 2

    output = capsys.readouterr().err
    assert "unavailable" in output
    assert str(missing_root) not in output


def test_select_option_rejects_a_symlinked_root_before_artifact_publication(
    tmp_path: Path, capsys
) -> None:
    _write_artifact(tmp_path, _artifact())
    _write_state(tmp_path, _state(current_phase="plan", plan_status="deciding", g1_passed=None))
    root_link = tmp_path.parent / f"{tmp_path.name}-root-link"
    root_link.symlink_to(tmp_path, target_is_directory=True)

    assert run_wf_select_option(_args(root_link, "D-001", "O-002")) == 2

    assert "unavailable" in capsys.readouterr().err
    artifact = json.loads(
        (tmp_path / ".workflow" / "artifacts" / "planning-options.json").read_text(
            encoding="utf-8"
        )
    )
    assert artifact["decisions"][0]["selected_option_id"] is None

def test_select_option_resolves_once_and_keeps_the_canonical_root_after_retarget(
    tmp_path: Path, monkeypatch
) -> None:
    original_root = tmp_path / "original"
    redirected_parent = tmp_path / "redirected-parent"
    redirected_root = redirected_parent / "original"
    for root in (original_root, redirected_root):
        _write_artifact(root, _artifact())
        _write_state(root, _state(current_phase="plan", plan_status="deciding", g1_passed=None))

    supplied_parent = tmp_path / "supplied-parent"
    supplied_parent.symlink_to(tmp_path, target_is_directory=True)
    supplied_root = supplied_parent / "original"
    resolver_calls: list[object] = []
    original_find_repo_root = workflow_state.find_repo_root

    def find_and_retarget(explicit_root=None):
        resolver_calls.append(explicit_root)
        root = original_find_repo_root(explicit_root)
        if len(resolver_calls) == 1:
            supplied_parent.unlink()
            supplied_parent.symlink_to(redirected_parent, target_is_directory=True)
        return root

    state_roots: list[Path] = []
    original_open_state_directory = workflow_state._open_workflow_state_directory

    def capture_state_root(root: Path) -> int:
        state_roots.append(root)
        return original_open_state_directory(root)

    artifact_roots: list[Path] = []
    original_artifact_transaction = planning_options._planning_options_transaction

    def capture_artifact_root(root: Path):
        artifact_roots.append(root)
        return original_artifact_transaction(root)

    monkeypatch.setattr(workflow_state, "find_repo_root", find_and_retarget)
    monkeypatch.setattr(
        workflow_state, "_open_workflow_state_directory", capture_state_root
    )
    monkeypatch.setattr(
        planning_options, "_planning_options_transaction", capture_artifact_root
    )

    assert run_wf_select_option(_args(supplied_root, "D-001", "O-002")) == 0

    assert len(resolver_calls) == 1
    assert state_roots == [original_root, original_root]
    assert artifact_roots == [original_root]
    assert _load_state(original_root)["planningOptions"]["option"] == "O-002"
    assert _load_state(redirected_root).get("planningOptions") is None
    redirected_artifact = json.loads(
        (redirected_root / ".workflow" / "artifacts" / "planning-options.json").read_text(
            encoding="utf-8"
        )
    )
    assert redirected_artifact["decisions"][0]["selected_option_id"] is None


def test_marker_hash_reuses_a_multi_decision_artifact_for_each_decision(
    tmp_path: Path,
) -> None:
    artifact = _artifact(decision_count=2, selected=True)
    artifact["decisions"][0]["selected_option_id"] = "O-002"
    artifact["decisions"][0]["selected_by"] = "prior-operator"
    artifact["decisions"][0]["selected_at"] = "2026-08-24T10:01:00Z"
    artifact["selection_history"].append(
        {
            "decision_id": "D-001",
            "previous_option_id": "O-001",
            "selected_option_id": "O-002",
            "selected_by": "prior-operator",
            "selected_at": "2026-08-24T10:01:00Z",
            "source": "cli",
        }
    )
    _write_artifact(tmp_path, artifact)
    state = _state(current_phase="review", plan_status="completed", g1_passed=True)
    state["planningOptions"] = {
        "artifactHash": load_planning_options(tmp_path).artifact_hash,
        "decision": "D-002",
        "option": "O-001",
        "appliedAt": "2026-08-24T10:02:00Z",
        "action": "replanned",
    }
    _write_state(tmp_path, state)

    assert run_wf_select_option(_args(tmp_path, "D-001", "O-002")) == 0
    reused = _load_state(tmp_path)

    assert reused["loop"]["replanCount"] == 0
    assert reused["gates"]["G3"]["scope_hash"] == "prior-scope"


def test_retry_confirms_directory_fsync_before_reconciling_visible_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    import awf.core.planning_options as planning_options

    _write_artifact(tmp_path, _artifact(selected=True))
    _write_state(tmp_path, _state(current_phase="review", plan_status="completed", g1_passed=True))
    original_fsync = planning_options.os.fsync
    fsync_calls = 0

    def fail_first_directory_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 4:
            raise OSError("artifact directory sync failed")
        original_fsync(descriptor)

    monkeypatch.setattr(planning_options.os, "fsync", fail_first_directory_fsync)
    assert run_wf_select_option(_args(tmp_path, "D-001", "O-002")) == 2
    assert _load_state(tmp_path)["loop"]["replanCount"] == 0

    original_replan = wf_commands.replan_workflow

    def assert_retry_was_synced(*args, **kwargs):
        assert fsync_calls >= 5
        return original_replan(*args, **kwargs)

    monkeypatch.setattr(wf_commands, "replan_workflow", assert_retry_was_synced)
    assert run_wf_select_option(_args(tmp_path, "D-001", "O-002")) == 0
    assert _load_state(tmp_path)["loop"]["replanCount"] == 1


def test_corrupt_current_phase_rejects_a_change_before_artifact_publication(
    tmp_path: Path, capsys
) -> None:
    _write_artifact(tmp_path, _artifact(selected=True))
    state = _state(current_phase="plan", plan_status="completed", g1_passed=True)
    state["currentPhase"] = "corrupt"
    _write_state(tmp_path, state)
    artifact_path = tmp_path / ".workflow" / "artifacts" / "planning-options.json"
    original = artifact_path.read_text(encoding="utf-8")

    assert run_wf_select_option(_args(tmp_path, "D-001", "O-002")) == 1

    assert "rejected" in capsys.readouterr().err
    assert artifact_path.read_text(encoding="utf-8") == original


def test_aborted_current_phase_status_rejects_a_change_before_publication(
    tmp_path: Path
) -> None:
    _write_artifact(tmp_path, _artifact(selected=True))
    state = _state(current_phase="plan", plan_status="aborted", g1_passed=True)
    _write_state(tmp_path, state)
    artifact_path = tmp_path / ".workflow" / "artifacts" / "planning-options.json"
    original = artifact_path.read_text(encoding="utf-8")

    assert run_wf_select_option(_args(tmp_path, "D-001", "O-002")) == 1

    assert artifact_path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "invalid_plan_state",
    [None, "corrupt", {"status": "aborted", "retries": 0}],
)
def test_invalid_plan_state_rejects_before_artifact_publication(
    tmp_path: Path, invalid_plan_state: object
) -> None:
    _write_artifact(tmp_path, _artifact(selected=True))
    state = _state(current_phase="review", plan_status="completed", g1_passed=True)
    if invalid_plan_state is None:
        state["phases"].pop("plan")
    else:
        state["phases"]["plan"] = invalid_plan_state
    _write_state(tmp_path, state)
    artifact_path = tmp_path / ".workflow" / "artifacts" / "planning-options.json"
    original = artifact_path.read_text(encoding="utf-8")

    assert run_wf_select_option(_args(tmp_path, "D-001", "O-002")) == 1

    assert artifact_path.read_text(encoding="utf-8") == original


def test_aborted_later_phase_rejects_before_artifact_publication(tmp_path: Path) -> None:
    _write_artifact(tmp_path, _artifact(selected=True))
    state = _state(current_phase="review", plan_status="completed", g1_passed=True)
    state["phases"]["review"]["status"] = "aborted"
    _write_state(tmp_path, state)
    artifact_path = tmp_path / ".workflow" / "artifacts" / "planning-options.json"
    original = artifact_path.read_text(encoding="utf-8")

    assert run_wf_select_option(_args(tmp_path, "D-001", "O-002")) == 1

    assert artifact_path.read_text(encoding="utf-8") == original


def test_planning_option_lifecycle_smoke_uses_cli_and_state_apis(
    tmp_path: Path, capsys
) -> None:
    selected_root = tmp_path / "selected-workflow"
    selected_root.mkdir()
    (selected_root / ".awf.toml").write_text("# fixture\n", encoding="utf-8")
    workflow_state.initialize_workflow(
        str(selected_root),
        "Persist an explicit rollout selection for an application delivery workflow.",
    )
    _write_derived_plan_artifacts(selected_root)
    _write_artifact(selected_root, _artifact())

    assert main(["wf", "gate", "plan", "--repo-root", str(selected_root)]) == 1
    selection_gate = capsys.readouterr()
    assert "  ✗ planning_options.selection: decision_selection_required\n" in selection_gate.out
    assert selection_gate.out.endswith("\nG-plan: FAIL\n")
    assert selection_gate.err == ""

    paused = workflow_state.record_orchestrator_decision(
        str(selected_root),
        "plan",
        decision="escalate_user",
        reason="decision_selection_required",
    )
    assert paused["currentPhase"] == "plan"
    assert paused["phases"]["plan"]["status"] == "deciding"
    assert paused["loop"]["pendingDecision"]["phase"] == "plan"

    before_first_selection = load_planning_options(selected_root)
    assert main(
        [
            "wf",
            "select-option",
            "--decision-id",
            "D-001",
            "--option-id",
            "O-002",
            "--actor",
            "operator",
            "--repo-root",
            str(selected_root),
            "--json",
        ]
    ) == 0
    first_selection_output = capsys.readouterr()
    after_first_selection = load_planning_options(selected_root)
    assert first_selection_output.out == json.dumps(
        {
            "decision_id": "D-001",
            "option_id": "O-002",
            "status": "selected",
            "selection_action": "selected",
            "workflow_action": "continued",
            "previous_hash": before_first_selection.artifact_hash,
            "current_hash": after_first_selection.artifact_hash,
        },
        sort_keys=True,
    ) + "\n"
    assert first_selection_output.err == ""
    assert after_first_selection.decisions[0].selected_option_id == "O-002"
    assert [
        (
            entry.decision_id,
            entry.previous_option_id,
            entry.selected_option_id,
            entry.selected_by,
            entry.source,
        )
        for entry in after_first_selection.selection_history
    ] == [("D-001", None, "O-002", "operator", "cli")]

    resumed = workflow_state.load_workflow_state(str(selected_root))
    assert resumed["currentPhase"] == "plan"
    assert resumed["phases"]["plan"]["status"] == "in_progress"
    assert resumed["planningOptions"]["action"] == "continued"
    assert "pendingDecision" not in resumed["loop"]
    assert [entry["action"] for entry in resumed["history"]] == ["deciding", "continued"]

    assert main(["wf", "gate", "plan", "--repo-root", str(selected_root)]) == 1
    stale_gate = capsys.readouterr()
    assert stale_gate.out.endswith("\nG-plan: FAIL\n")
    assert main(
        ["wf", "seal-plan", "--repo-root", str(selected_root), "--json"]
    ) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "reason": "seal_unavailable",
    }

    _write_derived_plan_artifacts(selected_root)
    assert main(
        ["wf", "seal-plan", "--repo-root", str(selected_root), "--json"]
    ) == 0
    sealed_selection = json.loads(capsys.readouterr().out)
    assert sealed_selection["status"] == "sealed"
    assert main(["wf", "gate", "plan", "--repo-root", str(selected_root)]) == 0
    passed_gate = capsys.readouterr()
    assert passed_gate.out.endswith("\nG-plan: PASS\n")
    assert passed_gate.err == ""
    g1_state = workflow_state.apply_gate_result(str(selected_root), "plan", True)
    assert g1_state["gates"]["G1"]["passed"] is True
    assert g1_state["phases"]["plan"]["status"] == "completed"
    assert g1_state["currentPhase"] == "review"

    later = workflow_state.load_workflow_state(str(selected_root))
    for phase in ("plan", "review", "approve", "impl"):
        later["phases"][phase] = {
            "status": "completed",
            "retries": 2,
            "executions": 4,
        }
    later["phases"]["verify"] = {
        "status": "in_progress",
        "retries": 3,
        "executions": 5,
    }
    later["phases"]["test"] = {"status": "pending", "retries": 4, "executions": 6}
    later["phases"]["done"] = {"status": "pending", "retries": 5, "executions": 7}
    later["currentPhase"] = "verify"
    later["gates"] = {
        "G1": {"passed": True},
        "G2": {"passed": True, "provider": "fixture", "provider_status": "PASS"},
        "G3": {"passed": True, "scope_hash": "scope-sha-001"},
        "G4": {"passed": True},
        "G5": {"passed": True, "provider": "fixture", "provider_status": "PASS"},
        "G6": {"passed": True},
    }
    later["totalExecutions"] = 17
    later["loop"]["replanCount"] = 2
    later["loop"]["maxReplans"] = 5
    workflow_state.save_workflow_state_snapshot(str(selected_root), later)

    before_changed_selection = load_planning_options(selected_root)
    assert main(
        [
            "wf",
            "select-option",
            "--decision-id",
            "D-001",
            "--option-id",
            "O-001",
            "--actor",
            "operator",
            "--repo-root",
            str(selected_root),
            "--json",
        ]
    ) == 0
    changed_selection_output = capsys.readouterr()
    after_changed_selection = load_planning_options(selected_root)
    assert changed_selection_output.out == json.dumps(
        {
            "decision_id": "D-001",
            "option_id": "O-001",
            "status": "selected",
            "selection_action": "selected",
            "workflow_action": "replanned",
            "previous_hash": before_changed_selection.artifact_hash,
            "current_hash": after_changed_selection.artifact_hash,
        },
        sort_keys=True,
    ) + "\n"
    assert changed_selection_output.err == ""
    assert [
        (
            entry.decision_id,
            entry.previous_option_id,
            entry.selected_option_id,
            entry.selected_by,
            entry.source,
        )
        for entry in after_changed_selection.selection_history
    ] == [
        ("D-001", None, "O-002", "operator", "cli"),
        ("D-001", "O-002", "O-001", "operator", "cli"),
    ]

    replanned = workflow_state.load_workflow_state(str(selected_root))
    assert replanned["currentPhase"] == "plan"
    assert {
        phase: (phase_state["status"], phase_state["retries"], phase_state["executions"])
        for phase, phase_state in replanned["phases"].items()
    } == {
        phase: ("pending", 0, 0)
        for phase in _PHASES
    }
    assert replanned["gates"] == {
        "G1": {"passed": None},
        "G2": {"passed": None, "provider": None, "provider_status": None},
        "G3": {"passed": None, "scope_hash": None},
        "G4": {"passed": None},
        "G5": {"passed": None, "provider": None, "provider_status": None},
        "G6": {"passed": None},
    }
    assert replanned["loop"]["replanCount"] == 3
    assert replanned["loop"]["maxReplans"] == 5
    assert replanned["totalExecutions"] == 17
    assert replanned["history"][-1]["action"] == "replanned"

    no_decision_root = tmp_path / "no-decision-workflow"
    no_decision_root.mkdir()
    (no_decision_root / ".awf.toml").write_text("# fixture\n", encoding="utf-8")
    workflow_state.initialize_workflow(
        str(no_decision_root),
        "Use the sole rollout approach for an application delivery workflow.",
    )
    _write_derived_plan_artifacts(no_decision_root)
    _write_artifact(
        no_decision_root,
        {
            "schema_version": 1,
            "status": "no_decision_required",
            "no_decision_reason": "Repository conventions leave one viable rollout.",
            "decisions": [],
            "selection_history": [],
        },
    )

    assert main(
        ["wf", "seal-plan", "--repo-root", str(no_decision_root), "--json"]
    ) == 0
    sealed_no_decision = json.loads(capsys.readouterr().out)
    assert sealed_no_decision["status"] == "sealed"

    assert main(["wf", "gate", "plan", "--repo-root", str(no_decision_root)]) == 0
    no_decision_gate = capsys.readouterr()
    assert no_decision_gate.out.endswith("\nG-plan: PASS\n")
    assert no_decision_gate.err == ""
    no_decision_state = workflow_state.apply_gate_result(str(no_decision_root), "plan", True)
    assert no_decision_state["gates"]["G1"]["passed"] is True
    assert no_decision_state["currentPhase"] == "review"
    assert no_decision_state["phases"]["plan"]["status"] == "completed"
    assert "pendingDecision" not in no_decision_state["loop"]
