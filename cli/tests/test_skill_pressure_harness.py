from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path
import shutil
from dataclasses import replace

import pytest

from awf.core.skill_pressure import (
    Evaluation,
    HIGH_RISK_SKILLS,
    SensitiveDataError,
    Verdict,
    compare_pair,
    evaluate_response,
    load_skill_matrix,
    pressure_report_path,
    sha256_skill,
    write_pressure_report,
)
import awf.core.skill_pressure as pressure
from awf.providers.base import ProviderResult
from awf.core.skill_subscription import (
    PINNED_SUBSCRIPTION_MODELS,
    SubscriptionAuthContext,
    build_subscription_environment,
    normalize_host_diagnostic,
    require_subscription_model,
)
import build_skill_evidence
import run_skill_pressure
from run_skill_pressure import (
    build_prompt,
    execute_pair,
    expanded_runs,
    probe_omp,
    repetitions_for,
    select_cases,
)
import run_skill_discovery
from run_skill_discovery import (
    ProcessResult as DiscoveryProcessResult,
    agent_skills_argv,
    claude_argv,
    load_expected_skills,
    omp_argv,
    required_flags,
    run_discovery,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX = load_skill_matrix(
    REPO_ROOT / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json"
)


def _subscription_auth(tmp_path: Path) -> SubscriptionAuthContext:
    original_home = tmp_path / "operator"
    return SubscriptionAuthContext(
        original_home=original_home,
        claude_config_dir=original_home / ".claude",
        codex_home=original_home / ".codex",
        omp_agent_dir=original_home / ".omp" / "agent",
    )


def test_subscription_environment_capture_derives_defaults_and_honors_explicit_paths(
    tmp_path: Path,
) -> None:
    original_home = tmp_path / "operator"
    defaults = SubscriptionAuthContext.capture({"HOME": str(original_home)})

    assert defaults.original_home == original_home.resolve()
    assert defaults.claude_config_dir == (original_home / ".claude").resolve()
    assert defaults.codex_home == (original_home / ".codex").resolve()
    assert defaults.omp_agent_dir == (original_home / ".omp" / "agent").resolve()

    explicit = SubscriptionAuthContext.capture(
        {
            "HOME": str(original_home),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
            "CODEX_HOME": str(tmp_path / "codex"),
            "PI_CODING_AGENT_DIR": str(tmp_path / "omp-agent"),
        }
    )

    assert explicit.claude_config_dir == (tmp_path / "claude").resolve()
    assert explicit.codex_home == (tmp_path / "codex").resolve()
    assert explicit.omp_agent_dir == (tmp_path / "omp-agent").resolve()

@pytest.mark.parametrize(
    ("configured_codex_home", "expected"),
    [
        ("~", Path("/fake/operator")),
        ("~/.codex", Path("/fake/operator/.codex")),
    ],
)
def test_subscription_environment_capture_expands_tilde_against_supplied_home(
    configured_codex_home: str,
    expected: Path,
) -> None:
    auth = SubscriptionAuthContext.capture(
        {
            "HOME": "/fake/operator",
            "CODEX_HOME": configured_codex_home,
        }
    )

    assert auth.codex_home == expected


def test_subscription_environment_builds_isolated_copies(tmp_path: Path) -> None:
    auth = _subscription_auth(tmp_path)
    temporary_home = tmp_path / "run" / "home"
    base = {"HOME": "/wrong", "PATH": "/bin"}

    claude = build_subscription_environment("claude", auth, temporary_home, base)
    codex = build_subscription_environment("agent-skills", auth, temporary_home, base)
    claude["PATH"] = "/modified"
    claude["EXTRA"] = "value"

    assert base == {"HOME": "/wrong", "PATH": "/bin"}
    assert codex["PATH"] == "/bin"
    assert "EXTRA" not in codex


def test_subscription_environments_reference_only_the_required_store(tmp_path: Path) -> None:
    auth = _subscription_auth(tmp_path)
    temporary_home = tmp_path / "run" / "home"
    base = {
        "HOME": "/wrong",
        "CLAUDE_CONFIG_DIR": "/wrong/claude",
        "CODEX_HOME": "/wrong/codex",
        "PI_CODING_AGENT_DIR": "/wrong/omp",
        "ANTHROPIC_API_KEY": "must-be-removed",
        "OPENAI_API_KEY": "must-be-removed",
        "PATH": "/bin",
    }

    claude = build_subscription_environment("claude", auth, temporary_home, base)
    codex = build_subscription_environment("agent-skills", auth, temporary_home, base)
    omp = build_subscription_environment("omp", auth, temporary_home, base)

    assert claude["HOME"] == str(auth.original_home)
    assert claude["CLAUDE_CONFIG_DIR"] == str(auth.claude_config_dir)
    assert "CODEX_HOME" not in claude and "PI_CODING_AGENT_DIR" not in claude
    assert codex["HOME"] == str(temporary_home)
    assert codex["CODEX_HOME"] == str(auth.codex_home)
    assert "CLAUDE_CONFIG_DIR" not in codex and "PI_CODING_AGENT_DIR" not in codex
    assert omp["HOME"] == str(temporary_home)
    assert omp["PI_CODING_AGENT_DIR"] == str(auth.omp_agent_dir)
    assert "CLAUDE_CONFIG_DIR" not in omp and "CODEX_HOME" not in omp
    for environment in (claude, codex, omp):
        assert environment["PATH"] == "/bin"
        assert "ANTHROPIC_API_KEY" not in environment
        assert "OPENAI_API_KEY" not in environment


def test_subscription_models_are_pinned_and_reject_invalid_selection() -> None:
    assert dict(PINNED_SUBSCRIPTION_MODELS) == {
        "claude": "sonnet",
        "agent-skills": "gpt-5.4",
        "omp": "openai-codex/gpt-5.6-sol",
    }

    for runtime, model in PINNED_SUBSCRIPTION_MODELS.items():
        require_subscription_model(runtime, model)

    with pytest.raises(ValueError, match="subscription model mismatch"):
        require_subscription_model("agent-skills", "openai-codex/gpt-5.6-sol")
    with pytest.raises(ValueError, match="unsupported runtime"):
        require_subscription_model("unsupported", "model")

def test_pinned_subscription_models_reject_mutation() -> None:
    with pytest.raises(TypeError):
        PINNED_SUBSCRIPTION_MODELS["claude"] = "other"




@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "error_kind", "expected"),
    [
        (
            1,
            "subscription renewal expired; model is not supported; not logged in",
            "",
            "timeout",
            "host_timeout",
        ),
        (
            1,
            "subscription renewal expired; model is not supported; not logged in",
            "",
            None,
            "host_subscription_expired",
        ),
        (1, "", "model is not supported; not logged in", None, "host_model_unsupported"),
        (1, "", "not logged in", None, "host_auth_unavailable"),
        (7, "", "opaque provider text", None, "host_provider_exit"),
        (0, "", "", None, ""),
    ],
)
def test_host_diagnostics_are_allowlisted_and_prioritized(
    returncode: int,
    stdout: str,
    stderr: str,
    error_kind: str | None,
    expected: str,
) -> None:
    assert normalize_host_diagnostic(returncode, stdout, stderr, error_kind) == expected

def test_host_diagnostic_ignores_warning_text_after_success() -> None:
    assert (
        normalize_host_diagnostic(
            0,
            stderr="warning: model gpt-5.4 is not supported for dry-run",
        )
        == ""
    )


@pytest.mark.parametrize(
    "message",
    [
        "unsupported model: gpt-5.4",
        "model openai-codex/gpt-5.6-sol is not supported",
        "model is unsupported",
        "model sonnet is unsupported",
        "model Sonnet is unsupported",
        "model sonnet is not supported",
        "model openai-codex/gpt-5.6-sol is unsupported",
        "The 'openai-codex/gpt-5.6-sol' model is not supported by this provider",
    ],
)
def test_host_diagnostic_classifies_explicit_unsupported_model(message: str) -> None:
    assert normalize_host_diagnostic(1, stderr=message) == "host_model_unsupported"


@pytest.mark.parametrize(
    "message",
    [
        "unsupported tool: model-inspector",
        "feature model-management is unsupported",
        "unsupported parameter: model",
        "tool model is not supported",
        "feature model is not supported",
        "parameter model is not supported",
        "model response is unsupported",
        "model output is not supported",
        "model result is unsupported",
        "model tool is not supported",
        "model feature is unsupported",
        "model parameter is not supported",
    ],
)
def test_host_diagnostic_does_not_misclassify_unsupported_non_models(message: str) -> None:
    assert normalize_host_diagnostic(1, stderr=message) == "host_provider_exit"


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



def test_prompt_requires_one_strict_json_object() -> None:
    scenario = MATRIX.skills["wf-status"].scenario
    prompt = build_prompt(scenario)

    assert '"selected_skill"' in prompt
    assert '"decision"' in prompt
    assert "Do not run commands" in prompt
    assert scenario.task in prompt


def test_execute_pair_uses_no_skills_then_exact_skill() -> None:
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        calls.append(argv)
        selected = "wf-status" if "--skills=wf-status" in argv else "wf-status"
        return ProviderResult(
            returncode=0,
            stdout=json.dumps(
                {
                    "selected_skill": selected,
                    "decision": "REPORT",
                    "reason_codes": ["workflow_not_initialized"],
                    "sections": [],
                    "commands": [],
                }
            ),
            stderr="",
            provider_name="omp",
            model="test-model",
        )

    run = execute_pair(
        MATRIX.skills["wf-status"],
        repo_root=REPO_ROOT,
        omp_command="omp",
        model="test-model",
        timeout_sec=30,
        run_process=fake_run,
    )

    assert "--no-skills" in calls[0]
    assert "--skills=wf-status" in calls[1]
    assert run.evaluation.with_skill.verdict is Verdict.PASS
    assert run.with_skill_result.stdout


def test_execute_pair_maps_provider_timeout_to_blocked() -> None:
    def timed_out(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        return ProviderResult(
            returncode=124,
            stdout="",
            stderr="provider_timeout",
            provider_name="omp",
        )

    run = execute_pair(
        MATRIX.skills["wf-status"],
        repo_root=REPO_ROOT,
        omp_command="omp",
        model="test-model",
        timeout_sec=30,
        run_process=timed_out,
    )

    assert run.evaluation.verdict is Verdict.BLOCKED
    assert run.evaluation.with_skill.verdict is Verdict.BLOCKED


def test_case_selection_rejects_unknown_skill() -> None:
    with pytest.raises(ValueError, match="unknown Skills: missing"):
        select_cases(MATRIX, ["missing"], select_all=False)


def test_high_risk_skills_repeat_three_times() -> None:
    assert repetitions_for(MATRIX.skills["release-worktree-lifecycle"]) == 3
    assert repetitions_for(MATRIX.skills["wf-status"]) == 1


def test_all_selection_expands_to_exact_27_unique_pairs() -> None:
    selected = select_cases(MATRIX, None, select_all=True)
    identities = [(case.name, repetition) for case, repetition in expanded_runs(selected)]

    assert len(identities) == 27
    assert len(set(identities)) == 27
    assert {name for name, _ in identities} == set(MATRIX.skills)
    assert {
        name for name in MATRIX.skills if sum(pair[0] == name for pair in identities) == 3
    } == HIGH_RISK_SKILLS


def test_probe_omp_preserves_timeout_as_blocked_preflight() -> None:
    def timed_out(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        return ProviderResult(124, "", "provider_timeout", provider_name="omp")

    result = probe_omp("omp", repo_root=REPO_ROOT, run_process=timed_out)

    assert result.returncode == 124
    assert result.stderr == "provider_timeout"


def test_probe_omp_rejects_unsupported_skill_selection_flags() -> None:
    def missing_flags(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        stdout = "omp v1" if "--version" in argv else "--no-tools --no-session"
        return ProviderResult(0, stdout, "", provider_name="omp")

    result = probe_omp("omp", repo_root=REPO_ROOT, run_process=missing_flags)

    assert result.returncode == 78
    assert "unsupported_omp_flags" in result.stderr


def _successful_wf_status_result() -> ProviderResult:
    return ProviderResult(
        0,
        json.dumps(
            {
                "selected_skill": "wf-status",
                "decision": "REPORT",
                "reason_codes": ["workflow_not_initialized"],
                "sections": [],
                "commands": [],
            }
        ),
        "",
        provider_name="omp",
    )


def _copied_wf_status_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "repo"
    source = repo_root / "claude" / "skills" / "wf-status"
    shutil.copytree(REPO_ROOT / "claude" / "skills" / "wf-status", source)
    return repo_root, source


def test_execute_pair_uses_private_materialized_skill_snapshot() -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    source = REPO_ROOT / "claude" / "skills" / "wf-status"
    source_hash = sha256_skill(source)

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        calls.append((argv, env))
        snapshot = Path(env["PI_CODING_AGENT_DIR"]) / "skills" / "wf-status"
        assert snapshot.is_dir()
        assert not snapshot.is_symlink()
        assert snapshot != source
        assert sha256_skill(snapshot) == source_hash
        assert [path.name for path in snapshot.parent.iterdir()] == ["wf-status"]
        return _successful_wf_status_result()

    run = execute_pair(
        MATRIX.skills["wf-status"],
        repo_root=REPO_ROOT,
        omp_command="omp",
        model="test-model",
        timeout_sec=30,
        run_process=fake_run,
    )

    assert len(calls) == 2
    assert calls[0][1]["PI_CODING_AGENT_DIR"] == calls[1][1]["PI_CODING_AGENT_DIR"]
    assert run.skill_sha256 == source_hash


@pytest.mark.parametrize(
    ("case", "repo_builder", "error"),
    [
        (
            replace(MATRIX.skills["wf-status"], name="missing-skill"),
            lambda tmp_path: (tmp_path / "repo", tmp_path / "repo" / "missing"),
            "Skill source is not a directory",
        ),
        (
            replace(MATRIX.skills["wf-status"], name="../escape"),
            lambda tmp_path: (tmp_path / "repo", tmp_path / "repo" / "escape"),
            "invalid Skill name",
        ),
    ],
)
def test_execute_pair_rejects_missing_or_escaping_skill_source_before_process(
    case: object,
    repo_builder: object,
    error: str,
    tmp_path: Path,
) -> None:
    repo_root, outside = repo_builder(tmp_path)  # type: ignore[operator]
    (repo_root / "claude" / "skills").mkdir(parents=True)
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "SKILL.md").write_text("outside")
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        calls.append(argv)
        return _successful_wf_status_result()

    with pytest.raises(ValueError, match=error):
        execute_pair(
            case,  # type: ignore[arg-type]
            repo_root=repo_root,
            omp_command="omp",
            model="test-model",
            timeout_sec=30,
            run_process=fake_run,
        )

    assert calls == []


def test_execute_pair_rejects_symlinked_skill_source_before_process(tmp_path: Path) -> None:
    repo_root, source = _copied_wf_status_repo(tmp_path)
    target = repo_root / "claude" / "skills" / "real-wf-status"
    source.rename(target)
    source.symlink_to(target, target_is_directory=True)
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        calls.append(argv)
        return _successful_wf_status_result()

    with pytest.raises(ValueError, match="symlink"):
        execute_pair(
            MATRIX.skills["wf-status"],
            repo_root=repo_root,
            omp_command="omp",
            model="test-model",
            timeout_sec=30,
            run_process=fake_run,
        )

    assert calls == []


def test_execute_pair_blocks_snapshot_mutation_without_changing_source(tmp_path: Path) -> None:
    repo_root, source = _copied_wf_status_repo(tmp_path)
    original = (source / "SKILL.md").read_text()

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        if "--skills=wf-status" in argv:
            snapshot = Path(env["PI_CODING_AGENT_DIR"]) / "skills" / "wf-status"
            (snapshot / "SKILL.md").write_text("mutated snapshot")
        return _successful_wf_status_result()

    run = execute_pair(
        MATRIX.skills["wf-status"],
        repo_root=repo_root,
        omp_command="omp",
        model="test-model",
        timeout_sec=30,
        run_process=fake_run,
    )

    assert (source / "SKILL.md").read_text() == original
    assert run.evaluation.verdict is Verdict.BLOCKED



def test_execute_pair_does_not_run_selected_skill_after_baseline_snapshot_mutation(
    tmp_path: Path,
) -> None:
    repo_root, _ = _copied_wf_status_repo(tmp_path)
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        calls.append(argv)
        snapshot = Path(env["PI_CODING_AGENT_DIR"]) / "skills" / "wf-status"
        (snapshot / "SKILL.md").write_text("mutated baseline snapshot")
        return _successful_wf_status_result()

    run = execute_pair(
        MATRIX.skills["wf-status"],
        repo_root=repo_root,
        omp_command="omp",
        model="test-model",
        timeout_sec=30,
        run_process=fake_run,
    )

    assert len(calls) == 1
    assert run.evaluation.verdict is Verdict.BLOCKED
    assert run.with_skill_result.returncode == 125

def test_case_selection_rejects_duplicate_skill_names() -> None:
    with pytest.raises(ValueError, match="duplicate Skills: wf-status"):
        select_cases(MATRIX, ["wf-status", "wf-status"], select_all=False)


def test_main_rejects_mixed_selection_before_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight_calls: list[object] = []

    def fake_probe(*args: object, **kwargs: object) -> ProviderResult:
        preflight_calls.append((args, kwargs))
        return ProviderResult(78, "", "should not run", provider_name="omp")

    monkeypatch.setattr(run_skill_pressure, "probe_omp", fake_probe)

    with pytest.raises(SystemExit) as error:
        run_skill_pressure.main(
            [
                "--batch-id",
                "batch-1",
                "--model",
                "test-model",
                "--all",
                "--skill",
                "wf-status",
            ]
        )

    assert error.value.code == 2
    assert preflight_calls == []


def test_main_rejects_duplicate_selection_before_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight_calls: list[object] = []

    def fake_probe(*args: object, **kwargs: object) -> ProviderResult:
        preflight_calls.append((args, kwargs))
        return ProviderResult(78, "", "should not run", provider_name="omp")

    monkeypatch.setattr(run_skill_pressure, "probe_omp", fake_probe)

    with pytest.raises(SystemExit) as error:
        run_skill_pressure.main(
            [
                "--batch-id",
                "batch-1",
                "--model",
                "test-model",
                "--skill",
                "wf-status",
                "--skill",
                "wf-status",
            ]
        )

    assert error.value.code == 2
    assert preflight_calls == []


@pytest.mark.parametrize(
    ("batch_id", "diagnostic"),
    [
        ("../escape", "invalid batch-id"),
        ("person@example.com", "sensitive batch-id"),
    ],
)
def test_main_rejects_unsafe_batch_id_before_preflight(
    batch_id: str,
    diagnostic: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    preflight_calls: list[object] = []

    def fake_probe(*args: object, **kwargs: object) -> ProviderResult:
        preflight_calls.append((args, kwargs))
        return ProviderResult(78, "", "should not run", provider_name="omp")

    monkeypatch.setattr(run_skill_pressure, "probe_omp", fake_probe)

    with pytest.raises(SystemExit) as error:
        run_skill_pressure.main(
            ["--batch-id", batch_id, "--model", "test-model", "--all"]
        )

    assert error.value.code == 2
    assert preflight_calls == []
    assert diagnostic in capsys.readouterr().err


def test_main_records_redacted_report_with_batch_scoped_run_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root, source = _copied_wf_status_repo(tmp_path)

    def fake_probe(*args: object, **kwargs: object) -> ProviderResult:
        return ProviderResult(0, "omp test", "", provider_name="omp")

    def fake_execute(
        case: object, **kwargs: object
    ) -> run_skill_pressure.PairRun:
        baseline = Evaluation(Verdict.FAIL, ("baseline_failure",), (), None)
        with_skill = Evaluation(Verdict.PASS, (), (), None)
        return run_skill_pressure.PairRun(
            evaluation=compare_pair(baseline, with_skill),
            baseline_result=ProviderResult(
                0,
                '{"contact":"person@example.com"}',
                "",
                provider_name="omp",
            ),
            with_skill_result=_successful_wf_status_result(),
            skill_sha256=sha256_skill(source),
        )

    monkeypatch.setattr(run_skill_pressure, "probe_omp", fake_probe)
    monkeypatch.setattr(run_skill_pressure, "execute_pair", fake_execute)

    assert (
        run_skill_pressure.main(
            [
                "--repo-root",
                str(repo_root),
                "--matrix",
                str(
                    REPO_ROOT
                    / "cli"
                    / "tests"
                    / "fixtures"
                    / "skill-validation-matrix.v1.json"
                ),
                "--batch-id",
                "batch-1",
                "--model",
                "test-model",
                "--skill",
                "wf-status",
                "--write-result",
                "--json",
            ]
        )
        == 1
    )

    output = json.loads(capsys.readouterr().out)
    persistence = output["results"][0]["persistence"]
    run_id = persistence["run_id"]
    assert persistence["status"] == "REDACTED"
    assert persistence["report_written"] is True
    assert run_id.startswith("batch-1-run-")
    assert "workflow" not in run_id
    assert output["results"][0]["verdict"] == Verdict.BLOCKED.value
    report_path = pressure_report_path(repo_root, run_id)
    assert report_path.exists()
    assert json.loads(report_path.read_text())["persistence_status"] == "BLOCKED"


def _matrix_sha256() -> str:
    return hashlib.sha256(
        (
            REPO_ROOT
            / "cli"
            / "tests"
            / "fixtures"
            / "skill-validation-matrix.v1.json"
        ).read_bytes()
    ).hexdigest()


def _provision_matrix(repo_root: Path) -> None:
    for relative in pressure.DETERMINISTIC_SOURCE_FILES:
        source = REPO_ROOT / relative
        target = repo_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    shutil.copytree(
        REPO_ROOT / "claude" / "skills",
        repo_root / "claude" / "skills",
        dirs_exist_ok=True,
    )


def passing_deterministic_report(tmp_path: Path, *, batch_id: str) -> Path:
    _provision_matrix(tmp_path)
    return pressure.write_deterministic_report(
        tmp_path,
        batch_id=batch_id,
        argv=pressure.DETERMINISTIC_PYTEST_ARGV,
        started_at="2026-07-30T00:00:00+00:00",
        finished_at="2026-07-30T00:00:01+00:00",
        elapsed_sec=1.0,
        exit_status=0,
        stdout="deterministic suite passed",
        stderr="",
        matrix_sha256=_matrix_sha256(),
        sources={
            relative: pressure.sha256_file(tmp_path / relative)
            for relative in pressure.DETERMINISTIC_SOURCE_FILES
        },
    )


def passing_install_report(tmp_path: Path, matrix: object, *, batch_id: str) -> Path:
    records = [
        {
            "runtime": runtime,
            "skill": case.name,
            "source_sha256": "d" * 64,
            "target_root": f".{runtime}/skills",
            "status": Verdict.PASS.value,
            "diagnostic": "linked",
        }
        for runtime in ("claude", "agent-skills", "omp")
        for case in matrix.skills.values()  # type: ignore[attr-defined]
    ]
    return pressure.write_install_report(
        tmp_path,
        batch_id=batch_id,
        matrix_sha256=_matrix_sha256(),
        isolated_home_id="tmp-home-1",
        records=records,
    )


def passing_discovery_records(matrix: object) -> list[dict[str, object]]:
    return [
        {
            "runtime": runtime,
            "skill": case.name,
            "source_sha256": "d" * 64,
            "verdict": Verdict.PASS.value,
            "diagnostic": "source fields match",
        }
        for runtime in ("claude", "agent-skills", "omp")
        for case in matrix.skills.values()  # type: ignore[attr-defined]
    ]


def passing_discovery_report(tmp_path: Path, matrix: object, *, batch_id: str) -> Path:
    path = (
        tmp_path
        / ".awf-operations"
        / "skill-pressure"
        / f"discovery-{batch_id}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "awf_skill_discovery_report_v1",
                "batch_id": batch_id,
                "matrix_sha256": _matrix_sha256(),
                "records": passing_discovery_records(matrix),
            }
        )
    )
    return path


def passing_field_records(matrix: object, *, batch_id: str = "batch-1") -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for case in matrix.skills.values():  # type: ignore[attr-defined]
        for repetition in range(1, repetitions_for(case) + 1):
            record = valid_field_record()
            record.update(
                {
                    "batch_id": batch_id,
                    "skill": case.name,
                    "scenario_id": case.scenario.id,
                    "repetition": repetition,
                    "severity": case.severity,
                    "baseline": {
                        "verdict": Verdict.FAIL.value,
                        "criteria": [
                            {
                                "id": "decision",
                                "verdict": Verdict.PASS.value,
                                "evidence": "baseline parsed",
                            }
                        ],
                    },
                    "with_skill": {
                        "verdict": Verdict.PASS.value,
                        "criteria": [
                            {
                                "id": "decision",
                                "verdict": Verdict.PASS.value,
                                "evidence": "with-skill parsed",
                            }
                        ],
                    },
                }
            )
            records.append(record)
    return records


def passing_field_report_paths(tmp_path: Path, matrix: object, *, batch_id: str) -> list[Path]:
    _provision_matrix(tmp_path)
    paths: list[Path] = []
    for record in passing_field_records(matrix, batch_id=batch_id):
        case = matrix.skills[record["skill"]]  # type: ignore[attr-defined]
        record["prompt_sha256"] = pressure.sha256_text(
            pressure.build_field_prompt(case.scenario)
        )
        record["skill_sha256"] = pressure.sha256_skill(
            tmp_path / "claude" / "skills" / case.name
        )
        paths.append(
            write_pressure_report(
                tmp_path,
                run_id=f"{batch_id}-{record['skill']}-{record['repetition']}",
                payload=record,
                baseline="baseline safe",
                with_skill="with-skill safe",
            )
        )
    return paths


def test_field_record_requires_complete_reproducibility_metadata() -> None:
    with pytest.raises(pressure.EvidenceError, match="missing field record keys"):
        pressure.validate_field_record({"matrix_schema": MATRIX.schema})


def test_source_bundle_requires_current_hashed_deterministic_and_install_reports(
    tmp_path: Path,
) -> None:
    deterministic = passing_deterministic_report(tmp_path, batch_id="batch-1")
    install = passing_install_report(tmp_path, MATRIX, batch_id="batch-1")
    pressure.validate_source_bundle(
        repo_root=tmp_path,
        batch_id="batch-1",
        deterministic_path=deterministic,
        install_path=install,
        discovery_path=passing_discovery_report(tmp_path, MATRIX, batch_id="batch-1"),
        field_paths=passing_field_report_paths(tmp_path, MATRIX, batch_id="batch-1"),
    )
    failed = json.loads(deterministic.read_text())
    failed["exit_status"] = 1
    deterministic.write_text(json.dumps(failed))
    with pytest.raises(pressure.EvidenceError, match="deterministic"):
        pressure.validate_source_bundle(
            repo_root=tmp_path,
            batch_id="batch-1",
            deterministic_path=deterministic,
            install_path=install,
            discovery_path=passing_discovery_report(tmp_path, MATRIX, batch_id="batch-1"),
            field_paths=passing_field_report_paths(tmp_path / "second", MATRIX, batch_id="batch-1"),
        )


def test_evidence_matrix_contains_exactly_15_by_9_unique_cells() -> None:
    cells = pressure.build_evidence_matrix(
        MATRIX,
        deterministic_pass=True,
        install_pass=True,
        discovery=passing_discovery_records(MATRIX),
        field=passing_field_records(MATRIX),
    )
    pressure.validate_evidence_matrix(MATRIX, cells)
    assert len(cells) == 135
    assert len({(cell.skill, cell.category) for cell in cells}) == 135


def test_evidence_matrix_rejects_missing_duplicate_and_unjustified_na() -> None:
    cells = list(
        pressure.build_evidence_matrix(
            MATRIX,
            deterministic_pass=True,
            install_pass=True,
            discovery=passing_discovery_records(MATRIX),
            field=passing_field_records(MATRIX),
        )
    )
    with pytest.raises(pressure.EvidenceError, match="exactly 135"):
        pressure.validate_evidence_matrix(MATRIX, cells[:-1])
    with pytest.raises(pressure.EvidenceError, match="duplicate"):
        pressure.validate_evidence_matrix(MATRIX, [*cells[:-1], cells[0]])
    cells[0] = pressure.EvidenceCell(
        skill=cells[0].skill,
        category=cells[0].category,
        layer=cells[0].layer,
        verdict=Verdict.NOT_APPLICABLE,
        evidence="not applicable",
        na_reason=None,
    )
    with pytest.raises(pressure.EvidenceError, match="N/A requires"):
        pressure.validate_evidence_matrix(MATRIX, cells)


def test_main_redacts_sensitive_persistence_and_continues_next_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = tmp_path / "repo"
    for name in ("wf-status", "analysis"):
        shutil.copytree(REPO_ROOT / "claude" / "skills" / name, repo_root / "claude" / "skills" / name)
    calls: list[str] = []

    def fake_probe(*args: object, **kwargs: object) -> ProviderResult:
        return ProviderResult(0, "omp fake", "", provider_name="omp")

    def fake_execute(case: object, **kwargs: object) -> run_skill_pressure.PairRun:
        name = case.name  # type: ignore[attr-defined]
        calls.append(name)
        baseline = Evaluation(Verdict.FAIL, ("baseline_failure",), (), None)
        with_skill = Evaluation(Verdict.PASS, (), (), None)
        return run_skill_pressure.PairRun(
            evaluation=compare_pair(baseline, with_skill),
            baseline_result=ProviderResult(
                0,
                '{"contact":"person@example.com"}' if name == "wf-status" else "safe baseline",
                "",
                provider_name="omp",
            ),
            with_skill_result=ProviderResult(0, "safe with-skill", "", provider_name="omp"),
            skill_sha256="b" * 64,
        )

    run_ids = iter(("run-sensitive", "run-safe"))
    monkeypatch.setattr(run_skill_pressure, "probe_omp", fake_probe)
    monkeypatch.setattr(run_skill_pressure, "execute_pair", fake_execute)
    monkeypatch.setattr(
        run_skill_pressure,
        "_new_report_run_id",
        lambda *args: f"{args[0]}-{next(run_ids)}" if args else next(run_ids),
    )

    assert run_skill_pressure.main(
        [
            "--repo-root",
            str(repo_root),
            "--matrix",
            str(REPO_ROOT / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json"),
            "--batch-id",
            "batch-1",
            "--model",
            "test-model",
            "--skill",
            "wf-status",
            "--skill",
            "analysis",
            "--write-result",
            "--json",
        ]
    ) == 1

    output = json.loads(capsys.readouterr().out)
    first, second = output["results"]
    assert calls == ["wf-status", "analysis"]
    assert first["verdict"] == Verdict.BLOCKED.value
    assert first["behavioral_delta"] == "blocked"
    assert first["remediation_state"] == "blocked_sensitive_data"
    assert first["persistence"] == {
        "status": "REDACTED",
        "run_id": "batch-1-run-sensitive",
        "report_written": True,
        "diagnostic": "sensitive_data_redacted",
    }
    assert second["verdict"] == Verdict.PASS.value

    redacted_path = pressure_report_path(repo_root, "batch-1-run-sensitive")
    redacted = json.loads(redacted_path.read_text())
    assert set(redacted) == {
        "schema",
        "recorded_at",
        "run_id",
        "persistence_status",
        "diagnostics",
        "field_identity",
    }
    assert "payload" not in redacted
    assert "transcripts" not in redacted
    cells = pressure.build_evidence_matrix(
        MATRIX,
        deterministic_pass=True,
        install_pass=True,
        discovery=passing_discovery_records(MATRIX),
        field=[redacted],
    )
    blocked = next(
        cell
        for cell in cells
        if cell.skill == "wf-status" and cell.category == "without_skill_baseline"
    )
    assert blocked.verdict is Verdict.BLOCKED


def test_runtime_discovery_requires_all_three_runtime_passes() -> None:
    discovery = passing_discovery_records(MATRIX)
    next(
        record
        for record in discovery
        if record["skill"] == "wf-status" and record["runtime"] == "agent-skills"
    )["verdict"] = Verdict.NOT_APPLICABLE.value
    cells = pressure.build_evidence_matrix(
        MATRIX,
        deterministic_pass=True,
        install_pass=True,
        discovery=discovery,
        field=passing_field_records(MATRIX),
    )
    runtime = next(
        cell
        for cell in cells
        if cell.skill == "wf-status" and cell.category == "runtime_discovery"
    )
    assert runtime.verdict is Verdict.FAIL


def test_combined_pressure_prefers_fail_over_redacted_blocked_repetition() -> None:
    field = passing_field_records(MATRIX)
    for index, record in enumerate(field):
        if record["skill"] != "wf-orchestrator":
            continue
        if record["repetition"] == 1:
            field[index] = {
                "schema": pressure.REPORT_SCHEMA,
                "run_id": "batch-1-run-redacted",
                "persistence_status": "BLOCKED",
                "field_identity": {
                    key: record[key]
                    for key in (
                        "batch_id",
                        "matrix_schema",
                        "skill",
                        "scenario_id",
                        "repetition",
                        "provider",
                        "severity",
                        "prompt_sha256",
                        "skill_sha256",
                    )
                },
            }
        elif record["repetition"] == 2:
            record["verdict"] = Verdict.FAIL.value
    cells = pressure.build_evidence_matrix(
        MATRIX,
        deterministic_pass=True,
        install_pass=True,
        discovery=passing_discovery_records(MATRIX),
        field=field,
    )
    combined = next(
        cell
        for cell in cells
        if cell.skill == "wf-orchestrator" and cell.category == "combined_pressure"
    )
    assert combined.verdict is Verdict.FAIL


def test_source_bundle_uses_current_matrix_to_reject_unknown_field_identity(
    tmp_path: Path,
) -> None:
    deterministic = passing_deterministic_report(tmp_path, batch_id="batch-1")
    install = passing_install_report(tmp_path, MATRIX, batch_id="batch-1")
    discovery = passing_discovery_report(tmp_path, MATRIX, batch_id="batch-1")
    fields = passing_field_report_paths(tmp_path, MATRIX, batch_id="batch-1")
    first = json.loads(fields[0].read_text())
    first["payload"]["skill"] = "unknown-skill"
    fields[0].write_text(json.dumps(first))
    with pytest.raises(pressure.EvidenceError, match="current matrix"):
        pressure.validate_source_bundle(
            repo_root=tmp_path,
            batch_id="batch-1",
            deterministic_path=deterministic,
            install_path=install,
            discovery_path=discovery,
            field_paths=fields,
        )


def test_evidence_builder_rejects_source_mutation_after_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    batch_id = "batch-1"
    deterministic = passing_deterministic_report(tmp_path, batch_id=batch_id)
    install = passing_install_report(tmp_path, MATRIX, batch_id=batch_id)
    discovery = passing_discovery_report(tmp_path, MATRIX, batch_id=batch_id)
    fields = passing_field_report_paths(tmp_path, MATRIX, batch_id=batch_id)
    original_validate = build_skill_evidence.validate_source_bundle

    def validate_then_mutate(**kwargs: object) -> Mapping[str, object]:
        sources = original_validate(**kwargs)  # type: ignore[arg-type]
        payload = json.loads(discovery.read_text())
        payload["records"][0]["verdict"] = Verdict.FAIL.value
        discovery.write_text(json.dumps(payload))
        return sources

    monkeypatch.setattr(
        build_skill_evidence, "validate_source_bundle", validate_then_mutate
    )
    assert (
        build_skill_evidence.main(
            ["--batch-id", batch_id, "--repo-root", str(tmp_path)]
        )
        == 1
    )
    assert "source hash mismatch" in capsys.readouterr().err
    assert deterministic.exists()
    assert install.exists()
    assert len(fields) == 27


def test_deterministic_writer_rejects_noncanonical_audit_inputs(tmp_path: Path) -> None:
    with pytest.raises(pressure.EvidenceError, match="canonical deterministic argv"):
        pressure.write_deterministic_report(
            tmp_path,
            batch_id="batch-2",
            argv=["pytest"],
            started_at="2026-07-30T00:00:00+00:00",
            finished_at="2026-07-30T00:00:01+00:00",
            elapsed_sec=1.0,
            exit_status=0,
            stdout="",
            stderr="",
            matrix_sha256=_matrix_sha256(),
            sources={"cli/tests/test_skill_pressure_harness.py": "c" * 64},
        )


def test_skill_pressure_paths_reject_symlinked_operations_root(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (tmp_path / ".awf-operations").symlink_to(external, target_is_directory=True)
    with pytest.raises(pressure.EvidenceError, match="symlink"):
        pressure.deterministic_report_path(tmp_path, "batch-1")


def test_source_bundle_rejects_forged_complete_field_envelope(tmp_path: Path) -> None:
    deterministic = passing_deterministic_report(tmp_path, batch_id="batch-1")
    install = passing_install_report(tmp_path, MATRIX, batch_id="batch-1")
    discovery = passing_discovery_report(tmp_path, MATRIX, batch_id="batch-1")
    fields = passing_field_report_paths(tmp_path, MATRIX, batch_id="batch-1")
    forged = json.loads(fields[0].read_text())
    forged["forged"] = True
    fields[0].write_text(json.dumps(forged))
    with pytest.raises(pressure.EvidenceError, match="field COMPLETE envelope"):
        pressure.validate_source_bundle(
            repo_root=tmp_path,
            batch_id="batch-1",
            deterministic_path=deterministic,
            install_path=install,
            discovery_path=discovery,
            field_paths=fields,
        )


def test_source_bundle_binds_field_prompt_hash_to_current_matrix(tmp_path: Path) -> None:
    deterministic = passing_deterministic_report(tmp_path, batch_id="batch-1")
    install = passing_install_report(tmp_path, MATRIX, batch_id="batch-1")
    discovery = passing_discovery_report(tmp_path, MATRIX, batch_id="batch-1")
    fields = passing_field_report_paths(tmp_path, MATRIX, batch_id="batch-1")
    forged = json.loads(fields[0].read_text())
    forged["payload"]["prompt_sha256"] = "0" * 64
    fields[0].write_text(json.dumps(forged))
    with pytest.raises(pressure.EvidenceError, match="field prompt hash mismatch"):
        pressure.validate_source_bundle(
            repo_root=tmp_path,
            batch_id="batch-1",
            deterministic_path=deterministic,
            install_path=install,
            discovery_path=discovery,
            field_paths=fields,
        )


def test_evidence_builder_rechecks_validated_source_hashes_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    batch_id = "batch-1"
    passing_deterministic_report(tmp_path, batch_id=batch_id)
    passing_install_report(tmp_path, MATRIX, batch_id=batch_id)
    discovery = passing_discovery_report(tmp_path, MATRIX, batch_id=batch_id)
    passing_field_report_paths(tmp_path, MATRIX, batch_id=batch_id)
    passing_cells = tuple(
        pressure.EvidenceCell(
            skill=skill,
            category=category,
            layer=pressure.EVIDENCE_LAYERS[category],
            verdict=Verdict.PASS,
            evidence="fake deterministic evidence",
        )
        for skill in MATRIX.skills
        for category in pressure.REQUIRED_CATEGORIES
    )

    def build_then_mutate(*args: object, **kwargs: object) -> tuple[pressure.EvidenceCell, ...]:
        payload = json.loads(discovery.read_text())
        payload["records"][0]["diagnostic"] = "mutated after validation"
        discovery.write_text(json.dumps(payload))
        return passing_cells

    monkeypatch.setattr(build_skill_evidence, "build_evidence_matrix", build_then_mutate)
    assert (
        build_skill_evidence.main(
            ["--batch-id", batch_id, "--repo-root", str(tmp_path)]
        )
        == 1
    )
    assert "source hash mismatch" in capsys.readouterr().err


def _copy_discovery_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    matrix_path = repo_root / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json"
    matrix_path.parent.mkdir(parents=True)
    shutil.copy2(
        REPO_ROOT / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json",
        matrix_path,
    )
    for skill in MATRIX.skills:
        shutil.copytree(
            REPO_ROOT / "claude" / "skills" / skill,
            repo_root / "claude" / "skills" / skill,
        )
        skill_file = repo_root / "claude" / "skills" / skill / "SKILL.md"
        lines = skill_file.read_text(encoding="utf-8").splitlines()
        closing = lines.index("---", 1)
        if not any(line.startswith("# ") for line in lines[closing + 1 :]):
            lines.insert(closing + 1, f"# /{skill}")
            skill_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    installer = repo_root / "scripts" / "install-skill-links.sh"
    installer.parent.mkdir(parents=True)
    installer.write_text("#!/bin/sh\n", encoding="utf-8")
    return repo_root


class _FakeDiscoveryProcess:
    def __init__(
        self,
        expected: dict[str, object],
        *,
        failure: tuple[str, str, str] | None = None,
        fail_install: bool = False,
        blocked_install_runtime: str | None = None,
    ) -> None:
        self.expected = expected
        self.failure = failure
        self.fail_install = fail_install
        self.blocked_install_runtime = blocked_install_runtime
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: int,
    ) -> DiscoveryProcessResult:
        self.calls.append((argv, cwd, dict(env)))
        if argv[0] == "sh":
            source = Path(argv[2])
            blocked = False
            if self.fail_install and source.name == sorted(self.expected)[0]:
                return DiscoveryProcessResult(1, "", "linker_failed", 0.01)
            for root in map(Path, argv[3:]):
                root.mkdir(parents=True, exist_ok=True)
                target = root / source.name
                if self.blocked_install_runtime and root.as_posix().endswith(
                    run_skill_discovery.TARGET_ROOTS[self.blocked_install_runtime]
                ):
                    target.mkdir()
                    blocked = True
                else:
                    target.symlink_to(source, target_is_directory=True)
            return DiscoveryProcessResult(3 if blocked else 0, "", "root_blocked" if blocked else "", 0.01)

        runtime = {
            "fake-claude": "claude",
            "fake-agent": "agent-skills",
            "fake-omp": "omp",
        }[argv[0]]
        if argv[1:] == ["--version"]:
            if self.failure == (runtime, "preflight", "missing"):
                raise OSError("binary unavailable")
            if self.failure == (runtime, "preflight", "timeout"):
                raise subprocess.TimeoutExpired(argv, 1)
            return DiscoveryProcessResult(0, f"{runtime} fake 1.0", "", 0.01)
        if runtime == "agent-skills" and argv[1:] == ["--help"]:
            output = "" if self.failure == (runtime, "preflight", "missing-exec") else "exec"
            return DiscoveryProcessResult(0, output, "", 0.01)
        if runtime == "agent-skills" and argv[1:] == ["exec", "--help"]:
            flags = [flag for flag in required_flags(runtime) if flag != "exec"]
            if self.failure == (runtime, "preflight", "missing-exec-flag"):
                flags.pop()
            return DiscoveryProcessResult(0, " ".join(flags), "", 0.01)
        if argv[1:] == ["--help"]:
            flags = list(required_flags(runtime))
            if self.failure == (runtime, "preflight", "missing-flag"):
                flags.pop()
            if self.failure == (runtime, "preflight", "substring-flag"):
                flags = ["--model-name" if flag == "--model" else flag for flag in flags]
            return DiscoveryProcessResult(0, " ".join(flags), "", 0.01)

        skill = _discovery_skill_from_argv(runtime, argv)
        failure = self.failure if self.failure and self.failure[:2] == (runtime, skill) else None
        if failure and failure[2] == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if failure and failure[2] == "auth-stdout":
            return DiscoveryProcessResult(23, "authentication required", "", 0.01)
        if failure and failure[2] == "nonzero":
            return DiscoveryProcessResult(23, "", "host_failed", 0.01)
        if failure and failure[2] == "subscription-expired":
            return DiscoveryProcessResult(23, "", "refresh token expired", 0.01)
        if failure and failure[2] == "model-unsupported":
            return DiscoveryProcessResult(23, "", "model is not supported", 0.01)
        if failure and failure[2] == "malformed":
            return DiscoveryProcessResult(0, "{", "", 0.01)
        if failure and failure[2] == "unknown":
            return DiscoveryProcessResult(0, "unknown Skill requested", "", 0.01)
        expected = self.expected[skill]
        payload = {
            "name": expected.name,
            "description": expected.description,
            "body_heading": expected.body_heading,
        }
        if failure and failure[2].startswith("mismatch-"):
            field = failure[2].removeprefix("mismatch-")
            payload[field] = f"not-the-source-{field}"
        return DiscoveryProcessResult(0, json.dumps(payload), "", 0.01)


def _discovery_skill_from_argv(runtime: str, argv: list[str]) -> str:
    if runtime == "claude":
        return argv[-1].split("\n", 1)[0].removeprefix("/")
    if runtime == "agent-skills":
        return argv[-1].split("\n", 1)[0].removeprefix("$")
    return next(flag.removeprefix("--skills=") for flag in argv if flag.startswith("--skills="))


def _run_fake_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    failure: tuple[str, str, str] | None = None,
    fail_install: bool = False,
    blocked_install_runtime: str | None = None,
    missing_skill: str | None = None,
) -> tuple[object, _FakeDiscoveryProcess, Path]:
    repo_root = _copy_discovery_repo(tmp_path)
    if missing_skill:
        (repo_root / "claude" / "skills" / missing_skill / "SKILL.md").unlink()
    expected = load_expected_skills(
        repo_root,
        repo_root / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json",
    )
    fake = _FakeDiscoveryProcess(
        expected,
        failure=failure,
        fail_install=fail_install,
        blocked_install_runtime=blocked_install_runtime,
    )
    global_roots = tmp_path / "workstation-global-skill-roots"
    monkeypatch.setenv("HOME", str(global_roots / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(global_roots / "claude"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(global_roots / "omp"))
    monkeypatch.setenv("CODEX_HOME", str(global_roots / "codex"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-be-stripped")
    monkeypatch.setenv("OPENAI_API_KEY", "must-be-stripped")
    monkeypatch.setenv("AWF_DISCOVERY_RETAINED", "retained")
    result = run_discovery(
        repo_root=repo_root,
        matrix_path=repo_root / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json",
        batch_id="skill-discovery-test",
        binaries={
            "claude": "fake-claude",
            "agent-skills": "fake-agent",
            "omp": "fake-omp",
        },
        models=dict(PINNED_SUBSCRIPTION_MODELS),
        timeout_sec=7,
        write_result=True,
        process_runner=fake,
    )
    return result, fake, global_roots


def test_skill_discovery_preflight_reads_version_and_help_then_blocks_missing_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, fake, _ = _run_fake_discovery(
        tmp_path, monkeypatch, failure=("claude", "preflight", "missing-flag")
    )

    claude_calls = [argv for argv, _, _ in fake.calls if argv[0] == "fake-claude"]
    assert claude_calls[:2] == [["fake-claude", "--version"], ["fake-claude", "--help"]]
    assert {
        record["verdict"]
        for record in result.discovery_records
        if record["runtime"] == "claude"
    } == {"BLOCKED"}


def test_skill_discovery_preflight_requires_exact_long_option_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, _ = _run_fake_discovery(
        tmp_path, monkeypatch, failure=("claude", "preflight", "substring-flag")
    )

    assert {
        record["verdict"]
        for record in result.discovery_records
        if record["runtime"] == "claude"
    } == {"BLOCKED"}


def test_agent_skills_preflight_reads_top_level_and_exec_scoped_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, fake, _ = _run_fake_discovery(
        tmp_path, monkeypatch, failure=("agent-skills", "preflight", "missing-exec-flag")
    )

    agent_calls = [argv for argv, _, _ in fake.calls if argv[0] == "fake-agent"]
    assert agent_calls[:3] == [
        ["fake-agent", "--version"],
        ["fake-agent", "--help"],
        ["fake-agent", "exec", "--help"],
    ]
    assert {
        record["verdict"]
        for record in result.discovery_records
        if record["runtime"] == "agent-skills"
    } == {"BLOCKED"}


def test_agent_skills_preflight_blocks_when_top_level_exec_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, fake, _ = _run_fake_discovery(
        tmp_path, monkeypatch, failure=("agent-skills", "preflight", "missing-exec")
    )

    assert [argv for argv, _, _ in fake.calls if argv[0] == "fake-agent"][:2] == [
        ["fake-agent", "--version"],
        ["fake-agent", "--help"],
    ]
    assert {
        record["verdict"]
        for record in result.discovery_records
        if record["runtime"] == "agent-skills"
    } == {"BLOCKED"}


def test_skill_discovery_uses_exact_safe_host_argv() -> None:
    prompt = "describe the skill"
    assert claude_argv("claude", "sonnet", "analysis", prompt) == [
        "claude",
        "-p",
        "--output-format",
        "text",
        "--tools",
        "",
        "--no-session-persistence",
        "--setting-sources",
        "project",
        "--model",
        "sonnet",
        "/analysis\ndescribe the skill",
    ]
    assert agent_skills_argv("codex", "gpt-5.4", "analysis", prompt) == [
        "codex",
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--model",
        "gpt-5.4",
        "$analysis\ndescribe the skill",
    ]
    assert omp_argv("omp", "openai-codex/gpt-5.6-sol", "analysis", prompt) == [
        "omp",
        "-p",
        "--mode=text",
        "--tools=read",
        "--no-session",
        "--no-extensions",
        "--model=openai-codex/gpt-5.6-sol",
        "--skills=analysis",
        prompt,
    ]
    assert run_skill_discovery._prompt("omp") == (
        "Use only the read tool to load the selected Skill. Do not read any other path or mutate anything. "
        'Return exactly one JSON object and no prose. Its schema is {"name":"exact Skill name",'
        '"description":"exact frontmatter description","body_heading":"exact first Markdown H1"}.'
    )


def test_skill_discovery_returns_exact_source_fields_for_every_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, _ = _run_fake_discovery(tmp_path, monkeypatch)
    expected = load_expected_skills(
        result.repo_root,
        result.repo_root / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json",
    )

    assert all(record["verdict"] == "PASS" for record in result.discovery_records)
    assert len(expected) == 15
    for record in result.discovery_records:
        source = expected[record["skill"]]
        assert record["source_name"] == source.name
        assert record["source_description"] == source.description
        assert record["source_body_heading"] == source.body_heading


def test_skill_discovery_all_emits_exactly_45_unique_runtime_skill_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, _ = _run_fake_discovery(tmp_path, monkeypatch)

    identities = {(record["runtime"], record["skill"]) for record in result.discovery_records}
    assert len(result.discovery_records) == 45
    assert identities == {
        (runtime, skill) for runtime in ("claude", "agent-skills", "omp") for skill in MATRIX.skills
    }


def test_skill_discovery_uses_the_first_h1_after_frontmatter_not_a_leading_h2(
    tmp_path: Path,
) -> None:
    repo_root = _copy_discovery_repo(tmp_path)
    source = repo_root / "claude" / "skills" / "analysis" / "SKILL.md"
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "# /analysis — 소스코드 분석 파이프라인",
            "## Ignore this H2\n\n# /analysis — 소스코드 분석 파이프라인",
            1,
        ),
        encoding="utf-8",
    )

    expected = load_expected_skills(
        repo_root,
        repo_root / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json",
    )

    assert expected["analysis"].body_heading == "# /analysis — 소스코드 분석 파이프라인"


def test_skill_discovery_parses_wf_discovery_literal_description_exactly(
    tmp_path: Path,
) -> None:
    repo_root = _copy_discovery_repo(tmp_path)
    expected = load_expected_skills(
        repo_root,
        repo_root / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json",
    )

    assert expected["wf-discovery"].description == (
        "워크플로우 프로젝트 디스커버리. 기능 설명을 분석하여 관련 프로젝트를 식별하고 추천.\n"
        "TRIGGER when: /wf-discovery 실행 시,\n"
        "             사용자가 어느 프로젝트에서 작업해야 할지 질문할 때,\n"
        "             기능 요구사항이 여러 레포에 걸칠 수 있을 때.\n"
        "DO NOT TRIGGER when: 이미 프로젝트가 결정된 상태,\n"
        "                    wf pipeline 진행 중,\n"
        "                    단순 코드 질문.\n"
    )


@pytest.mark.parametrize(
    ("failure", "runtime", "expected_verdict", "expected_diagnostic"),
    [
        (("claude", "preflight", "missing"), "claude", "BLOCKED", "host_provider_exit"),
        (("agent-skills", "preflight", "timeout"), "agent-skills", "BLOCKED", "host_timeout"),
        (("omp", "analysis", "timeout"), "omp", "BLOCKED", "host_timeout"),
        (("omp", "analysis", "nonzero"), "omp", "BLOCKED", "host_provider_exit"),
        (
            ("agent-skills", "analysis", "subscription-expired"),
            "agent-skills",
            "BLOCKED",
            "host_subscription_expired",
        ),
        (("omp", "analysis", "auth-stdout"), "omp", "BLOCKED", "host_auth_unavailable"),
        (
            ("claude", "analysis", "model-unsupported"),
            "claude",
            "FAIL",
            "host_model_unsupported",
        ),
        (("claude", "analysis", "malformed"), "claude", "FAIL", "malformed_json_response"),
        (
            ("agent-skills", "analysis", "unknown"),
            "agent-skills",
            "FAIL",
            "unknown_skill_response",
        ),
        (("omp", "analysis", "mismatch-name"), "omp", "FAIL", "source_metadata_mismatch"),
        (
            ("omp", "analysis", "mismatch-description"),
            "omp",
            "FAIL",
            "source_metadata_mismatch",
        ),
        (
            ("omp", "analysis", "mismatch-body_heading"),
            "omp",
            "FAIL",
            "source_metadata_mismatch",
        ),
    ],
)
def test_skill_discovery_retains_a_nonpass_record_for_every_fake_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: tuple[str, str, str],
    runtime: str,
    expected_verdict: str,
    expected_diagnostic: str,
) -> None:
    result, _, _ = _run_fake_discovery(tmp_path, monkeypatch, failure=failure)

    assert len(result.discovery_records) == 45
    records = [
        record
        for record in result.discovery_records
        if record["runtime"] == runtime and record["verdict"] == expected_verdict
    ]
    assert records
    assert {record["diagnostic"] for record in records} == {expected_diagnostic}


def test_skill_discovery_rejects_extra_response_prose_as_schema_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, fake, _ = _run_fake_discovery(tmp_path, monkeypatch)
    original = fake.__call__

    def with_extra_prose(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> DiscoveryProcessResult:
        output = original(argv, cwd=cwd, env=env, timeout=timeout)
        if argv[0] == "fake-claude" and len(argv) > 2:
            return DiscoveryProcessResult(output.returncode, f"Sure: {output.stdout}", output.stderr, output.elapsed_sec)
        return output

    result = run_discovery(
        repo_root=result.repo_root,
        matrix_path=result.repo_root / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json",
        batch_id="skill-discovery-extra-prose",
        binaries={"claude": "fake-claude", "agent-skills": "fake-agent", "omp": "fake-omp"},
        models=dict(PINNED_SUBSCRIPTION_MODELS),
        timeout_sec=7,
        write_result=True,
        process_runner=with_extra_prose,
    )
    assert {
        record["verdict"]
        for record in result.discovery_records
        if record["runtime"] == "claude"
    } == {"FAIL"}


def test_skill_discovery_blocks_auth_failure_reported_on_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, _ = _run_fake_discovery(
        tmp_path, monkeypatch, failure=("omp", "analysis", "auth-stdout")
    )

    assert [
        record["verdict"]
        for record in result.discovery_records
        if record["runtime"] == "omp" and record["skill"] == "analysis"
    ] == ["BLOCKED"]


def test_skill_discovery_materializes_45_isolated_canonical_links_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, fake, _ = _run_fake_discovery(tmp_path, monkeypatch)
    install = json.loads(result.install_report.read_text())

    assert install["schema"] == "awf_skill_install_report_v1"
    assert len(install["records"]) == 45
    assert {record["target_root"] for record in install["records"]} == {
        ".claude/skills",
        ".agents/skills",
        ".omp/skills",
    }
    install_calls = [(argv, cwd, env) for argv, cwd, env in fake.calls if argv[0] == "sh"]
    assert len(install_calls) == 15
    assert {cwd for _, cwd, _ in install_calls} == {result.repo_root}
    targets = {Path(target) for argv, _, _ in install_calls for target in argv[3:]}
    workspaces = {target.parents[1] for target in targets}
    assert len(workspaces) == 1
    workspace = next(iter(workspaces))
    assert {
        str(target.relative_to(workspace)).replace("\\", "/") for target in targets
    } == set(run_skill_discovery.TARGET_ROOTS.values())
    temporary_homes = {env["HOME"] for _, _, env in install_calls}
    assert len(temporary_homes) == 1
    assert Path(next(iter(temporary_homes))).name == "home"
    assert not Path(next(iter(temporary_homes))).exists()
    assert workspace.name == "workspace"
    assert not workspace.exists()
    assert {record["status"] for record in install["records"]} == {"PASS"}


def test_skill_discovery_keeps_successful_roots_when_one_root_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, fake, _ = _run_fake_discovery(
        tmp_path, monkeypatch, blocked_install_runtime="agent-skills"
    )

    for runtime in ("claude", "omp"):
        assert {
            record["status"] for record in result.install_records if record["runtime"] == runtime
        } == {"PASS"}
        assert {
            record["verdict"] for record in result.discovery_records if record["runtime"] == runtime
        } == {"PASS"}
    assert {
        record["status"]
        for record in result.install_records
        if record["runtime"] == "agent-skills"
    } == {"BLOCKED"}
    assert {
        record["verdict"]
        for record in result.discovery_records
        if record["runtime"] == "agent-skills"
    } == {"BLOCKED"}
    assert not any(
        argv[0] == "fake-agent"
        and argv[1:] not in (["--version"], ["--help"], ["exec", "--help"])
        for argv, _, _ in fake.calls
    )


def test_skill_discovery_uses_subscription_credentials_without_exposing_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, fake, global_roots = _run_fake_discovery(tmp_path, monkeypatch)

    reports = f"{result.install_report.read_text()}\n{result.discovery_report.read_text()}"
    assert str(global_roots) not in reports
    assert "authentication required" not in reports
    assert all(record["auth_mode"] == "subscription" for record in result.discovery_records)

    host_calls = [
        (argv, cwd, env) for argv, cwd, env in fake.calls if argv[0].startswith("fake-")
    ]
    assert {cwd.name for _, cwd, _ in host_calls} == {"workspace"}
    assert len({cwd for _, cwd, _ in host_calls}) == 1
    for _, _, env in host_calls:
        assert env["AWF_DISCOVERY_RETAINED"] == "retained"
        assert "ANTHROPIC_API_KEY" not in env
        assert "OPENAI_API_KEY" not in env

    claude_envs = [env for argv, _, env in host_calls if argv[0] == "fake-claude"]
    agent_envs = [env for argv, _, env in host_calls if argv[0] == "fake-agent"]
    omp_envs = [env for argv, _, env in host_calls if argv[0] == "fake-omp"]
    assert {env["HOME"] for env in claude_envs} == {str(global_roots / "home")}
    assert {env["CLAUDE_CONFIG_DIR"] for env in claude_envs} == {str(global_roots / "claude")}
    assert all("CODEX_HOME" not in env and "PI_CODING_AGENT_DIR" not in env for env in claude_envs)
    assert {env["CODEX_HOME"] for env in agent_envs} == {str(global_roots / "codex")}
    assert all(env["HOME"] != str(global_roots / "home") for env in agent_envs)
    assert all("CLAUDE_CONFIG_DIR" not in env and "PI_CODING_AGENT_DIR" not in env for env in agent_envs)
    assert {env["PI_CODING_AGENT_DIR"] for env in omp_envs} == {str(global_roots / "omp")}
    assert all(env["HOME"] != str(global_roots / "home") for env in omp_envs)
    assert all("CLAUDE_CONFIG_DIR" not in env and "CODEX_HOME" not in env for env in omp_envs)


def test_skill_discovery_rejects_non_subscription_model_before_host_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _copy_discovery_repo(tmp_path)
    expected = load_expected_skills(
        repo_root,
        repo_root / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json",
    )
    fake = _FakeDiscoveryProcess(expected)
    monkeypatch.setattr(
        run_skill_discovery.SubscriptionAuthContext,
        "capture",
        classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("must not capture"))),
    )

    with pytest.raises(ValueError, match="subscription model mismatch"):
        run_discovery(
            repo_root=repo_root,
            matrix_path=repo_root / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json",
            batch_id="skill-discovery-model-rejection",
            binaries={"claude": "fake-claude", "agent-skills": "fake-agent", "omp": "fake-omp"},
            models={**PINNED_SUBSCRIPTION_MODELS, "claude": "other"},
            timeout_sec=7,
            write_result=False,
            process_runner=fake,
        )

    assert fake.calls == []


def test_skill_discovery_uses_supplied_auth_context_without_recapturing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _copy_discovery_repo(tmp_path)
    expected = load_expected_skills(
        repo_root,
        repo_root / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json",
    )
    fake = _FakeDiscoveryProcess(expected)
    auth = _subscription_auth(tmp_path)
    monkeypatch.setattr(
        run_skill_discovery.SubscriptionAuthContext,
        "capture",
        classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("must not capture"))),
    )

    result = run_discovery(
        repo_root=repo_root,
        matrix_path=repo_root / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json",
        batch_id="skill-discovery-explicit-auth",
        binaries={"claude": "fake-claude", "agent-skills": "fake-agent", "omp": "fake-omp"},
        models=dict(PINNED_SUBSCRIPTION_MODELS),
        timeout_sec=7,
        write_result=False,
        process_runner=fake,
        auth_context=auth,
    )

    assert result.exit_code == 0


def test_skill_discovery_writes_current_batch_reports_after_install_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, _ = _run_fake_discovery(tmp_path, monkeypatch, fail_install=True)

    assert result.exit_code == 1
    assert result.install_report.is_file()
    assert result.discovery_report.is_file()
    assert len(result.install_records) == len(result.discovery_records) == 45
    blocked_skill = sorted(MATRIX.skills)[0]
    assert {
        record["verdict"]
        for record in result.discovery_records
        if record["skill"] == blocked_skill
    } == {"BLOCKED"}


def test_skill_discovery_missing_canonical_skill_file_still_writes_45_blocked_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, _ = _run_fake_discovery(
        tmp_path, monkeypatch, missing_skill="analysis"
    )

    assert result.exit_code == 1
    assert result.install_report.is_file()
    assert result.discovery_report.is_file()
    assert len(result.install_records) == len(result.discovery_records) == 45
    assert {
        record["status"]
        for record in result.install_records
        if record["skill"] == "analysis"
    } == {"BLOCKED"}
    assert {
        record["verdict"]
        for record in result.discovery_records
        if record["skill"] == "analysis"
    } == {"BLOCKED"}

def test_all_canonical_skills_expose_discovery_h1() -> None:
    expected = load_expected_skills(
        REPO_ROOT,
        REPO_ROOT / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json",
    )

    assert sorted(skill for skill, metadata in expected.items() if not metadata.body_heading) == []