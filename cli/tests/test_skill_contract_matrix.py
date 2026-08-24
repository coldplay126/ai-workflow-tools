from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

import pytest
from jsonschema import Draft202012Validator

from awf.core.skill_pressure import (
    HIGH_RISK_SKILLS,
    REQUIRED_CATEGORIES,
    SUPPORTED_RUNTIMES,
    MatrixError,
    load_skill_matrix,
)
from awf.core.state import PHASE_GATE, PHASE_ORDER


REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX_PATH = REPO_ROOT / "cli" / "tests" / "fixtures" / "skill-validation-matrix.v1.json"
SKILLS_ROOT = REPO_ROOT / "claude" / "skills"
AGENT_CARD_ROOT = SKILLS_ROOT / "wf-orchestrator" / "templates"
EXPECTED_AGENT_CARD_STEMS = {
    "plan",
    "review",
    "approve",
    "impl",
    "verify",
    "test",
    "done",
}
EXPECTED_SUCCESSOR_BY_PHASE = {
    "plan": "review",
    "review": "approve",
    "approve": "impl",
    "impl": "verify",
    "verify": "test",
    "test": "done",
    "done": None,
}
EXPECTED_NULLABLE_AGENT_CARD_PATHS = {
    "approve.gate.on_fail.rejected.next_phase",
    "done.gate.id",
    "done.gate.on_pass.next_phase",
    "verify.input.optional_context.1.path",
}
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)
YAML_NON_STRING_SCALAR_RE = re.compile(
    r"""(?ix)
    (?:
        true | false | null | ~ | yes | no | on | off | \.(?:nan|inf)
        | 0[xX][0-9a-f_]+ | 0[oO][0-7_]+ | 0[bB][01_]+
        | [-+]?(?:[0-9][0-9_]*(?:\.[0-9_]*)?|\.[0-9_]+)(?:[eE][-+]?[0-9_]+)?
    )
    """
)
EXPECTED_SKILLS = {
    "analysis",
    "multi-agent",
    "phase-approve",
    "phase-done",
    "phase-impl",
    "phase-plan",
    "phase-review",
    "phase-test",
    "phase-verify",
    "release-worktree-lifecycle",
    "wf",
    "wf-discovery",
    "wf-orchestrator",
    "wf-reset",
    "wf-status",
}


def _matrix_payload() -> dict[str, object]:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _write_payload(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _top_level_value(frontmatter: str, key: str) -> str | None:
    value: str | None = None
    for line in frontmatter.splitlines():
        if line.startswith((" ", "\t")) or ":" not in line:
            continue
        raw_key, raw_value = line.split(":", maxsplit=1)
        if raw_key.strip() != key:
            continue
        assert value is None, f"duplicate top-level contract key: {key}"
        value = _condition_string(raw_value, f"{key} metadata")
    return value


def _condition_string(raw_value: str, label: str) -> str:
    raw_value = raw_value.strip()
    assert raw_value, "condition values must be nonempty"
    if raw_value[0] in {"'", '"'}:
        quote = raw_value[0]
        assert len(raw_value) > 1 and raw_value[-1] == quote, (
            f"{label} must be a string"
        )
        value = raw_value[1:-1]
        assert value.strip(), "condition values must be nonempty"
        return value

    assert raw_value[-1] not in {"'", '"'}, f"{label} must be a string"
    assert raw_value[0] not in "[{", f"{label} must be a string"
    assert not YAML_NON_STRING_SCALAR_RE.fullmatch(raw_value), (
        f"{label} must be a string"
    )
    assert not re.match(r"[^:\s][^:]*:\s", raw_value), (
        f"{label} must be a string"
    )
    return raw_value


def _condition_values(frontmatter: str, key: str) -> tuple[str, ...]:
    assert key in {"trigger", "skip"}
    matches = list(re.finditer(r"(?m)^conditions:[ \t]*(.*)$", frontmatter))
    if not matches:
        return ()

    assert len(matches) == 1, "conditions must be declared once"
    assert not matches[0].group(1).strip(), "conditions must be a nested mapping"

    conditions: dict[str, list[str]] = {}
    current_key: str | None = None
    for line in frontmatter[matches[0].end() :].splitlines():
        if not line:
            continue
        if not line.startswith((" ", "\t")):
            break

        condition = re.fullmatch(r"  (trigger|skip):(?:\s*(.*))?", line)
        if condition is not None:
            condition_key, inline = condition.groups()
            assert condition_key not in conditions, f"duplicate condition key: {condition_key}"
            conditions[condition_key] = []
            current_key = condition_key
            if inline and inline.strip():
                value = _condition_string(inline, "condition value")
                conditions[condition_key].append(value)
                current_key = None
            continue

        item = re.fullmatch(r"    -\s*(.*)", line)
        if item is not None:
            assert current_key is not None, "condition list item has no owning key"
            value = _condition_string(item.group(1), "condition list item")
            conditions[current_key].append(value)
            continue

        raise AssertionError(f"unexpected nested conditions content: {line}")

    for values in conditions.values():
        assert values, "condition values must be nonempty"
    return tuple(conditions.get(key, ()))


def test_matrix_locks_exact_first_party_skill_inventory() -> None:
    matrix = load_skill_matrix(MATRIX_PATH)
    source_names = {
        path.parent.name for path in SKILLS_ROOT.glob("*/SKILL.md")
    }

    assert set(matrix.skills) == EXPECTED_SKILLS
    assert source_names == EXPECTED_SKILLS
    assert {name for name, case in matrix.skills.items() if case.high_risk} == HIGH_RISK_SKILLS
    assert "chat" not in matrix.skills


def test_every_skill_declares_all_categories_and_runtimes() -> None:
    matrix = load_skill_matrix(MATRIX_PATH)

    for case in matrix.skills.values():
        assert set(case.categories) == REQUIRED_CATEGORIES
        assert set(case.runtimes) == SUPPORTED_RUNTIMES
        assert case.severity in {"critical", "important", "minor"}
        assert case.scenario.skill == case.name

def test_analysis_dry_run_only_contract_matches_field_matrix() -> None:
    ready_command = "awf ready --gate analysis --repo-root . --json"
    dry_run_command = (
        "awf analyze api auth --repo-root . --dry-run --output-format json"
    )
    source = (SKILLS_ROOT / "analysis" / "SKILL.md").read_text(encoding="utf-8")
    scenario = load_skill_matrix(MATRIX_PATH).skills["analysis"].scenario

    assert source.index(ready_command) < source.index(
        "awf analyze {service} {unit} --repo-root . --dry-run --output-format json"
    )
    assert re.search(
        r'`decision: "dry_run_only"`.*?shared decision.*?`decision: "STOP"`.*?'
        r"reason code.*?`dry_run_only`",
        source,
        re.DOTALL,
    )
    assert scenario.expected.decisions == ("STOP",)
    assert scenario.expected.allowed_commands == (ready_command, dry_run_command)
    assert scenario.expected.required_reason_codes == ("dry_run_only",)
    assert scenario.expected.required_commands == (ready_command, dry_run_command)
    assert scenario.expected.ordered_commands == (ready_command, dry_run_command)


def test_matrix_rejects_unknown_category(tmp_path: Path) -> None:
    path = tmp_path / "matrix.json"
    path.write_text(
        '{"schema":"awf_skill_validation_matrix_v1","skills":['
        '{"name":"x","type":"test","entry_kind":"conditions",'
        '"high_risk":false,"categories":["unknown"],'
        '"runtimes":["claude","agent-skills","omp"],'
        '"scenario":{"id":"x.test","skill":"x","task":"x",'
        '"expected":{"decisions":["REPORT"]}}}]}'
    )

    with pytest.raises(MatrixError, match="categories"):
        load_skill_matrix(path)


@pytest.mark.parametrize(
    ("decisions", "reason"),
    [
        (["UNKNOWN"], "unknown decision"),
        (["REPORT", "REPORT"], "duplicated decision"),
    ],
)
def test_matrix_rejects_unknown_or_duplicated_decisions(
    tmp_path: Path,
    decisions: list[str],
    reason: str,
) -> None:
    payload = _matrix_payload()
    payload["skills"][0]["scenario"]["expected"]["decisions"] = decisions
    path = tmp_path / "matrix.json"
    _write_payload(path, payload)

    with pytest.raises(MatrixError, match=reason):
        load_skill_matrix(path)


@pytest.mark.parametrize(
    ("field", "mutation", "value"),
    [
        ("type", "missing", None),
        ("type", "non-string", 1),
        ("type", "empty", ""),
        ("entry_kind", "missing", None),
        ("entry_kind", "non-string", 1),
        ("entry_kind", "empty", ""),
        ("scenario.id", "missing", None),
        ("scenario.id", "non-string", 1),
        ("scenario.id", "empty", ""),
        ("scenario.task", "missing", None),
        ("scenario.task", "non-string", 1),
        ("scenario.task", "empty", ""),
    ],
)
def test_matrix_rejects_missing_non_string_or_empty_required_scalars(
    tmp_path: Path,
    field: str,
    mutation: str,
    value: object,
) -> None:
    payload = _matrix_payload()
    case = payload["skills"][0]
    target = case["scenario"] if field.startswith("scenario.") else case
    key = field.rsplit(".", maxsplit=1)[-1]
    if mutation == "missing":
        target.pop(key)
    else:
        target[key] = value
    path = tmp_path / "matrix.json"
    _write_payload(path, payload)

    with pytest.raises(MatrixError, match=key):
        load_skill_matrix(path)


def test_matrix_rejects_duplicate_scenario_ids(tmp_path: Path) -> None:
    payload = _matrix_payload()
    payload["skills"][1]["scenario"]["id"] = payload["skills"][0]["scenario"]["id"]
    path = tmp_path / "matrix.json"
    _write_payload(path, payload)

    with pytest.raises(MatrixError, match="scenario id"):
        load_skill_matrix(path)


def test_matrix_skills_mapping_is_read_only() -> None:
    matrix = load_skill_matrix(MATRIX_PATH)

    with pytest.raises(TypeError):
        matrix.skills["chat"] = matrix.skills["analysis"]  # type: ignore[index]


@pytest.mark.parametrize(
    ("frontmatter", "message"),
    [
        ("conditions:\n  trigger: []\n  skip: valid", "condition value must be a string"),
        ("conditions:\n  trigger: valid\n  skip: {}", "condition value must be a string"),
        (
            "conditions:\n  trigger:\n    - \n  skip: valid",
            "condition values must be nonempty",
        ),
        (
            "conditions:\n  trigger:\n    - []\n  skip: valid",
            "condition list item must be a string",
        ),
        (
            "conditions:\n  trigger:\n    - {}\n  skip: valid",
            "condition list item must be a string",
        ),
        (
            "conditions:\n  trigger:\n    - key: value\n  skip: valid",
            "condition list item must be a string",
        ),
        (
            "conditions:\n  trigger:\n    - valid\n    skip:\n      - invalid",
            "unexpected nested conditions content",
        ),
    ],
)
def test_condition_values_reject_invalid_nested_shapes(
    frontmatter: str, message: str
) -> None:
    with pytest.raises(AssertionError, match=message):
        _condition_values(frontmatter, "trigger")


@pytest.mark.parametrize(
    "value",
    ["true", "TRUE", "false", "False", "null", "NULL", "~", "42", "-3.14", "1e3"],
)
@pytest.mark.parametrize(
    "frontmatter_template",
    [
        "conditions:\n  trigger: {value}\n  skip: valid",
        "conditions:\n  trigger:\n    - {value}\n  skip: valid",
    ],
)
def test_condition_values_reject_unquoted_yaml_scalars(
    value: str, frontmatter_template: str
) -> None:
    with pytest.raises(AssertionError, match="condition (value|list item) must be a string"):
        _condition_values(frontmatter_template.format(value=value), "trigger")


@pytest.mark.parametrize(
    "frontmatter",
    [
        'conditions:\n  trigger: "key: value"\n  skip: valid',
        'conditions:\n  trigger:\n    - "key: value"\n  skip: valid',
    ],
)
def test_condition_values_accept_quoted_scalars_with_colons(frontmatter: str) -> None:
    assert _condition_values(frontmatter, "trigger") == ("key: value",)


def test_every_skill_frontmatter_identity_and_conditions_are_semantic() -> None:
    matrix = load_skill_matrix(MATRIX_PATH)
    for name, case in sorted(matrix.skills.items()):
        path = SKILLS_ROOT / name / "SKILL.md"
        match = FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
        assert match is not None
        frontmatter = match.group(1)
        assert _top_level_value(frontmatter, "name") == name
        assert re.fullmatch(r"\d+\.\d+\.\d+", _top_level_value(frontmatter, "version") or "")
        assert _top_level_value(frontmatter, "type") == case.type
        triggers = _condition_values(frontmatter, "trigger")
        skips = _condition_values(frontmatter, "skip")
        assert triggers, f"{name}: missing trigger"
        assert skips, f"{name}: missing skip"
        assert set(triggers).isdisjoint(skips)
        if case.entry_kind == "slash":
            assert any(f"/{name}" in trigger for trigger in triggers)
        if name == "wf-discovery":
            assert "an active workflow pipeline already owns the request" in skips
        if name == "release-worktree-lifecycle":
            assert any(
                "managed deployment worktree creation or reuse" in trigger
                for trigger in triggers
            )


def _load_agent_cards() -> dict[str, object]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((AGENT_CARD_ROOT / "agent-cards").glob("*.json"))
    }


def _agent_card_schema_errors(cards: dict[str, object]) -> list[str]:
    schema = json.loads((AGENT_CARD_ROOT / "agent-card.schema.json").read_text())
    validator = Draft202012Validator(schema)
    invalid: list[str] = []
    for name, card in sorted(cards.items()):
        for error in validator.iter_errors(card):
            location = ".".join(str(part) for part in error.path)
            invalid.append(f"{name}.json:{location}: {error.message}")
    return invalid


def _assert_agent_cards_match_declared_schema(cards: dict[str, object]) -> None:
    assert _agent_card_schema_errors(cards) == []


def _null_paths(value: object, path: tuple[str, ...] = ()) -> set[str]:
    if value is None:
        return {".".join(path)}
    if isinstance(value, dict):
        return {
            null_path
            for key, nested_value in value.items()
            for null_path in _null_paths(nested_value, (*path, str(key)))
        }
    if isinstance(value, list):
        return {
            null_path
            for index, nested_value in enumerate(value)
            for null_path in _null_paths(nested_value, (*path, str(index)))
        }
    return set()


def _assert_agent_card_state_machine(cards: dict[str, object]) -> None:
    assert set(cards) == EXPECTED_AGENT_CARD_STEMS
    assert {
        phase: cards[phase]["gate"]["on_pass"]["next_phase"]
        for phase in EXPECTED_AGENT_CARD_STEMS
    } == EXPECTED_SUCCESSOR_BY_PHASE
    assert {
        f"{phase}.{null_path}"
        for phase, card in cards.items()
        for null_path in _null_paths(card)
    } == EXPECTED_NULLABLE_AGENT_CARD_PATHS
    for card in cards.values():
        for failure_route in card["gate"]["on_fail"].values():
            next_phase = failure_route.get("next_phase")
            if next_phase is not None:
                assert next_phase in EXPECTED_AGENT_CARD_STEMS


def test_every_phase_agent_card_matches_declared_schema() -> None:
    _assert_agent_cards_match_declared_schema(_load_agent_cards())


def test_agent_card_state_machine_matches_documented_semantics() -> None:
    cards = _load_agent_cards()

    _assert_agent_card_state_machine(cards)
    assert cards["done"]["gate"] == {
        "id": None,
        "pass_conditions": ["user confirms"],
        "on_pass": {"next_phase": None},
        "on_fail": {},
    }
    assert cards["approve"]["gate"]["on_fail"]["rejected"]["next_phase"] is None
    assert cards["verify"]["input"]["optional_context"][1]["key"] == "git_diff"
    assert cards["verify"]["input"]["optional_context"][1]["path"] is None


def test_agent_card_contract_rejects_invalid_mutations() -> None:
    cards = _load_agent_cards()
    del cards["done"]
    with pytest.raises(AssertionError):
        _assert_agent_card_state_machine(cards)

    cards = _load_agent_cards()
    cards["plan"]["gate"]["on_pass"]["next_phase"] = None
    with pytest.raises(AssertionError):
        _assert_agent_card_state_machine(cards)

    cards = _load_agent_cards()
    cards["plan"]["gate"]["on_fail"]["missing_artifact"]["next_phase"] = "plna"
    with pytest.raises(AssertionError):
        _assert_agent_card_state_machine(cards)

    cards = _load_agent_cards()
    del cards["plan"]["gate"]["on_pass"]["next_phase"]
    with pytest.raises(AssertionError):
        _assert_agent_cards_match_declared_schema(cards)

PHASE_CONTRACTS = {
    "plan": {
        "predecessor": None,
        "gate": "G1",
        "next": "review",
        "retry": 3,
        "fail_next": {
            "missing_artifact": "plan",
            "clarification_needed": "plan",
            "fr_coverage_gap": "plan",
        },
        "hil": False,
        "modes": {"inline", "delegated"},
    },
    "review": {
        "predecessor": "plan",
        "gate": "G2",
        "next": "approve",
        "retry": 2,
        "fail_next": {"critical_found": "plan", "high_only": None},
        "hil": False,
        "modes": {"inline", "delegated"},
    },
    "approve": {
        "predecessor": "review",
        "gate": "G3",
        "next": "impl",
        "retry": 1,
        "fail_next": {"revision": "plan", "rejected": None},
        "hil": True,
        "modes": {"inline"},
    },
    "impl": {
        "predecessor": "approve",
        "gate": "G4",
        "next": "verify",
        "retry": 5,
        "fail_next": {"incomplete_tasks": "impl"},
        "hil": False,
        "modes": {"inline", "delegated"},
    },
    "verify": {
        "predecessor": "impl",
        "gate": "G5",
        "next": "test",
        "retry": 2,
        "fail_next": {
            "scope_violation": "approve",
            "impl_bug": "impl",
            "arch_issue": "plan",
        },
        "hil": False,
        "modes": {"inline", "delegated"},
    },
    "test": {
        "predecessor": "verify",
        "gate": "G6",
        "next": "done",
        "retry": 3,
        "fail_next": {"regression_failure": "impl"},
        "hil": False,
        "modes": {"inline", "delegated"},
    },
    "done": {
        "predecessor": "test",
        "gate": None,
        "next": None,
        "retry": 0,
        "fail_next": {},
        "hil": True,
        "modes": {"inline"},
    },
}


def _skill_frontmatter(path: Path) -> dict[str, str | None]:
    match = FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    assert match is not None
    block = match.group(1)
    return {
        key: _top_level_value(block, key)
        for key in ("name", "version", "phase", "gate")
    }


def test_phase_cards_match_state_machine_skill_metadata_and_routes() -> None:
    assert PHASE_ORDER == ["plan", "review", "approve", "impl", "verify", "test", "done"]
    for index, (phase, expected) in enumerate(PHASE_CONTRACTS.items()):
        assert expected["predecessor"] == (PHASE_ORDER[index - 1] if index else None)
        assert PHASE_GATE.get(phase) == expected["gate"]
        card = json.loads((AGENT_CARD_ROOT / "agent-cards" / f"{phase}.json").read_text())
        metadata = _skill_frontmatter(SKILLS_ROOT / f"phase-{phase}" / "SKILL.md")
        assert card["name"] == metadata["name"] == f"phase-{phase}"
        assert metadata["phase"] == phase
        assert metadata.get("gate") == expected["gate"]
        assert card["gate"]["id"] == expected["gate"]
        assert card["gate"]["on_pass"]["next_phase"] == expected["next"]
        assert {
            key: route.get("next_phase")
            for key, route in card["gate"]["on_fail"].items()
        } == expected["fail_next"]
        assert card["retry"]["max"] == expected["retry"]
        assert card["hil"] is expected["hil"]
        assert set(card["capabilities"]["execution_modes"]) == expected["modes"]


DATABASE_STAGE_CONTRACTS = {
    "plan": {
        "conditions": (
            "database.signal",
            "database.risk_class",
            "database.decision",
            "database.production_schema",
        ),
        "decision_artifact": True,
    },
    "verify": {
        "conditions": (
            "database.production_schema",
            "database.equivalence",
            "database.integrity",
            "database.query_plan",
            "database.migration",
            "database.rollback",
        ),
        "decision_artifact": False,
    },
    "test": {
        "conditions": ("database.production_schema", "database.local_test"),
        "decision_artifact": False,
    },
}


def test_database_cards_declare_typed_artifacts_and_mandatory_conditions() -> None:
    schema = json.loads((AGENT_CARD_ROOT / "agent-card.schema.json").read_text())
    database_schema = schema["properties"]["input"]["properties"]["database"]
    assert database_schema["required"] == [
        "stage",
        "profile_artifact",
        "decision_artifact",
        "evidence_artifact",
        "production_schema_required",
    ]
    assert database_schema["properties"]["stage"]["enum"] == ["plan", "verify", "test"]
    assert database_schema["properties"]["production_schema_required"]["const"] is True

    artifact_schema = schema["properties"]["output"]["properties"]["artifacts"]["items"]
    assert artifact_schema["properties"]["required_when_database_signal"] == {
        "type": "boolean",
        "default": False,
    }

    condition_schema = schema["properties"]["gate"]["properties"]["database_conditions"]
    assert condition_schema["items"]["enum"] == [
        "database.signal",
        "database.risk_class",
        "database.decision",
        "database.production_schema",
        "database.equivalence",
        "database.integrity",
        "database.query_plan",
        "database.migration",
        "database.rollback",
        "database.local_test",
    ]

    for phase, expected in DATABASE_STAGE_CONTRACTS.items():
        card = json.loads(
            (AGENT_CARD_ROOT / "agent-cards" / f"{phase}.json").read_text()
        )
        database_input = card["input"]["database"]
        assert database_input["stage"] == phase
        assert database_input["profile_artifact"] == "manifest.json"
        assert database_input["decision_artifact"] == "artifacts/database-decision.json"
        assert (
            database_input["evidence_artifact"]
            == "artifacts/database-validation-evidence.json"
        )
        assert database_input["production_schema_required"] is True
        assert tuple(card["gate"]["database_conditions"]) == expected["conditions"]

        database_artifacts = {
            artifact["key"]: artifact
            for artifact in card["output"]["artifacts"]
            if artifact["key"].startswith("database_")
        }
        assert database_artifacts["database_validation_evidence"] == {
            "key": "database_validation_evidence",
            "path": "artifacts/database-validation-evidence.json",
            "format": "json",
            "required": False,
            "required_when_database_signal": True,
        }
        if expected["decision_artifact"]:
            assert database_artifacts["database_decision"] == {
                "key": "database_decision",
                "path": "artifacts/database-decision.json",
                "format": "json",
                "required": False,
                "required_when_database_signal": True,
            }

    plan_capabilities = json.loads(
        (AGENT_CARD_ROOT / "agent-cards" / "plan.json").read_text()
    )["capabilities"]
    assert plan_capabilities["file_write"] is True
    assert plan_capabilities["sandbox_modes"] == ["workspace-write"]
    for phase in ("verify", "test"):
        capabilities = json.loads(
            (AGENT_CARD_ROOT / "agent-cards" / f"{phase}.json").read_text()
        )["capabilities"]
        assert capabilities["file_write"] is False
        assert capabilities["sandbox_modes"] == ["workspace-write"]

    for agent in ("spec-writer.md", "spec-verifier.md", "happy-path-tester.md"):
        source = (REPO_ROOT / "claude" / "agents" / agent).read_text(
            encoding="utf-8"
        )
        assert "codex_sandbox: workspace-write" in source

    cli_readme = (REPO_ROOT / "cli" / "README.md").read_text(encoding="utf-8")
    assert "Plan runs with `file_write: true` and `workspace-write`" in cli_readme
    assert (
        "verify/test keep `file_write: false` but use `workspace-write`"
        in cli_readme
    )


def test_database_card_schema_rejects_phase_specific_mutations() -> None:
    cards = _load_agent_cards()
    _assert_agent_cards_match_declared_schema(cards)

    cards = _load_agent_cards()
    del cards["plan"]["input"]["database"]
    with pytest.raises(AssertionError):
        _assert_agent_cards_match_declared_schema(cards)

    cards = _load_agent_cards()
    cards["verify"]["input"]["database"]["stage"] = "plan"
    with pytest.raises(AssertionError):
        _assert_agent_cards_match_declared_schema(cards)

    cards = _load_agent_cards()
    cards["test"]["gate"]["database_conditions"] = ["database.signal"]
    with pytest.raises(AssertionError):
        _assert_agent_cards_match_declared_schema(cards)

    cards = _load_agent_cards()
    evidence = next(
        artifact
        for artifact in cards["verify"]["output"]["artifacts"]
        if artifact["key"] == "database_validation_evidence"
    )
    del evidence["required_when_database_signal"]
    with pytest.raises(AssertionError):
        _assert_agent_cards_match_declared_schema(cards)

    cards = _load_agent_cards()
    cards["plan"]["output"]["artifacts"][0]["unexpected"] = True
    with pytest.raises(AssertionError):
        _assert_agent_cards_match_declared_schema(cards)

    cards = _load_agent_cards()
    del cards["plan"]["output"]["artifacts"][0]["format"]
    with pytest.raises(AssertionError):
        _assert_agent_cards_match_declared_schema(cards)


OUTCOME_TOKENS = {
    "analysis": ("allow", "dry_run_only"),
    "multi-agent": ("PASS", "FAIL", "ESCALATE"),
    "release-worktree-lifecycle": (
        "reuse",
        "preview",
        "ready",
        "removed",
        "blocked",
        "exit code `4`",
    ),
    "wf": ("unknown", "dry-run"),
    "wf-orchestrator": ("allow", "dry_run_only", "nonzero"),
    "wf-reset": ("archive", "rollback"),
    "wf-status": ("provider_status", ".workflow"),
}


def test_skills_retain_required_outcome_vocabulary() -> None:
    missing: list[str] = []
    for skill, tokens in OUTCOME_TOKENS.items():
        text = (SKILLS_ROOT / skill / "SKILL.md").read_text(encoding="utf-8")
        for token in tokens:
            if token not in text:
                missing.append(f"{skill}:{token}")
    assert missing == []


EXTENSIONS = {
    "json": ".json",
    "md": ".md",
    "yaml": ".yaml",
    "yml": ".yml",
    "txt": ".txt",
}


def _normalized_relative_path(raw_path: str, label: str) -> PurePosixPath:
    relative = PurePosixPath(raw_path)
    assert raw_path == str(relative), f"invalid {label}: {raw_path}"
    assert relative.parts and relative.parts != (".",), f"invalid {label}: {raw_path}"
    assert not relative.is_absolute(), f"invalid {label}: {raw_path}"
    assert ".." not in relative.parts, f"invalid {label}: {raw_path}"
    return relative


def _resource_mapping(line: str) -> tuple[int, str, str] | None:
    if not line or line.lstrip().startswith("#") or ":" not in line:
        return None
    indentation = len(line) - len(line.lstrip(" "))
    key, value = line.strip().split(":", maxsplit=1)
    assert key, "resource declaration key must be nonempty"
    return indentation, key.strip(), value.strip()


def _resource_paths(
    skill_text: str, manifest_categories: set[str]
) -> dict[str, list[str]]:
    match = FRONTMATTER_RE.match(skill_text)
    assert match is not None
    lines = match.group(1).splitlines()
    declarations: dict[str, list[str]] = {}

    def collect_section(start: int, section_indentation: int) -> tuple[int, list[str]]:
        paths: list[str] = []
        index = start
        while index < len(lines):
            mapping = _resource_mapping(lines[index])
            if mapping is None:
                index += 1
                continue
            indentation, _, value = mapping
            if indentation <= section_indentation:
                break
            if value:
                paths.append(_condition_string(value, "resource declaration"))
            index += 1
        return index, paths

    def add_category(category: str, paths: list[str]) -> None:
        assert category in manifest_categories, (
            f"undeclared resource category: {category}"
        )
        assert category not in declarations, f"duplicate resource category: {category}"
        assert paths, f"empty resource category declaration: {category}"
        declarations[category] = paths

    index = 0
    while index < len(lines):
        mapping = _resource_mapping(lines[index])
        if mapping is None or mapping[0] != 0:
            index += 1
            continue
        _, key, value = mapping
        if key == "resources":
            assert not value, "resources must be a nested mapping"
            index += 1
            while index < len(lines):
                category_mapping = _resource_mapping(lines[index])
                if category_mapping is None:
                    index += 1
                    continue
                indentation, category, category_value = category_mapping
                if indentation == 0:
                    break
                if indentation != 2:
                    index += 1
                    continue
                assert not category_value, "resource categories must be nested mappings"
                index, paths = collect_section(index + 1, indentation)
                add_category(category, paths)
            continue
        if key in manifest_categories:
            assert not value, "resource categories must be nested mappings"
            index, paths = collect_section(index + 1, 0)
            add_category(key, paths)
            continue
        index += 1

    assert set(declarations) == manifest_categories
    return declarations


def _assert_manifest_resources(
    skill_dir: Path, manifest: dict[str, object], skill_text: str
) -> None:
    manifest_categories = manifest["categories"]
    assert isinstance(manifest_categories, dict)
    assert manifest_categories
    resource_paths = _resource_paths(skill_text, set(manifest_categories))

    for category, declaration in manifest_categories.items():
        assert isinstance(declaration, dict)
        category_type = declaration["type"]
        assert category_type in EXTENSIONS
        category_path = declaration.get("path", category)
        assert isinstance(category_path, str)
        relative_category = _normalized_relative_path(
            category_path, "resource category path"
        )
        resource_dir = skill_dir / relative_category
        extension = EXTENSIONS[category_type]
        assert resource_dir.is_dir(), f"missing resource dir: {resource_dir}"
        resources = sorted(resource_dir.rglob(f"*{extension}"))
        assert resources, f"empty resource category: {skill_dir.name}/{category}"
        for resource in resources:
            assert resource.is_file()
            if extension == ".json":
                json.loads(resource.read_text())

        for raw_path in resource_paths[category]:
            relative_resource = _normalized_relative_path(raw_path, "resource path")
            assert relative_resource.parts[: len(relative_category.parts)] == (
                relative_category.parts
            ), f"resource path outside category: {raw_path}"
            assert relative_resource.suffix == extension, (
                f"resource extension mismatch: {raw_path}"
            )
            resource = skill_dir / relative_resource
            assert resource.is_relative_to(skill_dir), (
                f"resource path outside skill root: {raw_path}"
            )
            assert resource.is_file(), f"missing declared resource: {resource}"


def test_manifests_match_identity_and_every_declared_resource() -> None:
    for manifest_path in sorted(SKILLS_ROOT.glob("*/manifest.json")):
        skill_dir = manifest_path.parent
        manifest = json.loads(manifest_path.read_text())
        skill_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        metadata = _skill_frontmatter(skill_dir / "SKILL.md")
        assert manifest["skill"] == metadata["name"] == skill_dir.name
        assert manifest["version"] == metadata["version"]
        _assert_manifest_resources(skill_dir, manifest, skill_text)


def test_workflow_artifact_paths_are_workflow_relative() -> None:
    for card_path in sorted((AGENT_CARD_ROOT / "agent-cards").glob("*.json")):
        card = json.loads(card_path.read_text())
        declarations = (
            card["input"].get("required_artifacts", [])
            + card["output"].get("artifacts", [])
        )
        for declaration in declarations:
            relative = PurePosixPath(declaration["path"])
            assert not relative.is_absolute()
            assert ".." not in relative.parts


def test_wf_slash_dispatcher_exposes_only_supported_routes() -> None:
    text = (SKILLS_ROOT / "wf" / "SKILL.md").read_text(encoding="utf-8")
    route_section = text.split("## Ownership", 1)[0]
    routes = set(re.findall(r"(?m)^- `(/wf[^`]*)`", route_section))
    assert routes == {
        "/wf",
        "/wf init <concept>",
        "/wf resume",
        "/wf status",
        "/wf reset <action>",
    }
    assert "/wf ship" not in routes


def _duplicate_frontmatter_key(text: str, key: str) -> str:
    frontmatter_end = text.index("\n---", len("---"))
    return f"{text[:frontmatter_end]}\n{key}: duplicate\n{text[frontmatter_end:]}"


@pytest.mark.parametrize("key", ("name", "version", "phase", "gate"))
def test_contract_metadata_rejects_duplicate_top_level_keys(key: str) -> None:
    skill_text = (SKILLS_ROOT / "phase-plan" / "SKILL.md").read_text(encoding="utf-8")

    with pytest.raises(
        AssertionError,
        match=rf"duplicate top-level contract key: {key}",
    ):
        _top_level_value(_duplicate_frontmatter_key(skill_text, key), key)


def _write_manifest_skill_fixture(
    tmp_path: Path, resource_category: str, resource_path: str
) -> None:
    skill_dir = tmp_path / "analysis"
    resource_dir = skill_dir / "modes"
    resource_dir.mkdir(parents=True)
    (skill_dir / "manifest.json").write_text(
        json.dumps(
            {
                "skill": "analysis",
                "version": "1.0.0",
                "categories": {"modes": {"type": "json", "path": "modes"}},
            }
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            (
                "---",
                "name: analysis",
                "version: 1.0.0",
                "resources:",
                f"  {resource_category}:",
                f'    document: "{resource_path}"',
                "---",
            )
        ),
        encoding="utf-8",
    )
    (resource_dir / "other.json").write_text("{}", encoding="utf-8")


@pytest.mark.parametrize(
    ("resource_category", "resource_path", "message"),
    [
        ("modes", "modes/document.json", "missing declared resource"),
        ("other", "other/document.json", "undeclared resource category"),
        ("modes", "../outside.json", "invalid resource path"),
        ("modes", "modes/document.md", "resource extension mismatch"),
    ],
)
def test_manifest_resource_contract_rejects_invalid_declared_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource_category: str,
    resource_path: str,
    message: str,
) -> None:
    _write_manifest_skill_fixture(tmp_path, resource_category, resource_path)
    monkeypatch.setattr(
        "test_skill_contract_matrix.SKILLS_ROOT",
        tmp_path,
    )

    with pytest.raises(AssertionError, match=message):
        test_manifests_match_identity_and_every_declared_resource()
