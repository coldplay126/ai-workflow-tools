"""Strict planning-options artifact contract tests."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import awf.core.planning_options as planning_options

from awf.core.gates import evaluate_planning_options_gate
from awf.core.planning_options import (
    PlanningOptionsError,
    load_planning_options,
    resolve_planning_options_policy,
    seal_planning_options,
    select_planning_option,
)


def _option(
    option_id: str,
    *,
    summary: str = "Use a dual-read rollout",
    affected_work: list[str] | None = None,
    acceptance_delta: str = "Requires shadow parity before cutover",
    work_risks: list[str] | None = None,
    transition_risks: list[str] | None = None,
    rollback_or_exit: str = "Disable new reads and retain the old source of truth",
) -> dict[str, object]:
    return {
        "id": option_id,
        "summary": summary,
        "affected_work": affected_work or ["service", "migration", "observability"],
        "acceptance_delta": acceptance_delta,
        "work_risks": work_risks or ["Additional implementation and test paths"],
        "transition_risks": transition_risks or ["Temporary dual-state reconciliation"],
        "rollback_or_exit": rollback_or_exit,
    }


def _decision(
    decision_id: str = "D-001",
    *,
    options: list[dict[str, object]] | None = None,
    selected_option_id: str | None = None,
    selected_by: str | None = None,
    selected_at: str | None = None,
) -> dict[str, object]:
    return {
        "id": decision_id,
        "question": "Which rollout model should the feature use?",
        "materiality_axes": ["compatibility_migration", "security_slo"],
        "options": options
        or [
            _option("O-001"),
            _option(
                "O-002",
                summary="Replace the source in one cutover",
                affected_work=["service", "runbook"],
                acceptance_delta="Requires a completed cutover rehearsal",
                work_risks=["Less implementation and test coverage"],
                transition_risks=["Rollback requires a service pause"],
                rollback_or_exit="Restore the prior deployment before reopening traffic",
            ),
        ],
        "recommended_option_id": "O-001",
        "recommendation_rationale": "It preserves rollback while proving parity before cutover.",
        "selected_option_id": selected_option_id,
        "selected_by": selected_by,
        "selected_at": selected_at,
    }


def _artifact(
    *,
    status: str = "selection_required",
    decisions: list[dict[str, object]] | None = None,
    selection_history: list[dict[str, object]] | None = None,
    no_decision_reason: str | None = None,
) -> dict[str, object]:
    if status == "no_decision_required":
        decisions = []
        no_decision_reason = (
            no_decision_reason
            or "Repository conventions determine the only viable implementation without changing transition risk."
        )
    return {
        "schema_version": 1,
        "status": status,
        "no_decision_reason": no_decision_reason,
        "decisions": decisions if decisions is not None else [_decision()],
        "selection_history": selection_history or [],
    }


def _artifact_path(root: Path) -> Path:
    artifact_dir = root / ".workflow" / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir / "planning-options.json"


def _write_artifact(root: Path, artifact: dict[str, object]) -> Path:
    path = _artifact_path(root)
    path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    return path


def _assert_code(exc: pytest.ExceptionInfo[PlanningOptionsError], code: str) -> None:
    assert exc.value.code == code


_PLANNING_OPTIONS_GATE_CONDITIONS = [
    "planning_options.artifact",
    "planning_options.shape",
    "planning_options.selection",
    "planning_options.recommendation",
    "planning_options.materiality",
    "planning_options.provenance",
]


def _write_planning_options_manifest(root: Path, required: bool) -> None:
    workflow = root / ".workflow"
    workflow.mkdir(parents=True, exist_ok=True)
    (workflow / "manifest.json").write_text(
        json.dumps({"planning_options": {"required": required}}), encoding="utf-8"
    )


def test_loads_no_decision_record_as_immutable_normalized_canonical_hash(tmp_path: Path):
    artifact = _artifact(status="no_decision_required")
    _write_artifact(tmp_path, artifact)

    loaded = load_planning_options(tmp_path)

    assert loaded.status == "no_decision_required"
    assert loaded.no_decision_reason == artifact["no_decision_reason"]
    assert loaded.decisions == ()
    assert loaded.selection_history == ()
    assert loaded.artifact_hash == hashlib.sha256(
        json.dumps(
            artifact, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()
    with pytest.raises(AttributeError):
        loaded.status = "selected"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda artifact: artifact.__setitem__("unexpected", True), "artifact_invalid"),
        (lambda artifact: artifact.__setitem__("schema_version", True), "artifact_invalid"),
        (lambda artifact: artifact.__setitem__("schema_version", "1"), "artifact_invalid"),
        (lambda artifact: artifact.__setitem__("schema_version", 1.0), "artifact_invalid"),
        (lambda artifact: artifact.__setitem__("status", "pending"), "artifact_invalid"),
        (lambda artifact: artifact.__setitem__("no_decision_reason", "reason"), "artifact_invalid"),
    ],
)
def test_rejects_strict_top_level_schema(tmp_path: Path, mutate, code: str):
    artifact = _artifact()
    mutate(artifact)
    _write_artifact(tmp_path, artifact)

    with pytest.raises(PlanningOptionsError) as exc:
        load_planning_options(tmp_path)

    _assert_code(exc, code)


def test_rejects_duplicate_json_keys(tmp_path: Path):
    path = _artifact_path(tmp_path)
    path.write_text(
        '{"schema_version":1,"schema_version":1,"status":"no_decision_required",'
        '"no_decision_reason":"Only viable approach without transition risk",'
        '"decisions":[],"selection_history":[]}',
        encoding="utf-8",
    )

    with pytest.raises(PlanningOptionsError) as exc:
        load_planning_options(tmp_path)

    _assert_code(exc, "artifact_invalid")

def test_converts_deep_artifact_json_recursion_to_stable_error(tmp_path: Path):
    _artifact_path(tmp_path).write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")

    with pytest.raises(PlanningOptionsError) as exc:
        load_planning_options(tmp_path)

    _assert_code(exc, "artifact_invalid")


def test_converts_deep_manifest_json_recursion_to_stable_error(tmp_path: Path):
    workflow = tmp_path / ".workflow"
    workflow.mkdir()
    (workflow / "manifest.json").write_text(
        "[" * 2000 + "0" + "]" * 2000, encoding="utf-8"
    )

    with pytest.raises(PlanningOptionsError) as exc:
        resolve_planning_options_policy(tmp_path)

    _assert_code(exc, "profile_invalid")


def test_converts_artifact_decoder_recursion_to_stable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_artifact(tmp_path, _artifact())

    def raise_recursion(*args, **kwargs):
        raise RecursionError("decoder recursion")

    monkeypatch.setattr("awf.core.planning_options.json.loads", raise_recursion)

    with pytest.raises(PlanningOptionsError) as exc:
        load_planning_options(tmp_path)

    _assert_code(exc, "artifact_invalid")


def test_converts_manifest_decoder_recursion_to_stable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workflow = tmp_path / ".workflow"
    workflow.mkdir()
    (workflow / "manifest.json").write_text("{}", encoding="utf-8")

    def raise_recursion(*args, **kwargs):
        raise RecursionError("decoder recursion")

    monkeypatch.setattr("awf.core.planning_options.json.loads", raise_recursion)

    with pytest.raises(PlanningOptionsError) as exc:
        resolve_planning_options_policy(tmp_path)

    _assert_code(exc, "profile_invalid")

@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"\xff", "artifact_invalid"),
        (b"{" + b" " * (128 * 1024 + 1), "artifact_invalid"),
    ],
)
def test_rejects_non_utf8_and_bounded_artifact_content(
    tmp_path: Path, content: bytes, code: str
):
    _artifact_path(tmp_path).write_bytes(content)

    with pytest.raises(PlanningOptionsError) as exc:
        load_planning_options(tmp_path)

    _assert_code(exc, code)


@pytest.mark.parametrize("unsafe_target", ["artifact", "artifacts", "workflow"])
def test_rejects_symlinked_workflow_artifact_path(tmp_path: Path, unsafe_target: str):
    outside = tmp_path / "outside"
    outside.mkdir()
    valid = _artifact(status="no_decision_required")
    (outside / "planning-options.json").write_text(json.dumps(valid), encoding="utf-8")

    workflow = tmp_path / ".workflow"
    artifacts = workflow / "artifacts"
    if unsafe_target == "workflow":
        workflow.symlink_to(outside, target_is_directory=True)
    elif unsafe_target == "artifacts":
        workflow.mkdir()
        artifacts.symlink_to(outside, target_is_directory=True)
    else:
        artifacts.mkdir(parents=True)
        (artifacts / "planning-options.json").symlink_to(outside / "planning-options.json")

    with pytest.raises(PlanningOptionsError) as exc:
        load_planning_options(tmp_path)

    _assert_code(exc, "artifact_invalid")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda artifact: artifact["decisions"][0].__setitem__("id", "D-1"),
        lambda artifact: artifact["decisions"][0]["options"][0].__setitem__("id", "O-1"),
        lambda artifact: artifact.__setitem__(
            "decisions", [_decision(), _decision("D-001")]
        ),
        lambda artifact: artifact["decisions"][0].__setitem__(
            "options", [_option("O-001"), _option("O-001")]
        ),
    ],
)
def test_rejects_invalid_or_duplicate_stable_ids(tmp_path: Path, mutate):
    artifact = _artifact()
    mutate(artifact)
    _write_artifact(tmp_path, artifact)

    with pytest.raises(PlanningOptionsError) as exc:
        load_planning_options(tmp_path)

    _assert_code(exc, "artifact_invalid")


@pytest.mark.parametrize("option_count", [1, 4])
def test_requires_two_or_three_options_for_each_material_decision(
    tmp_path: Path, option_count: int
):
    artifact = _artifact()
    artifact["decisions"][0]["options"] = [
        _option(
            f"O-{index:03d}",
            summary=f"Approach {index}",
            affected_work=[f"work {index}"],
            acceptance_delta=f"acceptance {index}",
            work_risks=[f"work risk {index}"],
            transition_risks=[f"transition risk {index}"],
            rollback_or_exit=f"rollback {index}",
        )
        for index in range(1, option_count + 1)
    ]
    artifact["decisions"][0]["recommended_option_id"] = "O-001"
    _write_artifact(tmp_path, artifact)

    with pytest.raises(PlanningOptionsError) as exc:
        load_planning_options(tmp_path)

    _assert_code(exc, "artifact_invalid")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda artifact: artifact["decisions"][0].__setitem__(
            "recommended_option_id", "O-002"
        ),
        lambda artifact: artifact["decisions"][0].__setitem__(
            "materiality_axes", ["routine_library_choice"]
        ),
        lambda artifact: artifact["decisions"][0]["options"][0].__setitem__(
            "work_risks", []
        ),
        lambda artifact: artifact["decisions"][0]["options"][0].__setitem__(
            "transition_risks", []
        ),
        lambda artifact: artifact["decisions"][0]["options"][0].__setitem__(
            "rollback_or_exit", ""
        ),
    ],
)
def test_requires_recommendation_first_and_complete_materiality_details(
    tmp_path: Path, mutate
):
    artifact = _artifact()
    mutate(artifact)
    _write_artifact(tmp_path, artifact)

    with pytest.raises(PlanningOptionsError) as exc:
        load_planning_options(tmp_path)

    _assert_code(exc, "artifact_invalid")


def test_rejects_options_equivalent_after_substantive_normalization(tmp_path: Path):
    artifact = _artifact()
    duplicated = _option(
        "O-002",
        summary=" use, a DUAL-read rollout! ",
        affected_work=["observability", "migration", "service"],
        acceptance_delta="requires shadow parity before cutover",
        work_risks=["additional implementation and test paths"],
        transition_risks=["temporary dual-state reconciliation"],
        rollback_or_exit="disable new reads and retain the old source of truth",
    )
    artifact["decisions"][0]["options"] = [_option("O-001"), duplicated]
    _write_artifact(tmp_path, artifact)

    with pytest.raises(PlanningOptionsError) as exc:
        load_planning_options(tmp_path)

    _assert_code(exc, "artifact_invalid")


def test_validates_selected_records_and_append_only_history(tmp_path: Path):
    selected_at = "2026-08-24T10:00:00Z"
    artifact = _artifact(
        status="selected",
        decisions=[
            _decision(
                selected_option_id="O-002",
                selected_by="steven",
                selected_at=selected_at,
            )
        ],
        selection_history=[
            {
                "decision_id": "D-001",
                "previous_option_id": None,
                "selected_option_id": "O-002",
                "selected_by": "steven",
                "selected_at": selected_at,
                "source": "cli",
            }
        ],
    )
    _write_artifact(tmp_path, artifact)

    loaded = load_planning_options(tmp_path)

    assert loaded.status == "selected"
    assert loaded.decisions[0].selected_option_id == "O-002"
    assert loaded.selection_history[0].source == "cli"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda artifact: artifact["decisions"][0].__setitem__("selected_by", None),
        lambda artifact: artifact["decisions"][0].__setitem__(
            "selected_at", "2026-08-24T10:00:00+00:00"
        ),
        lambda artifact: artifact["selection_history"][0].__setitem__(
            "selected_option_id", "O-003"
        ),
        lambda artifact: artifact["selection_history"][0].__setitem__(
            "selected_at", "2026-08-24T10:01:00Z"
        ),
    ],
)
def test_rejects_invalid_selected_state_and_history(tmp_path: Path, mutate):
    selected_at = "2026-08-24T10:00:00Z"
    artifact = _artifact(
        status="selected",
        decisions=[
            _decision(
                selected_option_id="O-002",
                selected_by="steven",
                selected_at=selected_at,
            )
        ],
        selection_history=[
            {
                "decision_id": "D-001",
                "previous_option_id": None,
                "selected_option_id": "O-002",
                "selected_by": "steven",
                "selected_at": selected_at,
                "source": "cli",
            }
        ],
    )
    mutate(artifact)
    _write_artifact(tmp_path, artifact)

    with pytest.raises(PlanningOptionsError) as exc:
        load_planning_options(tmp_path)

    _assert_code(exc, "artifact_invalid")


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "password=top-secret",
        "access_token=top-secret",
        "bearer top-secret",
        "https://user:password@example.test/path",
        "-----BEGIN PRIVATE KEY-----",
        "raw_data: customer export",
        "raw production records",
    ],
)
def test_rejects_sensitive_or_raw_text_in_artifacts(tmp_path: Path, unsafe_text: str):
    artifact = _artifact()
    artifact["decisions"][0]["options"][0]["summary"] = unsafe_text
    _write_artifact(tmp_path, artifact)

    with pytest.raises(PlanningOptionsError) as exc:
        load_planning_options(tmp_path)

    _assert_code(exc, "artifact_invalid")

def test_rejects_unsafe_markdown_uri_in_artifact_text(tmp_path: Path):
    artifact = _artifact()
    artifact["decisions"][0]["options"][0]["summary"] = (
        "[runbook](javascript:alert(1))"
    )
    _write_artifact(tmp_path, artifact)

    with pytest.raises(PlanningOptionsError) as exc:
        load_planning_options(tmp_path)

    _assert_code(exc, "artifact_invalid")

@pytest.mark.parametrize(
    "unsafe_text",
    [
        "java&#x73;cript:alert(1)",
        "java&#115;cript:alert(1)",
        r"javascript\:alert(1)",
        "password&#x3D;top-secret",
        "java&amp;#x73;cript:alert(1)",
    ],
)
def test_rejects_entity_or_commonmark_obfuscated_unsafe_text(
    tmp_path: Path, unsafe_text: str
):
    artifact = _artifact()
    artifact["decisions"][0]["options"][0]["summary"] = unsafe_text
    _write_artifact(tmp_path, artifact)

    with pytest.raises(PlanningOptionsError) as exc:
        load_planning_options(tmp_path)

    _assert_code(exc, "artifact_invalid")

@pytest.mark.parametrize(
    ("field", "unsafe_text"),
    [("summary", "\ud800"), ("question", "\udfff")],
)
def test_converts_lone_surrogate_text_to_stable_error(
    tmp_path: Path, field: str, unsafe_text: str
):
    artifact = _artifact()
    if field == "summary":
        artifact["decisions"][0]["options"][0][field] = unsafe_text
    else:
        artifact["decisions"][0][field] = unsafe_text
    _artifact_path(tmp_path).write_text(json.dumps(artifact), encoding="utf-8")

    with pytest.raises(PlanningOptionsError) as exc:
        load_planning_options(tmp_path)

    _assert_code(exc, "artifact_invalid")


def test_accepts_three_materially_distinct_options(tmp_path: Path):
    artifact = _artifact()
    artifact["decisions"][0]["options"].append(
        _option(
            "O-003",
            summary="Stage the cutover behind a feature flag",
            affected_work=["service", "feature flag"],
            acceptance_delta="Requires an operator-visible staged rollout",
            work_risks=["Additional rollout coordination"],
            transition_risks=["Flag state must be preserved during rollback"],
            rollback_or_exit="Disable the flag and retain the previous read path",
        )
    )
    _write_artifact(tmp_path, artifact)

    loaded = load_planning_options(tmp_path)

    assert tuple(option.id for option in loaded.decisions[0].options) == (
        "O-001",
        "O-002",
        "O-003",
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda artifact: artifact["decisions"][0]["options"][0].pop("summary"),
        lambda artifact: artifact["decisions"][0].__setitem__("extra", True),
        lambda artifact: artifact.__setitem__("no_decision_reason", "unexpected"),
    ],
)
def test_rejects_exact_nested_schema_and_selection_required_invariants(
    tmp_path: Path, mutate
):
    artifact = _artifact()
    mutate(artifact)
    _write_artifact(tmp_path, artifact)

    with pytest.raises(PlanningOptionsError) as exc:
        load_planning_options(tmp_path)

    _assert_code(exc, "artifact_invalid")


@pytest.mark.parametrize(
    "artifact",
    [
        {
            "schema_version": 1,
            "status": "no_decision_required",
            "no_decision_reason": None,
            "decisions": [],
            "selection_history": [],
        },
        {
            "schema_version": 1,
            "status": "no_decision_required",
            "no_decision_reason": "Only viable approach without transition risk",
            "decisions": [_decision()],
            "selection_history": [],
        },
        _artifact(status="selected"),
    ],
)
def test_rejects_status_specific_invariants(tmp_path: Path, artifact: dict[str, object]):
    _write_artifact(tmp_path, artifact)

    with pytest.raises(PlanningOptionsError) as exc:
        load_planning_options(tmp_path)

    _assert_code(exc, "artifact_invalid")


def test_accepts_monotonic_append_only_selection_history_chain(tmp_path: Path):
    artifact = _artifact(
        status="selected",
        decisions=[
            _decision(
                selected_option_id="O-002",
                selected_by="mira",
                selected_at="2026-08-24T10:01:00Z",
            )
        ],
        selection_history=[
            {
                "decision_id": "D-001",
                "previous_option_id": None,
                "selected_option_id": "O-001",
                "selected_by": "steven",
                "selected_at": "2026-08-24T10:00:00Z",
                "source": "cli",
            },
            {
                "decision_id": "D-001",
                "previous_option_id": "O-001",
                "selected_option_id": "O-002",
                "selected_by": "mira",
                "selected_at": "2026-08-24T10:01:00Z",
                "source": "cli",
            },
        ],
    )
    _write_artifact(tmp_path, artifact)

    loaded = load_planning_options(tmp_path)

    assert len(loaded.selection_history) == 2
    assert loaded.selection_history[-1].previous_option_id == "O-001"


def test_rejects_nonmonotonic_selection_history_chain(tmp_path: Path):
    artifact = _artifact(
        status="selected",
        decisions=[
            _decision(
                selected_option_id="O-002",
                selected_by="mira",
                selected_at="2026-08-24T10:00:00Z",
            )
        ],
        selection_history=[
            {
                "decision_id": "D-001",
                "previous_option_id": None,
                "selected_option_id": "O-001",
                "selected_by": "steven",
                "selected_at": "2026-08-24T10:01:00Z",
                "source": "cli",
            },
            {
                "decision_id": "D-001",
                "previous_option_id": "O-001",
                "selected_option_id": "O-002",
                "selected_by": "mira",
                "selected_at": "2026-08-24T10:00:00Z",
                "source": "cli",
            },
        ],
    )
    _write_artifact(tmp_path, artifact)

    with pytest.raises(PlanningOptionsError) as exc:
        load_planning_options(tmp_path)

    _assert_code(exc, "artifact_invalid")


@pytest.mark.parametrize("unsafe_text", ["x" * 4097, "<script>alert(1)</script>"])
def test_rejects_oversized_or_raw_html_text(tmp_path: Path, unsafe_text: str):
    artifact = _artifact()
    artifact["decisions"][0]["options"][0]["summary"] = unsafe_text
    _write_artifact(tmp_path, artifact)

    with pytest.raises(PlanningOptionsError) as exc:
        load_planning_options(tmp_path)

    _assert_code(exc, "artifact_invalid")


def test_legacy_manifest_without_profile_validates_present_artifact(tmp_path: Path):
    workflow = tmp_path / ".workflow"
    workflow.mkdir()
    (workflow / "manifest.json").write_text(
        json.dumps({"version": "1.0.0"}), encoding="utf-8"
    )
    _write_artifact(tmp_path, _artifact(status="no_decision_required"))

    policy = resolve_planning_options_policy(tmp_path)

    assert policy.required is False
    assert policy.status == "no_decision_required"
    assert policy.artifact is not None

    _write_artifact(tmp_path, {"schema_version": 1})
    with pytest.raises(PlanningOptionsError) as exc:
        resolve_planning_options_policy(tmp_path)
    _assert_code(exc, "artifact_invalid")


def test_missing_manifest_and_artifact_is_legacy_not_required(tmp_path: Path):
    policy = resolve_planning_options_policy(tmp_path)

    assert policy.status == "legacy_not_required"
    assert policy.required is False
    assert policy.artifact is None

def test_manifest_without_planning_profile_and_artifact_is_legacy_not_required(
    tmp_path: Path,
):
    workflow = tmp_path / ".workflow"
    workflow.mkdir()
    (workflow / "manifest.json").write_text(
        json.dumps({"version": "1.0.0"}), encoding="utf-8"
    )

    policy = resolve_planning_options_policy(tmp_path)

    assert policy.status == "legacy_not_required"
    assert policy.required is False
    assert policy.artifact is None


def test_explicit_profile_opt_out_without_artifact_is_not_required(tmp_path: Path):
    workflow = tmp_path / ".workflow"
    workflow.mkdir()
    (workflow / "manifest.json").write_text(
        json.dumps({"planning_options": {"required": False}}), encoding="utf-8"
    )

    policy = resolve_planning_options_policy(tmp_path)

    assert policy.status == "not_required"
    assert policy.required is False
    assert policy.artifact is None


def test_manifest_profile_requires_artifact_and_loads_it_when_present(tmp_path: Path):
    workflow = tmp_path / ".workflow"
    workflow.mkdir()
    (workflow / "manifest.json").write_text(
        json.dumps({"planning_options": {"required": True}}), encoding="utf-8"
    )

    policy = resolve_planning_options_policy(tmp_path)

    assert policy.status == "required"
    assert policy.required is True
    assert policy.artifact is None

    _write_artifact(tmp_path, _artifact(status="no_decision_required"))
    loaded_policy = resolve_planning_options_policy(tmp_path)
    assert loaded_policy.status == "no_decision_required"
    assert loaded_policy.artifact is not None


@pytest.mark.parametrize(
    "profile",
    [
        {"planning_options": {}},
        {"planning_options": {"required": "true"}},
        {"planning_options": {"required": True, "extra": False}},
    ],
)
def test_rejects_malformed_planning_options_profile(tmp_path: Path, profile: dict[str, object]):
    workflow = tmp_path / ".workflow"
    workflow.mkdir()
    (workflow / "manifest.json").write_text(json.dumps(profile), encoding="utf-8")

    with pytest.raises(PlanningOptionsError) as exc:
        resolve_planning_options_policy(tmp_path)

    _assert_code(exc, "profile_invalid")


def test_profile_opt_out_still_validates_present_artifact(tmp_path: Path):
    workflow = tmp_path / ".workflow"
    workflow.mkdir()
    (workflow / "manifest.json").write_text(
        json.dumps({"planning_options": {"required": False}}), encoding="utf-8"
    )
    _write_artifact(tmp_path, _artifact())

    policy = resolve_planning_options_policy(tmp_path)

    assert policy.status == "selection_required"
    assert policy.required is False
    assert policy.artifact is not None

    _write_artifact(tmp_path, {"schema_version": 1})
    with pytest.raises(PlanningOptionsError) as exc:
        resolve_planning_options_policy(tmp_path)
    _assert_code(exc, "artifact_invalid")


def test_planning_options_gate_covers_required_legacy_and_selection_states(
    tmp_path: Path,
):
    selected_at = "2026-08-24T10:00:00Z"
    selected = _artifact(
        status="selected",
        decisions=[
            _decision(
                selected_option_id="O-002",
                selected_by="steven",
                selected_at=selected_at,
            )
        ],
        selection_history=[
            {
                "decision_id": "D-001",
                "previous_option_id": None,
                "selected_option_id": "O-002",
                "selected_by": "steven",
                "selected_at": selected_at,
                "source": "cli",
            }
        ],
    )
    cases = [
        ("legacy", None, None, True, "status=legacy_not_required"),
        ("required_missing", True, None, False, "artifact_missing"),
        ("selection_required", True, _artifact(), False, "decision_selection_required"),
        ("selected", True, selected, False, "status=selected"),
        (
            "no_decision_required",
            True,
            _artifact(status="no_decision_required"),
            False,
            "status=no_decision_required",
        ),
    ]

    for name, required, artifact, expected_passed, selection_detail in cases:
        root = tmp_path / name
        root.mkdir()
        if required is not None:
            _write_planning_options_manifest(root, required)
        if artifact is not None:
            _write_artifact(root, artifact)

        passed, evaluations = evaluate_planning_options_gate(root)
        conditions = [item["condition"] for item in evaluations]
        checks = {item["condition"]: item for item in evaluations}

        assert conditions == _PLANNING_OPTIONS_GATE_CONDITIONS
        assert passed is expected_passed
        assert checks["planning_options.selection"]["detail"] == selection_detail


def test_planning_options_gate_uses_fixed_sanitized_detail_for_malformed_artifact(
    tmp_path: Path,
):
    _write_planning_options_manifest(tmp_path, True)
    _write_artifact(tmp_path, {"schema_version": 1, "password": "top-secret"})

    passed, evaluations = evaluate_planning_options_gate(tmp_path)

    assert not passed
    conditions = [item["condition"] for item in evaluations]
    assert conditions == _PLANNING_OPTIONS_GATE_CONDITIONS
    assert {item["detail"] for item in evaluations} == {"artifact_invalid"}

def test_select_option_appends_one_history_entry_and_reuses_an_existing_selection(
    tmp_path: Path,
):
    _write_artifact(tmp_path, _artifact())

    selected = select_planning_option(tmp_path, "D-001", "O-002", "operator")
    reused = select_planning_option(tmp_path, "D-001", "O-002", "another-operator")

    assert set(selected.__dataclass_fields__) == {
        "decision_id",
        "option_id",
        "status",
        "changed",
        "previous_hash",
        "current_hash",
    }
    assert selected.decision_id == "D-001"
    assert selected.option_id == "O-002"
    assert selected.status == "selected"
    assert selected.changed is True
    assert selected.previous_hash != selected.current_hash
    assert reused.changed is False
    assert reused.previous_hash == reused.current_hash
    loaded = load_planning_options(tmp_path)
    assert loaded.status == "selected"
    assert loaded.decisions[0].selected_option_id == "O-002"
    assert len(loaded.selection_history) == 1
    assert loaded.selection_history[0].previous_option_id is None
    assert loaded.selection_history[0].selected_by == "operator"


@pytest.mark.parametrize("unsafe_path", ["artifact_symlink", "artifact_hardlink", "lock_hardlink"])
def test_select_option_rejects_unsafe_artifact_and_lock_links(
    tmp_path: Path, unsafe_path: str
):
    artifact_path = _write_artifact(tmp_path, _artifact())
    outside = tmp_path / "outside.json"
    original = artifact_path.read_text(encoding="utf-8")
    outside.write_text(original, encoding="utf-8")

    if unsafe_path == "artifact_symlink":
        artifact_path.unlink()
        artifact_path.symlink_to(outside)
    elif unsafe_path == "artifact_hardlink":
        artifact_path.unlink()
        os.link(outside, artifact_path)
    else:
        os.link(
            outside,
            artifact_path.with_name("planning-options.json.lock"),
        )

    with pytest.raises(PlanningOptionsError) as exc:
        select_planning_option(tmp_path, "D-001", "O-002", "operator")

    _assert_code(exc, "artifact_invalid")
    assert outside.read_text(encoding="utf-8") == original


def test_select_option_serializes_concurrent_reuse_without_duplicate_history(
    tmp_path: Path,
):
    _write_artifact(tmp_path, _artifact())

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: select_planning_option(tmp_path, "D-001", "O-002", "operator"),
                range(2),
            )
        )

    loaded = load_planning_options(tmp_path)
    assert sum(result.changed for result in results) == 1
    assert len(loaded.selection_history) == 1


def test_select_option_rejects_a_symlinked_repository_root(tmp_path: Path):
    _write_artifact(tmp_path, _artifact())
    root_link = tmp_path.parent / f"{tmp_path.name}-selection-root"
    root_link.symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(PlanningOptionsError) as exc:
        select_planning_option(root_link, "D-001", "O-002", "operator")

    _assert_code(exc, "repo_root_invalid")


def test_select_option_rejects_owner_mismatch_without_mutating_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    artifact_path = _write_artifact(tmp_path, _artifact())
    original = artifact_path.read_text(encoding="utf-8")
    current_uid = os.getuid()
    monkeypatch.setattr(planning_options.os, "getuid", lambda: current_uid + 1)

    with pytest.raises(PlanningOptionsError) as exc:
        select_planning_option(tmp_path, "D-001", "O-002", "operator")

    _assert_code(exc, "artifact_invalid")
    assert artifact_path.read_text(encoding="utf-8") == original


def test_select_option_rejects_preexisting_random_temporary_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    artifact_path = _write_artifact(tmp_path, _artifact())
    outside = tmp_path / "outside.json"
    outside.write_text("outside remains intact", encoding="utf-8")
    monkeypatch.setattr(planning_options.secrets, "token_hex", lambda _: "fixed")
    temporary = artifact_path.with_name(".planning-options.json.fixed.tmp")
    os.link(outside, temporary)

    with pytest.raises(PlanningOptionsError) as exc:
        select_planning_option(tmp_path, "D-001", "O-002", "operator")

    _assert_code(exc, "artifact_invalid")
    assert outside.read_text(encoding="utf-8") == "outside remains intact"


@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_select_option_cleans_its_temporary_after_atomic_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
):
    artifact_path = _write_artifact(tmp_path, _artifact())
    original = artifact_path.read_text(encoding="utf-8")

    def fail(*_args, **_kwargs):
        raise OSError("injected")

    monkeypatch.setattr(planning_options.os, failure, fail)
    with pytest.raises(OSError):
        select_planning_option(tmp_path, "D-001", "O-002", "operator")

    assert artifact_path.read_text(encoding="utf-8") == original
    assert not list(artifact_path.parent.glob(".planning-options.json.*.tmp"))


def test_select_option_cleans_temporary_after_directory_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    artifact_path = _write_artifact(tmp_path, _artifact())
    original_fsync = planning_options.os.fsync
    calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("injected artifact directory fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(planning_options.os, "fsync", fail_directory_fsync)
    with pytest.raises(OSError):
        select_planning_option(tmp_path, "D-001", "O-002", "operator")

    assert load_planning_options(tmp_path).status == "selected"
    assert not list(artifact_path.parent.glob(".planning-options.json.*.tmp"))


def test_reconciliation_journal_failure_leaves_the_prior_artifact_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    artifact_path = _write_artifact(tmp_path, _artifact())
    original = artifact_path.read_text(encoding="utf-8")
    monkeypatch.setattr(
        planning_options,
        "_write_reconciliation_marker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected")),
    )

    with pytest.raises(OSError):
        select_planning_option(tmp_path, "D-001", "O-002", "operator")

    assert artifact_path.read_text(encoding="utf-8") == original

def _write_plan_artifacts(root: Path) -> dict[str, str]:
    artifacts = root / ".workflow" / "artifacts"
    contents = {
        "constitution.md": "# Constitution\n",
        "spec.md": "# Spec\n",
        "plan.md": "# Plan\n",
        "tasks.md": "- [ ] T01 Work\n",
        "test-criteria.md": "# Criteria\n",
    }
    for name, content in contents.items():
        (artifacts / name).write_text(content, encoding="utf-8")
    return contents


def test_seal_binds_selected_options_and_exactly_five_plan_artifacts(
    tmp_path: Path,
):
    _write_planning_options_manifest(tmp_path, True)
    _write_artifact(tmp_path, _artifact())
    contents = _write_plan_artifacts(tmp_path)

    with pytest.raises(PlanningOptionsError) as exc:
        seal_planning_options(tmp_path)
    _assert_code(exc, "selection_required")

    select_planning_option(tmp_path, "D-001", "O-001", "operator")
    sealed = seal_planning_options(tmp_path)
    marker = json.loads(
        (tmp_path / ".workflow" / "artifacts" / "planning-provenance.json").read_text(
            encoding="utf-8"
        )
    )

    assert sealed.planning_options_hash == load_planning_options(tmp_path).artifact_hash
    assert marker == {
        "schema_version": 1,
        "planning_options_hash": sealed.planning_options_hash,
        "artifacts": {
            name: hashlib.sha256(content.encode("utf-8")).hexdigest()
            for name, content in contents.items()
        },
    }
    assert len(marker["artifacts"]) == 5

    passed, evaluations = evaluate_planning_options_gate(tmp_path)
    assert passed
    assert next(
        item
        for item in evaluations
        if item["condition"] == "planning_options.provenance"
    )["passed"]

    select_planning_option(tmp_path, "D-001", "O-002", "operator")
    passed, evaluations = evaluate_planning_options_gate(tmp_path)
    assert not passed
    assert next(
        item
        for item in evaluations
        if item["condition"] == "planning_options.provenance"
    )["detail"] == "provenance_changed"

    seal_planning_options(tmp_path)
    (tmp_path / ".workflow" / "artifacts" / "plan.md").write_text(
        "# Regenerated plan\n", encoding="utf-8"
    )
    passed, evaluations = evaluate_planning_options_gate(tmp_path)
    assert not passed
    assert next(
        item
        for item in evaluations
        if item["condition"] == "planning_options.provenance"
    )["detail"] == "provenance_changed"


def test_next_selection_timestamp_uses_canonical_strictly_monotonic_microseconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 24, 10, 0, 0, 123456, tzinfo=timezone.utc)

    artifact = _artifact(
        status="selected",
        decisions=[
            _decision(
                selected_option_id="O-001",
                selected_by="operator",
                selected_at="2026-08-24T10:00:00.123456Z",
            )
        ],
        selection_history=[
            {
                "decision_id": "D-001",
                "previous_option_id": None,
                "selected_option_id": "O-001",
                "selected_by": "operator",
                "selected_at": "2026-08-24T10:00:00.123456Z",
                "source": "cli",
            }
        ],
    )
    _write_artifact(tmp_path, artifact)
    monkeypatch.setattr(planning_options, "datetime", FrozenDatetime)

    timestamp = planning_options._next_selection_timestamp(
        load_planning_options(tmp_path).selection_history
    )

    assert timestamp == "2026-08-24T10:00:00.123457Z"
