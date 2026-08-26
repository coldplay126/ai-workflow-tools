"""Focused contracts for the explicit parent-only Done confirmation command."""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from awf.cli import build_parser
from awf.commands import wf as wf_commands
from awf.commands.wf import run_wf_confirm, run_wf_next
from awf.core.done import DoneConfirmationError, apply_done_confirmation


_PHASES = ("plan", "review", "approve", "impl", "verify", "test", "done")


def _state() -> dict[str, object]:
    phases = {phase: {"status": "completed", "retries": 0} for phase in _PHASES}
    phases["done"] = {"status": "pending", "retries": 0}
    return {
        "id": "done-test",
        "repo": "done-test",
        "branch": "main",
        "currentPhase": "done",
        "phases": phases,
        "gates": {
            "G1": {"passed": True},
            "G2": {"passed": True, "provider": "fixture", "provider_status": "PASS"},
            "G3": {"passed": True, "scope_hash": "a" * 64},
            "G4": {"passed": True},
            "G5": {"passed": True, "provider": "fixture", "provider_status": "PASS"},
            "G6": {"passed": True},
        },
        "totalExecutions": 0,
        "loop": {"replanCount": 0, "maxReplans": 3, "history": []},
        "history": [],
    }


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "workflow"
    artifacts = root / ".workflow" / "artifacts"
    artifacts.mkdir(parents=True)
    (root / ".workflow" / "state.json").write_text(json.dumps(_state()), encoding="utf-8")
    return root


def _load_state(root: Path) -> dict[str, object]:
    return json.loads((root / ".workflow" / "state.json").read_text(encoding="utf-8"))


def test_complete_records_strict_confirmation_state_and_history(tmp_path: Path) -> None:
    root = _root(tmp_path)

    result = apply_done_confirmation(
        str(root),
        decision="complete",
        actor="release-manager",
        pr_url="https://github.com/example/workflow/pull/42",
    )

    confirmation = json.loads(
        (root / ".workflow" / "artifacts" / "confirmation.json").read_text(encoding="utf-8")
    )
    state = _load_state(root)
    assert result.status == "completed"
    assert confirmation["decision"] == "complete"
    assert confirmation["pr_url"] == "https://github.com/example/workflow/pull/42"
    assert set(confirmation) == {
        "schema_version",
        "workflow_id",
        "decision",
        "actor",
        "pr_url",
        "recorded_at",
    }
    assert state["currentPhase"] == "completed"
    assert state["phases"]["done"]["status"] == "completed"
    assert state["history"][-1]["action"] == "confirmed"


def test_complete_reuses_only_the_completed_confirmation(tmp_path: Path) -> None:
    root = _root(tmp_path)
    first = apply_done_confirmation(str(root), decision="complete", actor="release-manager")
    second = apply_done_confirmation(str(root), decision="complete", actor="another-manager")

    assert first.reused is False
    assert second.reused is True
    assert second.status == "completed"
    assert len(_load_state(root)["history"]) == 1


def test_hold_records_audited_block_without_final_confirmation(tmp_path: Path) -> None:
    root = _root(tmp_path)

    result = apply_done_confirmation(str(root), decision="hold", actor="release-manager")

    state = _load_state(root)
    assert result.status == "held"
    assert state["currentPhase"] == "done"
    assert state["phases"]["done"]["status"] == "pending"
    assert state["history"][-1]["action"] == "held"
    assert not (root / ".workflow" / "artifacts" / "confirmation.json").exists()


def test_done_confirmation_completes_a_current_legacy_in_progress_done(tmp_path: Path) -> None:
    root = _root(tmp_path)
    state = _load_state(root)
    state["phases"]["done"]["status"] = "in_progress"
    (root / ".workflow" / "state.json").write_text(json.dumps(state), encoding="utf-8")

    result = apply_done_confirmation(str(root), decision="complete", actor="release-manager")

    assert result.status == "completed"
    assert _load_state(root)["phases"]["done"]["status"] == "completed"


def test_done_confirmation_fails_closed_without_current_g6(tmp_path: Path) -> None:
    root = _root(tmp_path)
    state = _load_state(root)
    state["gates"]["G6"]["passed"] = False
    (root / ".workflow" / "state.json").write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(DoneConfirmationError, match="g6_not_passed"):
        apply_done_confirmation(str(root), decision="complete", actor="release-manager")

    state = _load_state(root)
    assert state["currentPhase"] == "done"
    assert state["phases"]["done"]["status"] == "pending"
    assert not (root / ".workflow" / "artifacts" / "confirmation.json").exists()


def test_done_confirmation_rejects_malformed_existing_artifact(tmp_path: Path) -> None:
    root = _root(tmp_path)
    confirmation = root / ".workflow" / "artifacts" / "confirmation.json"
    confirmation.write_text('{"decision":"complete","unexpected":true}', encoding="utf-8")

    with pytest.raises(DoneConfirmationError, match="confirmation_artifact_invalid"):
        apply_done_confirmation(str(root), decision="complete", actor="release-manager")

    state = _load_state(root)
    assert state["currentPhase"] == "done"
    assert state["history"] == []


@pytest.mark.parametrize(
    "pr_url",
    [
        "http://github.com/example/workflow/pull/42",
        "https://github.com/example/workflow/pull/42?token=secret",
        "https://token@github.com/example/workflow/pull/42",
        "https://github.com/example/workflow/issues/42",
    ],
)
def test_done_confirmation_rejects_invalid_pr_urls(tmp_path: Path, pr_url: str) -> None:
    root = _root(tmp_path)

    with pytest.raises(DoneConfirmationError, match="pr_url_invalid"):
        apply_done_confirmation(
            str(root),
            decision="complete",
            actor="release-manager",
            pr_url=pr_url,
        )

    assert not (root / ".workflow" / "artifacts" / "confirmation.json").exists()


def test_confirm_parser_requires_explicit_decision_and_routes_parent_handler() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["wf", "confirm", "--actor", "release-manager"])

    args = parser.parse_args(
        [
            "wf",
            "confirm",
            "--decision",
            "complete",
            "--actor",
            "release-manager",
            "--pr-url",
            "https://github.com/example/workflow/pull/42",
            "--repo-root",
            "/tmp/workflow",
            "--json",
        ]
    )
    assert args.handler is run_wf_confirm
    assert args.decision == "complete"
    assert args.actor == "release-manager"
    assert args.pr_url == "https://github.com/example/workflow/pull/42"
    assert args.json is True


def test_confirm_rejects_internal_noninteractive_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _root(tmp_path)

    return_code = run_wf_confirm(
        Namespace(
            repo_root=str(root),
            decision="complete",
            actor="release-manager",
            pr_url=None,
            json=True,
            non_interactive=True,
        )
    )

    assert return_code == 2
    assert json.loads(capsys.readouterr().out)["code"] == "done_non_interactive_forbidden"
    assert not (root / ".workflow" / "artifacts" / "confirmation.json").exists()


def test_confirm_requires_interactive_tty(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    return_code = run_wf_confirm(
        Namespace(
            repo_root=str(root),
            decision="complete",
            actor="release-manager",
            pr_url=None,
            json=True,
            non_interactive=False,
        )
    )

    assert return_code == 2
    assert json.loads(capsys.readouterr().out)["code"] == "done_tty_required"
    assert not (root / ".workflow" / "artifacts" / "confirmation.json").exists()


@pytest.mark.parametrize("phase", ["done", None])
def test_next_never_delegates_done_even_in_noninteractive_mode(
    monkeypatch: pytest.MonkeyPatch,
    phase: str | None,
) -> None:
    state = _state()
    calls: list[str] = []
    monkeypatch.setattr(
        wf_commands,
        "enforce_ready_gate",
        lambda *_args, **_kwargs: calls.append("ready-gate") or 0,
    )
    monkeypatch.setattr(wf_commands, "load_awf_config", lambda _root: {})
    monkeypatch.setattr(wf_commands, "load_workflow_state", lambda _root: state)
    monkeypatch.setattr(
        wf_commands,
        "load_workflow_provider_config",
        lambda _root: calls.append("provider-config") or {},
    )
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
            dry_run=False,
            output_format="text",
            phase=phase,
        )
    )

    assert return_code == 2
    assert calls == []
