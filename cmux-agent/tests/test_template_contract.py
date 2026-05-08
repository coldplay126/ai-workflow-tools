"""Repository cmux template contract tests."""

from __future__ import annotations

import json
from pathlib import Path

from cmux_agent.application.prompting import PromptBuilder
from cmux_agent.domain.models import Agent, AgentRole


REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_ROOT = REPO_ROOT / "templates" / "cmux"
PROFILES = ("feature", "bugfix", "review")
ALLOWED_PROVIDERS = {"claude", "codex", "gemini"}


def _load_profile(profile: str) -> dict:
    path = TEMPLATES_ROOT / profile / "cmux-agent.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _entry_provider(entry: str | dict) -> str:
    if isinstance(entry, str):
        return entry
    return str(entry.get("provider", ""))


def test_cmux_profiles_have_valid_config_and_matching_worker_protocols():
    assert (TEMPLATES_ROOT / "ORCHESTRATOR-COMMON.md").is_file()
    assert (TEMPLATES_ROOT / "workers" / "WORKER-COMMON.md").is_file()

    for profile in PROFILES:
        profile_dir = TEMPLATES_ROOT / profile
        assert (profile_dir / ".agent-custom" / "ORCHESTRATOR.md").is_file()

        config = _load_profile(profile)
        assert "orchestrator" in config

        for name, entry in config.items():
            provider = _entry_provider(entry)
            assert provider in ALLOWED_PROVIDERS
            if name.startswith("worker-"):
                assert (TEMPLATES_ROOT / "workers" / f"{name.upper()}.md").is_file()


def test_prompt_builder_materializes_profile_protocol_files(tmp_path):
    builder = PromptBuilder(
        outbox_path=str(tmp_path / ".agent" / "outbox"),
        inbox_base=str(tmp_path / ".agent" / "inbox"),
    )

    for profile in PROFILES:
        out_dir = tmp_path / profile
        out_dir.mkdir()
        config = _load_profile(profile)
        agents = [Agent(run_id="run-1", role=AgentRole.ORCHESTRATOR, name="orchestrator")]
        agents.extend(
            Agent(run_id="run-1", role=AgentRole.WORKER, name=name)
            for name in sorted(config)
            if name.startswith("worker-")
        )

        builder.write_protocol_files(out_dir, agents, template_dir=TEMPLATES_ROOT / profile)

        assert (out_dir / "ORCHESTRATOR-COMMON.md").is_file()
        assert (out_dir / "ORCHESTRATOR.md").is_file()
        assert (out_dir / "WORKER-COMMON.md").is_file()
        for agent in agents:
            if agent.role == AgentRole.WORKER:
                assert (out_dir / f"{agent.name.upper()}.md").is_file()
