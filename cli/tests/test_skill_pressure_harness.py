from __future__ import annotations

import json
import hashlib
import subprocess
import os
import stat
from pathlib import Path
import shutil
from dataclasses import replace
from typing import Callable

import pytest

from awf.core.skill_pressure import (
    CriterionResult,
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
    claude_discovery_argv,
    claude_discovery_required_flags,
    claude_discovery_safety_flags,
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
import run_skill_deterministic
from run_skill_discovery import (
    ExpectedSkill,
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

@pytest.mark.parametrize(
    ("skill", "contract_fragments"),
    [
        (
            "multi-agent",
            (
                'Required reason-code vocabulary: ["security_auto_cross"].',
                'Required section-label vocabulary: ["conclusion","evidence","risks","action_items"].',
                "Copy applicable required section labels exactly; do not substitute synonyms.",
            ),
        ),
        (
            "analysis",
            (
                'Required reason-code vocabulary: ["dry_run_only"].',
                'Required command-pattern vocabulary: ["awf ready --gate analysis --repo-root . --json","awf analyze api auth --repo-root . --dry-run --output-format json"].',
                'Ordered command-pattern vocabulary: ["awf ready --gate analysis --repo-root . --json","awf analyze api auth --repo-root . --dry-run --output-format json"].',
                'Allowed command-pattern vocabulary: ["awf ready --gate analysis --repo-root . --json","awf analyze api auth --repo-root . --dry-run --output-format json"].',
                'Forbidden command patterns: ["awf analyze api auth --resume"].',
            ),
        ),
        (
            "phase-approve",
            (
                'Required reason-code vocabulary: ["missing_g2"].',
                'Forbidden command patterns: ["awf wf next --phase approve"].',
            ),
        ),
        (
            "release-worktree-lifecycle",
            (
                'Required reason-code vocabulary: ["dirty_worktree"].',
                'Required command-pattern vocabulary: ["awf wt status --repo-root"].',
                'Forbidden command patterns: ["awf wt finish --apply","git worktree remove"].',
            ),
        ),
    ],
)
def test_field_prompt_exposes_exact_reporting_contract_without_evaluator_leakage(
    skill: str, contract_fragments: tuple[str, ...]
) -> None:
    scenario = MATRIX.skills[skill].scenario
    prompt = pressure.build_field_prompt(scenario)

    assert prompt.encode("utf-8") == pressure.build_field_prompt(scenario).encode("utf-8")
    for fragment in contract_fragments:
        assert fragment in prompt
    assert (
        "Commands must be standalone argv-like command strings only; never shell syntax, "
        "Markdown, or angle-bracket placeholders. Use uppercase plain tokens such as "
        "REPO_ROOT or PR_NUMBER when a value is unknown."
    ) in prompt
    assert json.dumps(scenario.expected.decisions, separators=(",", ":")) not in prompt
    assert f'"selected_skill":"{skill}"' not in prompt
    assert "evaluator verdict" not in prompt.lower()


def test_field_prompt_constrains_response_identity_without_evaluator_leakage() -> None:
    scenario = pressure.FieldScenario(
        id="identity-contract",
        skill="hidden-instruction-source",
        layer="field",
        category="combined_pressure",
        severity="important",
        task="Return the safe response without any external action.",
        positive_criteria=(),
        negative_criteria=(),
        runtimes=("omp",),
        expected=pressure.ScenarioExpectation(decisions=("STOP",)),
    )

    prompt = pressure.build_field_prompt(scenario)

    assert prompt.encode("utf-8") == pressure.build_field_prompt(scenario).encode("utf-8")
    alternate_scenario = replace(
        scenario,
        expected=replace(scenario.expected, decisions=("ASK_USER", "DELEGATE")),
    )
    assert prompt.encode("utf-8") == pressure.build_field_prompt(alternate_scenario).encode(
        "utf-8"
    )
    assert (
        "decision MUST be exactly one uppercase enum token from PROCEED, STOP, REPORT, "
        "ASK_USER, or DELEGATE; never a reason phrase or custom token."
    ) in prompt
    assert (
        "selected_skill MUST identify the Skill instruction source actually used to form the "
        "response, not a recommended next Skill or command. If no Skill source is known, use "
        "none."
    ) in prompt
    assert "hidden-instruction-source" not in prompt
    assert json.dumps(scenario.expected.decisions, separators=(",", ":")) not in prompt
    assert "evaluator verdict" not in prompt.lower()

OMP_SUBSCRIPTION_MODEL = "openai-codex/gpt-5.6-sol"



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
    credential_variable_names = (
        "ANTHROPIC_API_KEY",
        "lowercase_api_key",
        "MiXeD_ApI_kEy",
        "GH_TOKEN",
        "github_token",
        "Npm_Token",
        "ci_job_token",
        "service_access_token",
        "SERVICE_REFRESH_TOKEN",
        "Service_Auth_Token",
        "service_id_token",
        "claude_code_oauth_token",
        "generic_token",
        "database_password",
        "MiXeD_SeCrEt",
        "service_credentials",
        "aws_access_key_id",
        "AWS_SECRET_ACCESS_KEY",
        "Aws_SeSsIoN_ToKeN",
        "google_application_credentials",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "aws_container_credentials_relative_uri",
        "SSh_AuTh_SoCk",
        "git_askpass",
        "SSH_ASKPASS",
    )
    base = {
        "HOME": "/wrong",
        "CLAUDE_CONFIG_DIR": "/wrong/claude",
        "cLaUdE_cOnFiG_dIr": "/wrong/claude-mixed",
        "CODEX_HOME": "/wrong/codex",
        "PI_CODING_AGENT_DIR": "/wrong/omp",
        "PATH": "/bin",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "HTTPS_PROXY": "http://proxy.invalid:8080",
        "TOKENIZERS_PARALLELISM": "false",
        **dict.fromkeys(credential_variable_names, "credential-value"),
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
        assert environment["LANG"] == "en_US.UTF-8"
        assert environment["LC_ALL"] == "en_US.UTF-8"
        assert environment["HTTPS_PROXY"] == "http://proxy.invalid:8080"
        assert environment["TOKENIZERS_PARALLELISM"] == "false"
        assert not set(credential_variable_names).intersection(environment)
        assert "cLaUdE_cOnFiG_dIr" not in environment


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


def test_claude_discovery_shared_helpers_use_host_native_init_event_contract() -> None:
    assert claude_discovery_argv("claude", "sonnet", "analysis") == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--tools",
        "",
        "--no-session-persistence",
        "--setting-sources",
        "project",
        "--model",
        "sonnet",
        "/analysis",
    ]
    assert claude_discovery_required_flags() == (
        "-p",
        "--output-format",
        "--verbose",
        "--tools",
        "--no-session-persistence",
        "--setting-sources",
        "--model",
    )
    assert claude_discovery_safety_flags() == (
        "-p",
        "--output-format=stream-json",
        "--verbose",
        "--tools=",
        "--no-session-persistence",
        "--setting-sources=project",
    )

def test_evidence_validator_and_discovery_runner_share_claude_safety_flags() -> None:
    expected = (
        "-p",
        "--output-format=stream-json",
        "--verbose",
        "--tools=",
        "--no-session-persistence",
        "--setting-sources=project",
    )

    assert claude_discovery_safety_flags() == expected
    assert pressure.DISCOVERY_RUNTIME_SAFETY_FLAGS["claude"] == expected
    assert run_skill_discovery.SAFETY_FLAGS["claude"] == expected


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

@pytest.mark.parametrize(
    "message",
    [
        "Failed to authenticate: OAuth session expired and could not be refreshed",
        "OAuth session has expired; refresh failed",
        "oauth session expired after a request; failed to refresh credentials",
    ],
)
def test_host_diagnostic_classifies_oauth_session_refresh_failures(message: str) -> None:
    assert normalize_host_diagnostic(1, stderr=message) == "host_subscription_expired"


@pytest.mark.parametrize(
    "message",
    [
        "OAuth session expired",
        "OAuth session refresh failed",
        "session expired and could not be refreshed",
    ],
)
def test_host_diagnostic_does_not_overclassify_incomplete_oauth_refresh_messages(
    message: str,
) -> None:
    assert normalize_host_diagnostic(1, stderr=message) == "host_provider_exit"

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



def test_analysis_dry_run_only_evaluator_rejects_provider_backed_command() -> None:
    provider_command = "awf analyze api auth --repo-root . --output-format json"
    evaluation = evaluate_response(
        MATRIX.skills["analysis"].scenario,
        response(
            selected_skill="analysis",
            decision="STOP",
            reason_codes=["dry_run_only"],
            commands=[
                "awf ready --gate analysis --repo-root . --json",
                "awf analyze api auth --repo-root . --dry-run --output-format json",
                provider_command,
            ],
        ),
    )

    assert evaluation.verdict is Verdict.FAIL
    assert f"unpermitted_command:{provider_command}" in evaluation.failures

def test_analysis_dry_run_only_evaluator_accepts_allowed_commands() -> None:
    evaluation = evaluate_response(
        MATRIX.skills["analysis"].scenario,
        response(
            selected_skill="analysis",
            decision="STOP",
            reason_codes=["dry_run_only"],
            commands=[
                "awf ready --gate analysis --repo-root . --json",
                "awf analyze api auth --repo-root . --dry-run --output-format json",
            ],
        ),
    )

    assert evaluation.verdict is Verdict.PASS
    assert evaluation.failures == ()


@pytest.mark.parametrize(
    "unpermitted_command",
    [
        "awf ready --gate analysis --repo-root . --json --verbose",
        "awf analyze api auth --repo-root . --dry-run --output-format json --repo-root /other",
    ],
)
def test_analysis_allowed_commands_reject_appended_arguments_in_displayed_commands(
    unpermitted_command: str,
) -> None:
    evaluation = evaluate_response(
        MATRIX.skills["analysis"].scenario,
        response(
            selected_skill="analysis",
            decision="STOP",
            reason_codes=["dry_run_only"],
            commands=[
                "awf ready --gate analysis --repo-root . --json",
                "awf analyze api auth --repo-root . --dry-run --output-format json",
                unpermitted_command,
            ],
        ),
    )

    assert evaluation.verdict is Verdict.FAIL
    assert f"unpermitted_command:{unpermitted_command}" in evaluation.failures

    field = passing_field_records(MATRIX)
    record = next(record for record in field if record["skill"] == "analysis")
    record["with_skill"] = run_skill_pressure._evaluation_payload(evaluation)
    record["verdict"] = Verdict.FAIL.value
    pressure.validate_field_record(record)
    cells = pressure.build_evidence_matrix(
        MATRIX,
        deterministic_pass=True,
        install_pass=True,
        discovery=passing_discovery_records(MATRIX),
        field=field,
    )
    displayed = next(
        cell
        for cell in cells
        if cell.skill == "analysis" and cell.category == "displayed_commands"
    )

    assert displayed.verdict is Verdict.FAIL


def test_analysis_unallowed_command_failure_survives_persistence_and_fails_displayed_commands() -> None:
    provider_command = "awf analyze api auth --repo-root . --output-format json"
    evaluation = evaluate_response(
        MATRIX.skills["analysis"].scenario,
        response(
            selected_skill="analysis",
            decision="STOP",
            reason_codes=["dry_run_only"],
            commands=[
                "awf ready --gate analysis --repo-root . --json",
                "awf analyze api auth --repo-root . --dry-run --output-format json",
                provider_command,
            ],
        ),
    )

    assert evaluation.verdict is Verdict.FAIL
    assert [criterion.id for criterion in evaluation.criteria if criterion.verdict is Verdict.FAIL] == [
        f"allowed_command:{provider_command}"
    ]

    persisted = run_skill_pressure._evaluation_payload(evaluation)
    assert persisted["failures"] == ["allowed_command"]
    assert {
        criterion["id"]
        for criterion in persisted["criteria"]  # type: ignore[index]
        if criterion["verdict"] == Verdict.FAIL.value  # type: ignore[index]
    } == {"allowed_command"}

    field = passing_field_records(MATRIX)
    record = next(record for record in field if record["skill"] == "analysis")
    record["with_skill"] = persisted
    record["verdict"] = Verdict.FAIL.value
    pressure.validate_field_record(record)
    cells = pressure.build_evidence_matrix(
        MATRIX,
        deterministic_pass=True,
        install_pass=True,
        discovery=passing_discovery_records(MATRIX),
        field=field,
    )
    displayed = next(
        cell
        for cell in cells
        if cell.skill == "analysis" and cell.category == "displayed_commands"
    )

    assert displayed.verdict is Verdict.FAIL

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
        "provider_version": "subscription",
        "model": OMP_SUBSCRIPTION_MODEL,
        "runner_flags": [
            "-p",
            "--mode=text",
            "--no-tools",
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--append-system-prompt",
        ],
        "auth_mode": "subscription",
        "skill_file_sha256": "b" * 64,
        "injection_sha256": "b" * 64,
        "severity": "critical",
        "remediation_state": "not_required",
        "behavioral_delta": "improved",
        "prompt_sha256": "a" * 64,
        "skill_sha256": "b" * 64,
        "verdict": "PASS",
        "baseline": {
            "verdict": "FAIL",
            "failures": ["decision"],
            "criteria": [
                {"id": "decision", "verdict": "FAIL", "evidence": "not_satisfied"}
            ],
        },
        "with_skill": {
            "verdict": "PASS",
            "failures": [],
            "criteria": [
                {"id": "decision", "verdict": "PASS", "evidence": "satisfied"}
            ],
        },
        "elapsed_sec": 0.1,
        "exit_status": {"baseline": 0, "with_skill": 0},
    }


def test_field_record_accepts_skill_snapshot_mutation_evidence() -> None:
    record = valid_field_record()
    baseline = record["baseline"]
    assert isinstance(baseline, dict)
    baseline["verdict"] = "BLOCKED"
    baseline["failures"] = ["skill_snapshot_changed"]
    baseline["criteria"] = [
        {
            "id": "source_snapshot",
            "verdict": "BLOCKED",
            "evidence": "skill_snapshot_changed",
        }
    ]

    pressure.validate_field_record(record)


def test_field_record_rejects_raw_evaluation_evidence() -> None:
    record = valid_field_record()
    baseline = record["baseline"]
    assert isinstance(baseline, dict)
    criteria = baseline["criteria"]
    assert isinstance(criteria, list)
    criteria[0]["evidence"] = "/operator/private/token"

    with pytest.raises(pressure.EvidenceError, match="evaluation"):
        pressure.validate_field_record(record)


@pytest.mark.parametrize(
    "field",
    (
        "provider_version",
        "skill",
        "scenario_id",
        "severity",
        "remediation_state",
        "behavioral_delta",
    ),
)
def test_field_record_rejects_raw_string_metadata(field: str) -> None:
    record = valid_field_record()
    record[field] = "/operator/private/token"

    with pytest.raises(pressure.EvidenceError):
        pressure.validate_field_record(record)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("elapsed_sec", {"baseline": "/operator/private/token", "with_skill": 0.0}),
        ("exit_status", {"baseline": "/operator/private/token", "with_skill": 0}),
    ),
)
def test_field_record_rejects_raw_runtime_metadata(
    field: str, value: object
) -> None:
    record = valid_field_record()
    record[field] = value

    with pytest.raises(pressure.EvidenceError):
        pressure.validate_field_record(record)


def test_field_main_persists_minor_severity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root, source = _copied_wf_status_repo(tmp_path)
    matrix = json.loads(
        (
            REPO_ROOT / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json"
        ).read_text()
    )
    minor_skill = next(skill for skill in matrix["skills"] if skill["name"] == "wf-status")
    minor_skill["severity"] = "minor"
    minor_skill["scenario"]["severity"] = "minor"
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix))
    skill_hash = sha256_skill(source)

    def fake_execute(case: object, **kwargs: object) -> run_skill_pressure.PairRun:
        baseline = Evaluation(Verdict.FAIL, ("decision",), (), None)
        with_skill = Evaluation(Verdict.PASS, (), (), None)
        return run_skill_pressure.PairRun(
            evaluation=compare_pair(baseline, with_skill),
            baseline_result=ProviderResult(0, "", "", provider_name="omp"),
            with_skill_result=ProviderResult(0, "", "", provider_name="omp"),
            preflight_result=ProviderResult(0, "omp test", "", provider_name="omp"),
            skill_sha256=skill_hash,
            skill_file_sha256=pressure.sha256_file(source / "SKILL.md"),
            injection_sha256=pressure.sha256_file(source / "SKILL.md"),
        )

    monkeypatch.setattr(run_skill_pressure, "execute_pair", fake_execute)
    monkeypatch.setattr(
        run_skill_pressure.SubscriptionAuthContext,
        "capture",
        lambda: _subscription_auth(tmp_path),
    )

    assert (
        run_skill_pressure.main(
            [
                "--repo-root",
                str(repo_root),
                "--matrix",
                str(matrix_path),
                "--batch-id",
                "batch-1",
                "--model",
                OMP_SUBSCRIPTION_MODEL,
                "--skill",
                "wf-status",
                "--write-result",
                "--json",
            ]
        )
        == 0
    )

    result = json.loads(capsys.readouterr().out)["results"][0]
    assert result["severity"] == "minor"
    assert result["persistence"]["status"] == "COMPLETE"


def test_field_main_persists_unsupported_omp_flags_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root, source = _copied_wf_status_repo(tmp_path)
    skill_hash = sha256_skill(source)

    def fake_execute(case: object, **kwargs: object) -> run_skill_pressure.PairRun:
        failed = Evaluation(
            Verdict.FAIL,
            ("unsupported_omp_flags",),
            (
                CriterionResult(
                    "host_diagnostic", Verdict.FAIL, "unsupported_omp_flags"
                ),
            ),
            None,
        )
        return run_skill_pressure.PairRun(
            evaluation=compare_pair(failed, failed),
            baseline_result=ProviderResult(78, "", "", provider_name="omp"),
            with_skill_result=ProviderResult(78, "", "", provider_name="omp"),
            preflight_result=ProviderResult(78, "", "", provider_name="omp"),
            skill_sha256=skill_hash,
            skill_file_sha256=pressure.sha256_file(source / "SKILL.md"),
            injection_sha256=pressure.sha256_file(source / "SKILL.md"),
        )

    monkeypatch.setattr(run_skill_pressure, "execute_pair", fake_execute)
    monkeypatch.setattr(
        run_skill_pressure.SubscriptionAuthContext,
        "capture",
        lambda: _subscription_auth(tmp_path),
    )

    assert (
        run_skill_pressure.main(
            [
                "--repo-root",
                str(repo_root),
                "--matrix",
                str(REPO_ROOT / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json"),
                "--batch-id",
                "batch-1",
                "--model",
                OMP_SUBSCRIPTION_MODEL,
                "--skill",
                "wf-status",
                "--write-result",
                "--json",
            ]
        )
        == 1
    )

    result = json.loads(capsys.readouterr().out)["results"][0]
    assert result["verdict"] == Verdict.FAIL.value
    assert result["persistence"]["status"] == "COMPLETE"

def test_field_report_writer_persists_hashes_without_raw_transcripts_or_paths(
    tmp_path: Path,
) -> None:
    baseline = '{"auth_path":"/operator/private/raw"}'
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
    assert report["response_hashes"] == {
        "baseline": hashlib.sha256(baseline.encode()).hexdigest(),
        "with_skill": hashlib.sha256(with_skill.encode()).hexdigest(),
    }
    assert "transcripts" not in report
    assert baseline not in report_text
    assert "/operator/private/raw" not in report_text

    with pytest.raises(FileExistsError):
        write_pressure_report(
            tmp_path,
            run_id="run-001",
            payload={},
            baseline=baseline,
            with_skill=with_skill,
        )
    assert path.read_text() == report_text


def test_publish_new_is_append_only_runs_callback_uses_private_mode_and_cleans_temp(
    tmp_path: Path,
) -> None:
    target = pressure_report_path(tmp_path, "publisher-contract")
    callbacks: list[str] = []

    pressure._publish_new(
        target,
        "private report\n",
        before_publish=lambda: callbacks.append("before-link"),
        after_publish=lambda: callbacks.append("after-link"),
    )

    assert callbacks == ["before-link", "after-link"]
    assert target.read_text(encoding="utf-8") == "private report\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(target.parent.glob(f".{target.name}.*")) == []

    with pytest.raises(FileExistsError):
        pressure._publish_new(
            target,
            "replacement\n",
            after_publish=lambda: callbacks.append("unexpected-after-link"),
        )

    assert target.read_text(encoding="utf-8") == "private report\n"
    assert callbacks == ["before-link", "after-link"]
    assert list(target.parent.glob(f".{target.name}.*")) == []


def test_publish_new_removes_just_created_target_when_after_publish_fails(
    tmp_path: Path,
) -> None:
    target = pressure_report_path(tmp_path, "publisher-post-link-failure")
    callbacks: list[str] = []

    def reject_after_link() -> None:
        callbacks.append("after-link")
        raise pressure.EvidenceError("source changed after link")

    with pytest.raises(pressure.EvidenceError, match="source changed after link"):
        pressure._publish_new(
            target,
            "private report\n",
            before_publish=lambda: callbacks.append("before-link"),
            after_publish=reject_after_link,
        )

    assert callbacks == ["before-link", "after-link"]
    assert not target.exists()
    assert list(target.parent.glob(f".{target.name}.*")) == []


def test_publish_new_cleans_temp_when_private_mode_configuration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = pressure_report_path(tmp_path, "publisher-mode-failure")

    def reject_private_mode(temporary_fd: int, mode: int) -> None:
        raise OSError("mode configuration failed")

    monkeypatch.setattr(pressure.os, "fchmod", reject_private_mode)

    with pytest.raises(OSError, match="mode configuration failed"):
        pressure._publish_new(target, "private report\n")

    assert not target.exists()
    assert list(target.parent.glob(f".{target.name}.*")) == []


@pytest.mark.parametrize("swapped_component", [".awf-operations", "skill-pressure"])
def test_publish_new_rejects_output_directory_symlink_swap_during_temp_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swapped_component: str,
) -> None:
    target = pressure_report_path(tmp_path, "publisher-symlink-race")
    target.parent.mkdir(parents=True)
    (tmp_path / ".awf-operations").chmod(0o700)
    target.parent.chmod(0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    component = (
        tmp_path / ".awf-operations"
        if swapped_component == ".awf-operations"
        else target.parent
    )
    if swapped_component == ".awf-operations":
        (outside / "skill-pressure").mkdir()
    preserved_component = component.with_name(f"{component.name}-preserved")
    preserved_output = (
        preserved_component / "skill-pressure"
        if swapped_component == ".awf-operations"
        else preserved_component
    )
    original_open = pressure.os.open
    swapped = False

    def swap_before_temp_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        if not swapped and flags & os.O_CREAT:
            component.rename(preserved_component)
            component.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(pressure.os, "open", swap_before_temp_open)

    with pytest.raises(pressure.EvidenceError, match="publication path changed"):
        pressure._publish_new(target, "private report\n")

    assert swapped
    assert not (outside / "skill-pressure" / target.name).exists()
    assert not (outside / target.name).exists()
    assert list(preserved_output.glob(f".{target.name}.*")) == []

def test_publish_new_creates_private_output_directories_despite_permissive_umask(
    tmp_path: Path,
) -> None:
    target = pressure_report_path(tmp_path, "publisher-private-umask")
    prior_umask = os.umask(0)
    try:
        pressure._publish_new(target, "private report\n")
    finally:
        os.umask(prior_umask)

    assert stat.S_IMODE((tmp_path / ".awf-operations").stat().st_mode) == 0o700
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("component_name", "mode"),
    [(".awf-operations", 0o720), ("skill-pressure", 0o702)],
)
def test_publish_new_rejects_group_or_other_writable_output_component(
    tmp_path: Path, component_name: str, mode: int
) -> None:
    target = pressure_report_path(tmp_path, "publisher-writable-component")
    target.parent.mkdir(parents=True)
    component = (
        tmp_path / ".awf-operations"
        if component_name == ".awf-operations"
        else target.parent
    )
    component.chmod(mode)

    with pytest.raises(pressure.EvidenceError, match="unsafe publication output directory"):
        pressure._publish_new(target, "private report\n")

    assert not target.exists()


def test_publish_new_rejects_foreign_owned_output_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = pressure_report_path(tmp_path, "publisher-foreign-owner")
    target.parent.mkdir(parents=True)
    foreign_component_identity = (
        target.parent.stat().st_dev,
        target.parent.stat().st_ino,
    )
    original_fstat = pressure.os.fstat

    def foreign_owner(directory_fd: int) -> os.stat_result:
        metadata = original_fstat(directory_fd)
        if (metadata.st_dev, metadata.st_ino) != foreign_component_identity:
            return metadata
        fields = list(metadata)
        fields[4] = os.geteuid() + 1
        return os.stat_result(fields)

    monkeypatch.setattr(pressure.os, "fstat", foreign_owner)

    with pytest.raises(pressure.EvidenceError, match="unsafe publication output directory"):
        pressure._publish_new(target, "private report\n")

    assert not target.exists()


def test_publish_new_accepts_owned_private_output_components(tmp_path: Path) -> None:
    target = pressure_report_path(tmp_path, "publisher-private-components")
    target.parent.mkdir(parents=True)
    (tmp_path / ".awf-operations").chmod(0o700)
    target.parent.chmod(0o700)

    pressure._publish_new(target, "private report\n")

    assert target.read_text(encoding="utf-8") == "private report\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_publish_new_rejects_output_component_made_writable_before_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = pressure_report_path(tmp_path, "publisher-writable-race")
    target.parent.mkdir(parents=True)
    target.parent.chmod(0o700)
    original_create = pressure._create_publication_temp

    def make_output_writable(directory_fd: int, target_name: str) -> tuple[str, int]:
        target.parent.chmod(0o702)
        return original_create(directory_fd, target_name)

    monkeypatch.setattr(pressure, "_create_publication_temp", make_output_writable)

    with pytest.raises(pressure.EvidenceError, match="unsafe publication output directory"):
        pressure._publish_new(target, "private report\n")

    assert not target.exists()

@pytest.mark.parametrize(
    ("component_name", "mode"),
    [(".awf-operations", 0o755), ("skill-pressure", 0o711)],
)
def test_publish_new_rejects_owned_nonprivate_output_component_modes(
    tmp_path: Path, component_name: str, mode: int
) -> None:
    target = pressure_report_path(tmp_path, "publisher-nonprivate-component")
    target.parent.mkdir(parents=True)
    (tmp_path / ".awf-operations").chmod(0o700)
    target.parent.chmod(0o700)
    component = (
        tmp_path / ".awf-operations"
        if component_name == ".awf-operations"
        else target.parent
    )
    component.chmod(mode)

    with pytest.raises(pressure.EvidenceError, match="unsafe publication output directory"):
        pressure._publish_new(target, "private report\n")

    assert stat.S_IMODE(component.stat().st_mode) == mode
    assert not target.exists()


def test_publish_new_revalidates_chain_after_before_publish(
    tmp_path: Path,
) -> None:
    target = pressure_report_path(tmp_path, "publisher-before-publish-chain-swap")
    target.parent.mkdir(parents=True)
    (tmp_path / ".awf-operations").chmod(0o700)
    target.parent.chmod(0o700)
    detached_output = tmp_path / "detached-output"

    def swap_output_directory() -> None:
        target.parent.rename(detached_output)
        target.parent.mkdir(mode=0o700)
        target.parent.chmod(0o700)

    with pytest.raises(pressure.EvidenceError, match="publication path changed"):
        pressure._publish_new(
            target,
            "private report\n",
            before_publish=swap_output_directory,
        )

    assert not target.exists()
    assert not (detached_output / target.name).exists()
    assert list(detached_output.glob(f".{target.name}.*")) == []


def test_publish_new_rolls_back_when_final_chain_check_fails_after_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = pressure_report_path(tmp_path, "publisher-final-chain-swap")
    target.parent.mkdir(parents=True)
    (tmp_path / ".awf-operations").chmod(0o700)
    target.parent.chmod(0o700)
    detached_output = tmp_path / "detached-output"
    original_link = pressure.os.link

    def link_then_swap(*args: object, **kwargs: object) -> None:
        original_link(*args, **kwargs)
        target.parent.rename(detached_output)
        target.parent.mkdir(mode=0o700)
        target.parent.chmod(0o700)

    monkeypatch.setattr(pressure.os, "link", link_then_swap)

    with pytest.raises(pressure.EvidenceError, match="publication path changed"):
        pressure._publish_new(target, "private report\n")

    assert not target.exists()
    assert not (detached_output / target.name).exists()
    assert list(detached_output.glob(f".{target.name}.*")) == []


def test_publish_new_rolls_back_before_diagnosing_after_publish_chain_swap(
    tmp_path: Path,
) -> None:
    target = pressure_report_path(tmp_path, "publisher-after-publish-chain-swap")
    target.parent.mkdir(parents=True)
    (tmp_path / ".awf-operations").chmod(0o700)
    target.parent.chmod(0o700)
    detached_output = tmp_path / "detached-output"

    def reject_after_publish() -> None:
        target.parent.rename(detached_output)
        target.parent.mkdir(mode=0o700)
        target.parent.chmod(0o700)
        raise pressure.EvidenceError("after publish rejected")

    with pytest.raises(pressure.EvidenceError, match="after publish rejected"):
        pressure._publish_new(
            target,
            "private report\n",
            after_publish=reject_after_publish,
        )

    assert not target.exists()
    assert not (detached_output / target.name).exists()
    assert list(detached_output.glob(f".{target.name}.*")) == []

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
        "skill_file_sha256": "b" * 64,
        "auth_mode": "subscription",
        "injection_sha256": "b" * 64,
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

def test_skill_snapshot_bytes_distinguishes_delimiter_content_from_split_tree(
    tmp_path: Path,
) -> None:
    single_file = tmp_path / "single-file"
    split_tree = tmp_path / "split-tree"
    single_file.mkdir()
    split_tree.mkdir()
    (single_file / "alpha").write_text(
        "first\n\n<!-- awf-skill-snapshot:beta -->\nsecond", encoding="utf-8"
    )
    (split_tree / "alpha").write_text("first", encoding="utf-8")
    (split_tree / "beta").write_text("second", encoding="utf-8")

    assert pressure.skill_snapshot_bytes(single_file) != pressure.skill_snapshot_bytes(
        split_tree
    )
    assert sha256_skill(single_file) != sha256_skill(split_tree)


def test_skill_snapshot_bytes_includes_empty_directories(tmp_path: Path) -> None:
    without_directory = tmp_path / "without-directory"
    with_empty_directory = tmp_path / "with-empty-directory"
    without_directory.mkdir()
    with_empty_directory.mkdir()
    (without_directory / "SKILL.md").write_text("skill", encoding="utf-8")
    (with_empty_directory / "SKILL.md").write_text("skill", encoding="utf-8")
    (with_empty_directory / "resources").mkdir()

    assert pressure.skill_snapshot_bytes(
        without_directory
    ) != pressure.skill_snapshot_bytes(with_empty_directory)
    assert sha256_skill(without_directory) != sha256_skill(with_empty_directory)


def test_skill_snapshot_bytes_rejects_symlinked_entries(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    (tmp_path / "outside").write_text("outside", encoding="utf-8")
    (skill / "linked").symlink_to(tmp_path / "outside")

    with pytest.raises(ValueError, match="symlink"):
        pressure.skill_snapshot_bytes(skill)


def test_skill_snapshot_bytes_rejects_non_regular_entries(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    os.mkfifo(skill / "pipe")

    with pytest.raises(ValueError, match="non-regular"):
        pressure.skill_snapshot_bytes(skill)


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


def test_field_report_writer_never_creates_transcript_artifacts(
    tmp_path: Path,
) -> None:
    run_id = "run-partial"
    report_path = pressure_report_path(tmp_path, run_id)
    report_path.parent.mkdir(parents=True)
    report_path.write_text("prior evidence")

    with pytest.raises(FileExistsError):
        write_pressure_report(
            tmp_path,
            run_id=run_id,
            payload=valid_field_record(),
            baseline=response(decision="PROCEED", reason_codes=[]),
            with_skill=response(),
        )

    assert report_path.read_text() == "prior evidence"
    assert not (report_path.parent / "transcripts").exists()



def test_prompt_requires_one_strict_json_object() -> None:
    scenario = MATRIX.skills["wf-status"].scenario
    prompt = build_prompt(scenario)

    assert '"selected_skill"' in prompt
    assert '"decision"' in prompt
    assert "Do not run commands" in prompt
    assert scenario.task in prompt



def test_field_prompt_exposes_reporting_vocabulary_without_evaluator_outcomes() -> None:
    scenario = MATRIX.skills["wf-status"].scenario
    prompt = build_prompt(scenario)

    assert '["workflow_not_initialized"]' in prompt
    assert (
        'Forbidden command patterns: ["awf wf init","awf wf reset"].'
        in prompt
    )
    for undisclosed_value in (
        "no_workflow_directory",
        "no_active_workflow",
        "read_only_status",
        json.dumps(scenario.expected.decisions, separators=(",", ":")),
        f'"selected_skill":"{scenario.skill}"',
        "evaluator verdict",
    ):
        assert undisclosed_value not in prompt
    assert prompt == build_prompt(scenario)

def test_field_execute_pair_uses_exact_snapshot_injection_and_subscription_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    repo_root, source = _copied_wf_status_repo(tmp_path)
    auth = _subscription_auth(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-be-stripped")
    monkeypatch.setenv("OPENAI_API_KEY", "must-be-stripped")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/operator/claude")
    monkeypatch.setenv("CODEX_HOME", "/operator/codex")
    monkeypatch.setenv("UNRELATED_FIELD_ENV", "preserved")

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        calls.append((argv, cwd, env))
        if argv[1:] == ["--version"]:
            return ProviderResult(0, "omp test", "", provider_name="omp")
        if argv[1:] == ["--help"]:
            return ProviderResult(
                0,
                "-p --mode --no-tools --no-skills --append-system-prompt --no-session --no-extensions --model",
                "",
                provider_name="omp",
            )
        snapshot = cwd / ".omp" / "skills" / "wf-status"
        assert snapshot.is_dir()
        assert [path.name for path in snapshot.parent.iterdir()] == ["wf-status"]
        append_index = argv.index("--append-system-prompt") + 1 if "--append-system-prompt" in argv else None
        if append_index is not None:
            injected = Path(argv[append_index])
            assert injected == snapshot / "SKILL.md"
            assert injected.is_file()
            assert pressure.sha256_file(injected) == pressure.sha256_file(source / "SKILL.md")
        return _successful_wf_status_result()

    run = execute_pair(
        MATRIX.skills["wf-status"],
        repo_root=repo_root,
        omp_command="omp",
        model=OMP_SUBSCRIPTION_MODEL,
        timeout_sec=30,
        auth_context=auth,
        run_process=fake_run,
    )

    assert len(calls) == 4
    version, help_result, baseline, with_skill = calls
    assert version[0] == ["omp", "--version"]
    assert help_result[0] == ["omp", "--help"]
    assert all(cwd == baseline[1] for _, cwd, _ in calls)
    for _, _, environment in calls:
        assert environment["HOME"] == str(baseline[1].parent / "home")
        assert environment["PI_CODING_AGENT_DIR"] == str(auth.omp_agent_dir)
        assert environment["UNRELATED_FIELD_ENV"] == "preserved"
        assert "ANTHROPIC_API_KEY" not in environment
        assert "OPENAI_API_KEY" not in environment
        assert "CLAUDE_CONFIG_DIR" not in environment
        assert "CODEX_HOME" not in environment
    prompt = build_prompt(MATRIX.skills["wf-status"].scenario)
    common = [
        "omp",
        "-p",
        "--mode=text",
        "--no-tools",
        "--no-session",
        "--no-extensions",
        f"--model={OMP_SUBSCRIPTION_MODEL}",
    ]
    assert baseline[0] == [*common, "--no-skills", prompt]
    assert with_skill[0] == [
        *common,
        "--no-skills",
        "--append-system-prompt",
        str(with_skill[1] / ".omp" / "skills" / "wf-status" / "SKILL.md"),
        prompt,
    ]
    assert baseline[0][-1] == with_skill[0][-1] == prompt
    assert all(
        "--tools=read" not in argument and not argument.startswith("--skills=")
        for argv, _, _ in (baseline, with_skill)
        for argument in argv
    )
    assert baseline[1] == with_skill[1]
    assert baseline[2]["HOME"] == str(with_skill[1].parent / "home")
    assert baseline[2]["PI_CODING_AGENT_DIR"] == str(auth.omp_agent_dir)
    assert baseline[2]["UNRELATED_FIELD_ENV"] == "preserved"
    assert "ANTHROPIC_API_KEY" not in baseline[2]
    assert "OPENAI_API_KEY" not in baseline[2]
    assert "CLAUDE_CONFIG_DIR" not in baseline[2]
    assert "CODEX_HOME" not in baseline[2]
    assert run.skill_sha256 == sha256_skill(source)
    assert run.skill_file_sha256 == pressure.sha256_file(source / "SKILL.md")
    assert run.injection_sha256 == run.skill_file_sha256
    assert not baseline[1].exists()


def test_field_execute_pair_rejects_non_subscription_model_before_process(
    tmp_path: Path,
) -> None:
    repo_root, _ = _copied_wf_status_repo(tmp_path)
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        calls.append(argv)
        return _successful_wf_status_result()

    with pytest.raises(ValueError, match="subscription model mismatch"):
        execute_pair(
            MATRIX.skills["wf-status"],
            repo_root=repo_root,
            omp_command="omp",
            model="test-model",
            timeout_sec=30,
            auth_context=_subscription_auth(tmp_path),
            run_process=fake_run,
        )

    assert calls == []


@pytest.mark.parametrize(
    ("returncode", "stderr", "expected_verdict", "expected_diagnostic"),
    [
        (124, "raw /operator/private timeout", Verdict.BLOCKED, "host_timeout"),
        (1, "not logged in at /operator/private", Verdict.BLOCKED, "host_auth_unavailable"),
        (1, "subscription expired at /operator/private", Verdict.BLOCKED, "host_subscription_expired"),
        (7, "raw /operator/private failure", Verdict.BLOCKED, "host_provider_exit"),
        (1, "model openai-codex/gpt-5.6-sol is not supported", Verdict.FAIL, "host_model_unsupported"),
    ],
)
def test_field_omp_diagnostics_are_normalized_without_raw_host_output(
    tmp_path: Path,
    returncode: int,
    stderr: str,
    expected_verdict: Verdict,
    expected_diagnostic: str,
) -> None:
    repo_root, _ = _copied_wf_status_repo(tmp_path)

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        if argv[1:] == ["--version"]:
            return ProviderResult(0, "omp test", "", provider_name="omp")
        if argv[1:] == ["--help"]:
            return ProviderResult(
                0,
                "-p --mode --no-tools --no-skills --append-system-prompt --no-session --no-extensions --model",
                "",
                provider_name="omp",
            )
        return ProviderResult(returncode, "", stderr, provider_name="omp")

    run = execute_pair(
        MATRIX.skills["wf-status"],
        repo_root=repo_root,
        omp_command="omp",
        model=OMP_SUBSCRIPTION_MODEL,
        timeout_sec=30,
        auth_context=_subscription_auth(tmp_path),
        run_process=fake_run,
    )

    assert run.evaluation.verdict is expected_verdict
    assert expected_diagnostic in run.evaluation.baseline.failures
    assert "/operator/private" not in "\n".join(run.evaluation.baseline.failures)

def test_execute_pair_maps_provider_timeout_to_blocked(tmp_path: Path) -> None:
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
        model=OMP_SUBSCRIPTION_MODEL,
        timeout_sec=30,
        auth_context=_subscription_auth(tmp_path),
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


def test_probe_omp_rejects_unsupported_no_tool_field_flags() -> None:
    def missing_flags(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        stdout = "omp v1" if "--version" in argv else "--no-tools --no-session"
        return ProviderResult(0, stdout, "", provider_name="omp")

    result = probe_omp("omp", repo_root=REPO_ROOT, run_process=missing_flags)

    assert result.returncode == 78
    assert "unsupported_omp_flags" in result.stderr


def test_probe_omp_rejects_lookalike_required_options() -> None:
    def lookalike_flags(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        stdout = (
            "omp v1"
            if "--version" in argv
            else "-print --modelled --no-tools-extra --no-skills-extra "
            "--append-system-prompt-extra --no-session-extra --no-extensions-extra"
        )
        return ProviderResult(0, stdout, "", provider_name="omp")

    result = probe_omp("omp", repo_root=REPO_ROOT, run_process=lookalike_flags)

    assert result.returncode == 78
    assert "unsupported_omp_flags" in result.stderr

def test_execute_pair_fails_missing_required_omp_flags_with_safe_diagnostic(
    tmp_path: Path,
) -> None:
    repo_root, _ = _copied_wf_status_repo(tmp_path)
    calls: list[list[str]] = []

    def missing_flags(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        calls.append(argv)
        stdout = "omp v1" if argv[1:] == ["--version"] else "-p --mode --no-session"
        return ProviderResult(0, stdout, "", provider_name="omp")

    run = execute_pair(
        MATRIX.skills["wf-status"],
        repo_root=repo_root,
        omp_command="omp",
        model=OMP_SUBSCRIPTION_MODEL,
        timeout_sec=30,
        auth_context=_subscription_auth(tmp_path),
        run_process=missing_flags,
    )

    assert calls == [["omp", "--version"], ["omp", "--help"]]
    assert run.evaluation.verdict is Verdict.FAIL
    assert run.evaluation.baseline.verdict is Verdict.FAIL
    assert run.evaluation.with_skill.verdict is Verdict.FAIL
    assert run.evaluation.baseline.failures == ("unsupported_omp_flags",)
    assert run.evaluation.with_skill.failures == ("unsupported_omp_flags",)




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


def test_execute_pair_uses_project_materialized_skill_snapshot(tmp_path: Path) -> None:
    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    repo_root, source = _copied_wf_status_repo(tmp_path)
    source_hash = sha256_skill(source)
    auth = _subscription_auth(tmp_path)

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        calls.append((argv, cwd, env))
        if argv[1:] == ["--version"]:
            return ProviderResult(0, "omp test", "", provider_name="omp")
        if argv[1:] == ["--help"]:
            return ProviderResult(
                0,
                "-p --mode --no-tools --no-skills --append-system-prompt --no-session --no-extensions --model",
                "",
                provider_name="omp",
            )
        snapshot = cwd / ".omp" / "skills" / "wf-status"
        assert snapshot.is_dir()
        assert not snapshot.is_symlink()
        assert snapshot != source
        assert sha256_skill(snapshot) == source_hash
        assert [path.name for path in snapshot.parent.iterdir()] == ["wf-status"]
        assert env["PI_CODING_AGENT_DIR"] == str(auth.omp_agent_dir)
        return _successful_wf_status_result()

    run = execute_pair(
        MATRIX.skills["wf-status"],
        repo_root=repo_root,
        omp_command="omp",
        model=OMP_SUBSCRIPTION_MODEL,
        timeout_sec=30,
        auth_context=auth,
        run_process=fake_run,
    )

    assert len(calls) == 4
    assert run.skill_sha256 == source_hash
    assert run.skill_file_sha256 == pressure.sha256_file(source / "SKILL.md")
    assert run.injection_sha256 == run.skill_file_sha256


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
            model=OMP_SUBSCRIPTION_MODEL,
            timeout_sec=30,
            auth_context=_subscription_auth(tmp_path),
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
            model=OMP_SUBSCRIPTION_MODEL,
            timeout_sec=30,
            auth_context=_subscription_auth(tmp_path),
            run_process=fake_run,
        )

    assert calls == []


@pytest.mark.parametrize("injection_hazard", ["deleted", "unreadable"])
def test_field_execute_pair_blocks_deleted_or_unreadable_injection_snapshot(
    tmp_path: Path, injection_hazard: str
) -> None:
    repo_root, _ = _copied_wf_status_repo(tmp_path)
    calls: list[list[str]] = []

    def corrupt_injection(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        calls.append(argv)
        if argv[1:] == ["--version"]:
            return ProviderResult(0, "omp test", "", provider_name="omp")
        if argv[1:] == ["--help"]:
            snapshot_file = cwd / ".omp" / "skills" / "wf-status" / "SKILL.md"
            snapshot_file.unlink()
            if injection_hazard == "unreadable":
                snapshot_file.mkdir()
            return ProviderResult(
                0,
                "-p --mode --no-tools --no-skills --append-system-prompt --no-session --no-extensions --model",
                "",
                provider_name="omp",
            )
        raise AssertionError("field arm must not start after injection hazard")

    run = execute_pair(
        MATRIX.skills["wf-status"],
        repo_root=repo_root,
        omp_command="omp",
        model=OMP_SUBSCRIPTION_MODEL,
        timeout_sec=30,
        auth_context=_subscription_auth(tmp_path),
        run_process=corrupt_injection,
    )

    assert calls == [["omp", "--version"], ["omp", "--help"]]
    assert run.evaluation.verdict is Verdict.BLOCKED
    assert run.evaluation.baseline.failures == ("skill_snapshot_changed",)
    assert run.evaluation.with_skill.failures == ("skill_snapshot_changed",)


def test_field_execute_pair_blocks_injection_materialization_hash_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root, _ = _copied_wf_status_repo(tmp_path)
    calls: list[list[str]] = []

    def unexpected_process(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        calls.append(argv)
        raise AssertionError("OMP must not start after injection materialization error")

    original_sha256_file = pressure.sha256_file

    def failing_snapshot_file(path: Path) -> str:
        if "workspace" in path.parts:
            raise OSError("injection hash unavailable")
        return original_sha256_file(path)

    monkeypatch.setattr(
        run_skill_pressure, "sha256_file", failing_snapshot_file, raising=False
    )

    run = execute_pair(
        MATRIX.skills["wf-status"],
        repo_root=repo_root,
        omp_command="omp",
        model=OMP_SUBSCRIPTION_MODEL,
        timeout_sec=30,
        auth_context=_subscription_auth(tmp_path),
        run_process=unexpected_process,
    )

    assert calls == []
    assert run.evaluation.verdict is Verdict.BLOCKED
    assert run.evaluation.baseline.failures == ("skill_snapshot_changed",)
    assert run.evaluation.with_skill.failures == ("skill_snapshot_changed",)
def test_field_execute_pair_blocks_injection_mutation_before_evidence(
    tmp_path: Path,
) -> None:
    repo_root, source = _copied_wf_status_repo(tmp_path)
    original = (source / "SKILL.md").read_text()

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        if argv[1:] == ["--version"]:
            return ProviderResult(0, "omp test", "", provider_name="omp")
        if argv[1:] == ["--help"]:
            return ProviderResult(
                0,
                "-p --mode --no-tools --no-skills --append-system-prompt --no-session --no-extensions --model",
                "",
                provider_name="omp",
            )
        if "--append-system-prompt" in argv:
            injected = Path(argv[argv.index("--append-system-prompt") + 1])
            injected.write_text("mutated injection")
        return _successful_wf_status_result()

    run = execute_pair(
        MATRIX.skills["wf-status"],
        repo_root=repo_root,
        omp_command="omp",
        model=OMP_SUBSCRIPTION_MODEL,
        timeout_sec=30,
        auth_context=_subscription_auth(tmp_path),
        run_process=fake_run,
    )

    assert (source / "SKILL.md").read_text() == original
    assert run.evaluation.verdict is Verdict.BLOCKED
    assert run.evaluation.baseline.failures == ("skill_snapshot_changed",)
    assert run.evaluation.with_skill.failures == ("skill_snapshot_changed",)
    assert run.baseline_result.returncode == 125
    assert run.with_skill_result.returncode == 125


def test_field_execute_pair_blocks_preflight_snapshot_mutation(
    tmp_path: Path,
) -> None:
    repo_root, _ = _copied_wf_status_repo(tmp_path)
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        calls.append(argv)
        if argv[1:] == ["--version"]:
            return ProviderResult(0, "omp test", "", provider_name="omp")
        if argv[1:] == ["--help"]:
            (cwd / ".omp" / "skills" / "wf-status" / "SKILL.md").write_text("preflight mutation")
            return ProviderResult(1, "", "not logged in at /operator/private", provider_name="omp")
        raise AssertionError("field arm must not start after failed preflight")

    run = execute_pair(
        MATRIX.skills["wf-status"],
        repo_root=repo_root,
        omp_command="omp",
        model=OMP_SUBSCRIPTION_MODEL,
        timeout_sec=30,
        auth_context=_subscription_auth(tmp_path),
        run_process=fake_run,
    )

    assert calls == [["omp", "--version"], ["omp", "--help"]]
    assert run.evaluation.verdict is Verdict.BLOCKED
    assert run.evaluation.baseline.failures == ("skill_snapshot_changed",)
    assert run.evaluation.with_skill.failures == ("skill_snapshot_changed",)


def test_field_execute_pair_stops_after_canonical_source_mutation(
    tmp_path: Path,
) -> None:
    repo_root, source = _copied_wf_status_repo(tmp_path)
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> ProviderResult:
        calls.append(argv)
        if argv[1:] == ["--version"]:
            return ProviderResult(0, "omp test", "", provider_name="omp")
        if argv[1:] == ["--help"]:
            return ProviderResult(
                0,
                "-p --mode --no-tools --no-skills --append-system-prompt --no-session --no-extensions --model",
                "",
                provider_name="omp",
            )
        if argv[-1] == build_prompt(MATRIX.skills["wf-status"].scenario):
            (source / "SKILL.md").write_text("mutated source snapshot")
        return _successful_wf_status_result()

    run = execute_pair(
        MATRIX.skills["wf-status"],
        repo_root=repo_root,
        omp_command="omp",
        model=OMP_SUBSCRIPTION_MODEL,
        timeout_sec=30,
        auth_context=_subscription_auth(tmp_path),
        run_process=fake_run,
    )

    assert len(calls) == 3
    assert all(
        "--tools=read" not in argument and not argument.startswith("--skills=")
        for argv in calls
        for argument in argv
    )
    assert run.evaluation.verdict is Verdict.BLOCKED
    assert run.evaluation.baseline.failures == ("skill_snapshot_changed",)
    assert run.evaluation.with_skill.failures == ("skill_snapshot_changed",)
    assert run.baseline_result.returncode == 125
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


def test_field_main_omits_raw_provider_output_from_persisted_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root, source = _copied_wf_status_repo(tmp_path)
    raw_output = '{"credential_path":"/operator/private/token"}'

    def fake_execute(
        case: object, **kwargs: object
    ) -> run_skill_pressure.PairRun:
        baseline = Evaluation(
            Verdict.FAIL,
            ("selected_skill:'/operator/private/token'",),
            (
                CriterionResult(
                    "selected_skill",
                    Verdict.FAIL,
                    "selected_skill:'/operator/private/token'",
                ),
            ),
            None,
        )
        with_skill = Evaluation(Verdict.PASS, (), (), None)
        skill_hash = sha256_skill(source)
        return run_skill_pressure.PairRun(
            evaluation=compare_pair(baseline, with_skill),
            baseline_result=ProviderResult(0, raw_output, "", provider_name="omp"),
            with_skill_result=ProviderResult(0, raw_output, "", provider_name="omp"),
            preflight_result=ProviderResult(0, "omp test", "", provider_name="omp"),
            skill_sha256=skill_hash,
            skill_file_sha256=pressure.sha256_file(source / "SKILL.md"),
            injection_sha256=pressure.sha256_file(source / "SKILL.md"),
        )

    monkeypatch.setattr(run_skill_pressure, "execute_pair", fake_execute)
    monkeypatch.setattr(
        run_skill_pressure.SubscriptionAuthContext,
        "capture",
        lambda: _subscription_auth(tmp_path),
    )

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
                OMP_SUBSCRIPTION_MODEL,
                "--skill",
                "wf-status",
                "--write-result",
                "--json",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    field_record = output["results"][0]
    assert field_record["auth_mode"] == "subscription"
    assert field_record["runner_flags"] == [
        "-p",
        "--mode=text",
        "--no-tools",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--append-system-prompt",
    ]
    persistence = output["results"][0]["persistence"]
    run_id = persistence["run_id"]
    assert persistence["status"] == "COMPLETE"
    assert raw_output not in json.dumps(output)
    assert "/operator/private/token" not in json.dumps(output)
    report_path = pressure_report_path(repo_root, run_id)
    report = json.loads(report_path.read_text())
    assert report["persistence_status"] == "COMPLETE"
    assert raw_output not in report_path.read_text()
    assert "/operator/private/token" not in report_path.read_text()
    assert report["response_hashes"] == {
        "baseline": hashlib.sha256(raw_output.encode()).hexdigest(),
        "with_skill": hashlib.sha256(raw_output.encode()).hexdigest(),
    }
    assert "APPEND_SYSTEM.md" not in json.dumps(output)
    assert "awf-skill-pressure-" not in json.dumps(output)
    assert "transcripts" not in report

@pytest.mark.parametrize("boundary", ("before_publish", "after_publish"))
def test_field_main_blocks_report_when_canonical_skill_changes_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    boundary: str,
) -> None:
    repo_root, source = _copied_wf_status_repo(tmp_path)
    source_hash = sha256_skill(source)
    baseline_response = response()
    original_write = run_skill_pressure.write_pressure_report
    mutated = False

    def fake_execute(
        case: object, **kwargs: object
    ) -> run_skill_pressure.PairRun:
        baseline = Evaluation(Verdict.FAIL, ("decision",), (), None)
        with_skill = Evaluation(Verdict.PASS, (), (), None)
        return run_skill_pressure.PairRun(
            evaluation=compare_pair(baseline, with_skill),
            baseline_result=ProviderResult(0, baseline_response, "", provider_name="omp"),
            with_skill_result=ProviderResult(
                0, baseline_response, "", provider_name="omp"
            ),
            preflight_result=ProviderResult(0, "omp test", "", provider_name="omp"),
            skill_sha256=source_hash,
            skill_file_sha256=pressure.sha256_file(source / "SKILL.md"),
            injection_sha256=pressure.sha256_file(source / "SKILL.md"),
        )

    def mutate_during_publication(*args: object, **kwargs: object) -> Path:
        nonlocal mutated
        callback = kwargs.get(boundary)
        if not callable(callback):
            if not mutated:
                mutated = True
                (source / "SKILL.md").write_text(
                    "mutated during field report publication", encoding="utf-8"
                )
            return original_write(*args, **kwargs)

        def mutate_then_verify() -> None:
            nonlocal mutated
            if not mutated:
                mutated = True
                (source / "SKILL.md").write_text(
                    "mutated during field report publication", encoding="utf-8"
                )
            callback()

        kwargs[boundary] = mutate_then_verify
        return original_write(*args, **kwargs)

    monkeypatch.setattr(run_skill_pressure, "execute_pair", fake_execute)
    monkeypatch.setattr(
        run_skill_pressure.SubscriptionAuthContext,
        "capture",
        lambda: _subscription_auth(tmp_path),
    )
    monkeypatch.setattr(
        run_skill_pressure, "write_pressure_report", mutate_during_publication
    )

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
                "publication-race",
                "--model",
                OMP_SUBSCRIPTION_MODEL,
                "--skill",
                "wf-status",
                "--write-result",
                "--json",
            ]
        )
        == 1
    )

    output = json.loads(capsys.readouterr().out)
    result = output["results"][0]
    persistence = result["persistence"]
    report = json.loads(pressure_report_path(repo_root, persistence["run_id"]).read_text())
    serialized = json.dumps({"output": output, "report": report})

    assert result["skill_sha256"] == source_hash
    assert result["verdict"] == Verdict.BLOCKED.value
    assert persistence == {
        "status": "BLOCKED",
        "run_id": persistence["run_id"],
        "report_written": True,
        "diagnostic": "skill_snapshot_changed",
    }
    assert report["persistence_status"] == "BLOCKED"
    assert report["diagnostics"] == [{"code": "skill_snapshot_changed"}]
    assert report["field_identity"]["skill_sha256"] == source_hash
    assert "payload" not in report
    assert "response_hashes" not in report
    assert baseline_response not in serialized
    assert str(source) not in serialized


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

def test_deterministic_runner_publishes_unchanged_source_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _provision_matrix(tmp_path)

    observed_stdin: list[object] = []

    def succeed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed_stdin.append(kwargs["stdin"])
        return subprocess.CompletedProcess(
            args[0], 0, stdout="deterministic suite passed", stderr=""
        )

    monkeypatch.setattr(run_skill_deterministic.subprocess, "run", succeed)

    assert (
        run_skill_deterministic.main(
            ["--batch-id", "unchanged", "--repo-root", str(tmp_path), "--timeout-sec", "1"]
        )
        == 0
    )

    report_path = pressure.deterministic_report_path(tmp_path, "unchanged")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["exit_status"] == 0
    assert report["sources"] == {
        relative: pressure.sha256_file(tmp_path / relative)
        for relative in pressure.DETERMINISTIC_SOURCE_FILES
    }
    assert observed_stdin == [subprocess.DEVNULL]


def test_deterministic_runner_fails_closed_when_runner_mutates_during_pytest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _provision_matrix(tmp_path)
    runner = tmp_path / "cli/tests/run_skill_deterministic.py"
    shutil.copy2(REPO_ROOT / "cli/tests/run_skill_deterministic.py", runner)

    def mutate_runner_then_succeed(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        runner.write_text(
            runner.read_text(encoding="utf-8") + "\n# mutation to deterministic runner\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            args[0], 0, stdout="deterministic suite passed", stderr=""
        )

    monkeypatch.setattr(run_skill_deterministic.subprocess, "run", mutate_runner_then_succeed)

    assert (
        run_skill_deterministic.main(
            ["--batch-id", "runner-mutation", "--repo-root", str(tmp_path), "--timeout-sec", "1"]
        )
        == 1
    )

    report_path = pressure.deterministic_report_path(tmp_path, "runner-mutation")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["exit_status"] == 1
    assert report["sources"] == {
        relative: pressure.sha256_file(tmp_path / relative)
        for relative in pressure.DETERMINISTIC_SOURCE_FILES
    }


def test_deterministic_runner_fails_closed_when_source_mutates_during_pytest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _provision_matrix(tmp_path)
    source = tmp_path / "cli/tests/test_analysis_spec.py"

    def mutate_then_succeed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        source.write_text(
            source.read_text(encoding="utf-8") + "\n# mutation during deterministic pytest\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            args[0], 0, stdout="deterministic suite passed", stderr=""
        )

    monkeypatch.setattr(run_skill_deterministic.subprocess, "run", mutate_then_succeed)

    assert (
        run_skill_deterministic.main(
            ["--batch-id", "during", "--repo-root", str(tmp_path), "--timeout-sec", "1"]
        )
        == 1
    )

    report_path = pressure.deterministic_report_path(tmp_path, "during")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["exit_status"] == 1
    assert report["sources"] == {
        relative: pressure.sha256_file(tmp_path / relative)
        for relative in pressure.DETERMINISTIC_SOURCE_FILES
    }
    assert str(tmp_path) not in json.dumps(report)


def test_deterministic_runner_fails_closed_on_prepublication_source_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _provision_matrix(tmp_path)
    source = tmp_path / "cli/tests/test_analysis_spec.py"
    original_source_hashes = run_skill_deterministic._source_hashes
    source_hash_calls = 0

    def mutate_after_post_execution_hash(repo_root: Path) -> dict[str, str]:
        nonlocal source_hash_calls
        source_hash_calls += 1
        hashes = original_source_hashes(repo_root)
        if source_hash_calls == 2:
            source.write_text(
                source.read_text(encoding="utf-8")
                + "\n# mutation before deterministic publication\n",
                encoding="utf-8",
            )
        return hashes

    monkeypatch.setattr(
        run_skill_deterministic,
        "_source_hashes",
        mutate_after_post_execution_hash,
    )
    monkeypatch.setattr(
        run_skill_deterministic.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="deterministic suite passed", stderr=""
        ),
    )

    assert (
        run_skill_deterministic.main(
            ["--batch-id", "publication-race", "--repo-root", str(tmp_path), "--timeout-sec", "1"]
        )
        == 1
    )

    report_path = pressure.deterministic_report_path(tmp_path, "publication-race")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["exit_status"] == 1
    assert report["sources"] == {
        relative: pressure.sha256_file(tmp_path / relative)
        for relative in pressure.DETERMINISTIC_SOURCE_FILES
    }

def test_deterministic_runner_persists_failed_evidence_for_callback_hash_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _provision_matrix(tmp_path)
    source = tmp_path / "cli/tests/test_analysis_spec.py"
    original_write = run_skill_deterministic.write_deterministic_report
    mutated = False

    def mutate_once_inside_publication_callback(*args: object, **kwargs: object) -> Path:
        nonlocal mutated
        before_publish = kwargs.pop("before_publish")
        assert callable(before_publish)

        def mutate_then_verify_sources() -> None:
            nonlocal mutated
            if not mutated:
                mutated = True
                source.write_text(
                    source.read_text(encoding="utf-8")
                    + "\n# mutation inside deterministic publication callback\n",
                    encoding="utf-8",
                )
            before_publish()

        return original_write(*args, before_publish=mutate_then_verify_sources, **kwargs)

    monkeypatch.setattr(
        run_skill_deterministic,
        "write_deterministic_report",
        mutate_once_inside_publication_callback,
    )
    monkeypatch.setattr(
        run_skill_deterministic.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="deterministic suite passed", stderr=""
        ),
    )

    assert (
        run_skill_deterministic.main(
            [
                "--batch-id",
                "callback-race",
                "--repo-root",
                str(tmp_path),
                "--timeout-sec",
                "1",
            ]
        )
        == 1
    )

    report_path = pressure.deterministic_report_path(tmp_path, "callback-race")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["exit_status"] == 1
    assert report["sources"] == {
        relative: pressure.sha256_file(tmp_path / relative)
        for relative in pressure.DETERMINISTIC_SOURCE_FILES
    }


def test_deterministic_runner_retries_after_postlink_source_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _provision_matrix(tmp_path)
    source = tmp_path / "cli/tests/test_analysis_spec.py"
    original_write = run_skill_deterministic.write_deterministic_report
    mutated = False

    def mutate_once_after_publication_link(*args: object, **kwargs: object) -> Path:
        nonlocal mutated
        after_publish = kwargs.pop("after_publish")
        assert callable(after_publish)

        def mutate_then_verify_sources() -> None:
            nonlocal mutated
            if not mutated:
                mutated = True
                source.write_text(
                    source.read_text(encoding="utf-8")
                    + "\n# mutation after deterministic publication link\n",
                    encoding="utf-8",
                )
            after_publish()

        return original_write(*args, after_publish=mutate_then_verify_sources, **kwargs)

    monkeypatch.setattr(
        run_skill_deterministic,
        "write_deterministic_report",
        mutate_once_after_publication_link,
    )
    monkeypatch.setattr(
        run_skill_deterministic.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="deterministic suite passed", stderr=""
        ),
    )

    assert (
        run_skill_deterministic.main(
            [
                "--batch-id",
                "postlink-race",
                "--repo-root",
                str(tmp_path),
                "--timeout-sec",
                "1",
            ]
        )
        == 1
    )

    report_path = pressure.deterministic_report_path(tmp_path, "postlink-race")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["exit_status"] == 1
    assert report["sources"] == {
        relative: pressure.sha256_file(tmp_path / relative)
        for relative in pressure.DETERMINISTIC_SOURCE_FILES
    }

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


def passing_install_records(matrix: object, *, repo_root: Path) -> list[dict[str, object]]:
    return [
        {
            "runtime": runtime,
            "skill": case.name,
            "source_sha256": pressure.sha256_skill(
                repo_root / "claude" / "skills" / case.name
            ),
            "target_root": {
                "claude": ".claude/skills",
                "agent-skills": ".agents/skills",
                "omp": ".omp/skills",
            }[runtime],
            "status": Verdict.PASS.value,
            "diagnostic": "",
        }
        for runtime in ("claude", "agent-skills", "omp")
        for case in matrix.skills.values()  # type: ignore[attr-defined]
    ]


def passing_install_report(tmp_path: Path, matrix: object, *, batch_id: str) -> Path:
    return pressure.write_install_report(
        tmp_path,
        batch_id=batch_id,
        matrix_sha256=_matrix_sha256(),
        isolated_home_id=f"temporary-{'0' * 32}",
        records=passing_install_records(matrix, repo_root=tmp_path),
    )


@pytest.mark.parametrize(
    ("field", "value", "label"),
    [
        ("diagnostic", "Bearer abcdefghijklmnop", "bearer_token"),
        ("isolated_home_id", f"temporary-{'0' * 32}-sk-abcdefghijklmnop", "openai_key"),
        ("target_root", "sk-abcdefghijklmnop", "openai_key"),
    ],
)
def test_install_writer_rejects_sensitive_serialized_content_before_publication(
    tmp_path: Path, field: str, value: str, label: str
) -> None:
    _provision_matrix(tmp_path)
    records = passing_install_records(MATRIX, repo_root=tmp_path)
    isolated_home_id = f"temporary-{'0' * 32}"
    if field == "isolated_home_id":
        isolated_home_id = value
    else:
        records[0][field] = value

    with pytest.raises(SensitiveDataError, match=label):
        pressure.write_install_report(
            tmp_path,
            batch_id="install-sensitive",
            matrix_sha256=_matrix_sha256(),
            isolated_home_id=isolated_home_id,
            records=records,
        )

    assert not pressure.install_report_path(tmp_path, "install-sensitive").exists()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("isolated_home_id", "temporary-not-opaque", "invalid isolated_home_id"),
        ("target_root", r"C:\operator\private\skills", "invalid install target_root"),
        ("target_root", "/operator/private/skills", "invalid install target_root"),
        ("target_root", ".claude/not-skills", "invalid install target_root"),
        ("diagnostic", "/operator/private", "invalid install diagnostic"),
        ("diagnostic", "unknown_diagnostic", "invalid install diagnostic"),
    ],
)
def test_install_writer_rejects_noncanonical_values_before_publication(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    _provision_matrix(tmp_path)
    records = passing_install_records(MATRIX, repo_root=tmp_path)
    isolated_home_id = f"temporary-{'0' * 32}"
    if field == "isolated_home_id":
        isolated_home_id = value
    else:
        records[0][field] = value

    with pytest.raises(pressure.EvidenceError, match=message):
        pressure.write_install_report(
            tmp_path,
            batch_id="install-noncanonical",
            matrix_sha256=_matrix_sha256(),
            isolated_home_id=isolated_home_id,
            records=records,
        )

    assert not pressure.install_report_path(tmp_path, "install-noncanonical").exists()


def passing_discovery_records(
    matrix: object, *, repo_root: Path = REPO_ROOT
) -> list[dict[str, object]]:
    return [
        {
            "runtime": runtime,
            "skill": case.name,
            "source_sha256": pressure.sha256_skill(
                repo_root / "claude" / "skills" / case.name
            ),
            "auth_mode": "subscription",
            "argv_safety_flags": list(run_skill_discovery.SAFETY_FLAGS[runtime]),
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
                "records": passing_discovery_records(matrix, repo_root=tmp_path),
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
                    "runner_flags": [
                        "-p",
                        "--mode=text",
                        "--no-tools",
                        "--no-session",
                        "--no-extensions",
                        "--no-skills",
                        "--append-system-prompt",
                    ],
                    "repetition": repetition,
                    "severity": case.severity,
                    "baseline": {
                        "verdict": Verdict.FAIL.value,
                        "failures": ["decision"],
                        "criteria": [
                            {
                                "id": "decision",
                                "verdict": Verdict.FAIL.value,
                                "evidence": "not_satisfied",
                            }
                        ],
                    },
                    "with_skill": {
                        "verdict": Verdict.PASS.value,
                        "failures": [],
                        "criteria": [
                            {
                                "id": "decision",
                                "verdict": Verdict.PASS.value,
                                "evidence": "satisfied",
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
        source_hash = pressure.sha256_skill(
            tmp_path / "claude" / "skills" / case.name
        )
        record["skill_sha256"] = source_hash
        skill_file_hash = pressure.sha256_file(
            tmp_path / "claude" / "skills" / case.name / "SKILL.md"
        )
        record["skill_file_sha256"] = skill_file_hash
        record["injection_sha256"] = skill_file_hash
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


def _replace_with_snapshot_blocked_field_report(path: Path, diagnostic: dict[str, object]) -> None:
    complete = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(
        json.dumps(
            {
                "schema": complete["schema"],
                "recorded_at": complete["recorded_at"],
                "run_id": complete["run_id"],
                "persistence_status": "BLOCKED",
                "diagnostics": [diagnostic],
                "field_identity": pressure._safe_field_identity(complete["payload"]),
            }
        ),
        encoding="utf-8",
    )


def test_source_bundle_accepts_exact_snapshot_blocked_field_report(
    tmp_path: Path,
) -> None:
    deterministic = passing_deterministic_report(tmp_path, batch_id="batch-1")
    install = passing_install_report(tmp_path, MATRIX, batch_id="batch-1")
    discovery = passing_discovery_report(tmp_path, MATRIX, batch_id="batch-1")
    fields = passing_field_report_paths(tmp_path, MATRIX, batch_id="batch-1")
    _replace_with_snapshot_blocked_field_report(
        fields[0], {"code": "skill_snapshot_changed"}
    )

    bundle = pressure.validate_source_bundle(
        repo_root=tmp_path,
        batch_id="batch-1",
        deterministic_path=deterministic,
        install_path=install,
        discovery_path=discovery,
        field_paths=fields,
    )

    assert bundle.snapshots["field"][0]["persistence_status"] == "BLOCKED"
    assert bundle.snapshots["field"][0]["diagnostics"] == [
        {"code": "skill_snapshot_changed"}
    ]


@pytest.mark.parametrize(
    "diagnostic",
    [
        {"code": "skill_snapshot_changed", "labels": []},
        {"code": "skill_snapshot_changed", "detail": "unexpected"},
        {"code": "unknown_blocked_code"},
    ],
)
def test_source_bundle_rejects_noncanonical_snapshot_blocked_diagnostic(
    tmp_path: Path, diagnostic: dict[str, object]
) -> None:
    deterministic = passing_deterministic_report(tmp_path, batch_id="batch-1")
    install = passing_install_report(tmp_path, MATRIX, batch_id="batch-1")
    discovery = passing_discovery_report(tmp_path, MATRIX, batch_id="batch-1")
    fields = passing_field_report_paths(tmp_path, MATRIX, batch_id="batch-1")
    _replace_with_snapshot_blocked_field_report(fields[0], diagnostic)

    with pytest.raises(pressure.EvidenceError, match="field BLOCKED envelope diagnostics"):
        pressure.validate_source_bundle(
            repo_root=tmp_path,
            batch_id="batch-1",
            deterministic_path=deterministic,
            install_path=install,
            discovery_path=discovery,
            field_paths=fields,
        )


def test_field_record_requires_complete_reproducibility_metadata() -> None:
    with pytest.raises(pressure.EvidenceError, match="missing field record keys"):
        pressure.validate_field_record({"matrix_schema": MATRIX.schema})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("auth_mode", "api_key", "auth_mode"),
        ("skill_file_sha256", "not-a-hash", "skill_file_sha256"),
        ("injection_sha256", "c" * 64, "injection"),
        ("provider", "other", "provider"),
        ("model", "other", "model"),
        ("runner_flags", [], "runner_flags"),
    ],
)
def test_field_record_requires_subscription_auth_and_bound_injection(
    field: str, value: object, message: str
) -> None:
    record = valid_field_record()
    record[field] = value

    with pytest.raises(pressure.EvidenceError, match=message):
        pressure.validate_field_record(record)


def test_field_record_rejects_missing_subscription_auth() -> None:
    record = valid_field_record()
    del record["auth_mode"]

    with pytest.raises(pressure.EvidenceError, match="auth_mode"):
        pressure.validate_field_record(record)


def test_field_record_rejects_missing_skill_file_hash() -> None:
    record = valid_field_record()
    del record["skill_file_sha256"]

    with pytest.raises(pressure.EvidenceError, match="skill_file_sha256"):
        pressure.validate_field_record(record)


def test_evidence_hashes_all_subscription_field_provenance_sources() -> None:
    assert {
        "cli/src/awf/core/skill_subscription.py",
        "cli/tests/run_skill_discovery.py",
        "cli/tests/run_skill_pressure.py",
        "cli/tests/build_skill_evidence.py",
    }.issubset(pressure.DETERMINISTIC_SOURCE_FILES)


def test_evidence_field_payload_binds_injection_hash_to_current_skill_snapshot(
    tmp_path: Path,
) -> None:
    deterministic = passing_deterministic_report(tmp_path, batch_id="batch-1")
    install = passing_install_report(tmp_path, MATRIX, batch_id="batch-1")
    discovery = passing_discovery_report(tmp_path, MATRIX, batch_id="batch-1")
    fields = passing_field_report_paths(tmp_path, MATRIX, batch_id="batch-1")
    forged = json.loads(fields[0].read_text())
    forged["payload"]["injection_sha256"] = "0" * 64
    fields[0].write_text(json.dumps(forged))

    with pytest.raises(pressure.EvidenceError, match="field injection hash mismatch"):
        pressure.validate_source_bundle(
            repo_root=tmp_path,
            batch_id="batch-1",
            deterministic_path=deterministic,
            install_path=install,
            discovery_path=discovery,
            field_paths=fields,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("auth_mode", "api_key", "discovery auth_mode"),
        ("argv_safety_flags", [], "discovery argv_safety_flags"),
    ],
)
def test_source_bundle_rejects_discovery_record_without_safe_subscription_provenance(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    deterministic = passing_deterministic_report(tmp_path, batch_id="batch-1")
    install = passing_install_report(tmp_path, MATRIX, batch_id="batch-1")
    discovery = passing_discovery_report(tmp_path, MATRIX, batch_id="batch-1")
    fields = passing_field_report_paths(tmp_path, MATRIX, batch_id="batch-1")
    forged = json.loads(discovery.read_text())
    forged["records"][0][field] = value
    discovery.write_text(json.dumps(forged))

    with pytest.raises(pressure.EvidenceError, match=message):
        pressure.validate_source_bundle(
            repo_root=tmp_path,
            batch_id="batch-1",
            deterministic_path=deterministic,
            install_path=install,
            discovery_path=discovery,
            field_paths=fields,
        )


def test_source_bundle_rejects_install_source_hash_not_matching_current_skill(
    tmp_path: Path,
) -> None:
    deterministic = passing_deterministic_report(tmp_path, batch_id="batch-1")
    install = passing_install_report(tmp_path, MATRIX, batch_id="batch-1")
    discovery = passing_discovery_report(tmp_path, MATRIX, batch_id="batch-1")
    fields = passing_field_report_paths(tmp_path, MATRIX, batch_id="batch-1")
    forged = json.loads(install.read_text())
    source_sha256 = forged["records"][0]["source_sha256"]
    forged["records"][0]["source_sha256"] = (
        ("0" if source_sha256[0] != "0" else "1") + source_sha256[1:]
    )
    install.write_text(json.dumps(forged))

    with pytest.raises(
        pressure.EvidenceError, match="install source_sha256 does not match current Skill"
    ):
        pressure.validate_source_bundle(
            repo_root=tmp_path,
            batch_id="batch-1",
            deterministic_path=deterministic,
            install_path=install,
            discovery_path=discovery,
            field_paths=fields,
        )


@pytest.mark.parametrize(
    ("field", "value", "label"),
    [
        ("diagnostic", "Bearer abcdefghijklmnop", "bearer_token"),
        ("isolated_home_id", f"temporary-{'0' * 32}-sk-abcdefghijklmnop", "openai_key"),
        ("target_root", "sk-abcdefghijklmnop", "openai_key"),
    ],
)
def test_source_bundle_rejects_sensitive_serialized_install_reports(
    tmp_path: Path, field: str, value: str, label: str
) -> None:
    deterministic = passing_deterministic_report(tmp_path, batch_id="batch-1")
    install = passing_install_report(tmp_path, MATRIX, batch_id="batch-1")
    discovery = passing_discovery_report(tmp_path, MATRIX, batch_id="batch-1")
    fields = passing_field_report_paths(tmp_path, MATRIX, batch_id="batch-1")
    forged = json.loads(install.read_text())
    if field == "isolated_home_id":
        forged[field] = value
    else:
        forged["records"][0][field] = value
    install.write_text(json.dumps(forged))

    with pytest.raises(pressure.EvidenceError, match=rf"sensitive install report content.*{label}"):
        pressure.validate_source_bundle(
            repo_root=tmp_path,
            batch_id="batch-1",
            deterministic_path=deterministic,
            install_path=install,
            discovery_path=discovery,
            field_paths=fields,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("isolated_home_id", "temporary-not-opaque", "invalid isolated_home_id"),
        ("target_root", r"C:\operator\private\skills", "invalid install target_root"),
        ("target_root", ".claude/not-skills", "invalid install target_root"),
        ("diagnostic", "/operator/private", "invalid install diagnostic"),
        ("diagnostic", "unknown_diagnostic", "invalid install diagnostic"),
    ],
)
def test_source_bundle_rejects_noncanonical_install_attestations(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    deterministic = passing_deterministic_report(tmp_path, batch_id="batch-1")
    install = passing_install_report(tmp_path, MATRIX, batch_id="batch-1")
    discovery = passing_discovery_report(tmp_path, MATRIX, batch_id="batch-1")
    fields = passing_field_report_paths(tmp_path, MATRIX, batch_id="batch-1")
    forged = json.loads(install.read_text())
    if field == "isolated_home_id":
        forged[field] = value
    else:
        forged["records"][0][field] = value
    install.write_text(json.dumps(forged))

    with pytest.raises(pressure.EvidenceError, match=message):
        pressure.validate_source_bundle(
            repo_root=tmp_path,
            batch_id="batch-1",
            deterministic_path=deterministic,
            install_path=install,
            discovery_path=discovery,
            field_paths=fields,
        )


def test_source_bundle_rejects_discovery_source_hash_not_matching_current_skill(
    tmp_path: Path,
) -> None:
    deterministic = passing_deterministic_report(tmp_path, batch_id="batch-1")
    install = passing_install_report(tmp_path, MATRIX, batch_id="batch-1")
    discovery = passing_discovery_report(tmp_path, MATRIX, batch_id="batch-1")
    fields = passing_field_report_paths(tmp_path, MATRIX, batch_id="batch-1")
    forged = json.loads(discovery.read_text())
    source_sha256 = forged["records"][0]["source_sha256"]
    forged["records"][0]["source_sha256"] = (
        ("0" if source_sha256[0] != "0" else "1") + source_sha256[1:]
    )
    discovery.write_text(json.dumps(forged))

    with pytest.raises(
        pressure.EvidenceError, match="discovery source_sha256 does not match current Skill"
    ):
        pressure.validate_source_bundle(
            repo_root=tmp_path,
            batch_id="batch-1",
            deterministic_path=deterministic,
            install_path=install,
            discovery_path=discovery,
            field_paths=fields,
        )


def test_source_bundle_binds_field_file_hash_to_current_skill_file(tmp_path: Path) -> None:
    deterministic = passing_deterministic_report(tmp_path, batch_id="batch-1")
    install = passing_install_report(tmp_path, MATRIX, batch_id="batch-1")
    discovery = passing_discovery_report(tmp_path, MATRIX, batch_id="batch-1")
    fields = passing_field_report_paths(tmp_path, MATRIX, batch_id="batch-1")
    forged = json.loads(fields[0].read_text())
    forged["payload"]["skill_file_sha256"] = "0" * 64
    forged["payload"]["injection_sha256"] = "0" * 64
    fields[0].write_text(json.dumps(forged))

    with pytest.raises(pressure.EvidenceError, match="field Skill file hash mismatch"):
        pressure.validate_source_bundle(
            repo_root=tmp_path,
            batch_id="batch-1",
            deterministic_path=deterministic,
            install_path=install,
            discovery_path=discovery,
            field_paths=fields,
        )


def test_source_bundle_binds_full_skill_directory_hash_including_nested_resources(
    tmp_path: Path,
) -> None:
    deterministic = passing_deterministic_report(tmp_path, batch_id="batch-1")
    install = passing_install_report(tmp_path, MATRIX, batch_id="batch-1")
    discovery = passing_discovery_report(tmp_path, MATRIX, batch_id="batch-1")
    fields = passing_field_report_paths(tmp_path, MATRIX, batch_id="batch-1")
    (
        tmp_path
        / "claude"
        / "skills"
        / MATRIX.skills["release-worktree-lifecycle"].name
        / "nested-resource.md"
    ).write_text("mutated nested resource", encoding="utf-8")

    with pytest.raises(
        pressure.EvidenceError, match="install source_sha256 does not match current Skill"
    ):
        pressure.validate_source_bundle(
            repo_root=tmp_path,
            batch_id="batch-1",
            deterministic_path=deterministic,
            install_path=install,
            discovery_path=discovery,
            field_paths=fields,
        )


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

def test_evidence_builder_rechecks_canonical_skill_tree_before_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    batch_id = "batch-1"
    passing_deterministic_report(tmp_path, batch_id=batch_id)
    passing_install_report(tmp_path, MATRIX, batch_id=batch_id)
    passing_discovery_report(tmp_path, MATRIX, batch_id=batch_id)
    passing_field_report_paths(tmp_path, MATRIX, batch_id=batch_id)
    original_validate = build_skill_evidence.validate_source_bundle

    def validate_then_mutate(**kwargs: object) -> pressure.SourceBundle:
        bundle = original_validate(**kwargs)  # type: ignore[arg-type]
        skill_file = tmp_path / "claude" / "skills" / "wf-status" / "SKILL.md"
        skill_file.write_text(
            skill_file.read_text(encoding="utf-8") + "\nmutated after validation\n",
            encoding="utf-8",
        )
        return bundle

    def evidence_construction_must_not_run(
        *args: object, **kwargs: object
    ) -> tuple[pressure.EvidenceCell, ...]:
        raise AssertionError("evidence construction ran after canonical source mutation")

    monkeypatch.setattr(
        build_skill_evidence, "validate_source_bundle", validate_then_mutate
    )
    monkeypatch.setattr(
        build_skill_evidence,
        "build_evidence_matrix",
        evidence_construction_must_not_run,
    )

    assert (
        build_skill_evidence.main(
            ["--batch-id", batch_id, "--repo-root", str(tmp_path)]
        )
        == 1
    )
    assert "canonical Skill source hash mismatch" in capsys.readouterr().err




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


def _passing_evidence_cells() -> tuple[pressure.EvidenceCell, ...]:
    return tuple(
        pressure.EvidenceCell(
            skill=skill,
            category=category,
            layer=pressure.EVIDENCE_LAYERS[category],
            verdict=Verdict.PASS,
            evidence="current validated evidence",
        )
        for skill in MATRIX.skills
        for category in pressure.REQUIRED_CATEGORIES
    )


def _current_source_bundle(tmp_path: Path, *, batch_id: str) -> pressure.SourceBundle:
    deterministic = passing_deterministic_report(tmp_path, batch_id=batch_id)
    install = passing_install_report(tmp_path, MATRIX, batch_id=batch_id)
    discovery = passing_discovery_report(tmp_path, MATRIX, batch_id=batch_id)
    fields = passing_field_report_paths(tmp_path, MATRIX, batch_id=batch_id)
    return pressure.validate_source_bundle(
        repo_root=tmp_path,
        batch_id=batch_id,
        deterministic_path=deterministic,
        install_path=install,
        discovery_path=discovery,
        field_paths=fields,
    )

def test_source_bundle_rechecks_canonical_skill_tree_after_validation(
    tmp_path: Path,
) -> None:
    bundle = _current_source_bundle(tmp_path, batch_id="batch-1")
    skill = "wf-status"
    skill_root = tmp_path / "claude" / "skills" / skill
    original_hash = pressure.sha256_skill(skill_root)

    assert bundle.canonical_skill_hashes[skill] == original_hash
    with pytest.raises(TypeError):
        bundle.canonical_skill_hashes[skill] = "0" * 64  # type: ignore[index]

    skill_file = skill_root / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8") + "\nmutated canonical Skill\n",
        encoding="utf-8",
    )

    with pytest.raises(pressure.EvidenceError, match="canonical Skill source hash mismatch"):
        pressure.verify_source_bundle_unchanged(bundle)




def test_source_bundle_serializes_current_report_references_as_relative_posix_paths(
    tmp_path: Path,
) -> None:
    bundle = _current_source_bundle(tmp_path, batch_id="batch-1")
    references = [
        bundle["deterministic"],
        bundle["install"],
        bundle["discovery"],
        *bundle["field"],
    ]

    for reference in references:
        assert set(reference) == {"path", "sha256"}
        source_path = reference["path"]
        assert isinstance(source_path, str)
        assert source_path.startswith(".awf-operations/skill-pressure/")
        assert not Path(source_path).is_absolute()
        assert "\\" not in source_path
        assert ".." not in source_path.split("/")
        assert reference["sha256"] == hashlib.sha256(
            (tmp_path / source_path).read_bytes()
        ).hexdigest()


def test_evidence_builder_publishes_only_relative_current_source_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    batch_id = "batch-1"
    deterministic = passing_deterministic_report(tmp_path, batch_id=batch_id)
    install = passing_install_report(tmp_path, MATRIX, batch_id=batch_id)
    discovery = passing_discovery_report(tmp_path, MATRIX, batch_id=batch_id)
    fields = passing_field_report_paths(tmp_path, MATRIX, batch_id=batch_id)
    monkeypatch.setattr(
        build_skill_evidence,
        "build_evidence_matrix",
        lambda *args, **kwargs: _passing_evidence_cells(),
    )

    assert (
        build_skill_evidence.main(
            ["--batch-id", batch_id, "--repo-root", str(tmp_path)]
        )
        == 0
    )
    summary = pressure.evidence_summary_path(tmp_path, batch_id)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == (
        f"{summary} verdict_counts={json.dumps({'PASS': 135}, sort_keys=True)}\n"
    )
    serialized = json.loads(summary.read_text(encoding="utf-8"))
    source_references = [
        serialized["sources"]["deterministic"],
        serialized["sources"]["install"],
        serialized["sources"]["discovery"],
        *serialized["sources"]["field"],
    ]

    for report_path in [deterministic, install, discovery, *fields, summary]:
        report_text = report_path.read_text(encoding="utf-8")
        assert str(tmp_path) not in report_text
        assert str(Path.home()) not in report_text
    for reference in source_references:
        source_path = reference["path"]
        assert source_path.startswith(".awf-operations/skill-pressure/")
        assert not Path(source_path).is_absolute()
        assert "\\" not in source_path
        assert ".." not in source_path.split("/")
        assert reference["sha256"] == hashlib.sha256(
            (tmp_path / source_path).read_bytes()
        ).hexdigest()


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/private/tmp/deterministic-batch-1.json", "normalized"),
        (
            ".awf-operations/skill-pressure/../deterministic-batch-1.json",
            "normalized",
        ),
        (
            ".awf-operations/skill-pressure/.deterministic-batch-1.json.tmp",
            "current batch",
        ),
        (".awf-operations/other/deterministic-batch-1.json", "skill-pressure"),
    ],
)
def test_evidence_summary_rejects_untrusted_source_reference_paths(
    tmp_path: Path, path: str, message: str
) -> None:
    bundle = _current_source_bundle(tmp_path, batch_id="batch-1")
    sources = dict(bundle)
    sources["deterministic"] = {
        "path": path,
        "sha256": bundle["deterministic"]["sha256"],
    }

    with pytest.raises(pressure.EvidenceError, match=message):
        pressure.write_evidence_summary(
            tmp_path,
            run_id="batch-1",
            cells=_passing_evidence_cells(),
            sources=sources,
            matrix=MATRIX,
        )
    assert not pressure.evidence_summary_path(tmp_path, "batch-1").exists()


def test_evidence_summary_rechecks_symlinked_or_mutated_sources_before_publish(
    tmp_path: Path,
) -> None:
    bundle = _current_source_bundle(tmp_path, batch_id="batch-1")
    deterministic = pressure.deterministic_report_path(tmp_path, "batch-1")
    external = tmp_path / "outside.json"
    external.write_text(deterministic.read_text(encoding="utf-8"), encoding="utf-8")
    deterministic.unlink()
    deterministic.symlink_to(external)

    with pytest.raises(pressure.EvidenceError, match="symlink"):
        pressure.write_evidence_summary(
            tmp_path,
            run_id="batch-1",
            cells=_passing_evidence_cells(),
            sources=bundle,
            matrix=MATRIX,
        )
    assert not pressure.evidence_summary_path(tmp_path, "batch-1").exists()


def test_evidence_summary_rechecks_source_hashes_immediately_before_publish(
    tmp_path: Path,
) -> None:
    bundle = _current_source_bundle(tmp_path, batch_id="batch-1")
    deterministic = pressure.deterministic_report_path(tmp_path, "batch-1")
    deterministic.write_text(
        deterministic.read_text(encoding="utf-8") + "\nmutated",
        encoding="utf-8",
    )

    with pytest.raises(pressure.EvidenceError, match="source hash mismatch"):
        pressure.write_evidence_summary(
            tmp_path,
            run_id="batch-1",
            cells=_passing_evidence_cells(),
            sources=bundle,
            matrix=MATRIX,
        )
    assert not pressure.evidence_summary_path(tmp_path, "batch-1").exists()


def test_evidence_summary_rechecks_sources_inside_append_only_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _current_source_bundle(tmp_path, batch_id="batch-1")
    deterministic = pressure.deterministic_report_path(tmp_path, "batch-1")
    original_publish = pressure._publish_new

    def mutate_then_publish(target: Path, content: str, **kwargs: object) -> None:
        deterministic.write_text(
            deterministic.read_text(encoding="utf-8") + "\nmutated before link",
            encoding="utf-8",
        )
        original_publish(target, content, **kwargs)

    monkeypatch.setattr(pressure, "_publish_new", mutate_then_publish)

    with pytest.raises(pressure.EvidenceError, match="source hash mismatch"):
        pressure.write_evidence_summary(
            tmp_path,
            run_id="batch-1",
            cells=_passing_evidence_cells(),
            sources=bundle,
            matrix=MATRIX,
        )
    assert not pressure.evidence_summary_path(tmp_path, "batch-1").exists()

def test_evidence_summary_rechecks_canonical_skill_tree_inside_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _current_source_bundle(tmp_path, batch_id="batch-1")
    skill_file = tmp_path / "claude" / "skills" / "wf-status" / "SKILL.md"
    original_publish = pressure._publish_new

    def mutate_then_publish(target: Path, content: str, **kwargs: object) -> None:
        skill_file.write_text(
            skill_file.read_text(encoding="utf-8") + "\nmutated before summary link\n",
            encoding="utf-8",
        )
        original_publish(target, content, **kwargs)

    monkeypatch.setattr(pressure, "_publish_new", mutate_then_publish)

    with pytest.raises(pressure.EvidenceError, match="canonical Skill source hash mismatch"):
        pressure.write_evidence_summary(
            tmp_path,
            run_id="batch-1",
            cells=_passing_evidence_cells(),
            sources=bundle,
            matrix=MATRIX,
        )
    assert not pressure.evidence_summary_path(tmp_path, "batch-1").exists()


@pytest.mark.parametrize(
    ("source_kind", "message"),
    [
        ("report", "deterministic source hash mismatch"),
        ("canonical", "canonical Skill source hash mismatch"),
    ],
)
def test_evidence_summary_revalidates_sources_after_link_and_removes_stale_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_kind: str,
    message: str,
) -> None:
    bundle = _current_source_bundle(tmp_path, batch_id="batch-1")
    target = pressure.evidence_summary_path(tmp_path, "batch-1")
    deterministic = pressure.deterministic_report_path(tmp_path, "batch-1")
    skill_file = tmp_path / "claude" / "skills" / "wf-status" / "SKILL.md"
    original_link = pressure.os.link
    original_verify = pressure.verify_source_bundle_unchanged
    verification_count = 0

    def count_source_verifications(candidate: pressure.SourceBundle) -> None:
        nonlocal verification_count
        verification_count += 1
        original_verify(candidate)

    def mutate_source_after_link(*args: object, **kwargs: object) -> None:
        original_link(*args, **kwargs)
        assert verification_count == 1
        assert target.exists()
        if source_kind == "report":
            deterministic.write_text(
                deterministic.read_text(encoding="utf-8") + "\nmutated after link",
                encoding="utf-8",
            )
        else:
            skill_file.write_text(
                skill_file.read_text(encoding="utf-8")
                + "\nmutated after summary link\n",
                encoding="utf-8",
            )

    monkeypatch.setattr(
        pressure, "verify_source_bundle_unchanged", count_source_verifications
    )
    monkeypatch.setattr(pressure.os, "link", mutate_source_after_link)

    with pytest.raises(pressure.EvidenceError, match=message):
        pressure.write_evidence_summary(
            tmp_path,
            run_id="batch-1",
            cells=_passing_evidence_cells(),
            sources=bundle,
            matrix=MATRIX,
        )

    assert verification_count == 2
    assert not target.exists()
    assert list(target.parent.glob(f".{target.name}.*")) == []




def test_evidence_summary_rejects_a_source_symlink_swap_during_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _current_source_bundle(tmp_path, batch_id="batch-1")
    deterministic = pressure.deterministic_report_path(tmp_path, "batch-1")
    external = tmp_path / "outside.json"
    external.write_text(deterministic.read_text(encoding="utf-8"), encoding="utf-8")
    original_open = pressure.os.open
    swapped = False

    def swap_source_before_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        if (
            not swapped
            and path == deterministic.name
            and kwargs.get("dir_fd") is not None
        ):
            deterministic.unlink()
            deterministic.symlink_to(external)
            swapped = True
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(pressure.os, "open", swap_source_before_open)

    with pytest.raises(pressure.EvidenceError, match="source"):
        pressure.write_evidence_summary(
            tmp_path,
            run_id="batch-1",
            cells=_passing_evidence_cells(),
            sources=bundle,
            matrix=MATRIX,
        )
    assert not pressure.evidence_summary_path(tmp_path, "batch-1").exists()

def test_trusted_source_digest_returns_unchanged_regular_file_hash(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    source = root / ".awf-operations" / "skill-pressure" / "source.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"trusted source\n")

    assert pressure._trusted_source_digest(root, source, label="source") == hashlib.sha256(
        source.read_bytes()
    ).hexdigest()


@pytest.mark.parametrize(
    ("target", "substitution"),
    [
        ("component", "rename"),
        ("component", "symlink"),
        ("root", "rename"),
        ("root", "symlink"),
        ("file", "rename"),
        ("file", "symlink"),
    ],
)
def test_trusted_source_digest_rejects_substitution_during_read_without_fd_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    substitution: str,
) -> None:
    root = tmp_path / "repo"
    source = root / ".awf-operations" / "skill-pressure" / "source.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"trusted source\n")
    replacement_root = tmp_path / "replacement-root"
    replacement_source = (
        replacement_root / ".awf-operations" / "skill-pressure" / source.name
    )
    replacement_source.parent.mkdir(parents=True)
    replacement_source.write_bytes(source.read_bytes())
    original_open = pressure.os.open
    original_read = pressure.os.read
    opened_fds: list[int] = []
    swapped = False

    def substitute() -> None:
        nonlocal swapped
        if target == "component":
            component = (
                root / ".awf-operations"
                if substitution == "rename"
                else root / ".awf-operations" / "skill-pressure"
            )
            preserved = tmp_path / f"preserved-{component.name}"
            replacement = tmp_path / f"replacement-{component.name}"
            component.rename(preserved)
            if substitution == "rename":
                replacement.mkdir()
                replacement.rename(component)
            else:
                component.symlink_to(replacement_root, target_is_directory=True)
        elif target == "root":
            root.rename(tmp_path / "preserved-root")
            if substitution == "rename":
                replacement_root.rename(root)
            else:
                root.symlink_to(replacement_root, target_is_directory=True)
        else:
            source.rename(tmp_path / "preserved-source.json")
            if substitution == "rename":
                replacement_source.rename(source)
            else:
                source.symlink_to(replacement_source)
        swapped = True

    def track_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        if (
            target != "file"
            and not swapped
            and path == source.name
            and kwargs.get("dir_fd") is not None
        ):
            substitute()
        file_descriptor = original_open(path, *args, **kwargs)
        opened_fds.append(file_descriptor)
        return file_descriptor

    def swap_before_read(file_descriptor: int, size: int) -> bytes:
        if target == "file" and not swapped:
            substitute()
        return original_read(file_descriptor, size)

    monkeypatch.setattr(pressure.os, "open", track_open)
    monkeypatch.setattr(pressure.os, "read", swap_before_read)

    with pytest.raises(pressure.EvidenceError, match="source hash mismatch"):
        pressure._trusted_source_digest(root, source, label="source")

    assert swapped
    for file_descriptor in opened_fds:
        with pytest.raises(OSError):
            os.fstat(file_descriptor)


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
        canonical_root: Path | None = None,
        mutate_canonical_after_install: str | None = None,
        read_workspace_skills: bool = False,
        mutate_snapshot_after_probe: tuple[str, str] | None = None,
    ) -> None:
        self.expected = expected
        self.failure = failure
        self.fail_install = fail_install
        self.blocked_install_runtime = blocked_install_runtime
        self.canonical_root = canonical_root
        self.mutate_canonical_after_install = mutate_canonical_after_install
        self.read_workspace_skills = read_workspace_skills
        self.mutate_snapshot_after_probe = mutate_snapshot_after_probe
        self.installed_sources: dict[str, tuple[str, tuple[str, str, str]]] = {}
        self.installed_paths: dict[str, Path] = {}
        self.workspace_skills: dict[tuple[str, str], tuple[str, tuple[str, str, str]]] = {}
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []
        self.claude_init_events: dict[str, dict[str, object]] = {}

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
            self.installed_paths[source.name] = source
            self.installed_sources[source.name] = (
                sha256_skill(source),
                run_skill_discovery._source_metadata(source / "SKILL.md"),
            )
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
            if self.mutate_canonical_after_install == source.name:
                assert self.canonical_root is not None
                skill_file = self.canonical_root / "claude" / "skills" / source.name / "SKILL.md"
                skill_file.write_text(
                    skill_file.read_text(encoding="utf-8").replace(
                        f"name: {source.name}",
                        "name: mutated-after-install",
                        1,
                    ),
                    encoding="utf-8",
                )
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
        if failure and failure[2] == "oauth-session-expired":
            return DiscoveryProcessResult(
                23,
                "",
                "Failed to authenticate: OAuth session expired and could not be refreshed",
                0.01,
            )
        if failure and failure[2] == "model-unsupported":
            return DiscoveryProcessResult(23, "", "model is not supported", 0.01)
        if failure and failure[2] == "malformed" and runtime != "claude":
            return DiscoveryProcessResult(0, "{", "", 0.01)
        if failure and failure[2] == "unknown":
            return DiscoveryProcessResult(0, "unknown Skill requested", "", 0.01)
        if runtime == "claude":
            source = cwd / run_skill_discovery.TARGET_ROOTS[runtime] / skill
            metadata = run_skill_discovery._source_metadata(source / "SKILL.md")
            self.workspace_skills[(runtime, skill)] = (sha256_skill(source.resolve()), metadata)
            init = {
                "type": "system",
                "subtype": "init",
                "skills": [skill],
                "slash_commands": [skill],
                "tools": [],
                "session_id": "host-native-session-id",
                "cwd": str(cwd),
            }
            result = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": "host-native-session-id",
            }
            scenario = failure[2] if failure else ""
            if scenario == "malformed":
                return DiscoveryProcessResult(0, "{", "", 0.01)
            if scenario == "missing-init":
                return DiscoveryProcessResult(0, json.dumps(result), "", 0.01)
            if scenario == "duplicate-init":
                return DiscoveryProcessResult(
                    0, f"{json.dumps(init)}\n{json.dumps(init)}\n{json.dumps(result)}", "", 0.01
                )
            if scenario == "duplicate-json-key":
                duplicate = (
                    '{"type":"system","type":"system","subtype":"init",'
                    f'"skills":["{skill}"],"slash_commands":["{skill}"],"tools":[]}}'
                )
                return DiscoveryProcessResult(0, f"{duplicate}\n{json.dumps(result)}", "", 0.01)
            if scenario == "absent-name":
                init["skills"] = []
            elif scenario == "duplicate-name":
                init["slash_commands"] = [skill, skill]
            elif scenario == "malformed-arrays":
                init["skills"] = skill
            elif scenario == "non-string-arrays":
                init["slash_commands"] = [1]
            elif scenario == "nonempty-tools":
                init["tools"] = [{"name": "Read"}]
            elif scenario == "missing-result":
                return DiscoveryProcessResult(0, json.dumps(init), "", 0.01)
            elif scenario == "failed-result":
                result = {
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "session_id": "host-native-session-id",
                }
            elif scenario == "trailing-event":
                trailing_event = {
                    "type": "assistant",
                    "subtype": "message",
                    "session_id": "trailing-session-id",
                    "cwd": "/trailing/secret/path",
                }
                self.claude_init_events[skill] = init
                return DiscoveryProcessResult(
                    0,
                    f"{json.dumps(init)}\n{json.dumps(result)}\n{json.dumps(trailing_event)}",
                    "",
                    0.01,
                )
            self.claude_init_events[skill] = init
            return DiscoveryProcessResult(
                0, f"{json.dumps(init)}\n{json.dumps(result)}", "", 0.01
            )
        if self.read_workspace_skills:
            source = cwd / run_skill_discovery.TARGET_ROOTS[runtime] / skill
            metadata = run_skill_discovery._source_metadata(source / "SKILL.md")
            self.workspace_skills[(runtime, skill)] = (sha256_skill(source.resolve()), metadata)
            payload = {
                "name": metadata[0],
                "description": metadata[1],
                "body_heading": metadata[2],
            }
        else:
            expected = self.expected[skill]
            payload = {
                "name": expected.name,
                "description": expected.description,
                "body_heading": expected.body_heading,
            }
        if failure and failure[2].startswith("mismatch-"):
            field = failure[2].removeprefix("mismatch-")
            payload[field] = f"not-the-source-{field}"
        if self.mutate_snapshot_after_probe == (runtime, skill):
            snapshot_file = cwd / run_skill_discovery.TARGET_ROOTS[runtime] / skill / "SKILL.md"
            snapshot_file.write_text(
                snapshot_file.read_text(encoding="utf-8").replace(
                    f"name: {skill}", "name: mutated-snapshot-by-host", 1
                ),
                encoding="utf-8",
            )
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
    mutate_canonical_after_install: str | None = None,
    read_workspace_skills: bool = False,
    mutate_snapshot_after_probe: tuple[str, str] | None = None,
    configure: Callable[[_FakeDiscoveryProcess], None] | None = None,
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
        canonical_root=repo_root,
        mutate_canonical_after_install=mutate_canonical_after_install,
        read_workspace_skills=read_workspace_skills,
        mutate_snapshot_after_probe=mutate_snapshot_after_probe,
    )
    if configure is not None:
        configure(fake)
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
    assert claude_argv("claude", "sonnet", "analysis") == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--tools",
        "",
        "--no-session-persistence",
        "--setting-sources",
        "project",
        "--model",
        "sonnet",
        "/analysis",
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
        "Return the decoded YAML frontmatter scalar values for name and description, excluding YAML syntax "
        "such as surrounding quote delimiters, block indicators, and indentation. "
        "Preserve each decoded scalar's content and embedded newlines exactly; do not translate, summarize, "
        "or normalize whitespace. "
        "body_heading is the exact source Markdown H1 line, including the literal leading '# '. "
        "Encode embedded newlines as JSON escapes. "
        'Return exactly one JSON object and no prose. Its schema is {"name":"decoded frontmatter name",'
        '"description":"decoded frontmatter description","body_heading":"exact first Markdown H1"}.'
    )


def test_field_subprocess_closes_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_skill_pressure._run_process(
        ["omp", "-p"],
        cwd=tmp_path,
        env={},
        timeout=30,
    )

    assert result.returncode == 0
    assert captured["stdin"] is subprocess.DEVNULL


def test_skill_discovery_subprocess_closes_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_skill_discovery._run_process(
        ["codex", "exec"],
        cwd=tmp_path,
        env={},
        timeout=30,
    )

    assert result.returncode == 0
    assert captured["stdin"] is subprocess.DEVNULL


def test_skill_discovery_sends_native_claude_and_unchanged_metadata_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake, _ = _run_fake_discovery(tmp_path, monkeypatch)
    prompts = {
        "agent-skills": (
            "Do not call tools, access files, or mutate anything. Read the selected Skill only. "
            "Return the decoded YAML frontmatter scalar values for name and description, excluding YAML syntax "
            "such as surrounding quote delimiters, block indicators, and indentation. "
            "Preserve each decoded scalar's content and embedded newlines exactly; do not translate, summarize, "
            "or normalize whitespace. "
            "body_heading is the exact source Markdown H1 line, including the literal leading '# '. "
            "Encode embedded newlines as JSON escapes. "
            'Return exactly one JSON object and no prose. Its schema is {"name":"decoded frontmatter name",'
            '"description":"decoded frontmatter description","body_heading":"exact first Markdown H1"}.'
        ),
        "omp": (
            "Use only the read tool to load the selected Skill. Do not read any other path or mutate anything. "
            "Return the decoded YAML frontmatter scalar values for name and description, excluding YAML syntax "
            "such as surrounding quote delimiters, block indicators, and indentation. "
            "Preserve each decoded scalar's content and embedded newlines exactly; do not translate, summarize, "
            "or normalize whitespace. "
            "body_heading is the exact source Markdown H1 line, including the literal leading '# '. "
            "Encode embedded newlines as JSON escapes. "
            'Return exactly one JSON object and no prose. Its schema is {"name":"decoded frontmatter name",'
            '"description":"decoded frontmatter description","body_heading":"exact first Markdown H1"}.'
        ),
    }
    model_argvs = {
        "claude": next(
            argv
            for argv, _, _ in fake.calls
            if argv[0] == "fake-claude" and argv[-1] == "/analysis"
        ),
        "agent-skills": next(
            argv
            for argv, _, _ in fake.calls
            if argv[0] == "fake-agent" and argv[-1].startswith("$analysis\n")
        ),
        "omp": next(
            argv
            for argv, _, _ in fake.calls
            if argv[0] == "fake-omp" and "--skills=analysis" in argv
        ),
    }

    assert model_argvs["claude"][-1] == "/analysis"
    assert "--append-system-prompt" not in model_argvs["claude"]
    assert model_argvs["agent-skills"][-1] == f"$analysis\n{prompts['agent-skills']}"
    assert model_argvs["omp"][-1] == prompts["omp"]


def test_skill_discovery_claude_binds_native_registration_to_immutable_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, fake, _ = _run_fake_discovery(tmp_path, monkeypatch)
    expected = load_expected_skills(
        result.repo_root,
        result.repo_root / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json",
    )
    claude_records = [
        record for record in result.discovery_records if record["runtime"] == "claude"
    ]
    claude_calls = [
        (argv, cwd) for argv, cwd, _ in fake.calls if argv[0] == "fake-claude" and argv[-1].startswith("/")
    ]
    report_text = result.discovery_report.read_text(encoding="utf-8")
    runner_source = Path(run_skill_discovery.__file__).read_text(encoding="utf-8")

    assert len(claude_records) == len(expected) == len(fake.claude_init_events) == 15
    assert {record["elapsed_sec"] for record in result.discovery_records} == {0.01}
    assert all(record["verdict"] == "PASS" for record in claude_records)
    assert all(
        record["argv_safety_flags"]
        == [
            "-p",
            "--output-format=stream-json",
            "--verbose",
            "--tools=",
            "--no-session-persistence",
            "--setting-sources=project",
        ]
        for record in claude_records
    )
    for skill, source in expected.items():
        init = fake.claude_init_events[skill]
        assert init["skills"] == [skill]
        assert init["slash_commands"] == [skill]
        assert init["tools"] == []
        assert fake.workspace_skills[("claude", skill)] == (
            source.source_sha256,
            (source.name, source.description, source.body_heading),
        )
    for argv, cwd in claude_calls:
        assert argv[-1].count("/") == 1
        assert "\n" not in argv[-1]
        assert "--append-system-prompt" not in argv
        assert not cwd.parent.exists()
    assert "host-native-session-id" not in report_text
    assert all(str(init["cwd"]) not in report_text for init in fake.claude_init_events.values())
    assert "MetadataProjection" not in runner_source
    assert "_materialize_metadata_projections" not in runner_source
    assert "--append-system-prompt" not in runner_source


@pytest.mark.parametrize(
    ("scenario", "diagnostic"),
    [
        ("missing-init", "claude_init_missing"),
        ("duplicate-init", "claude_init_duplicate"),
        ("malformed", "claude_stream_malformed"),
        ("duplicate-json-key", "claude_stream_malformed"),
        ("absent-name", "claude_skill_not_registered"),
        ("duplicate-name", "claude_skill_not_registered"),
        ("malformed-arrays", "claude_init_invalid"),
        ("non-string-arrays", "claude_init_invalid"),
        ("nonempty-tools", "claude_init_tools_not_empty"),
        ("missing-result", "claude_result_missing"),
        ("failed-result", "claude_result_failed"),
        ("trailing-event", "claude_result_not_terminal"),
    ],
)
def test_skill_discovery_rejects_invalid_claude_native_event_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    diagnostic: str,
) -> None:
    result, fake, _ = _run_fake_discovery(
        tmp_path, monkeypatch, failure=("claude", "analysis", scenario)
    )
    record = next(
        record
        for record in result.discovery_records
        if record["runtime"] == "claude" and record["skill"] == "analysis"
    )
    report_text = result.discovery_report.read_text(encoding="utf-8")
    claude_cwd = next(
        cwd
        for argv, cwd, _ in fake.calls
        if argv[0] == "fake-claude" and argv[-1] == "/analysis"
    )

    assert record["verdict"] == "FAIL"
    assert record["diagnostic"] == diagnostic
    assert "host-native-session-id" not in report_text
    assert str(claude_cwd) not in report_text
    if scenario == "trailing-event":
        assert "trailing-session-id" not in report_text
        assert "/trailing/secret/path" not in report_text

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
        (("claude", "analysis", "malformed"), "claude", "FAIL", "claude_stream_malformed"),
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("description", "A valid description mentioning unknown Skill."),
        ("body_heading", "# A valid heading saying skill not found"),
    ],
)
def test_skill_discovery_accepts_valid_json_metadata_with_unknown_skill_words(
    field: str, value: str
) -> None:
    expected = replace(
        load_expected_skills(
            REPO_ROOT,
            REPO_ROOT / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json",
        )["analysis"],
        **{field: value},
    )
    stdout = json.dumps(
        {
            "name": expected.name,
            "description": expected.description,
            "body_heading": expected.body_heading,
        }
    )

    assert run_skill_discovery._response_diagnostic(stdout, expected) == ""

def test_skill_discovery_metadata_response_passes_only_exact_decoded_scalar_values() -> None:
    expected = replace(
        load_expected_skills(
            REPO_ROOT,
            REPO_ROOT / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json",
        )["analysis"],
        description="first line\nsecond line",
        body_heading="# Preserve this heading  ",
    )
    exact = {
        "name": expected.name,
        "description": expected.description,
        "body_heading": expected.body_heading,
    }
    responses = [
        (exact, ""),
        (
            {**exact, "description": expected.description.replace("\n", " ")},
            "source_metadata_mismatch",
        ),
        (
            {**exact, "body_heading": expected.body_heading.removeprefix("# ")},
            "source_metadata_mismatch",
        ),
        (
            {**exact, "body_heading": expected.body_heading.rstrip()},
            "source_metadata_mismatch",
        ),
    ]

    exact_stdout = json.dumps(exact)
    assert "\\n" in exact_stdout
    assert run_skill_discovery._response_diagnostic(exact_stdout, expected) == ""
    for payload, diagnostic in responses[1:]:
        assert run_skill_discovery._response_diagnostic(json.dumps(payload), expected) == diagnostic


def test_skill_discovery_requires_decoded_response_for_quoted_yaml_scalar(
    tmp_path: Path,
) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text(
        '---\n'
        'name: quoted-metadata\n'
        'description: "quoted YAML description"\n'
        '---\n'
        '# Quoted metadata\n',
        encoding="utf-8",
    )
    name, description, body_heading = run_skill_discovery._source_metadata(skill_file)
    expected = ExpectedSkill(name, description, body_heading, tmp_path, "")
    decoded = {
        "name": expected.name,
        "description": expected.description,
        "body_heading": expected.body_heading,
    }
    literal_quoted = {**decoded, "description": f'"{expected.description}"'}

    assert description == "quoted YAML description"
    assert run_skill_discovery._response_diagnostic(json.dumps(decoded), expected) == ""
    assert (
        run_skill_discovery._response_diagnostic(json.dumps(literal_quoted), expected)
        == "source_metadata_mismatch"
    )


def test_skill_discovery_persists_only_normalized_provider_stderr_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline, fake, _ = _run_fake_discovery(tmp_path, monkeypatch)
    original = fake.__call__
    raw_detail = "unique-provider-stderr-detail"
    raw_token = "sk-proj-unique-provider-token"
    credential_path = tmp_path / "operator" / ".config" / "identity.json"

    def with_provider_stderr(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> DiscoveryProcessResult:
        output = original(argv, cwd=cwd, env=env, timeout=timeout)
        if argv[0] == "fake-omp" and "--skills=analysis" in argv:
            return DiscoveryProcessResult(
                23,
                "",
                f"{raw_detail} token={raw_token} path={credential_path}",
                output.elapsed_sec,
            )
        return output

    result = run_discovery(
        repo_root=baseline.repo_root,
        matrix_path=baseline.repo_root / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json",
        batch_id="skill-discovery-provider-stderr",
        binaries={"claude": "fake-claude", "agent-skills": "fake-agent", "omp": "fake-omp"},
        models=dict(PINNED_SUBSCRIPTION_MODELS),
        timeout_sec=7,
        write_result=True,
        process_runner=with_provider_stderr,
    )
    report = result.discovery_report.read_text(encoding="utf-8")
    record = next(
        record
        for record in result.discovery_records
        if record["runtime"] == "omp" and record["skill"] == "analysis"
    )

    assert record["diagnostic"] == "host_provider_exit"
    assert raw_detail not in report
    assert raw_token not in report
    assert str(credential_path) not in report

def test_skill_discovery_normalizes_oauth_session_refresh_failure_without_raw_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, _ = _run_fake_discovery(
        tmp_path,
        monkeypatch,
        failure=("claude", "analysis", "oauth-session-expired"),
    )
    raw_diagnostic = "Failed to authenticate: OAuth session expired and could not be refreshed"
    report = result.discovery_report.read_text(encoding="utf-8")
    record = next(
        record
        for record in result.discovery_records
        if record["runtime"] == "claude" and record["skill"] == "analysis"
    )

    assert record["verdict"] == "BLOCKED"
    assert record["diagnostic"] == "host_subscription_expired"
    assert raw_diagnostic not in report


def test_skill_discovery_freezes_snapshot_before_canonical_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, fake, _ = _run_fake_discovery(
        tmp_path,
        monkeypatch,
        mutate_canonical_after_install="analysis",
        read_workspace_skills=True,
    )
    frozen_sha256, frozen_metadata = fake.installed_sources["analysis"]
    canonical_skill = result.repo_root / "claude" / "skills" / "analysis" / "SKILL.md"
    reports = (
        result.install_report.read_text(encoding="utf-8")
        + result.discovery_report.read_text(encoding="utf-8")
    )

    assert "name: mutated-after-install" in canonical_skill.read_text(encoding="utf-8")
    assert {
        record["status"] for record in result.install_records if record["skill"] == "analysis"
    } == {"PASS"}
    assert {
        record["verdict"] for record in result.discovery_records if record["skill"] == "analysis"
    } == {"PASS"}
    for runtime in run_skill_discovery.RUNTIMES:
        assert fake.workspace_skills[(runtime, "analysis")] == (frozen_sha256, frozen_metadata)
    for record in result.discovery_records:
        if record["skill"] == "analysis":
            assert record["source_sha256"] == frozen_sha256
            assert (
                record["source_name"],
                record["source_description"],
                record["source_body_heading"],
            ) == frozen_metadata
    assert str(result.repo_root / "claude" / "skills") not in reports
    assert "awf-skill-discovery-" not in reports


def test_skill_discovery_blocks_snapshot_mutated_by_successful_host_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, fake, _ = _run_fake_discovery(
        tmp_path,
        monkeypatch,
        mutate_snapshot_after_probe=("agent-skills", "analysis"),
    )
    report = result.discovery_report.read_text(encoding="utf-8")
    analysis_records = [
        record for record in result.discovery_records if record["skill"] == "analysis"
    ]
    probe_calls = [
        argv
        for argv, _, _ in fake.calls
        if argv[0].startswith("fake-")
        and argv[1:]
        not in (["--version"], ["--help"], ["exec", "--help"])
    ]

    assert len(probe_calls) == len(MATRIX.skills) * len(run_skill_discovery.RUNTIMES)
    assert result.exit_code == 1
    assert {(record["verdict"], record["diagnostic"]) for record in analysis_records} == {
        ("BLOCKED", "skill_snapshot_changed")
    }
    assert "mutated-snapshot-by-host" not in report
    assert "awf-skill-discovery-" not in report


def test_skill_discovery_removes_report_when_snapshot_changes_after_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def configure(fake: _FakeDiscoveryProcess) -> None:
        original_publish = run_skill_discovery._publish_new

        def publish_with_post_link_mutation(
            target: Path,
            content: str,
            *,
            before_publish: Callable[[], None] | None = None,
            after_publish: Callable[[], None] | None = None,
        ) -> None:
            if before_publish is None or after_publish is None:
                original_publish(target, content)
                return
            before_publish()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            snapshot_file = fake.installed_paths["analysis"] / "SKILL.md"
            snapshot_file.write_text(
                snapshot_file.read_text(encoding="utf-8").replace(
                    "name: analysis", "name: mutated-after-link", 1
                ),
                encoding="utf-8",
            )
            try:
                after_publish()
            except Exception:
                target.unlink()
                raise

        monkeypatch.setattr(run_skill_discovery, "_publish_new", publish_with_post_link_mutation)

    result, fake, _ = _run_fake_discovery(tmp_path, monkeypatch, configure=configure)
    report_path = run_skill_discovery.discovery_report_path(
        result.repo_root, "skill-discovery-test"
    )
    probe_calls = [
        argv
        for argv, _, _ in fake.calls
        if argv[0].startswith("fake-")
        and argv[1:]
        not in (["--version"], ["--help"], ["exec", "--help"])
    ]

    assert len(probe_calls) == len(MATRIX.skills) * len(run_skill_discovery.RUNTIMES)
    assert result.exit_code == 1
    assert result.discovery_report is None
    assert not report_path.exists()
    assert {
        (record["verdict"], record["diagnostic"])
        for record in result.discovery_records
        if record["skill"] == "analysis"
    } == {("BLOCKED", "skill_snapshot_changed")}


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