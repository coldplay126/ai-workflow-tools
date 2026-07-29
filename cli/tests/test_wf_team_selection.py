from __future__ import annotations

from copy import deepcopy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.commands import wf as wf_command
from awf.core.team_config import select_secondary_team


_PHASES = ("plan", "review", "approve", "impl", "verify", "test", "done")
_EXPECTED = {
    "small": {phase: () for phase in _PHASES},
    "standard": {
        "plan": ("constitution_reviewer",),
        "review": ("code_reviewer",),
        "approve": (),
        "impl": (),
        "verify": ("code_reviewer",),
        "test": ("adversarial",),
        "done": (),
    },
    "high_risk": {
        "plan": ("constitution_reviewer",),
        "review": ("code_reviewer", "analyzer"),
        "approve": (),
        "impl": ("code_reviewer",),
        "verify": ("code_reviewer", "analyzer"),
        "test": ("adversarial", "quality_validation"),
        "done": (),
    },
}
_AGENT_BY_ROLE = {
    "constitution_reviewer": "plan-validator",
    "code_reviewer": "code-reviewer",
    "analyzer": "analyzer",
    "adversarial": "adversarial-tester",
    "quality_validation": "quality-validator",
}
_PROVIDER_BY_AGENT = {
    "plan-validator": "codex",
    "code-reviewer": "codex",
    "analyzer": "codex",
    "adversarial-tester": "codex",
    "quality-validator": "claude:sonnet",
}


def _resolve_agent(role: str) -> str | None:
    return _AGENT_BY_ROLE.get(role)


def _load_agent(agent: str) -> dict:
    return {
        "meta": {
            "name": agent,
            "provider_hint": _PROVIDER_BY_AGENT[agent],
            "model": "explicit-agent-card-model",
        }
    }


def _select(config: dict, phase: str, change_class: str, *, mode: str | None = None):
    return select_secondary_team(
        config,
        phase,
        change_class,
        mode=mode,
        resolve_agent=_resolve_agent,
        load_agent=_load_agent,
    )


@pytest.mark.parametrize(
    ("change_class", "phase", "expected_roles"),
    [
        (change_class, phase, roles)
        for change_class, phases in _EXPECTED.items()
        for phase, roles in phases.items()
    ],
)
def test_automatic_secondary_roles_cover_all_change_classes_and_phases(
    change_class: str,
    phase: str,
    expected_roles: tuple[str, ...],
) -> None:
    config = {"team_selection": {"enabled": True}, "phase_routing": {}}

    selection = _select(config, phase, change_class)

    assert selection.managed is True
    if not expected_roles:
        assert selection.has_secondary_work is False
        assert selection.team_config is None
        assert selection.provider_config is None
        return

    assert selection.source == "automatic"
    assert selection.has_secondary_work is True
    assert selection.team_config is not None
    assert tuple(role["id"] for role in selection.team_config["roles"]) == expected_roles
    assert tuple(role["provider"] for role in selection.team_config["roles"]) == tuple(
        _PROVIDER_BY_AGENT[_AGENT_BY_ROLE[role_id]] for role_id in expected_roles
    )
    assert len(selection.team_config["roles"]) <= 2
    assert selection.provider_config is not config
    assert selection.provider_config["phase_routing"][phase]["pattern"] == "team"
    assert selection.provider_config["phase_routing"][phase]["team"] == selection.team_config


def test_small_change_resolves_to_no_secondary_dispatch(monkeypatch, tmp_path) -> None:
    config = {"team_selection": {"enabled": True}, "phase_routing": {}}

    def unexpected_legacy_promotion(**_kwargs):
        raise AssertionError("opt-in no-secondary selection must not enter legacy dispatch")

    monkeypatch.setattr(wf_command, "_maybe_auto_promote_dual_strategy", unexpected_legacy_promotion)
    mode, routing, promoted, selection = wf_command._resolve_secondary_execution(
        user_mode=None,
        phase="test",
        change_class="small",
        provider_config=config,
        repo_root=str(tmp_path),
    )

    assert mode is None
    assert routing is config
    assert promoted is False
    assert selection.managed is True
    assert selection.has_secondary_work is False


def test_explicit_phase_team_is_preserved_exactly() -> None:
    explicit_team = {
        "name": "configured-reviewers",
        "roles": [
            {
                "id": "custom-reviewer",
                "provider": "custom-provider",
                "model": "custom-model",
                "agent_card": "custom-agent-card",
                "write_scope": ["cli/**"],
            }
        ],
        "execution": "parallel",
        "max_turns": 7,
        "timeout_sec": 777,
    }
    config = {
        "team_selection": {"enabled": True},
        "phase_routing": {
            "review": {
                "pattern": "team",
                "team": explicit_team,
                "fallback": {"mode": "cross"},
            }
        },
        "phase_models": {"review": {"inline_model": "explicit-primary-model"}},
    }

    selection = _select(config, "review", "high_risk")

    assert selection.source == "explicit-config"
    assert selection.team_config == explicit_team
    assert selection.team_config is not explicit_team
    assert selection.provider_config["phase_routing"]["review"]["team"] == explicit_team
    assert selection.provider_config["phase_models"] == config["phase_models"]
    assert "fallback" not in selection.provider_config["phase_routing"]["review"]


def test_explicit_empty_role_list_is_explicit_no_secondary_work() -> None:
    config = {
        "team_selection": {"enabled": True},
        "phase_routing": {"review": {"team": {"name": "none", "roles": []}}},
    }

    selection = _select(config, "review", "high_risk")

    assert selection.source == "explicit-config-none"
    assert selection.has_secondary_work is False
    assert selection.team_config is None



def test_explicit_solo_disables_explicit_and_automatic_secondary_work() -> None:
    config = {
        "team_selection": {"enabled": True},
        "phase_routing": {
            "review": {"team": {"name": "explicit", "roles": [{"id": "custom"}]}}
        },
    }

    selection = _select(config, "review", "high_risk", mode="solo")

    assert selection.source == "explicit-solo"
    assert selection.managed is True
    assert selection.has_secondary_work is False


def test_legacy_config_keeps_legacy_routing(monkeypatch, tmp_path) -> None:
    config = {"version": "2.3.0", "phase_routing": {"review": {"mode": "dual"}}}
    calls: list[dict] = []

    def legacy_promotion(**kwargs):
        calls.append(kwargs)
        return "cross", True

    monkeypatch.setattr(wf_command, "_maybe_auto_promote_dual_strategy", legacy_promotion)
    mode, routing, promoted, selection = wf_command._resolve_secondary_execution(
        user_mode=None,
        phase="review",
        change_class="high_risk",
        provider_config=config,
        repo_root=str(tmp_path),
    )

    assert selection.source == "legacy"
    assert selection.managed is False
    assert mode == "cross"
    assert promoted is True
    assert routing is config
    assert calls and calls[0]["provider_config"] is config


def test_selector_consumes_change_class_without_mutating_state_or_config() -> None:
    state = {"changeClass": "high_risk", "currentPhase": "review", "gates": {"G2": "pending"}}
    config = {"team_selection": {"enabled": True}, "phase_routing": {}}
    original_state = deepcopy(state)
    original_config = deepcopy(config)

    selection = _select(config, "review", state["changeClass"])

    assert tuple(role["id"] for role in selection.team_config["roles"]) == (
        "code_reviewer",
        "analyzer",
    )
    assert state == original_state
    assert config == original_config


def test_automatic_roles_are_deduplicated_by_resolved_agent_identity() -> None:
    config = {"team_selection": {"enabled": True}, "phase_routing": {}}

    selection = select_secondary_team(
        config,
        "review",
        "high_risk",
        resolve_agent=lambda _role: "shared-review-agent",
        load_agent=lambda agent: {"meta": {"name": agent, "provider_hint": "codex"}},
    )

    assert selection.team_config is not None
    assert [role["id"] for role in selection.team_config["roles"]] == ["code_reviewer"]


def test_unknown_change_class_fails_closed_without_secondary_work() -> None:
    config = {"team_selection": {"enabled": True}, "phase_routing": {}}

    selection = _select(config, "review", "unrecognized")

    assert selection.managed is True
    assert selection.source == "automatic-none"
    assert selection.has_secondary_work is False


def test_unresolved_automatic_roles_fail_closed_without_empty_team() -> None:
    config = {"team_selection": {"enabled": True}, "phase_routing": {}}

    selection = select_secondary_team(
        config,
        "review",
        "standard",
        resolve_agent=lambda _role: None,
        load_agent=lambda _agent: {},
    )

    assert selection.source == "automatic-unresolved"
    assert selection.has_secondary_work is False
    assert selection.team_config is None


def test_shipped_default_config_enables_dynamic_team_selection() -> None:
    template = (
        Path(__file__).resolve().parents[2]
        / "claude"
        / "skills"
        / "wf-orchestrator"
        / "templates"
        / "provider-config.default.json"
    )

    config = json.loads(template.read_text(encoding="utf-8"))
    assert config["team_selection"] == {"enabled": True}
