"""Provider-config v3.0.0 team routing helpers.

Extracts team/subagent pattern from provider-config.json and provides
backward-compatible access for v2.0.0 configs (pattern defaults to "subagent").
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any, Callable


_AUTOMATIC_SECONDARY_ROLES: dict[str, dict[str, tuple[str, ...]]] = {
    "small": {},
    "standard": {
        "plan": ("constitution_reviewer",),
        "review": ("code_reviewer",),
        "verify": ("code_reviewer",),
        "test": ("adversarial",),
    },
    "high_risk": {
        "plan": ("constitution_reviewer",),
        "impl": ("code_reviewer",),
        "review": ("code_reviewer", "analyzer"),
        "verify": ("code_reviewer", "analyzer"),
        "test": ("adversarial", "quality_validation"),
    },
}


@dataclass(frozen=True)
class SecondaryTeamSelection:
    """Immutable routing decision for work that follows the primary provider."""

    managed: bool
    source: str
    team_config: dict[str, Any] | None = None
    provider_config: dict[str, Any] | None = None

    @property
    def has_secondary_work(self) -> bool:
        return self.team_config is not None


def select_secondary_team(
    provider_config: dict[str, Any],
    phase: str,
    change_class: str | None,
    *,
    mode: str | None = None,
    resolve_agent: Callable[[str], str | None] | None = None,
    load_agent: Callable[[str], dict[str, Any]] | None = None,
) -> SecondaryTeamSelection:
    """Select a secondary-only team without mutating config or workflow state.

    Automatic selection is owned only when ``team_selection.enabled`` is
    explicitly true. The persisted ``change_class`` value is consumed as-is;
    unknown values fail closed instead of being reclassified or coerced.
    """
    if mode == "solo":
        return SecondaryTeamSelection(managed=True, source="explicit-solo")

    selection_cfg = provider_config.get("team_selection")
    if not isinstance(selection_cfg, dict) or selection_cfg.get("enabled") is not True:
        return SecondaryTeamSelection(managed=False, source="legacy")

    explicit_team = _explicit_phase_team(provider_config, phase)
    if explicit_team is not None:
        if not explicit_team["roles"]:
            return SecondaryTeamSelection(managed=True, source="explicit-config-none")
        team = deepcopy(explicit_team)
        return SecondaryTeamSelection(
            managed=True,
            source="explicit-config",
            team_config=team,
            provider_config=_secondary_only_provider_config(provider_config, phase, team),
        )

    class_roles = _AUTOMATIC_SECONDARY_ROLES.get(change_class)
    role_ids = class_roles.get(phase, ()) if class_roles is not None else ()
    if not role_ids:
        return SecondaryTeamSelection(managed=True, source="automatic-none")

    if resolve_agent is None or load_agent is None:
        from awf.core.spec_loader import load_agent_definition, resolve_agent_for_role

        resolve_agent = resolve_agent or resolve_agent_for_role
        load_agent = load_agent or load_agent_definition

    roles: list[dict[str, Any]] = []
    seen_agents: set[str] = set()
    for role_id in role_ids[:2]:
        try:
            agent_name = resolve_agent(role_id)
        except (OSError, ValueError):
            continue
        if not isinstance(agent_name, str) or not agent_name or agent_name in seen_agents:
            continue
        try:
            definition = load_agent(agent_name)
        except (FileNotFoundError, OSError, ValueError):
            continue
        if not isinstance(definition, dict):
            continue
        meta = definition.get("meta")
        if not isinstance(meta, dict):
            continue
        resolved_name = meta.get("name", agent_name)
        if not isinstance(resolved_name, str) or resolved_name != agent_name:
            continue
        provider_hint = meta.get("provider_hint")
        if not isinstance(provider_hint, str) or not provider_hint:
            continue
        seen_agents.add(agent_name)
        roles.append(
            {
                "id": role_id,
                "protocol": role_id,
                "provider": provider_hint,
            }
        )

    if not roles:
        return SecondaryTeamSelection(managed=True, source="automatic-unresolved")

    defaults = provider_config.get("defaults")
    configured_timeout = defaults.get("timeout_seconds") if isinstance(defaults, dict) else None
    timeout_sec = (
        configured_timeout
        if isinstance(configured_timeout, int)
        and not isinstance(configured_timeout, bool)
        and configured_timeout >= 10
        else 300
    )
    team = {
        "name": f"{change_class}-{phase}-secondary",
        "roles": roles,
        "execution": "parallel" if len(roles) > 1 else "sequential",
        "max_turns": 1,
        "timeout_sec": timeout_sec,
    }
    return SecondaryTeamSelection(
        managed=True,
        source="automatic",
        team_config=team,
        provider_config=_secondary_only_provider_config(provider_config, phase, team),
    )


def _explicit_phase_team(
    provider_config: dict[str, Any],
    phase: str,
) -> dict[str, Any] | None:
    routing = provider_config.get("phase_routing")
    phase_config = routing.get(phase) if isinstance(routing, dict) else None
    team = phase_config.get("team") if isinstance(phase_config, dict) else None
    roles = team.get("roles") if isinstance(team, dict) else None
    return team if isinstance(roles, list) else None


def _secondary_only_provider_config(
    provider_config: dict[str, Any],
    phase: str,
    team_config: dict[str, Any],
) -> dict[str, Any]:
    routing_config = deepcopy(provider_config)
    routing = routing_config.get("phase_routing")
    if not isinstance(routing, dict):
        routing = {}
        routing_config["phase_routing"] = routing
    routing[phase] = {
        "pattern": "team",
        "team": deepcopy(team_config),
    }
    return routing_config


def get_phase_pattern(provider_config: dict[str, Any], phase: str) -> str:
    """Return the execution pattern for a phase: 'team' or 'subagent'.

    v2.0.0 configs have no 'pattern' field → always 'subagent'.
    v3.0.0 configs may set 'pattern' per phase.
    """
    routing = provider_config.get("phase_routing")
    if not isinstance(routing, dict):
        return "subagent"
    phase_cfg = routing.get(phase)
    if not isinstance(phase_cfg, dict):
        return "subagent"
    pattern = phase_cfg.get("pattern", "subagent")
    return str(pattern) if isinstance(pattern, str) and pattern else "subagent"


def get_team_config(provider_config: dict[str, Any], phase: str) -> dict[str, Any] | None:
    """Extract team configuration for a phase. Returns None if not a team phase."""
    routing = provider_config.get("phase_routing")
    if not isinstance(routing, dict):
        return None
    phase_cfg = routing.get(phase)
    if not isinstance(phase_cfg, dict):
        return None
    if phase_cfg.get("pattern") != "team":
        return None
    team = phase_cfg.get("team")
    if not isinstance(team, dict):
        return None
    return team


def get_team_fallback(provider_config: dict[str, Any], phase: str) -> dict[str, Any] | None:
    """Get fallback config when team mode fails. Returns None if no fallback."""
    routing = provider_config.get("phase_routing")
    if not isinstance(routing, dict):
        return None
    phase_cfg = routing.get(phase)
    if not isinstance(phase_cfg, dict):
        return None
    fallback = phase_cfg.get("fallback")
    if isinstance(fallback, dict):
        return fallback
    return None


def get_subagent_mode(provider_config: dict[str, Any], phase: str) -> str:
    """Get subagent mode for a phase (used when pattern='subagent' or as fallback).

    Fallback order: phase routing → team fallback → config defaults.
    """
    default_mode = _get_default_mode(provider_config)
    routing = provider_config.get("phase_routing", {})
    if not isinstance(routing, dict):
        return default_mode
    phase_cfg = routing.get(phase, {})
    if not isinstance(phase_cfg, dict):
        return default_mode

    # For team phases: use fallback mode
    if phase_cfg.get("pattern") == "team":
        fallback = phase_cfg.get("fallback")
        if isinstance(fallback, dict):
            mode = fallback.get("mode")
            if isinstance(mode, str) and mode:
                return mode
        return default_mode

    mode = phase_cfg.get("mode")
    if isinstance(mode, str) and mode:
        return mode
    return default_mode


def _get_default_mode(provider_config: dict[str, Any]) -> str:
    """Extract defaults.mode from provider config."""
    defaults = provider_config.get("defaults")
    if isinstance(defaults, dict):
        mode = defaults.get("mode")
        if isinstance(mode, str) and mode:
            return mode
    return "inline"


def validate_provider_config(provider_config: dict[str, Any]) -> list[str]:
    """Validate provider-config.json structure. Returns list of error messages.

    Supports both v2.0.0 and v3.0.0 schemas. Team roles may opt into
    evidence-only baseline research, a named review lens, or an isolated OMP
    implementation lane. The latter is deliberately narrow: it needs an
    explicit task selector and a non-empty write scope.
    """
    errors: list[str] = []

    version = provider_config.get("version", "")
    if not version:
        errors.append("missing 'version' field")

    routing = provider_config.get("phase_routing")
    if not isinstance(routing, dict):
        errors.append("missing or invalid 'phase_routing'")
        return errors

    for phase, phase_cfg in routing.items():
        if not isinstance(phase_cfg, dict):
            errors.append(f"phase_routing.{phase}: expected dict")
            continue

        pattern = phase_cfg.get("pattern", "subagent")
        if pattern not in ("subagent", "team"):
            errors.append(f"phase_routing.{phase}.pattern: invalid value '{pattern}' (expected 'subagent' or 'team')")

        if pattern == "team":
            team = phase_cfg.get("team")
            if not isinstance(team, dict):
                errors.append(f"phase_routing.{phase}.team: required when pattern='team'")
                continue

            if not team.get("name"):
                errors.append(f"phase_routing.{phase}.team.name: required")

            roles = team.get("roles")
            if not isinstance(roles, list) or not roles:
                errors.append(f"phase_routing.{phase}.team.roles: must be a non-empty list")
            else:
                for i, role in enumerate(roles):
                    if not isinstance(role, dict):
                        errors.append(f"phase_routing.{phase}.team.roles[{i}]: expected dict")
                    elif not role.get("id"):
                        errors.append(f"phase_routing.{phase}.team.roles[{i}].id: required")

            execution = team.get("execution", "sequential")
            if execution not in ("sequential", "parallel"):
                errors.append(f"phase_routing.{phase}.team.execution: invalid '{execution}'")

            max_turns = team.get("max_turns", 3)
            if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns < 1:
                errors.append(f"phase_routing.{phase}.team.max_turns: must be a positive integer")

            timeout = team.get("timeout_sec", 600)
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout < 10:
                errors.append(f"phase_routing.{phase}.team.timeout_sec: must be >= 10")

            overlap_policy = team.get("on_write_scope_overlap", "fail")
            if overlap_policy not in ("fail", "sequential"):
                errors.append(
                    f"phase_routing.{phase}.team.on_write_scope_overlap: "
                    "must be 'fail' or 'sequential'"
                )

            # Validate role fields
            if isinstance(roles, list):
                for i, role in enumerate(roles):
                    if not isinstance(role, dict):
                        continue
                    provider = role.get("provider")
                    if provider is not None and not isinstance(provider, str):
                        errors.append(f"phase_routing.{phase}.team.roles[{i}].provider: must be a string")

                    write_scope = role.get("write_scope")
                    has_write_scope = (
                        isinstance(write_scope, list)
                        and bool(write_scope)
                        and all(
                            isinstance(path, str) and path.strip()
                            for path in write_scope
                        )
                    )
                    if write_scope is not None and not isinstance(write_scope, list):
                        errors.append(f"phase_routing.{phase}.team.roles[{i}].write_scope: must be a list")
                    elif isinstance(write_scope, list) and not all(
                        isinstance(path, str) and path.strip()
                        for path in write_scope
                    ):
                        errors.append(
                            f"phase_routing.{phase}.team.roles[{i}].write_scope: "
                            "must contain non-empty strings"
                        )

                    baseline_research = role.get("baseline_research", False)
                    if not isinstance(baseline_research, bool):
                        errors.append(
                            f"phase_routing.{phase}.team.roles[{i}].baseline_research: "
                            "must be a boolean"
                        )
                    elif baseline_research:
                        if phase not in ("plan", "review"):
                            errors.append(
                                f"phase_routing.{phase}.team.roles[{i}].baseline_research: "
                                "only allowed for plan or review"
                            )
                        if write_scope:
                            errors.append(
                                f"phase_routing.{phase}.team.roles[{i}].baseline_research: "
                                "must not declare write_scope"
                            )

                    review_lens = role.get("review_lens")
                    if review_lens is not None:
                        if not isinstance(review_lens, str) or not review_lens.strip():
                            errors.append(
                                f"phase_routing.{phase}.team.roles[{i}].review_lens: "
                                "must be a non-empty string"
                            )
                        if phase != "review":
                            errors.append(
                                f"phase_routing.{phase}.team.roles[{i}].review_lens: "
                                "only allowed for review"
                            )
                        if write_scope:
                            errors.append(
                                f"phase_routing.{phase}.team.roles[{i}].review_lens: "
                                "must not declare write_scope"
                            )

                    isolated_omp = role.get("isolated_omp", False)
                    if not isinstance(isolated_omp, bool):
                        errors.append(
                            f"phase_routing.{phase}.team.roles[{i}].isolated_omp: "
                            "must be a boolean"
                        )
                    elif isolated_omp:
                        if phase != "impl":
                            errors.append(
                                f"phase_routing.{phase}.team.roles[{i}].isolated_omp: "
                                "only allowed for impl"
                            )
                        if not has_write_scope:
                            errors.append(
                                f"phase_routing.{phase}.team.roles[{i}].isolated_omp: "
                                "requires a non-empty write_scope of non-empty strings"
                            )
                        task_selector = role.get("task_selector")
                        if (
                            not isinstance(task_selector, str)
                            or not (
                                task_selector == "parallel"
                                or re.fullmatch(r"T[0-9]+", task_selector)
                            )
                        ):
                            errors.append(
                                f"phase_routing.{phase}.team.roles[{i}].task_selector: "
                                "isolated_omp requires 'parallel' or an explicit T-number"
                            )

            # Validate fallback if present
            fallback = phase_cfg.get("fallback")
            if fallback is not None:
                if not isinstance(fallback, dict):
                    errors.append(f"phase_routing.{phase}.fallback: must be a dict")
                else:
                    fb_mode = fallback.get("mode")
                    if fb_mode is not None and not isinstance(fb_mode, str):
                        errors.append(f"phase_routing.{phase}.fallback.mode: must be a string")

    return errors


def upgrade_v2_to_v3(config: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a v2.0.0 config to v3.0.0 format in-place.

    Adds 'pattern': 'subagent' to all phase routing entries.
    Does not modify the file — returns the upgraded dict.
    """
    version = str(config.get("version", ""))
    if version.startswith("3"):
        return config  # already v3

    config["version"] = "3.0.0"
    routing = config.get("phase_routing")
    if not isinstance(routing, dict):
        config["phase_routing"] = {}
        return config
    for phase, phase_cfg in routing.items():
        if isinstance(phase_cfg, dict) and "pattern" not in phase_cfg:
            phase_cfg["pattern"] = "subagent"
    return config


# --- Default team configurations per phase ---

DEFAULT_PLAN_TEAM: dict[str, Any] = {
    "name": "plan-speckit",
    "roles": [
        {
            "id": "spec_writer",
            "provider": "claude-code",
            "write_scope": [".workflow/team/plan/board/**"],
        },
        {
            "id": "constitution_reviewer",
            "provider": "codex",
            "write_scope": [".workflow/team/plan/discussion/turn-*-constitution_reviewer.*"],
        },
    ],
    "execution": "sequential",
    "max_turns": 3,
    "timeout_sec": 600,
}

DEFAULT_IMPL_TEAM: dict[str, Any] = {
    "name": "impl-review",
    "roles": [
        {
            "id": "implementer",
            "provider": "claude-code",
            "write_scope": [".workflow/team/impl/board/**"],
        },
        {
            "id": "code_reviewer",
            "provider": "codex",
            "write_scope": [".workflow/team/impl/discussion/turn-*-code_reviewer.*"],
        },
    ],
    "execution": "sequential",
    "max_turns": 3,
    "timeout_sec": 900,
}

DEFAULT_TEST_TEAM: dict[str, Any] = {
    "name": "test-dual",
    "roles": [
        {
            "id": "happy_path",
            "provider": "claude-code",
            "write_scope": [".workflow/team/test/discussion/turn-*-happy_path.*"],
        },
        {
            "id": "adversarial",
            "provider": "codex",
            "write_scope": [".workflow/team/test/discussion/turn-*-adversarial.*"],
        },
    ],
    "execution": "sequential",
    "max_turns": 3,
    "timeout_sec": 600,
}

PHASE_DEFAULT_TEAMS: dict[str, dict[str, Any]] = {
    "plan": DEFAULT_PLAN_TEAM,
    "impl": DEFAULT_IMPL_TEAM,
    "test": DEFAULT_TEST_TEAM,
}
