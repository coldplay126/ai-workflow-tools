"""설정 파일 로딩 테스트."""

import json

from cmux_agent.cli import commands as command_module
from cmux_agent.cli.commands import (
    _clear_template_state,
    _load_config,
    _normalize_agent_entry,
    _resolve_template_dir,
    _restore_template_dir,
    _write_template_state,
    DEFAULT_CONFIG,
)
from cmux_agent.infrastructure.filesystem import AgentFileSystem


class TestLoadConfig:
    def test_default_when_no_file(self, tmp_path):
        config = _load_config(str(tmp_path))
        assert config == DEFAULT_CONFIG

    def test_loads_from_file(self, tmp_path):
        cfg = {
            "orchestrator": "claude",
            "worker-1": "codex",
            "worker-2": "gemini",
        }
        (tmp_path / "cmux-agent.json").write_text(json.dumps(cfg))

        config = _load_config(str(tmp_path))
        assert config["orchestrator"] == "claude"
        assert config["worker-1"] == "codex"
        assert config["worker-2"] == "gemini"

    def test_merges_with_default(self, tmp_path):
        cfg = {"worker-2": "gemini"}
        (tmp_path / "cmux-agent.json").write_text(json.dumps(cfg))

        config = _load_config(str(tmp_path))
        assert config["orchestrator"] == "claude"
        assert config["worker-1"] == "claude"
        assert config["worker-2"] == "gemini"

    def test_invalid_json_returns_default(self, tmp_path):
        (tmp_path / "cmux-agent.json").write_text("not json")
        config = _load_config(str(tmp_path))
        assert config == DEFAULT_CONFIG

    def test_loads_object_entries(self, tmp_path):
        cfg = {
            "orchestrator": {"provider": "claude", "flags": "--effort max"},
            "worker-impl": {"provider": "codex", "flags": "-c model_reasoning_effort=xhigh"},
        }
        (tmp_path / "cmux-agent.json").write_text(json.dumps(cfg))

        config = _load_config(str(tmp_path))
        assert config["orchestrator"] == {"provider": "claude", "flags": "--effort max"}
        assert config["worker-impl"] == {"provider": "codex", "flags": "-c model_reasoning_effort=xhigh"}

    def test_loads_mixed_entries(self, tmp_path):
        cfg = {
            "orchestrator": "claude",
            "worker-1": {"provider": "codex", "flags": "-c model_reasoning_effort=xhigh"},
        }
        (tmp_path / "cmux-agent.json").write_text(json.dumps(cfg))

        config = _load_config(str(tmp_path))
        assert config["orchestrator"] == "claude"
        assert config["worker-1"]["provider"] == "codex"

    def test_loads_from_restored_template_state(self, tmp_path, monkeypatch):
        template_dir = tmp_path / "templates" / "feature"
        template_dir.mkdir(parents=True)
        (template_dir / "cmux-agent.json").write_text(json.dumps({
            "orchestrator": "claude",
            "worker-impl": {"provider": "codex", "flags": "--fast"},
        }))

        fs = AgentFileSystem(tmp_path / ".agent")
        fs.init()
        _write_template_state(fs, "feature", template_dir)
        monkeypatch.setattr(command_module, "_active_template_dir", None)

        assert _restore_template_dir(fs) == template_dir
        config = _load_config(str(tmp_path))
        assert config["worker-impl"] == {"provider": "codex", "flags": "--fast"}

    def test_clear_template_state(self, tmp_path):
        template_dir = tmp_path / "template"
        template_dir.mkdir()
        fs = AgentFileSystem(tmp_path / ".agent")
        fs.init()
        _write_template_state(fs, "feature", template_dir)

        _clear_template_state(fs)

        assert _restore_template_dir(fs) is None


class TestResolveTemplateDir:
    def test_resolves_named_template_from_templates_dir(self, tmp_path):
        template_dir = tmp_path / "feature"
        template_dir.mkdir()
        (template_dir / "cmux-agent.json").write_text("{}")

        assert _resolve_template_dir("feature", str(tmp_path)) == template_dir

    def test_resolves_explicit_template_path(self, tmp_path):
        template_dir = tmp_path / "custom-template"
        template_dir.mkdir()
        (template_dir / "cmux-agent.json").write_text("{}")

        assert _resolve_template_dir(str(template_dir), None) == template_dir


class TestNormalizeAgentEntry:
    def test_string_entry(self):
        assert _normalize_agent_entry("claude") == {"provider": "claude"}

    def test_dict_entry(self):
        entry = {"provider": "codex", "flags": "-c model_reasoning_effort=xhigh"}
        result = _normalize_agent_entry(entry)
        assert result == {"provider": "codex", "flags": "-c model_reasoning_effort=xhigh"}

    def test_dict_without_flags(self):
        assert _normalize_agent_entry({"provider": "claude"}) == {"provider": "claude"}

    def test_dict_entry_is_copy(self):
        original = {"provider": "codex"}
        result = _normalize_agent_entry(original)
        result["flags"] = "test"
        assert "flags" not in original
