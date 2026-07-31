from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from awf.core.skill_pressure import (
    HIGH_RISK_SKILLS,
    REQUIRED_CATEGORIES,
    SUPPORTED_RUNTIMES,
    MatrixError,
    load_skill_matrix,
)


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
    match = re.search(rf"(?m)^{re.escape(key)}:\s*[\"']?([^\n\"']+)", frontmatter)
    return match.group(1).strip() if match else None


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
