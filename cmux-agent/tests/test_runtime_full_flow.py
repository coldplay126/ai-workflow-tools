"""cmux-agent operational full-flow fixture tests."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

from cmux_agent.cli import commands as command_module
from cmux_agent.domain.models import Agent, AgentRole, Run, RunStatus
from cmux_agent.infrastructure.cmux import CmuxResult
from cmux_agent.infrastructure.filesystem import AgentFileSystem
from cmux_agent.infrastructure.storage import StateStore


REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_ROOT = REPO_ROOT / "templates" / "cmux"


class FakeCmux:
    """Single fake cmux session shared across CLI command invocations."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._surface_seq = 2
        self._surfaces = {"surface:1"}

    def new_workspace(self, *, cwd: str | None = None) -> CmuxResult:
        self.calls.append(("new_workspace", {"cwd": cwd}))
        return CmuxResult(ok=True, stdout="OK workspace:1", stderr="")

    def close_workspace(self, workspace_id: str) -> CmuxResult:
        self.calls.append(("close_workspace", {"workspace_id": workspace_id}))
        return CmuxResult(ok=True, stdout=f"OK {workspace_id}", stderr="")

    def close_surface(self, surface_id: str) -> CmuxResult:
        self.calls.append(("close_surface", {"surface_id": surface_id}))
        self._surfaces.discard(surface_id)
        return CmuxResult(ok=True, stdout=f"OK {surface_id}", stderr="")

    def tree(self, workspace_id: str | None = None) -> CmuxResult:
        self.calls.append(("tree", {"workspace_id": workspace_id}))
        payload = {
            "windows": [
                {
                    "workspaces": [
                        {
                            "ref": "workspace:1",
                            "panes": [{"surfaces": [{"ref": "surface:1"}]}],
                        }
                    ]
                }
            ]
        }
        return CmuxResult(ok=True, stdout=json.dumps(payload), stderr="")

    def new_surface(self, *, pane_id=None, workspace_id=None) -> CmuxResult:
        surface = f"surface:{self._surface_seq}"
        self._surface_seq += 1
        self._surfaces.add(surface)
        self.calls.append(
            ("new_surface", {"workspace_id": workspace_id, "surface": surface})
        )
        return CmuxResult(ok=True, stdout=f"OK {surface} pane:1 workspace:1", stderr="")

    def rename_tab(self, title, *, surface_id=None, workspace_id=None) -> CmuxResult:
        self.calls.append(
            (
                "rename_tab",
                {"title": title, "surface_id": surface_id, "workspace_id": workspace_id},
            )
        )
        return CmuxResult(ok=True, stdout="", stderr="")

    def send_text(self, text, *, surface_id=None, workspace_id=None) -> CmuxResult:
        self.calls.append(
            (
                "send_text",
                {"text": text, "surface_id": surface_id, "workspace_id": workspace_id},
            )
        )
        return CmuxResult(ok=True, stdout="", stderr="")

    def send_key(self, key, *, surface_id=None, workspace_id=None) -> CmuxResult:
        self.calls.append(
            (
                "send_key",
                {"key": key, "surface_id": surface_id, "workspace_id": workspace_id},
            )
        )
        return CmuxResult(ok=True, stdout="", stderr="")

    def notify(self, title: str, body: str = "") -> CmuxResult:
        self.calls.append(("notify", {"title": title, "body": body}))
        return CmuxResult(ok=True, stdout="", stderr="")

    def log(self, message, *, level="info", source=None, workspace_id=None) -> CmuxResult:
        self.calls.append(
            (
                "log",
                {
                    "message": message,
                    "level": level,
                    "source": source,
                    "workspace_id": workspace_id,
                },
            )
        )
        return CmuxResult(ok=True, stdout="", stderr="")

    def is_surface_alive(self, surface_id: str) -> bool:
        return surface_id in self._surfaces


def _send_texts(fake: FakeCmux) -> list[dict]:
    return [payload for name, payload in fake.calls if name == "send_text"]


def test_send_creates_unique_dispatch_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(command_module, "_active_cwd", str(tmp_path))

    fs = AgentFileSystem(tmp_path / ".agent")
    fs.init()
    store = StateStore(fs.db_path)
    run = Run(run_id="run-1", status=RunStatus.RUNNING, workspace_id="workspace:1")
    store.save_run(run)
    store.save_agent(
        Agent(
            run_id=run.run_id,
            role=AgentRole.WORKER,
            name="worker-1",
            surface_id="surface:1",
        )
    )

    command_module.cmd_send(Namespace(recipient="worker-1", message="first"))
    command_module.cmd_send(Namespace(recipient="worker-1", message="second"))

    artifacts = sorted(fs.outbox.glob("*-controller-dispatch.json"))
    assert len(artifacts) == 2
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in artifacts]
    assert {payload["message"] for payload in payloads} == {"first", "second"}


def test_start_uses_template_provider_fallbacks(tmp_path, monkeypatch):
    fake = FakeCmux()
    monkeypatch.setattr(command_module, "CmuxAdapter", lambda: fake)
    monkeypatch.setattr(command_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(command_module, "_active_cwd", str(tmp_path))
    monkeypatch.setattr(command_module, "_active_template_dir", None)
    monkeypatch.setattr(
        "cmux_agent.application.runtime.shutil.which",
        lambda command: f"/bin/{command}" if command == "claude" else None,
    )

    command_module.cmd_start(
        Namespace(
            cwd=str(tmp_path),
            template="conductor",
            templates_dir=str(TEMPLATES_ROOT),
        )
    )

    send_texts = _send_texts(fake)
    # conductor 템플릿: orchestrator/plan/review/verify 의 claude entry는 opus 명시,
    # worker-impl fallback claude는 sonnet 명시 (BLIP Gem cycle §8 model routing).
    opus_cmd = "claude --model claude-opus-4-7 --effort max --permission-mode acceptEdits\n"
    sonnet_cmd = "claude --model claude-sonnet-4-6 --permission-mode acceptEdits\n"
    assert any(call["text"] == opus_cmd for call in send_texts)
    assert any(call["text"] == sonnet_cmd for call in send_texts)
    assert not any(call["text"].startswith("gemini ") for call in send_texts)
    assert not any(call["text"].startswith("codex ") for call in send_texts)
    assert any(
        name == "log"
        and payload["message"] == "provider fallback: worker-plan gemini -> claude"
        for name, payload in fake.calls
    )
    assert any(
        name == "log"
        and payload["message"] == "provider fallback: worker-impl codex -> claude"
        for name, payload in fake.calls
    )


def test_start_task_and_spawn_preserve_template_contract(tmp_path, monkeypatch):
    fake = FakeCmux()
    monkeypatch.setattr(command_module, "CmuxAdapter", lambda: fake)
    monkeypatch.setattr(command_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(command_module, "_active_cwd", str(tmp_path))
    monkeypatch.setattr(command_module, "_active_template_dir", None)

    command_module.cmd_start(
        Namespace(
            cwd=str(tmp_path),
            template="bugfix",
            templates_dir=str(TEMPLATES_ROOT),
        )
    )

    fs = AgentFileSystem(tmp_path / ".agent")
    store = StateStore(fs.db_path)
    run = store.get_active_run()
    assert run is not None
    assert run.status == RunStatus.RUNNING
    assert run.workspace_id == "workspace:1"

    state = json.loads((fs.base / "template-state.json").read_text(encoding="utf-8"))
    assert state["template"] == "bugfix"
    assert state["template_dir"] == str((TEMPLATES_ROOT / "bugfix").resolve())

    agents = {agent.name: agent for agent in store.get_agents(run.run_id)}
    assert set(agents) == {
        "controller",
        "orchestrator",
        "worker-fix",
        "worker-investigate",
    }
    assert agents["controller"].role == AgentRole.CONTROLLER
    assert agents["orchestrator"].surface_id == "surface:2"
    assert agents["worker-fix"].surface_id == "surface:3"
    assert agents["worker-investigate"].surface_id == "surface:4"

    assert (fs.base / "ORCHESTRATOR-COMMON.md").is_file()
    assert (fs.base / "ORCHESTRATOR.md").is_file()
    assert (fs.base / "WORKER-COMMON.md").is_file()
    assert (fs.base / "WORKER-FIX.md").is_file()
    assert (fs.base / "WORKER-INVESTIGATE.md").is_file()
    worker_fix_protocol = (fs.base / "WORKER-FIX.md").read_text(encoding="utf-8")
    assert "claude/agents/implementer.md" in worker_fix_protocol

    texts = _send_texts(fake)
    assert any(
        call["surface_id"] == "surface:1"
        and " -m cmux_agent " in call["text"]
        and f"--cwd {tmp_path}" in call["text"]
        and call["text"].endswith(" watch\n")
        for call in texts
    )
    assert any(
        call["surface_id"] == "surface:2"
        and call["text"] == "claude --model claude-opus-4-7 --effort max --permission-mode acceptEdits\n"
        for call in texts
    )
    assert any(
        call["surface_id"] == "surface:2"
        and "ORCHESTRATOR.md" in call["text"]
        and "dispatch artifact" in call["text"]
        for call in texts
    )
    assert any(
        call["surface_id"] == "surface:3"
        and call["text"] == "codex -c model_reasoning_effort=xhigh\n"
        for call in texts
    )
    assert any(
        call["surface_id"] == "surface:3"
        and "WORKER-FIX.md" in call["text"]
        and "docs/templates/gap.md" in call["text"]
        for call in texts
    )

    command_module.cmd_task(Namespace(request="Fix intermittent login failure"))

    task_prompt = _send_texts(fake)[-1]
    assert task_prompt["surface_id"] == "surface:2"
    assert "Fix intermittent login failure" in task_prompt["text"]
    assert "worker-fix" in task_prompt["text"]
    assert "worker-investigate" in task_prompt["text"]
    assert any(
        name == "send_key"
        and payload["surface_id"] == "surface:2"
        and payload["key"] == "enter"
        for name, payload in fake.calls
    )

    # Simulate the separate watcher/spawn process losing in-memory template state.
    monkeypatch.setattr(command_module, "_active_template_dir", None)
    command_module.cmd_spawn(Namespace(name="worker-review", provider=None, flags=None))

    review_worker = store.get_agent_by_name(run.run_id, "worker-review")
    assert review_worker is not None
    assert review_worker.surface_id == "surface:5"
    assert (fs.inbox / "worker-review").is_dir()
    assert (fs.base / "WORKER-REVIEW.md").is_file()
    assert "Agent Card" in (fs.base / "WORKER-REVIEW.md").read_text(encoding="utf-8")

    assert any(
        call["surface_id"] == "surface:5" and call["text"] == "claude\n"
        for call in _send_texts(fake)
    )

    event_names = [
        event["event"]
        for event in command_module._get_event_log(fs).read_all(run.run_id)
    ]
    assert event_names.count("run.created") == 1
    assert event_names.count("agent.registered") == 5
    assert "run.status_changed" in event_names


def test_start_can_attach_current_session_as_orchestrator(tmp_path, monkeypatch, capsys):
    fake = FakeCmux()
    monkeypatch.setattr(command_module, "CmuxAdapter", lambda: fake)
    monkeypatch.setattr(command_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(command_module, "_active_cwd", str(tmp_path))
    monkeypatch.setattr(command_module, "_active_template_dir", None)

    command_module.cmd_start(
        Namespace(
            cwd=str(tmp_path),
            template="review",
            templates_dir=str(TEMPLATES_ROOT),
            attach_orchestrator=True,
        )
    )

    fs = AgentFileSystem(tmp_path / ".agent")
    store = StateStore(fs.db_path)
    run = store.get_active_run()
    assert run is not None

    agents = {agent.name: agent for agent in store.get_agents(run.run_id)}
    assert set(agents) == {"controller", "orchestrator", "worker-review"}
    assert agents["controller"].surface_id == "surface:1"
    assert agents["orchestrator"].surface_id is None
    assert agents["worker-review"].surface_id == "surface:2"

    send_texts = _send_texts(fake)
    assert any(
        call["surface_id"] == "surface:1"
        and " -m cmux_agent " in call["text"]
        and f"--cwd {tmp_path}" in call["text"]
        and call["text"].endswith(" watch\n")
        for call in send_texts
    )
    assert not any(
        call["text"] == "claude --effort max\n" and call["surface_id"] is None
        for call in send_texts
    )
    assert any(
        call["surface_id"] == "surface:2"
        and "codex" in call["text"]
        for call in send_texts
    )
    assert any(
        call["surface_id"] == "surface:2"
        and "WORKER-REVIEW.md" in call["text"]
        for call in send_texts
    )

    output = capsys.readouterr().out
    assert "현재 세션이 orchestrator입니다." in output
    assert "ORCHESTRATOR.md" in output

    command_module.cmd_task(Namespace(request="Review the staged changes"))
    task_output = capsys.readouterr().out
    assert "attached orchestrator" in task_output
    assert "Review the staged changes" in task_output
    assert str(fs.outbox) in task_output


def test_smoke_runs_attach_spawn_task_and_cleanup(tmp_path, monkeypatch, capsys):
    fake = FakeCmux()
    smoke_cwd = tmp_path / "smoke"
    monkeypatch.setattr(command_module, "CmuxAdapter", lambda: fake)
    monkeypatch.setattr(command_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(command_module, "_active_cwd", str(tmp_path))
    monkeypatch.setattr(command_module, "_active_template_dir", None)

    def fake_wait(fs, store, run_id, *, timeout, poll_interval):
        artifact = fs.outbox / "smoke-spawn-test.json"
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        assert payload["agent"]["template"] == "test"
        assert payload["agent"]["provider"] == "codex"

        fake._surfaces.add("surface:3")
        agent = Agent(
            run_id=run_id,
            role=AgentRole.WORKER,
            name="worker-test",
            surface_id="surface:3",
        )
        store.save_agent(agent)
        fs.create_inbox(agent.name)
        (fs.base / "WORKER-TEST.md").write_text(
            "# cmux-agent worker-test 프로토콜\n", encoding="utf-8"
        )
        fs.move_to_processed(artifact)
        result = {
            "type": "result",
            "from": "controller",
            "result": "spawned worker-test (codex)",
            "context": {
                "control_action": "spawn_agent",
                "agent": {
                    "name": "worker-test",
                    "provider": "codex",
                    "surface_id": "surface:3",
                },
            },
        }
        fs.write_to_inbox("orchestrator", "smoke-result", result)
        return result

    monkeypatch.setattr(command_module, "_wait_for_smoke_spawn_result", fake_wait)

    command_module.cmd_smoke(
        Namespace(
            cwd=".",
            smoke_cwd=str(smoke_cwd),
            template=None,
            smoke_template="review",
            templates_dir=str(TEMPLATES_ROOT),
            smoke_templates_dir=str(TEMPLATES_ROOT),
            worker_template="test",
            provider="codex",
            timeout=1.0,
            poll_interval=0.0,
            keep=False,
        )
    )

    output = capsys.readouterr().out
    assert "Smoke PASS" in output
    assert "Spawned worker: worker-test (codex)" in output
    assert "사용 가능한 worker: worker-review, worker-test" in output
    assert any(
        name == "close_workspace" and payload["workspace_id"] == "workspace:1"
        for name, payload in fake.calls
    )
    assert not (smoke_cwd / ".agent").exists()
