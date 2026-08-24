"""Provider effort injection tests."""

from types import SimpleNamespace
from unittest import mock

from awf.providers.claude_code import ClaudeCodeProvider
from awf.providers.codex import CodexProvider
from awf.providers.gemini import GeminiProvider


class TestClaudeCodeProviderEffort:
    def test_default_no_effort(self):
        provider = ClaudeCodeProvider(command="echo", flags=["--print"])
        assert provider.effort is None

    def test_effort_set(self):
        provider = ClaudeCodeProvider(command="echo", flags=["--print"], effort="max")
        assert provider.effort == "max"

    def test_build_spawn_spec_preserves_effort_dirs_schema_and_prompt(self):
        provider = ClaudeCodeProvider(
            command="claude",
            flags=["--print"],
            effort="max",
            json_schema="/tmp/claude-schema.json",
        )

        spawn_spec = provider.build_spawn_spec("review", add_dirs=["/tmp/docs"], stream_json=True)

        assert spawn_spec.argv == [
            "claude",
            "--print",
            "--effort",
            "max",
            "--add-dir",
            "/tmp/docs",
            "--json-schema",
            "/tmp/claude-schema.json",
            "review",
        ]
        assert spawn_spec.stdin is None

    def test_set_permission_mode_replaces_existing_flag(self):
        provider = ClaudeCodeProvider(command="echo", flags=["--print", "--permission-mode", "default"])
        provider.set_permission_mode("bypassPermissions")
        assert provider.flags == ["--print", "--permission-mode", "bypassPermissions"]

    def test_set_model_adds_when_absent(self):
        provider = ClaudeCodeProvider(command="echo", flags=["--print"])
        provider.set_model("sonnet")
        assert provider.flags == ["--print", "--model", "sonnet"]

    def test_set_model_replaces_existing_flag(self):
        provider = ClaudeCodeProvider(command="echo", flags=["--print", "--model", "opus"])
        provider.set_model("claude-sonnet-5")
        assert provider.flags == ["--print", "--model", "claude-sonnet-5"]


class TestCodexProviderEffort:
    def test_default_no_reasoning_effort(self):
        provider = CodexProvider(command="echo", flags=["exec"])
        assert provider.reasoning_effort is None

    def test_reasoning_effort_set(self):
        provider = CodexProvider(command="echo", flags=["exec"], reasoning_effort="xhigh")
        assert provider.reasoning_effort == "xhigh"

    def test_set_sandbox_replaces_existing_flag(self):
        provider = CodexProvider(command="echo", flags=["exec", "--sandbox", "workspace-write"])
        provider.set_sandbox("read-only")
        assert provider.flags == ["exec", "--sandbox", "read-only"]

    def test_complete_passes_add_dirs_and_schema(self):
        provider = CodexProvider(
            command="codex",
            flags=["exec", "--sandbox", "read-only"],
            reasoning_effort="xhigh",
            output_schema_path="/tmp/awf-schema.json",
        )
        completed = SimpleNamespace(returncode=0, stdout="{}", stderr="")
        with mock.patch("awf.providers.codex.subprocess.run", return_value=completed) as run_mock:
            result = provider.complete("prompt", cwd="/tmp/repo", add_dirs=["/tmp/docs"])

        assert result.returncode == 0
        cmd = run_mock.call_args.args[0]
        assert cmd[:3] == ["codex", "exec", "--sandbox"]
        assert ["--add-dir", "/tmp/docs"] == cmd[cmd.index("--add-dir"):cmd.index("--add-dir") + 2]
        assert ["--output-schema", "/tmp/awf-schema.json"] == cmd[cmd.index("--output-schema"):cmd.index("--output-schema") + 2]
        assert ["-c", "model_reasoning_effort=xhigh"] == cmd[cmd.index("-c"):cmd.index("-c") + 2]
        assert run_mock.call_args.kwargs["input"] == "prompt"
        assert cmd[-1] == "-"


class TestGeminiProvider:
    def test_auto_model_omits_model_flag(self):
        provider = GeminiProvider(command="gemini", flags=["--output-format", "text"], model="")
        completed = SimpleNamespace(returncode=0, stdout="ok", stderr="")
        with mock.patch("awf.providers.gemini.subprocess.run", return_value=completed) as run_mock:
            result = provider.complete("prompt", cwd="/tmp/repo", add_dirs=["/tmp/docs"])

        assert result.returncode == 0
        cmd = run_mock.call_args.args[0]
        assert cmd[:3] == ["gemini", "--output-format", "text"]
        assert "--model" not in cmd
        assert ["--include-directories", "/tmp/docs"] == cmd[
            cmd.index("--include-directories"):cmd.index("--include-directories") + 2
        ]
        assert cmd[-2:] == ["--prompt", ""]
        assert run_mock.call_args.kwargs["input"] == "prompt"

    def test_explicit_model_is_passed(self):
        provider = GeminiProvider(
            command="gemini",
            flags=["--output-format", "text"],
            model="gemini-3.6-flash",
        )
        completed = SimpleNamespace(returncode=0, stdout="ok", stderr="")
        with mock.patch("awf.providers.gemini.subprocess.run", return_value=completed) as run_mock:
            provider.complete("prompt")

        cmd = run_mock.call_args.args[0]
        assert ["--model", "gemini-3.6-flash"] == cmd[
            cmd.index("--model"):cmd.index("--model") + 2
        ]

    def test_set_permission_mode_maps_yolo(self):
        provider = GeminiProvider(command="gemini", flags=["--output-format", "text"])
        provider.set_permission_mode("bypassPermissions")
        assert provider.flags == [
            "--output-format",
            "text",
            "--approval-mode",
            "yolo",
        ]


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

    def test_apply_phase_effort_injects_inline_model(self):
        """inline_model이 --model flag로 주입되어야 CLI invocation에서 모델 분기가 동작."""
        from awf.commands.wf import _apply_phase_effort

        provider = ClaudeCodeProvider(command="echo", flags=["--print"])
        provider_config = {
            "phase_models": {"impl": {"inline_model": "sonnet", "effort": "high"}}
        }

        _apply_phase_effort(provider, provider_config, "impl")
        assert provider.effort == "high"
        assert "--model" in provider.flags
        assert provider.flags[provider.flags.index("--model") + 1] == "sonnet"

    def test_apply_phase_effort_inline_model_replaces_existing(self):
        from awf.commands.wf import _apply_phase_effort

        provider = ClaudeCodeProvider(command="echo", flags=["--print", "--model", "opus"])
        provider_config = {
            "phase_models": {"impl": {"inline_model": "claude-sonnet-5"}}
        }

        _apply_phase_effort(provider, provider_config, "impl")
        assert provider.flags == ["--print", "--model", "claude-sonnet-5"]

    def test_apply_phase_sandbox_codex_read_only(self):
        from awf.commands.wf import _apply_phase_sandbox

        provider = CodexProvider(command="echo", flags=["exec", "--sandbox", "workspace-write"])
        _apply_phase_sandbox(provider, "review")
        assert provider.flags == ["exec", "--sandbox", "read-only"]

    def test_apply_phase_sandbox_verify_write(self):
        from awf.commands.wf import _apply_phase_sandbox

        provider = CodexProvider(command="echo", flags=["exec", "--sandbox", "read-only"])
        _apply_phase_sandbox(provider, "verify")
        assert provider.flags == ["exec", "--sandbox", "workspace-write"]

    def test_apply_phase_sandbox_plan_write(self):
        from awf.commands.wf import _apply_phase_sandbox

        provider = CodexProvider(command="echo", flags=["exec", "--sandbox", "read-only"])
        _apply_phase_sandbox(provider, "plan")
        assert provider.flags == ["exec", "--sandbox", "workspace-write"]

    def test_apply_phase_sandbox_impl_write(self):
        from awf.commands.wf import _apply_phase_sandbox

        provider = CodexProvider(command="echo", flags=["exec", "--sandbox", "read-only"])
        _apply_phase_sandbox(provider, "impl")
        assert provider.flags == ["exec", "--sandbox", "workspace-write"]

    def test_apply_workflow_output_schema_codex_uses_local_validation(self):
        from awf.commands.wf import _apply_workflow_output_schema

        provider = CodexProvider(command="echo", flags=["exec"])
        cleanup_path = _apply_workflow_output_schema(provider, "plan")
        assert cleanup_path is None
        assert provider.output_schema_path is None

    def test_apply_workflow_output_schema_claude(self):
        from awf.commands.wf import _apply_workflow_output_schema

        provider = ClaudeCodeProvider(command="echo", flags=["--print"])
        cleanup_path = _apply_workflow_output_schema(provider, "review")
        assert cleanup_path is None
        assert provider.json_schema is not None
        assert '"status"' in provider.json_schema

    def test_apply_phase_effort_no_config(self):
        from awf.commands.wf import _apply_phase_effort

        provider = ClaudeCodeProvider(command="echo", flags=["--print"])
        _apply_phase_effort(provider, {}, "plan")
        assert provider.effort is None
