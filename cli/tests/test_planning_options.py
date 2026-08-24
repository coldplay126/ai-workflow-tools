"""Strict planning-options artifact contract tests."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.planning_options import (
    PlanningOptionsError,
    load_planning_options,
    resolve_planning_options_policy,
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
