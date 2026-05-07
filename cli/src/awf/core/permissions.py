from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any


class PermissionDeniedError(PermissionError):
    pass


@dataclass
class PermissionRuleset:
    allowed_tools: list[str]
    disabled_tools: list[str]
    yolo: bool = False


def build_permission_ruleset(config_raw: dict[str, Any], yolo: bool = False) -> PermissionRuleset:
    permissions = config_raw.get("permissions", {})
    allowed = permissions.get("allowed_tools", [])
    disabled = permissions.get("disabled_tools", [])
    config_yolo = bool(permissions.get("yolo", False))
    return PermissionRuleset(
        allowed_tools=[str(item) for item in allowed if item],
        disabled_tools=[str(item) for item in disabled if item],
        yolo=bool(yolo or config_yolo),
    )


def provider_permission_name(provider_name: str, config_aliases: dict[str, str] | None = None) -> str:
    # Resolve config-based alias first, then hardcoded fallback
    if config_aliases and provider_name in config_aliases:
        resolved = config_aliases[provider_name]
    else:
        hardcoded = {"claude:sonnet": "claude-code"}
        resolved = hardcoded.get(provider_name, provider_name)
    return f"provider:{resolved}"


def sdk_tool_permission_name(tool_name: str) -> str:
    mapping = {
        "read_file": "tool:file.read",
        "glob_files": "tool:file.glob",
        "grep_files": "tool:file.grep",
        "git_diff": "tool:git.diff",
        "git_log": "tool:git.log",
        "mcp_call_tool": "tool:mcp.invoke",
        "mcp_read_resource": "tool:mcp.read",
    }
    return mapping.get(tool_name, f"tool:{tool_name}")


def _matches_any(tool_name: str, patterns: list[str]) -> bool:
    """Check if tool_name matches any pattern (exact or wildcard)."""
    for pattern in patterns:
        if pattern == tool_name or fnmatch(tool_name, pattern):
            return True
    return False


def check_permission(ruleset: PermissionRuleset, tool_name: str, action: str) -> None:
    if ruleset.yolo:
        return
    if _matches_any(tool_name, ruleset.disabled_tools):
        raise PermissionDeniedError(f"{tool_name} is disabled for action `{action}`")
    if _matches_any(tool_name, ruleset.allowed_tools):
        return
    raise PermissionDeniedError(
        f"{tool_name} is not allowed for action `{action}`. Add it to [permissions].allowed_tools or use --yolo."
    )
