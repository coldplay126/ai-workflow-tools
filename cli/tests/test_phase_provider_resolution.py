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
                > Agent Card source provider_hint
                > global default
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from awf.commands.wf import _resolve_phase_provider
from awf.commands import wf as wf_commands
from awf.cli import build_parser
from awf.core.config import AwfConfig
from awf.core import spec_loader
from awf.providers.base import ProviderResult


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
    def test_delegated_primary_wins_over_inline_model(self) -> None:
        config = {
            "phase_routing": {"impl": {"mode": "delegated", "primary": "codex"}},
            "phase_models": {"impl": {"inline_model": "sonnet"}},
        }
        result = _resolve_phase_provider(None, config, "impl", _FakeConfig())
        assert result == "codex"

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


def _write_phase_agent(root: Path, name: str, metadata: str) -> None:
    cards = root / ".workflow" / "agent-cards"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / "plan.json").write_text(
        json.dumps({"agent": {"name": name}}), encoding="utf-8"
    )
    agents = root / "claude" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.md").write_text(
        f"---\nname: {name}\n{metadata}\n---\nPlan the requested change.\n",
        encoding="utf-8",
    )


@pytest.fixture
def phase_runtime(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _write_phase_agent(root, "spec-writer", "provider_hint: claude-code\nmodel: opus")
    spec_loader.clear_cache()
    # Isolate installed sources while exercising the real repo-root-aware loader.
    monkeypatch.setattr(spec_loader, "_agent_search_paths", lambda: [])
    config = AwfConfig.defaults().merge({
        "provider": {
            "default": "codex",
            "claude-code": {"command": "claude"},
            "codex": {"command": "codex"},
        }
    })
    provider_config = {}
    state = {
        "id": "role-routing",
        "currentPhase": "plan",
        "phases": {"plan": {"status": "pending"}},
    }
    monkeypatch.setattr(wf_commands, "load_awf_config", lambda _root: config)
    monkeypatch.setattr(wf_commands, "load_workflow_state", lambda _root: state)
    monkeypatch.setattr(
        wf_commands, "load_workflow_provider_config", lambda _root: provider_config
    )
    monkeypatch.setattr(
        "awf.core.state.save_workflow_state_snapshot", lambda *_args: None
    )
    monkeypatch.setattr(wf_commands, "mark_phase_in_progress", lambda *_args: None)
    monkeypatch.setattr(wf_commands, "EventProcessor", Mock())
    monkeypatch.setattr(wf_commands, "resolve_execution_mode", lambda *_args: "legacy")
    calls = []
    returncodes = {}

    def run_provider(provider, prompt, cwd, label, **kwargs):
        spawn = provider.build_spawn_spec(prompt)
        try:
            calls.append((provider.name, spawn.argv))
        finally:
            spawn.cleanup()
        return ProviderResult(
            returncode=returncodes.get(provider.name, 0), stdout="", stderr=""
        ), 0.0

    monkeypatch.setattr(wf_commands, "_run_provider_with_heartbeat", run_provider)
    args = build_parser().parse_args([
        "wf", "next", "--phase", "plan", "--mode", "solo",
        "--no-ready-gate", "--repo-root", str(root),
    ])
    yield args, provider_config, calls, returncodes
    spec_loader.clear_cache()


def test_plan_agent_hint_overrides_global_codex_in_dry_run(phase_runtime, capsys):
    args, _, calls, _ = phase_runtime
    args.dry_run = True
    args.output_format = "json"

    assert wf_commands.run_wf_next(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "claude-code"
    assert payload["fallback_chain"] == ["claude-code"]
    assert calls == []


def test_plan_agent_hint_controls_executed_provider_and_model(phase_runtime):
    args, _, calls, _ = phase_runtime

    assert wf_commands.run_wf_next(args) == 0

    assert [name for name, _ in calls] == ["claude-code"]
    argv = calls[0][1]
    assert argv[0] == "claude"
    assert argv[argv.index("--model") + 1] == "opus"


def test_agent_inherit_model_preserves_configured_claude_model(phase_runtime):
    args, _, calls, _ = phase_runtime
    _write_phase_agent(Path(args.repo_root), "spec-writer", "provider_hint: claude-code\nmodel: inherit")
    config = wf_commands.load_awf_config(args.repo_root)
    config.raw["provider"]["claude-code"]["flags"] = ["--print", "--model", "sonnet"]

    assert wf_commands.run_wf_next(args) == 0

    assert [name for name, _ in calls] == ["claude-code"]
    argv = calls[0][1]
    assert argv[argv.index("--model") + 1] == "sonnet"


@pytest.mark.parametrize(
    ("explicit", "settings", "expected_provider", "expected_model"),
    [
        ("codex", {}, "codex", None),
        ("claude:sonnet", {}, "claude-code", "sonnet"),
        (None, {"phase_routing": {"plan": {"mode": "delegated", "primary": "codex"}}}, "codex", None),
        (None, {"phase_models": {"plan": {"inline_model": "sonnet"}}}, "claude-code", "sonnet"),
    ],
)
def test_explicit_routes_override_agent_defaults(
    phase_runtime, explicit, settings, expected_provider, expected_model
):
    args, provider_config, calls, _ = phase_runtime
    args.provider = explicit
    provider_config.update(settings)

    assert wf_commands.run_wf_next(args) == 0

    assert [name for name, _ in calls] == [expected_provider]
    argv = calls[0][1]
    if expected_model is None:
        assert "--model" not in argv
    else:
        assert argv[argv.index("--model") + 1] == expected_model


def test_claude_agent_model_does_not_leak_to_codex_fallback(phase_runtime):
    args, provider_config, calls, returncodes = phase_runtime
    provider_config["fallback_chain"] = ["codex"]
    returncodes["claude-code"] = 1

    assert wf_commands.run_wf_next(args) == 0

    assert [name for name, _ in calls] == ["claude-code", "codex"]
    claude_argv, codex_argv = calls[0][1], calls[1][1]
    assert claude_argv[claude_argv.index("--model") + 1] == "opus"
    assert codex_argv[0] == "codex"
    assert "--model" not in codex_argv


def test_codex_agent_claude_frontmatter_does_not_override_claude_fallback(phase_runtime):
    args, provider_config, calls, returncodes = phase_runtime
    _write_phase_agent(Path(args.repo_root), "implementer", "provider_hint: codex\nmodel: opus")
    provider_config["fallback_chain"] = ["claude:sonnet"]
    returncodes["codex"] = 1

    assert wf_commands.run_wf_next(args) == 0

    assert [name for name, _ in calls] == ["codex", "claude-code"]
    assert "--model" not in calls[0][1]
    claude_argv = calls[1][1]
    assert claude_argv[claude_argv.index("--model") + 1] == "sonnet"


@pytest.mark.parametrize("invalid_source", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_unknown_primary_provider_does_not_silently_execute_fallback(
    phase_runtime, invalid_source, dry_run
):
    args, provider_config, calls, _ = phase_runtime
    args.dry_run = dry_run
    provider_config["fallback_chain"] = ["codex"]
    if invalid_source:
        _write_phase_agent(Path(args.repo_root), "spec-writer", "provider_hint: misspelled-provider")
    else:
        args.provider = "misspelled-provider"

    assert wf_commands.run_wf_next(args) == 2
    assert calls == []


@pytest.mark.parametrize("card", ['{"agent": []}', '{"agent": {"name": "../spec-writer"}}', "{"])
def test_invalid_existing_agent_card_does_not_use_global_default(phase_runtime, card):
    args, _, calls, _ = phase_runtime
    (Path(args.repo_root) / ".workflow" / "agent-cards" / "plan.json").write_text(
        card, encoding="utf-8"
    )

    assert wf_commands.run_wf_next(args) == 2
    assert calls == []


@pytest.mark.parametrize("missing_definition", [False, True])
def test_legacy_agent_without_provider_metadata_retains_global_default(
    phase_runtime, missing_definition
):
    args, _, calls, _ = phase_runtime
    root = Path(args.repo_root)
    _write_phase_agent(root, "legacy-planner", "model: opus")
    if missing_definition:
        (root / "claude" / "agents" / "legacy-planner.md").unlink()

    assert wf_commands.run_wf_next(args) == 0
    assert [name for name, _ in calls] == ["codex"]
    assert "--model" not in calls[0][1]


def test_explicit_repo_root_isolates_same_agent_name_between_projects(
    phase_runtime, tmp_path, monkeypatch, capsys
):
    args, _, _, _ = phase_runtime
    other_root = tmp_path / "other-project"
    _write_phase_agent(other_root, "spec-writer", "provider_hint: codex\nmodel: opus")
    monkeypatch.setattr(
        spec_loader, "_agent_search_paths",
        lambda: [other_root / "claude" / "agents"],
    )
    # The process cwd must not override --repo-root or poison its cached role.
    monkeypatch.chdir(other_root)
    args.dry_run = True
    args.output_format = "json"

    assert wf_commands.run_wf_next(args) == 0
    assert json.loads(capsys.readouterr().out)["provider"] == "claude-code"

    args.repo_root = str(other_root)
    assert wf_commands.run_wf_next(args) == 0
    assert json.loads(capsys.readouterr().out)["provider"] == "codex"
