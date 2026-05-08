"""MessageBroker 테스트."""

import json
from pathlib import Path

import pytest

from cmux_agent.application.broker import MessageBroker
from cmux_agent.application.prompting import PromptBuilder
from cmux_agent.application.runtime import AgentRuntime
from cmux_agent.domain.models import Agent, AgentRole, Message, MessageStatus, Run
from cmux_agent.infrastructure.cmux import CmuxAdapter, CmuxResult
from cmux_agent.infrastructure.event_log import EventLog
from cmux_agent.infrastructure.filesystem import AgentFileSystem
from cmux_agent.infrastructure.storage import StateStore


class FakeCmux(CmuxAdapter):
    """cmux 호출을 기록만 하는 fake."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._surface_seq = 3

    def _run(self, *args, timeout=10):
        self.calls.append(("_run", {"args": args}))
        return CmuxResult(ok=True, stdout="", stderr="")

    def new_surface(self, *, pane_id=None, workspace_id=None):
        surface = f"surface:{self._surface_seq}"
        self._surface_seq += 1
        self.calls.append(("new_surface", {"workspace_id": workspace_id, "surface": surface}))
        return CmuxResult(ok=True, stdout=f"OK {surface} pane:1 workspace:1", stderr="")

    def rename_tab(self, title, *, surface_id=None, workspace_id=None):
        self.calls.append(("rename_tab", {"title": title, "surface_id": surface_id, "workspace_id": workspace_id}))
        return CmuxResult(ok=True, stdout="", stderr="")

    def send_text(self, text, *, surface_id=None, workspace_id=None):
        self.calls.append(("send_text", {"text": text, "surface_id": surface_id, "workspace_id": workspace_id}))
        return CmuxResult(ok=True, stdout="", stderr="")

    def send_key(self, key, *, surface_id=None, workspace_id=None):
        self.calls.append(("send_key", {"key": key, "surface_id": surface_id, "workspace_id": workspace_id}))
        return CmuxResult(ok=True, stdout="", stderr="")

    def notify(self, title: str, body: str = ""):
        self.calls.append(("notify", {"title": title, "body": body}))
        return CmuxResult(ok=True, stdout="", stderr="")

    def log(self, message, *, level="info", source=None, workspace_id=None):
        self.calls.append(("log", {"message": message, "level": level, "source": source, "workspace_id": workspace_id}))
        return CmuxResult(ok=True, stdout="", stderr="")

    def is_surface_alive(self, surface_id: str) -> bool:
        return True


@pytest.fixture
def setup(tmp_path):
    fs = AgentFileSystem(tmp_path / ".agent")
    fs.init()
    store = StateStore(fs.db_path)
    event_log = EventLog(fs.event_log_path)
    cmux = FakeCmux()

    run = Run(run_id="run-1")
    store.save_run(run)

    store.save_agent(Agent(
        run_id="run-1", role=AgentRole.ORCHESTRATOR,
        name="orchestrator", surface_id="s:1",
    ))
    store.save_agent(Agent(
        run_id="run-1", role=AgentRole.WORKER,
        name="worker-1", surface_id="s:2",
    ))
    store.save_agent(Agent(
        run_id="run-1", role=AgentRole.CONTROLLER,
        name="controller",
    ))

    prompt_builder = PromptBuilder(str(fs.outbox), str(fs.inbox))
    runtime = AgentRuntime(
        store=store,
        event_log=event_log,
        fs=fs,
        cmux=cmux,
        prompt_builder=prompt_builder,
        run_id="run-1",
        workspace_id="workspace:1",
    )
    broker = MessageBroker(
        store=store, event_log=event_log, fs=fs,
        cmux=cmux, prompt_builder=prompt_builder, run_id="run-1",
        workspace_id="workspace:1", runtime=runtime,
    )
    return broker, store, fs, event_log, cmux


class TestBrokerRouting:
    def test_dispatch_routes_to_worker(self, setup):
        broker, store, fs, *_ = setup

        artifact_path = fs.outbox / "test.json"
        artifact_path.write_text("{}")

        data = {
            "type": "dispatch",
            "sender": "orchestrator",
            "recipient": "worker-1",
            "message": "do stuff",
        }
        broker.handle_artifact(artifact_path, data)

        # inbox에 파일 생성 확인
        inbox_files = list((fs.inbox / "worker-1").iterdir())
        assert len(inbox_files) == 1

        # message 기록 확인
        messages = store.get_messages("run-1")
        assert len(messages) == 1
        assert messages[0].status == MessageStatus.DELIVERED

    def test_result_routes_to_orchestrator(self, setup):
        broker, store, fs, *_ = setup

        artifact_path = fs.outbox / "result.json"
        artifact_path.write_text("{}")

        data = {
            "type": "result",
            "sender": "worker-1",
            "recipient": "orchestrator",
            "message": "done",
        }
        broker.handle_artifact(artifact_path, data)

        inbox_files = list((fs.inbox / "orchestrator").iterdir())
        assert len(inbox_files) == 1

    def test_unknown_sender_moves_to_failed(self, setup):
        broker, _, fs, *_ = setup

        artifact_path = fs.outbox / "bad.json"
        artifact_path.write_text("{}")

        data = {
            "type": "dispatch",
            "sender": "unknown",
            "recipient": "worker-1",
            "message": "x",
        }
        broker.handle_artifact(artifact_path, data)

        assert list(fs.failed.iterdir())

    def test_unknown_recipient_moves_to_failed(self, setup):
        broker, _, fs, *_ = setup

        artifact_path = fs.outbox / "bad2.json"
        artifact_path.write_text("{}")

        data = {
            "type": "dispatch",
            "sender": "orchestrator",
            "recipient": "no-one",
            "message": "x",
        }
        broker.handle_artifact(artifact_path, data)

        assert list(fs.failed.iterdir())

    def test_validation_error_moves_to_failed(self, setup):
        broker, _, fs, *_ = setup

        artifact_path = fs.outbox / "err.json"
        artifact_path.write_text("{}")

        data = {"_error": "bad format", "type": "dispatch"}
        broker.handle_artifact(artifact_path, data)

        assert list(fs.failed.iterdir())

    def test_control_spawn_agent_creates_worker(self, setup):
        broker, store, fs, _, cmux = setup

        artifact_path = fs.outbox / "spawn.json"
        artifact_path.write_text("{}")

        data = {
            "type": "control",
            "sender": "orchestrator",
            "recipient": "controller",
            "message": "need isolated implementation worker",
            "action": "spawn_agent",
            "agent": {"name": "worker-api", "provider": "codex"},
        }
        broker.handle_artifact(artifact_path, data)

        worker = store.get_agent_by_name("run-1", "worker-api")
        assert worker is not None
        assert worker.role == AgentRole.WORKER
        assert worker.surface_id == "surface:3"
        assert (fs.inbox / "worker-api").exists()

        inbox_files = list((fs.inbox / "orchestrator").iterdir())
        assert len(inbox_files) == 1
        payload = json.loads(inbox_files[0].read_text())
        assert payload["result"] == "spawned worker-api (codex)"
        assert any(call[0] == "new_surface" for call in cmux.calls)
        assert any(
            call[0] == "send_text" and "npx @openai/codex" in call[1]["text"]
            for call in cmux.calls
        )
