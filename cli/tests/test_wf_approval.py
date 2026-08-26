"""Focused contract tests for the explicit parent-only G3 command."""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from awf.cli import build_parser
from awf.commands import wf as wf_commands
from awf.commands.wf import run_wf_approve, run_wf_next
from awf.core import approval, state as workflow_state
from awf.core.approval import ApprovalError, apply_approval


_PHASES = ("plan", "review", "approve", "impl", "verify", "test", "done")


def _state() -> dict[str, object]:
    phases = {phase: {"status": "pending", "retries": 0} for phase in _PHASES}
    phases["plan"]["status"] = "completed"
    phases["review"]["status"] = "completed"
    return {
        "id": "approval-test",
        "repo": "approval-test",
        "branch": "main",
        "currentPhase": "approve",
        "phases": phases,
        "gates": {
            "G1": {"passed": True},
            "G2": {"passed": True, "provider": "fixture", "provider_status": "PASS"},
            "G3": {"passed": None, "scope_hash": None},
            "G4": {"passed": None},
            "G5": {"passed": None, "provider": None, "provider_status": None},
            "G6": {"passed": None},
        },
        "totalExecutions": 0,
        "loop": {"replanCount": 0, "maxReplans": 3, "history": []},
        "history": [],
    }


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "workflow"
    artifacts = root / ".workflow" / "artifacts"
    artifacts.mkdir(parents=True)
    for name, content in {
        "constitution.md": "# Constitution\n\n- Keep approval immutable.\n",
        "spec.md": "# Spec\n\n- FR-001: explicit approval.\n",
        "plan.md": "# Plan\n\n- [FR-001] record approval.\n",
        "tasks.md": "# Tasks\n\n- [ ] T001 [FR-001] record approval.\n",
        "test-criteria.md": "# Criteria\n\n- Approval is durable.\n",
        "allowed-files.json": json.dumps(
            {"planned_files": ["src/feature.py"], "expanded_files": []}
        ),
    }.items():
        (artifacts / name).write_text(content, encoding="utf-8")
    (root / ".workflow" / "state.json").write_text(
        json.dumps(_state()),
        encoding="utf-8",
    )
    return root


def _load_state(root: Path) -> dict[str, object]:
    return json.loads((root / ".workflow" / "state.json").read_text(encoding="utf-8"))


def test_approve_records_matching_scope_artifact_gate_and_history(tmp_path: Path) -> None:
    root = _root(tmp_path)

    result = apply_approval(
        str(root),
        decision="approve",
        actor="release-manager",
    )

    state = _load_state(root)
    approval = json.loads(
        (root / ".workflow" / "artifacts" / "approval.json").read_text(encoding="utf-8")
    )
    assert result.status == "approved"
    assert result.scope_hash == approval["scope_hash"]
    assert state["gates"]["G3"]["scope_hash"] == approval["scope_hash"]
    assert state["gates"]["G3"]["passed"] is True
    assert approval["schema_version"] == 2
    assert state["gates"]["G3"]["planning_seal"] == approval["planning_seal"]
    assert state["gates"]["G3"]["plan_provenance"] == approval["plan_provenance"]
    assert set(approval["planning_seal"]["artifacts"]) == {
        "constitution.md",
        "spec.md",
        "plan.md",
        "tasks.md",
        "test-criteria.md",
        "allowed-files.json",
    }
    assert state["currentPhase"] == "impl"
    assert state["phases"]["approve"]["status"] == "completed"
    assert state["history"][-1]["action"] == "approved"


def test_approve_reuses_only_the_matching_completed_approval(tmp_path: Path) -> None:
    root = _root(tmp_path)
    first = apply_approval(str(root), decision="approve", actor="release-manager")

    second = apply_approval(str(root), decision="approve", actor="another-manager")

    assert first.scope_hash == second.scope_hash
    assert second.reused is True
    assert len(_load_state(root)["history"]) == 1


def test_revise_returns_to_plan_without_creating_approval_artifact(tmp_path: Path) -> None:
    root = _root(tmp_path)

    result = apply_approval(
        str(root),
        decision="revise",
        actor="release-manager",
        reason="Coverage needs another review.",
    )

    state = _load_state(root)
    assert result.status == "revised"
    assert state["currentPhase"] == "plan"
    assert state["gates"]["G3"] == {"passed": None, "scope_hash": None}
    assert state["history"][-1]["action"] == "revised"
    assert not (root / ".workflow" / "artifacts" / "approval.json").exists()


def test_reject_records_terminal_rejection_without_approval_artifact(tmp_path: Path) -> None:
    root = _root(tmp_path)

    result = apply_approval(
        str(root),
        decision="reject",
        actor="release-manager",
        reason="The requested rollout must not proceed.",
    )

    state = _load_state(root)
    assert result.status == "rejected"
    assert state["currentPhase"] == "rejected"
    assert state["phases"]["approve"]["status"] == "rejected"
    assert state["gates"]["G3"]["passed"] is False
    assert state["history"][-1]["action"] == "rejected"
    assert not (root / ".workflow" / "artifacts" / "approval.json").exists()


def test_revise_and_reject_require_an_explicit_reason(tmp_path: Path) -> None:
    root = _root(tmp_path)

    with pytest.raises(ApprovalError, match="reason_required"):
        apply_approval(str(root), decision="revise", actor="release-manager")
    with pytest.raises(ApprovalError, match="reason_required"):
        apply_approval(str(root), decision="reject", actor="release-manager")


def test_parser_requires_explicit_approval_decision() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["wf", "approve", "--actor", "release-manager"])


def test_parser_routes_explicit_approval_arguments_to_parent_handler() -> None:
    args = build_parser().parse_args(
        [
            "wf",
            "approve",
            "--decision",
            "approve",
            "--actor",
            "release-manager",
            "--repo-root",
            "/tmp/workflow",
            "--json",
        ]
    )

    assert args.handler is run_wf_approve
    assert args.decision == "approve"
    assert args.actor == "release-manager"
    assert args.json is True


def test_approval_cli_requires_interactive_tty(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    return_code = run_wf_approve(
        Namespace(
            repo_root=str(root),
            decision="approve",
            actor="release-manager",
            reason=None,
            json=True,
        )
    )

    assert return_code == 2
    assert json.loads(capsys.readouterr().out)["code"] == "approval_tty_required"
    assert not (root / ".workflow" / "artifacts" / "approval.json").exists()


def test_json_output_omits_the_reason_text(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    secret_like_reason = "Review required additional evidence."
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    return_code = run_wf_approve(
        Namespace(
            repo_root=str(root),
            decision="revise",
            actor="release-manager",
            reason=secret_like_reason,
            json=True,
        )
    )

    rendered = capsys.readouterr().out
    assert return_code == 0
    assert secret_like_reason not in rendered
    assert json.loads(rendered)["status"] == "revised"


def test_json_rejection_does_not_echo_sensitive_reason(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    sensitive_reason = "token=top-secret-value"
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    return_code = run_wf_approve(
        Namespace(
            repo_root=str(root),
            decision="approve",
            actor="release-manager",
            reason=sensitive_reason,
            json=True,
        )
    )

    rendered = capsys.readouterr().out
    assert return_code == 2
    assert sensitive_reason not in rendered
    assert json.loads(rendered)["code"] == "reason_invalid"


def test_next_never_delegates_the_approve_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    calls: list[str] = []
    monkeypatch.setattr(wf_commands, "enforce_ready_gate", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(wf_commands, "load_awf_config", lambda _root: {})
    monkeypatch.setattr(wf_commands, "load_workflow_state", lambda _root: state)
    monkeypatch.setattr(wf_commands, "load_workflow_provider_config", lambda _root: {})
    monkeypatch.setattr(
        wf_commands,
        "build_workflow_prompt",
        lambda *_args, **_kwargs: calls.append("prompt") or "must not run",
    )

    return_code = run_wf_next(
        Namespace(
            repo_root="unused",
            non_interactive=True,
            auto_apply=False,
            dry_run=True,
            output_format="text",
            phase=None,
        )
    )

    assert return_code == 2
    assert calls == []


def test_next_blocks_impl_before_ready_gate_when_g3_seal_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    apply_approval(str(root), decision="approve", actor="release-manager")
    calls: list[str] = []
    monkeypatch.setattr(wf_commands, "load_awf_config", lambda _root: {})
    monkeypatch.setattr(
        wf_commands,
        "validate_approved_planning_seal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ApprovalError("approval_seal_changed")
        ),
    )
    monkeypatch.setattr(
        wf_commands,
        "enforce_ready_gate",
        lambda *_args, **_kwargs: calls.append("ready") or 0,
    )

    return_code = run_wf_next(
        Namespace(
            repo_root=str(root),
            non_interactive=False,
            auto_apply=False,
            dry_run=False,
            output_format="text",
            phase=None,
        )
    )

    assert return_code == 2
    assert calls == []


def test_approve_fails_closed_when_required_plan_provenance_is_missing(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    (root / ".workflow" / "manifest.json").write_text(
        json.dumps({"planning_options": {"required": True}}),
        encoding="utf-8",
    )

    with pytest.raises(ApprovalError, match="plan_provenance_missing"):
        apply_approval(str(root), decision="approve", actor="release-manager")

    state = _load_state(root)
    assert state["gates"]["G3"]["passed"] is None
    assert not (root / ".workflow" / "artifacts" / "approval.json").exists()


def test_approve_fails_closed_when_identity_changes_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    snapshot = approval._approval_snapshot
    calls = 0

    def concurrent_snapshot(repo_root: Path):
        nonlocal calls
        calls += 1
        if calls == 2:
            (repo_root / ".workflow" / "artifacts" / "allowed-files.json").write_text(
                json.dumps({"planned_files": ["src/unapproved.py"], "expanded_files": []}),
                encoding="utf-8",
            )
        return snapshot(repo_root)

    monkeypatch.setattr(approval, "_approval_snapshot", concurrent_snapshot)

    with pytest.raises(ApprovalError, match="approval_identity_changed"):
        apply_approval(str(root), decision="approve", actor="release-manager")

    assert _load_state(root)["gates"]["G3"]["passed"] is None
    assert not (root / ".workflow" / "artifacts" / "approval.json").exists()


def test_approve_rolls_back_new_artifact_when_state_commit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    before_state = (root / ".workflow" / "state.json").read_bytes()

    def state_write_failed(*_args: object, **_kwargs: object) -> Path:
        raise OSError("state commit failed")

    monkeypatch.setattr(
        workflow_state,
        "_write_workflow_state_unlocked",
        state_write_failed,
    )

    with pytest.raises(ApprovalError, match="approval_failed"):
        apply_approval(str(root), decision="approve", actor="release-manager")

    assert (root / ".workflow" / "state.json").read_bytes() == before_state
    assert not (root / ".workflow" / "artifacts" / "approval.json").exists()
