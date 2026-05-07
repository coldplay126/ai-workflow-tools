"""설정 파일 로딩 테스트."""

import json
import os

from cmux_agent.cli.commands import _load_config, _normalize_agent_entry, DEFAULT_CONFIG


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
