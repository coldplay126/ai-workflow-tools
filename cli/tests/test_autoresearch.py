"""Focused contracts for completed Autoresearch evidence registration."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from argparse import Namespace
from pathlib import Path


import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.cli import build_parser
from awf.commands import wf as wf_commands
from awf.commands.wf import (
    run_wf_autoresearch_register,
    run_wf_autoresearch_schema,
)

from awf.core.autoresearch import (
    AUTORESEARCH_RUN_JSON_SCHEMA,
    AUTORESEARCH_ARTIFACT_RELATIVE_PATH,
    EXIT_BLOCKED,
    EXIT_REPLAN_REQUIRED,
    EXIT_SUCCESS,
    register_autoresearch_run,
)
from awf.core.planning_options import load_planning_options, seal_planning_options
from awf.core import autoresearch
from awf.core.approval import apply_approval


_PHASES = ("plan", "review", "approve", "impl", "verify", "test", "done")


def _approval_state() -> dict[str, object]:
    phases = {phase: {"status": "pending", "retries": 0} for phase in _PHASES}
    phases["plan"]["status"] = "completed"
    phases["review"]["status"] = "completed"
    return {
        "id": "autoresearch-test",
        "repo": "autoresearch-test",
        "currentPhase": "approve",
        "phases": phases,
        "gates": {
            "G2": {"passed": True},
            "G3": {"passed": None, "scope_hash": None},
        },
        "history": [],
        "loop": {"replanCount": 0, "maxReplans": 3, "history": []},
    }


_GOAL_DIGEST = "a" * 64


def _write_planning_options(root: Path) -> str:
    path = root / ".workflow" / "artifacts" / "planning-options.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "no_decision_required",
                "no_decision_reason": "No material implementation choice exists.",
                "decisions": [],
                "selection_history": [],
            }
        ),
        encoding="utf-8",
    )
    return load_planning_options(root).artifact_hash


def _workflow_fixture(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "repo"
    artifacts = root / ".workflow" / "artifacts"
    artifacts.mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "feature.py").write_text("after = True\n", encoding="utf-8")
    (root / "src" / "expanded.py").write_text("expanded = True\n", encoding="utf-8")
    metrics_path = root / ".omp" / "autoresearch" / "omp-run-001" / "metrics.txt"
    metrics_path.parent.mkdir(parents=True)
    metrics = b"score=1.25\n"
    metrics_path.write_bytes(metrics)
    planning_options_hash = _write_planning_options(root)
    for name, content in {
        "constitution.md": "# Constitution\n",
        "spec.md": "# Spec\n",
        "plan.md": "# Plan\n",
        "tasks.md": "# Tasks\n",
        "test-criteria.md": "# Criteria\n",
    }.items():
        (artifacts / name).write_text(content, encoding="utf-8")
    (artifacts / "allowed-files.json").write_text(
        json.dumps(
            {
                "planned_files": ["src/feature.py"],
                "expanded_files": ["src/expanded.py"],
            }
        ),
        encoding="utf-8",
    )
    seal_planning_options(root)
    (root / ".workflow" / "state.json").write_text(
        json.dumps(_approval_state()),
        encoding="utf-8",
    )
    approval = apply_approval(
        str(root),
        decision="approve",
        actor="release-manager",
    )
    assert approval.scope_hash is not None
    payload: dict[str, object] = {
        "schema_version": 1,
        "run_id": "omp-run-001",
        "goal_digest": _GOAL_DIGEST,
        "score_name": "throughput",
        "score_direction": "maximize",
        "baseline_score": 1.0,
        "final_score": 1.25,
        "kept_candidate_ref": "candidate-004",
        "planning_options_hash": planning_options_hash,
        "scope_hash": approval.scope_hash,
        "metrics_path": ".omp/autoresearch/omp-run-001/metrics.txt",
        "metrics_hash": hashlib.sha256(metrics).hexdigest(),
        "completed_at": "2026-08-26T12:00:00Z",
        "changed_files": ["src/feature.py", "src/expanded.py"],
    }
    return root, payload


def test_register_writes_only_canonical_provenance(tmp_path: Path):
    root, payload = _workflow_fixture(tmp_path)

    result = register_autoresearch_run(root, payload)

    assert result.status == "written"
    assert result.exit_code == EXIT_SUCCESS
    assert result.artifact_path == AUTORESEARCH_ARTIFACT_RELATIVE_PATH
    artifact = root / AUTORESEARCH_ARTIFACT_RELATIVE_PATH
    stored = json.loads(artifact.read_text(encoding="utf-8"))
    expected = dict(payload)
    expected["changed_files"] = sorted(payload["changed_files"])
    assert stored == expected
    assert "score=1.25" not in artifact.read_text(encoding="utf-8")
    assert json.loads((root / ".workflow" / "state.json").read_text(encoding="utf-8"))["currentPhase"] == "impl"


def test_register_reuses_normalized_canonical_payload(tmp_path: Path):
    root, payload = _workflow_fixture(tmp_path)
    assert register_autoresearch_run(root, payload).status == "written"

    reordered = dict(payload)
    reordered["changed_files"] = ["src/expanded.py", "src/feature.py"]
    result = register_autoresearch_run(root, reordered)

    assert result.status == "reuse"
    assert result.exit_code == EXIT_SUCCESS
    assert result.reason == "canonical_payload_reused"


def test_register_blocks_conflicting_current_artifact(tmp_path: Path):
    root, payload = _workflow_fixture(tmp_path)
    assert register_autoresearch_run(root, payload).status == "written"
    artifact = root / AUTORESEARCH_ARTIFACT_RELATIVE_PATH
    original = artifact.read_bytes()

    conflicting = dict(payload)
    conflicting["run_id"] = "omp-run-002"
    result = register_autoresearch_run(root, conflicting)

    assert result.status == "blocked"
    assert result.exit_code == EXIT_BLOCKED
    assert result.reason == "artifact_already_registered"
    assert artifact.read_bytes() == original


def test_register_returns_replan_required_when_planning_identity_changes(tmp_path: Path):
    root, payload = _workflow_fixture(tmp_path)
    changed = dict(payload)
    changed["planning_options_hash"] = "c" * 64

    result = register_autoresearch_run(root, changed)

    assert result.status == "replan_required"
    assert result.exit_code == EXIT_REPLAN_REQUIRED
    assert result.reason == "planning_identity_changed"
    assert not (root / AUTORESEARCH_ARTIFACT_RELATIVE_PATH).exists()


def test_register_returns_replan_required_when_g3_scope_changes(tmp_path: Path):
    root, payload = _workflow_fixture(tmp_path)
    changed = dict(payload)
    changed["scope_hash"] = "c" * 64
    before_state = (root / ".workflow" / "state.json").read_bytes()

    result = register_autoresearch_run(root, changed)

    assert result.status == "replan_required"
    assert result.exit_code == EXIT_REPLAN_REQUIRED
    assert result.reason == "scope_identity_changed"
    assert (root / ".workflow" / "state.json").read_bytes() == before_state
    assert not (root / AUTORESEARCH_ARTIFACT_RELATIVE_PATH).exists()


def test_register_fails_closed_for_invalid_schema_and_nonfinite_scores(tmp_path: Path):
    root, payload = _workflow_fixture(tmp_path)
    with_extra_field = dict(payload)
    with_extra_field["raw_metrics"] = {"score": 1.25}

    assert register_autoresearch_run(root, with_extra_field).status == "blocked"

    duplicate_key_json = json.dumps(payload)[:-1] + ',"run_id":"duplicate"}'
    assert register_autoresearch_run(root, duplicate_key_json).reason == "payload_invalid"

    root, payload = _workflow_fixture(tmp_path / "nonfinite")
    nonfinite = dict(payload)
    nonfinite["final_score"] = float("nan")
    result = register_autoresearch_run(root, nonfinite)

    assert result.status == "blocked"
    assert result.exit_code == EXIT_BLOCKED
    assert result.reason == "payload_invalid"
    assert not (root / AUTORESEARCH_ARTIFACT_RELATIVE_PATH).exists()
    boolean_score = dict(payload)
    boolean_score["baseline_score"] = True
    assert register_autoresearch_run(root, boolean_score).reason == "payload_invalid"


def test_register_requires_g3_and_impl_phase(tmp_path: Path):
    root, payload = _workflow_fixture(tmp_path)
    state_path = root / ".workflow" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["currentPhase"] = "verify"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = register_autoresearch_run(root, payload)

    assert result.status == "blocked"
    assert result.exit_code == EXIT_BLOCKED
    assert result.reason == "phase_not_impl"
    assert not (root / AUTORESEARCH_ARTIFACT_RELATIVE_PATH).exists()

    state["currentPhase"] = "impl"
    state["gates"]["G3"]["passed"] = False
    state_path.write_text(json.dumps(state), encoding="utf-8")
    assert register_autoresearch_run(root, payload).reason == "g3_not_passed"
    assert not (root / AUTORESEARCH_ARTIFACT_RELATIVE_PATH).exists()


def test_register_blocks_out_of_scope_and_unsafe_evidence_paths(tmp_path: Path):
    root, payload = _workflow_fixture(tmp_path)
    out_of_scope = dict(payload)
    out_of_scope["changed_files"] = ["src/not-planned.py"]

    result = register_autoresearch_run(root, out_of_scope)

    assert result.status == "blocked"
    assert result.exit_code == EXIT_BLOCKED
    assert result.reason == "changed_files_out_of_scope"

    root, payload = _workflow_fixture(tmp_path / "traversal")
    traversal = dict(payload)
    traversal["metrics_path"] = "../metrics.txt"
    result = register_autoresearch_run(root, traversal)

    assert result.status == "blocked"
    assert result.exit_code == EXIT_BLOCKED
    assert result.reason == "payload_invalid"
    assert not (root / AUTORESEARCH_ARTIFACT_RELATIVE_PATH).exists()

    root, payload = _workflow_fixture(tmp_path / "symlink")
    metrics_path = root / str(payload["metrics_path"])
    metrics_path.unlink()
    target = root / "metrics-target.txt"
    target.write_text("score=1.25\n", encoding="utf-8")
    metrics_path.symlink_to(target)
    result = register_autoresearch_run(root, payload)

    assert result.status == "blocked"
    assert result.reason == "metrics_invalid"
    assert not (root / AUTORESEARCH_ARTIFACT_RELATIVE_PATH).exists()


def test_register_rejects_empty_changed_files(tmp_path: Path) -> None:
    root, payload = _workflow_fixture(tmp_path)
    empty = dict(payload)
    empty["changed_files"] = []

    result = register_autoresearch_run(root, empty)

    assert result.status == "blocked"
    assert result.reason == "payload_invalid"
    assert not (root / AUTORESEARCH_ARTIFACT_RELATIVE_PATH).exists()


def test_register_blocks_allowed_files_bypass_after_approval(tmp_path: Path) -> None:
    root, payload = _workflow_fixture(tmp_path)
    (root / "src" / "unapproved.py").write_text("unapproved = True\n", encoding="utf-8")
    (root / ".workflow" / "artifacts" / "allowed-files.json").write_text(
        json.dumps(
            {
                "planned_files": ["src/feature.py", "src/unapproved.py"],
                "expanded_files": ["src/expanded.py"],
            }
        ),
        encoding="utf-8",
    )
    bypass = dict(payload)
    bypass["changed_files"] = ["src/unapproved.py"]

    result = register_autoresearch_run(root, bypass)

    assert result.status == "replan_required"
    assert result.reason == "planning_identity_changed"
    assert not (root / AUTORESEARCH_ARTIFACT_RELATIVE_PATH).exists()


def test_register_blocks_stale_six_artifact_seal(tmp_path: Path) -> None:
    root, payload = _workflow_fixture(tmp_path)
    (root / ".workflow" / "artifacts" / "test-criteria.md").write_text(
        "# Changed criteria\n",
        encoding="utf-8",
    )

    result = register_autoresearch_run(root, payload)

    assert result.status == "replan_required"
    assert result.reason == "planning_identity_changed"
    assert not (root / AUTORESEARCH_ARTIFACT_RELATIVE_PATH).exists()


def test_register_revalidates_seal_immediately_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, payload = _workflow_fixture(tmp_path)
    validate_evidence = autoresearch._validate_evidence
    calls = 0

    def mutate_after_first_validation(
        repo_root: Path,
        run: autoresearch.AutoresearchRun,
        approved,
    ) -> None:
        nonlocal calls
        validate_evidence(repo_root, run, approved)
        calls += 1
        if calls == 1:
            (repo_root / ".workflow" / "artifacts" / "allowed-files.json").write_text(
                json.dumps(
                    {
                        "planned_files": ["src/feature.py", "src/concurrent.py"],
                        "expanded_files": ["src/expanded.py"],
                    }
                ),
                encoding="utf-8",
            )

    monkeypatch.setattr(
        autoresearch,
        "_validate_evidence",
        mutate_after_first_validation,
    )

    result = register_autoresearch_run(root, payload)

    assert result.status == "replan_required"
    assert result.reason == "planning_identity_changed"
    assert not (root / AUTORESEARCH_ARTIFACT_RELATIVE_PATH).exists()


def test_autoresearch_register_parser_contract():
    args = build_parser().parse_args(
        [
            "wf",
            "autoresearch-register",
            "--result-json",
            "result.json",
            "--repo-root",
            ".",
            "--json",
        ]
    )

    assert args.handler is run_wf_autoresearch_register
    assert args.result_json == "result.json"
    assert args.json is True


def test_autoresearch_register_cli_writes_sanitized_result(
    tmp_path: Path, capsys
):
    root, payload = _workflow_fixture(tmp_path)
    result_file = root / "completed-autoresearch.json"
    result_file.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = run_wf_autoresearch_register(
        Namespace(result_json=str(result_file), repo_root=str(root), json=True)
    )

    assert exit_code == EXIT_SUCCESS
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "artifact_path": AUTORESEARCH_ARTIFACT_RELATIVE_PATH,
        "reason": "registered",
        "schema_version": 1,
        "status": "written",
    }


def test_autoresearch_register_cli_fails_closed_for_unreadable_result(
    tmp_path: Path, capsys
):
    exit_code = run_wf_autoresearch_register(
        Namespace(
            result_json=str(tmp_path / "missing.json"),
            repo_root=str(tmp_path),
            json=True,
        )
    )

    assert exit_code == EXIT_BLOCKED
    rendered = capsys.readouterr().out
    output = json.loads(rendered)
    assert output["status"] == "blocked"
    assert output["reason"] == "result_file_unreadable"
    assert "missing.json" not in rendered


def _assert_result_file_is_blocked(
    result_path: Path,
    *,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        wf_commands,
        "register_autoresearch_run",
        lambda *_args, **_kwargs: pytest.fail("unsafe result file reached registration"),
    )

    exit_code = run_wf_autoresearch_register(
        Namespace(result_json=str(result_path), repo_root=str(result_path.parent), json=True)
    )

    assert exit_code == EXIT_BLOCKED
    assert json.loads(capsys.readouterr().out)["reason"] == "result_file_unreadable"


def test_autoresearch_register_rejects_oversize_result_before_registration(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    result_path = tmp_path / "oversize.json"
    result_path.write_bytes(b"x" * (64 * 1024 + 1))

    _assert_result_file_is_blocked(result_path, monkeypatch=monkeypatch, capsys=capsys)


def test_autoresearch_register_rejects_symlink_result_before_registration(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    result_path = tmp_path / "result-link.json"
    result_path.symlink_to(target)

    _assert_result_file_is_blocked(result_path, monkeypatch=monkeypatch, capsys=capsys)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable on this platform")
def test_autoresearch_register_rejects_fifo_result_without_blocking(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    result_path = tmp_path / "result.fifo"
    os.mkfifo(result_path)

    _assert_result_file_is_blocked(result_path, monkeypatch=monkeypatch, capsys=capsys)


def test_autoresearch_schema_parser_and_output(capsys):
    args = build_parser().parse_args(["wf", "autoresearch-schema", "--json"])

    assert args.handler is run_wf_autoresearch_schema
    assert run_wf_autoresearch_schema(args) == 0
    assert json.loads(capsys.readouterr().out) == AUTORESEARCH_RUN_JSON_SCHEMA
