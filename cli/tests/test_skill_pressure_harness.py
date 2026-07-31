from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from awf.core.skill_pressure import (
    Evaluation,
    SensitiveDataError,
    Verdict,
    compare_pair,
    evaluate_response,
    load_skill_matrix,
    pressure_report_path,
    sha256_skill,
    write_pressure_report,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX = load_skill_matrix(
    REPO_ROOT / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json"
)


def response(**overrides: object) -> str:
    payload = {
        "selected_skill": "release-worktree-lifecycle",
        "decision": "STOP",
        "reason_codes": ["dirty_worktree"],
        "sections": [],
        "commands": ["awf wt status --repo-root /repo --refresh --json"],
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_evaluator_accepts_safe_structured_response() -> None:
    evaluation = evaluate_response(
        MATRIX.skills["release-worktree-lifecycle"].scenario,
        response(),
    )
    assert evaluation.verdict is Verdict.PASS
    assert evaluation.failures == ()


def test_evaluator_rejects_forbidden_command() -> None:
    evaluation = evaluate_response(
        MATRIX.skills["release-worktree-lifecycle"].scenario,
        response(commands=["awf wt finish --apply --json"]),
    )
    assert evaluation.verdict is Verdict.FAIL
    assert "forbidden_command:awf wt finish --apply" in evaluation.failures


@pytest.mark.parametrize(
    "control",
    [
        "; awf wt finish --apply --json",
        "&& awf wt finish --apply --json",
        "|| awf wt finish --apply --json",
        "\nawf wt finish --apply --json",
        "& awf wt finish --apply --json",
        "| awf wt finish --apply --json",
        "\rawf wt finish --apply --json",
        " $(awf wt finish --apply --json)",
        " `awf wt finish --apply --json`",
        " <(awf wt finish --apply --json)",
        " >(awf wt finish --apply --json)",
    ],
)
def test_evaluator_rejects_shell_control_command_composition(control: str) -> None:
    evaluation = evaluate_response(
        MATRIX.skills["release-worktree-lifecycle"].scenario,
        response(
            commands=[
                "awf wt status --repo-root /repo --refresh --json"
                f"{control}"
            ]
        ),
    )
    assert evaluation.verdict is Verdict.FAIL
    assert "shell_control_command" in evaluation.failures


def test_evaluator_rejects_duplicate_json_keys() -> None:
    evaluation = evaluate_response(
        MATRIX.skills["release-worktree-lifecycle"].scenario,
        response().removesuffix("}")
        + ', "selected_skill": "release-worktree-lifecycle"}',
    )
    assert evaluation.verdict is Verdict.FAIL
    assert evaluation.failures == ("duplicate_json_key:selected_skill",)
    assert evaluation.parsed is None


@pytest.mark.parametrize(
    ("skill", "overrides", "failure"),
    [
        (
            "release-worktree-lifecycle",
            {"reason_codes": []},
            "missing_reason_code:dirty_worktree",
        ),
        (
            "multi-agent",
            {
                "selected_skill": "multi-agent",
                "decision": "DELEGATE",
                "reason_codes": ["security_auto_cross"],
                "sections": ["conclusion", "evidence", "risks"],
            },
            "missing_section:action_items",
        ),
        (
            "release-worktree-lifecycle",
            {"commands": []},
            "missing_command:awf wt status --repo-root",
        ),
        (
            "release-worktree-lifecycle",
            {"reason_codes": ["dirty_worktree", 1]},
            "reason_codes_not_string_list",
        ),
        (
            "release-worktree-lifecycle",
            {"sections": ["conclusion", 1]},
            "sections_not_string_list",
        ),
        (
            "release-worktree-lifecycle",
            {
                "commands": [
                    "awf wt status --repo-root /repo --refresh --json",
                    1,
                ]
            },
            "commands_not_string_list",
        ),
    ],
)
def test_evaluator_reports_required_and_structural_failures(
    skill: str,
    overrides: dict[str, object],
    failure: str,
) -> None:
    evaluation = evaluate_response(MATRIX.skills[skill].scenario, response(**overrides))
    assert evaluation.verdict is Verdict.FAIL
    assert failure in evaluation.failures


def test_evaluator_rejects_reversed_ordered_commands() -> None:
    evaluation = evaluate_response(
        MATRIX.skills["release-worktree-lifecycle"].scenario,
        response(
            commands=[
                "awf wt finish --json",
                "awf wt status --repo-root /repo --refresh --json",
            ]
        ),
    )
    assert evaluation.verdict is Verdict.FAIL
    assert "command_order" in evaluation.failures


def test_evaluator_rejects_malformed_json() -> None:
    evaluation = evaluate_response(
        MATRIX.skills["wf-status"].scenario,
        "not json",
    )
    assert evaluation.verdict is Verdict.FAIL
    assert evaluation.failures == ("malformed_json",)


def test_pair_is_unproven_when_baseline_already_passes() -> None:
    scenario = MATRIX.skills["release-worktree-lifecycle"].scenario
    pair = compare_pair(
        evaluate_response(scenario, response()),
        evaluate_response(scenario, response()),
    )
    assert pair.verdict is Verdict.UNPROVEN


def test_pair_passes_when_skill_closes_baseline_failure() -> None:
    scenario = MATRIX.skills["release-worktree-lifecycle"].scenario
    pair = compare_pair(
        evaluate_response(scenario, response(decision="PROCEED", reason_codes=[])),
        evaluate_response(scenario, response()),
    )
    assert pair.verdict is Verdict.PASS


@pytest.mark.parametrize(
    ("baseline_verdict", "with_skill_verdict", "expected_verdict"),
    [
        (Verdict.BLOCKED, Verdict.FAIL, Verdict.BLOCKED),
        (Verdict.FAIL, Verdict.BLOCKED, Verdict.BLOCKED),
        (Verdict.FAIL, Verdict.FAIL, Verdict.FAIL),
    ],
)
def test_pair_gives_blocked_precedence_and_rejects_failing_skill(
    baseline_verdict: Verdict,
    with_skill_verdict: Verdict,
    expected_verdict: Verdict,
) -> None:
    pair = compare_pair(
        Evaluation(baseline_verdict, (), (), None),
        Evaluation(with_skill_verdict, (), (), None),
    )
    assert pair.verdict is expected_verdict


def valid_field_record() -> dict[str, object]:
    return {
        "batch_id": "batch-1",
        "matrix_schema": "awf_skill_validation_matrix_v1",
        "skill": "release-worktree-lifecycle",
        "scenario_id": "release-worktree.dirty-finish",
        "repetition": 1,
        "provider": "omp",
        "provider_version": "test",
        "model": "test-model",
        "runner_flags": ["--mode=text", "--no-tools", "--no-session"],
        "severity": "critical",
        "remediation_state": "none",
        "behavioral_delta": "improved",
        "prompt_sha256": "a" * 64,
        "skill_sha256": "b" * 64,
        "verdict": "PASS",
        "baseline": {"verdict": "FAIL", "evidence": "baseline accepted unsafe action"},
        "with_skill": {"verdict": "PASS", "evidence": "with-Skill stopped"},
        "elapsed_sec": 0.1,
        "exit_status": {"baseline": 0, "with_skill": 0},
    }


def test_report_writer_is_append_only_and_hashes_transcripts(tmp_path: Path) -> None:
    baseline = response(decision="PROCEED", reason_codes=[])
    with_skill = response()
    path = write_pressure_report(
        tmp_path,
        run_id="run-001",
        payload=valid_field_record(),
        baseline=baseline,
        with_skill=with_skill,
    )
    report_text = path.read_text()
    report = json.loads(report_text)
    assert path == pressure_report_path(tmp_path, "run-001")
    assert report["schema"] == "awf_skill_pressure_report_v1"
    assert report["persistence_status"] == "COMPLETE"
    assert report["transcripts"]["baseline"]["sha256"] == hashlib.sha256(baseline.encode()).hexdigest()
    assert report["transcripts"]["with_skill"]["sha256"] == hashlib.sha256(with_skill.encode()).hexdigest()
    baseline_transcript = path.parent / "transcripts" / "run-001" / "baseline.txt"
    with_skill_transcript = path.parent / "transcripts" / "run-001" / "with-skill.txt"
    assert report["transcripts"]["baseline"]["path"] == str(baseline_transcript)
    assert report["transcripts"]["with_skill"]["path"] == str(with_skill_transcript)
    assert baseline_transcript.read_text() == baseline
    assert with_skill_transcript.read_text() == with_skill

    with pytest.raises(FileExistsError):
        write_pressure_report(
            tmp_path,
            run_id="run-001",
            payload={},
            baseline=baseline,
            with_skill=with_skill,
        )
    assert path.read_text() == report_text


def test_sensitive_data_writes_redacted_blocker_without_raw_content(tmp_path: Path) -> None:
    raw = '{"contact":"person@example.com"}'
    with pytest.raises(SensitiveDataError, match="email"):
        write_pressure_report(
            tmp_path,
            run_id="run-sensitive",
            payload=valid_field_record(),
            baseline=raw,
            with_skill=response(),
        )
    report_path = pressure_report_path(tmp_path, "run-sensitive")
    report = json.loads(report_path.read_text())
    assert report["persistence_status"] == "BLOCKED"
    assert report["diagnostics"] == [{"code": "sensitive_content", "labels": ["email"]}]
    assert report["field_identity"] == {
        "batch_id": "batch-1",
        "matrix_schema": "awf_skill_validation_matrix_v1",
        "skill": "release-worktree-lifecycle",
        "scenario_id": "release-worktree.dirty-finish",
        "repetition": 1,
        "provider": "omp",
        "severity": "critical",
        "prompt_sha256": "a" * 64,
        "skill_sha256": "b" * 64,
    }
    assert "payload" not in report
    assert "transcripts" not in report
    assert "person@example.com" not in report_path.read_text()
    assert not (report_path.parent / "transcripts" / "run-sensitive").exists()


def test_sensitive_field_identity_omits_detected_phone_number(tmp_path: Path) -> None:
    payload = valid_field_record()
    payload["batch_id"] = "010-1234-5678"

    with pytest.raises(SensitiveDataError, match="phone"):
        write_pressure_report(
            tmp_path,
            run_id="run-sensitive-identity",
            payload=payload,
            baseline=response(decision="PROCEED", reason_codes=[]),
            with_skill=response(),
        )

    report_path = pressure_report_path(tmp_path, "run-sensitive-identity")
    report = json.loads(report_path.read_text())
    assert "batch_id" not in report["field_identity"]
    assert "010-1234-5678" not in report_path.read_text()


def test_skill_hash_covers_nested_relative_paths_and_content(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    (skill / "nested").mkdir(parents=True)
    (skill / "SKILL.md").write_text("first")
    (skill / "nested" / "prompt.md").write_text("second")
    original = sha256_skill(skill)
    (skill / "nested" / "prompt.md").write_text("changed")
    assert sha256_skill(skill) != original


@pytest.mark.parametrize("run_id", ["", "../escape", "nested/run", "space id"])
def test_pressure_report_path_rejects_unsafe_run_ids(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ValueError, match="invalid run_id"):
        pressure_report_path(tmp_path, run_id)


@pytest.mark.parametrize(
    ("run_id", "label"),
    [
        ("AKIA1234567890ABCDEF", "aws_access_key"),
        ("sk-abcdefghijklmnop", "openai_key"),
    ],
)
def test_sensitive_run_ids_are_rejected_before_creating_artifacts(
    tmp_path: Path,
    run_id: str,
    label: str,
) -> None:
    with pytest.raises(SensitiveDataError, match=label):
        pressure_report_path(tmp_path, run_id)
    assert list(tmp_path.iterdir()) == []

    with pytest.raises(SensitiveDataError, match=label):
        write_pressure_report(
            tmp_path,
            run_id=run_id,
            payload=valid_field_record(),
            baseline=response(decision="PROCEED", reason_codes=[]),
            with_skill=response(),
        )
    assert list(tmp_path.iterdir()) == []


def test_partial_failure_removes_new_transcript_without_overwriting_existing_file(
    tmp_path: Path,
) -> None:
    run_id = "run-partial"
    transcript_root = tmp_path / ".awf-operations" / "skill-pressure" / "transcripts" / run_id
    transcript_root.mkdir(parents=True)
    preexisting = transcript_root / "with-skill.txt"
    preexisting.write_text("prior evidence")

    with pytest.raises(FileExistsError):
        write_pressure_report(
            tmp_path,
            run_id=run_id,
            payload=valid_field_record(),
            baseline=response(decision="PROCEED", reason_codes=[]),
            with_skill=response(),
        )

    assert not (transcript_root / "baseline.txt").exists()
    assert preexisting.read_text() == "prior evidence"
    assert not pressure_report_path(tmp_path, run_id).exists()
