"""Provider fallback selection tests."""

from cmux_agent.application.runtime import (
    provider_command,
    resolve_provider_selection,
)


def _exists(*available: str):
    found = set(available)
    return lambda command: f"/bin/{command}" if command in found else None


def test_resolve_provider_uses_requested_provider_when_available():
    selected = resolve_provider_selection(
        {
            "provider": "gemini",
            "flags": "--skip-trust",
            "fallbacks": [{"provider": "claude", "flags": "--effort max"}],
        },
        command_exists=_exists("gemini", "claude"),
    )

    assert selected.provider == "gemini"
    assert selected.flags == "--skip-trust"
    assert not selected.used_fallback


def test_resolve_provider_uses_explicit_fallback_flags():
    selected = resolve_provider_selection(
        {
            "provider": "gemini",
            "flags": "--skip-trust",
            "fallbacks": [{"provider": "claude", "flags": "--effort max"}],
        },
        command_exists=_exists("claude"),
    )

    assert selected.provider == "claude"
    assert selected.flags == "--effort max"
    assert selected.requested_provider == "gemini"
    assert selected.used_fallback


def test_resolve_provider_uses_builtin_fallback_without_reusing_flags():
    selected = resolve_provider_selection(
        {"provider": "codex", "flags": "-c model_reasoning_effort=xhigh"},
        command_exists=_exists("gemini"),
    )

    assert selected.provider == "gemini"
    assert selected.flags == ""
    assert selected.used_fallback


def test_resolve_provider_keeps_original_when_no_provider_is_available():
    selected = resolve_provider_selection(
        {"provider": "gemini", "flags": "--skip-trust"},
        command_exists=_exists(),
    )

    assert selected.provider == "gemini"
    assert selected.flags == "--skip-trust"
    assert not selected.used_fallback


def test_provider_command_applies_selected_provider_flags():
    assert provider_command("claude", "--effort max") == "claude --effort max"
