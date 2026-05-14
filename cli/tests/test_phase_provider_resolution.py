"""Tests for `awf wf next` provider resolution priority.

Issue: prior implementation only consulted ``phase_models.{phase}.inline_model``
and silently ignored ``phase_routing.{phase}.mode == "delegated"`` +
``phase_routing.{phase}.primary``. Operators setting delegated routes (e.g.
``impl: { mode: "delegated", primary: "codex" }``) saw their cycle dispatched
via inline claude-code instead of the requested provider. See CLAUDE.md
"Codex Slave 규칙" and the 2026-05-14 multi-session dispatch-cmux finding.

These tests pin the new resolution order:
  CLI --provider > phase_routing.primary (when mode=delegated)
                > phase_models.inline_model
                > global default
plus the stderr warning surfaced when both routes are configured.
"""
from __future__ import annotations

import pytest

from awf.commands.wf import _resolve_phase_provider


class _FakeConfig:
    """Minimal AwfConfig stand-in — only ``provider_name()`` is consulted."""

    def __init__(self, default: str = "claude-code") -> None:
        self._default = default

    def provider_name(self) -> str:
        return self._default


class TestExplicitOverride:
    def test_cli_provider_wins_over_everything(self) -> None:
        config = {
            "phase_routing": {"impl": {"mode": "delegated", "primary": "codex"}},
            "phase_models": {"impl": {"inline_model": "sonnet"}},
        }
        result = _resolve_phase_provider("gemini", config, "impl", _FakeConfig())
        assert result == "gemini"


class TestDelegatedRouting:
    def test_delegated_primary_wins_over_inline_model(self, capsys) -> None:
        config = {
            "phase_routing": {"impl": {"mode": "delegated", "primary": "codex"}},
            "phase_models": {"impl": {"inline_model": "sonnet"}},
        }
        result = _resolve_phase_provider(None, config, "impl", _FakeConfig())
        assert result == "codex"
        err = capsys.readouterr().err
        assert "phase_routing.impl.primary='codex'" in err
        assert "phase_models.impl.inline_model='sonnet'" in err

    def test_delegated_primary_without_inline_model_no_warning(self, capsys) -> None:
        config = {
            "phase_routing": {"impl": {"mode": "delegated", "primary": "codex"}},
        }
        result = _resolve_phase_provider(None, config, "impl", _FakeConfig())
        assert result == "codex"
        assert capsys.readouterr().err == ""

    def test_delegated_primary_resolves_inline_alias(self) -> None:
        """`primary: "sonnet"` should still resolve through INLINE_MODEL_ALIASES."""
        config = {
            "phase_routing": {"review": {"mode": "delegated", "primary": "sonnet"}},
        }
        result = _resolve_phase_provider(None, config, "review", _FakeConfig())
        assert result == "claude:sonnet"


class TestDelegatedWithoutPrimary:
    def test_falls_through_to_inline_model(self) -> None:
        """mode=delegated but no primary set → keep existing inline_model path."""
        config = {
            "phase_routing": {"impl": {"mode": "delegated"}},
            "phase_models": {"impl": {"inline_model": "sonnet"}},
        }
        result = _resolve_phase_provider(None, config, "impl", _FakeConfig())
        assert result == "claude:sonnet"

    def test_falls_through_to_default_when_nothing_set(self) -> None:
        config = {"phase_routing": {"impl": {"mode": "delegated"}}}
        result = _resolve_phase_provider(None, config, "impl", _FakeConfig("codex"))
        assert result == "codex"


class TestInlineModeUnchanged:
    def test_inline_model_alone(self) -> None:
        """No phase_routing → behavior identical to pre-fix."""
        config = {"phase_models": {"impl": {"inline_model": "sonnet"}}}
        result = _resolve_phase_provider(None, config, "impl", _FakeConfig())
        assert result == "claude:sonnet"

    def test_global_default_when_no_phase_config(self) -> None:
        result = _resolve_phase_provider(None, {}, "impl", _FakeConfig("gemini"))
        assert result == "gemini"

    def test_explicit_mode_inline_does_not_use_primary(self) -> None:
        """mode=inline (or any non-delegated value) should not consume primary."""
        config = {
            "phase_routing": {"impl": {"mode": "inline", "primary": "codex"}},
            "phase_models": {"impl": {"inline_model": "sonnet"}},
        }
        result = _resolve_phase_provider(None, config, "impl", _FakeConfig())
        assert result == "claude:sonnet"


class TestDualMode:
    def test_dual_mode_does_not_promote_primary(self) -> None:
        """Per CLAUDE.md, dual mode runs primary+secondary in parallel but
        the inline runner still uses the phase_models/global path. Only the
        delegated mode short-circuits."""
        config = {
            "phase_routing": {
                "verify": {"mode": "dual", "primary": "inline", "secondary": "claude:sonnet"}
            },
            "phase_models": {"verify": {"inline_model": "sonnet"}},
        }
        result = _resolve_phase_provider(None, config, "verify", _FakeConfig())
        assert result == "claude:sonnet"


class TestEdgeCases:
    def test_phase_routing_empty_dict(self) -> None:
        config = {"phase_routing": {}, "phase_models": {"impl": {"inline_model": "sonnet"}}}
        result = _resolve_phase_provider(None, config, "impl", _FakeConfig())
        assert result == "claude:sonnet"

    def test_phase_routing_phase_missing(self) -> None:
        config = {
            "phase_routing": {"review": {"mode": "delegated", "primary": "codex"}},
            "phase_models": {"impl": {"inline_model": "sonnet"}},
        }
        result = _resolve_phase_provider(None, config, "impl", _FakeConfig())
        assert result == "claude:sonnet"

    def test_phase_routing_none_values(self) -> None:
        """Defensive: explicit None for nested config sections should not crash."""
        config = {"phase_routing": None, "phase_models": None}
        result = _resolve_phase_provider(None, config, "impl", _FakeConfig("codex"))
        assert result == "codex"
