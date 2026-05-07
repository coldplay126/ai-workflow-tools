"""Provider-config v3.0.0 team routing helpers.

Extracts team/subagent pattern from provider-config.json and provides
backward-compatible access for v2.0.0 configs (pattern defaults to "subagent").
"""
from __future__ import annotations

from typing import Any


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

    Supports both v2.0.0 and v3.0.0 schemas.
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

            # Validate role fields
            if isinstance(roles, list):
                for i, role in enumerate(roles):
                    if not isinstance(role, dict):
                        continue
                    provider = role.get("provider")
                    if provider is not None and not isinstance(provider, str):
                        errors.append(f"phase_routing.{phase}.team.roles[{i}].provider: must be a string")
                    ws = role.get("write_scope")
                    if ws is not None and not isinstance(ws, list):
                        errors.append(f"phase_routing.{phase}.team.roles[{i}].write_scope: must be a list")

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
