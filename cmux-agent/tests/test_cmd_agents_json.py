"""Agent roster probes must distinguish active runs from historical runs."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from cmux_agent.cli import commands as command_module
from cmux_agent.domain.models import Agent, AgentRole, Run, RunStatus
from cmux_agent.infrastructure.filesystem import AgentFileSystem
from cmux_agent.infrastructure.storage import StateStore


def _seed_run(tmp_path: Path) -> None:
    fs = AgentFileSystem(tmp_path / ".agent")
    fs.init()
    store = StateStore(fs.db_path)
    store.save_run(Run(run_id="run-1", status=RunStatus.RUNNING, workspace_id="workspace:1"))
    store.save_agent(Agent(run_id="run-1", role=AgentRole.ORCHESTRATOR, name="orchestrator", surface_id="surface:2"))
    store.save_agent(Agent(run_id="run-1", role=AgentRole.WORKER, name="worker-impl", surface_id="surface:3"))


def test_json_lists_agents(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(command_module, "_active_cwd", str(tmp_path))
    _seed_run(tmp_path)
    command_module.cmd_agents(Namespace(run_id=None, json=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == "run-1"
    names = {a["name"] for a in payload["agents"]}
    assert names == {"orchestrator", "worker-impl"}
    roles = {a["role"].lower() for a in payload["agents"]}
    assert roles == {"orchestrator", "worker"}


def test_json_empty_when_no_agents(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(command_module, "_active_cwd", str(tmp_path))
    fs = AgentFileSystem(tmp_path / ".agent")
    fs.init()
    store = StateStore(fs.db_path)
    store.save_run(Run(run_id="run-2", status=RunStatus.RUNNING, workspace_id="workspace:1"))
    command_module.cmd_agents(Namespace(run_id=None, json=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["agents"] == []


def test_json_without_run_is_empty_and_does_not_initialize_state(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    monkeypatch.setattr(command_module, "_active_cwd", str(tmp_path))
    command_module.cmd_agents(Namespace(run_id=None, json=True))

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"run_id": None, "agents": []}
    assert captured.err == ""
    assert not (tmp_path / ".agent").exists()


@pytest.mark.parametrize("status", [RunStatus.COMPLETED, RunStatus.FAILED])
def test_json_probe_excludes_finished_run_but_allows_explicit_history(
    tmp_path: Path, monkeypatch, capsys, status: RunStatus,
) -> None:
    monkeypatch.setattr(command_module, "_active_cwd", str(tmp_path))
    _seed_run(tmp_path)
    store = StateStore(tmp_path / ".agent" / "control-plane.sqlite3")
    try:
        store.update_run_status("run-1", status)
    finally:
        store.close()

    command_module.cmd_agents(Namespace(run_id=None, json=True))
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"run_id": None, "agents": []}
    assert captured.err == ""

    command_module.cmd_agents(Namespace(run_id="run-1", json=True))
    historical = json.loads(capsys.readouterr().out)
    assert historical["run_id"] == "run-1"
    assert {agent["name"] for agent in historical["agents"]} == {
        "orchestrator", "worker-impl",
    }


def test_text_output_unchanged_when_no_json_flag(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(command_module, "_active_cwd", str(tmp_path))
    _seed_run(tmp_path)
    command_module.cmd_agents(Namespace(run_id=None, json=False))
    out = capsys.readouterr().out
    assert "Run:" in out
    assert "worker-impl" in out
    # Should NOT be json
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
