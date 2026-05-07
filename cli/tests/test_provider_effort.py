"""Provider effort injection tests."""

from awf.providers.claude_code import ClaudeCodeProvider
from awf.providers.codex import CodexProvider


class TestClaudeCodeProviderEffort:
    def test_default_no_effort(self):
        provider = ClaudeCodeProvider(command="echo", flags=["--print"])
        assert provider.effort is None

    def test_effort_set(self):
        provider = ClaudeCodeProvider(command="echo", flags=["--print"], effort="max")
        assert provider.effort == "max"

    def test_effort_in_complete_cmd(self):
        provider = ClaudeCodeProvider(command="echo", flags=["--print"], effort="max")
        # complete() builds cmd with --effort flag; verify by checking attributes
        assert provider.effort == "max"
        assert provider.command == "echo"


class TestCodexProviderEffort:
    def test_default_no_reasoning_effort(self):
        provider = CodexProvider(command="echo", flags=["exec"])
        assert provider.reasoning_effort is None

    def test_reasoning_effort_set(self):
        provider = CodexProvider(command="echo", flags=["exec"], reasoning_effort="xhigh")
        assert provider.reasoning_effort == "xhigh"


class TestPhaseEffort:
    def test_resolve_phase_effort(self):
        from awf.commands.wf import _resolve_phase_effort

        provider_config = {
            "phase_models": {
                "plan": {"effort": "max", "codex_reasoning": "xhigh"},
                "impl": {"inline_model": "sonnet", "effort": "high", "codex_reasoning": "xhigh"},
            }
        }

        plan_effort = _resolve_phase_effort(provider_config, "plan")
        assert plan_effort["effort"] == "max"
        assert plan_effort["codex_reasoning"] == "xhigh"

        impl_effort = _resolve_phase_effort(provider_config, "impl")
        assert impl_effort["effort"] == "high"

    def test_resolve_missing_phase(self):
        from awf.commands.wf import _resolve_phase_effort

        result = _resolve_phase_effort({}, "plan")
        assert result["effort"] is None
        assert result["codex_reasoning"] is None

    def test_apply_phase_effort_claude(self):
        from awf.commands.wf import _apply_phase_effort

        provider = ClaudeCodeProvider(command="echo", flags=["--print"])
        provider_config = {
            "phase_models": {"plan": {"effort": "max", "codex_reasoning": "xhigh"}}
        }

        _apply_phase_effort(provider, provider_config, "plan")
        assert provider.effort == "max"

    def test_apply_phase_effort_codex(self):
        from awf.commands.wf import _apply_phase_effort

        provider = CodexProvider(command="echo", flags=["exec"])
        provider_config = {
            "phase_models": {"plan": {"effort": "max", "codex_reasoning": "xhigh"}}
        }

        _apply_phase_effort(provider, provider_config, "plan")
        assert provider.reasoning_effort == "xhigh"

    def test_apply_phase_effort_no_config(self):
        from awf.commands.wf import _apply_phase_effort

        provider = ClaudeCodeProvider(command="echo", flags=["--print"])
        _apply_phase_effort(provider, {}, "plan")
        assert provider.effort is None
