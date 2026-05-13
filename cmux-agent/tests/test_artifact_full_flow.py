"""Artifact watcher, broker, and runtime full-flow fixture tests."""

from __future__ import annotations

import json
from pathlib import Path

from cmux_agent.application.broker import MessageBroker
from cmux_agent.application.prompting import PromptBuilder
from cmux_agent.application.runtime import AgentRuntime
from cmux_agent.application.watcher import ArtifactWatcher
from cmux_agent.domain.models import (
    Agent,
    AgentRole,
    MessageStatus,
    MessageType,
    Run,
    RunStatus,
)
from cmux_agent.infrastructure.cmux import CmuxResult
from cmux_agent.infrastructure.event_log import EventLog
from cmux_agent.infrastructure.filesystem import AgentFileSystem
from cmux_agent.infrastructure.storage import StateStore


REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_ROOT = REPO_ROOT / "templates" / "cmux"


class FakeCmux:
    """Fake cmux session shared by watcher, broker, and runtime."""

    def __init__(
        self,
        *,
        surfaces: set[str] | None = None,
        new_surface_result: CmuxResult | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._surface_seq = 3
        self._surfaces = surfaces if surfaces is not None else {"surface:1", "surface:2"}
        self._new_surface_result = new_surface_result

    def new_surface(self, *, pane_id=None, workspace_id=None) -> CmuxResult:
        if self._new_surface_result is not None:
            self.calls.append(
                ("new_surface", {"workspace_id": workspace_id, "surface": None})
            )
            return self._new_surface_result
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

    def trigger_flash(
        self,
        *,
        surface_id: str | None = None,
        workspace_id: str | None = None,
    ) -> CmuxResult:
        self.calls.append(
            (
                "trigger_flash",
                {"surface_id": surface_id, "workspace_id": workspace_id},
            )
        )
        return CmuxResult(ok=True, stdout="", stderr="")

    def tree(self, workspace_id: str | None = None) -> CmuxResult:
        # §2.10 spawn pre-check: stub workspace as reachable in tests.
        self.calls.append(("tree", {"workspace_id": workspace_id}))
        return CmuxResult(ok=True, stdout="OK", stderr="")

    def read_screen(
        self,
        *,
        surface_id: str | None = None,
        workspace_id: str | None = None,
        lines: int = 30,
    ) -> CmuxResult:
        self.calls.append(
            (
                "read_screen",
                {"surface_id": surface_id, "workspace_id": workspace_id, "lines": lines},
            )
        )
        # idle 화면 (busy 마커 없음) 반환 → broker가 즉시 dispatch 진행
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


def _write_artifact(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_inbox(inbox_dir: Path) -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(inbox_dir.iterdir())
    ]


def _calls(fake: FakeCmux, name: str) -> list[dict]:
    return [payload for call_name, payload in fake.calls if call_name == name]


def test_runtime_uses_auto_worker_names_when_spawn_name_is_omitted(tmp_path):
    fs = AgentFileSystem(tmp_path / ".agent")
    fs.init()
    store = StateStore(fs.db_path)
    event_log = EventLog(fs.event_log_path)
    cmux = FakeCmux()

    run = Run(
        run_id="run-1",
        status=RunStatus.RUNNING,
        workspace_id="workspace:1",
    )
    store.save_run(run)
    for agent in (
        Agent(run_id=run.run_id, role=AgentRole.ORCHESTRATOR, name="orchestrator"),
        Agent(run_id=run.run_id, role=AgentRole.WORKER, name="worker-auto-1"),
    ):
        store.save_agent(agent)
        fs.create_inbox(agent.name)

    runtime = AgentRuntime(
        store=store,
        event_log=event_log,
        fs=fs,
        cmux=cmux,
        prompt_builder=PromptBuilder(str(fs.outbox), str(fs.inbox)),
        run_id=run.run_id,
        workspace_id=run.workspace_id,
        template_dir=None,
        provider_config={},
    )

    result = runtime.spawn_worker(name=None, provider=None, flags=None)

    assert result.ok
    assert result.name == "worker-auto-2"
    assert store.get_agent_by_name(run.run_id, "worker-auto-2") is not None
    assert (fs.inbox / "worker-auto-2").is_dir()


def test_runtime_uses_purpose_names_and_protocol_templates_for_dynamic_workers(tmp_path):
    fs = AgentFileSystem(tmp_path / ".agent")
    fs.init()
    store = StateStore(fs.db_path)
    event_log = EventLog(fs.event_log_path)
    cmux = FakeCmux()

    run = Run(
        run_id="run-1",
        status=RunStatus.RUNNING,
        workspace_id="workspace:1",
    )
    store.save_run(run)
    for agent in (
        Agent(run_id=run.run_id, role=AgentRole.ORCHESTRATOR, name="orchestrator"),
        Agent(run_id=run.run_id, role=AgentRole.WORKER, name="worker-review"),
    ):
        store.save_agent(agent)
        fs.create_inbox(agent.name)

    runtime = AgentRuntime(
        store=store,
        event_log=event_log,
        fs=fs,
        cmux=cmux,
        prompt_builder=PromptBuilder(str(fs.outbox), str(fs.inbox)),
        run_id=run.run_id,
        workspace_id=run.workspace_id,
        template_dir=TEMPLATES_ROOT / "feature",
        provider_config={
            "worker-review": {
                "provider": "codex",
                "flags": "-c model_reasoning_effort=high",
            },
        },
    )

    result = runtime.spawn_worker(name=None, template="review", provider=None, flags=None)

    assert result.ok
    assert result.name == "worker-review-2"
    assert result.provider == "codex"
    assert store.get_agent_by_name(run.run_id, "worker-review-2") is not None
    protocol = (fs.base / "WORKER-REVIEW-2.md").read_text(encoding="utf-8")
    assert "당신은 worker-review-2입니다." in protocol
    assert ".workflow/agent-cards/review.json" in protocol
    assert any(
        call["surface_id"] == "surface:3"
        and call["text"] == "codex -c model_reasoning_effort=high\n"
        for call in _calls(cmux, "send_text")
    )


def test_watcher_routes_artifacts_through_broker_and_runtime(tmp_path):
    fs = AgentFileSystem(tmp_path / ".agent")
    fs.init()
    store = StateStore(fs.db_path)
    event_log = EventLog(fs.event_log_path)
    cmux = FakeCmux()

    run = Run(
        run_id="run-1",
        status=RunStatus.RUNNING,
        workspace_id="workspace:1",
    )
    store.save_run(run)
    for agent in (
        Agent(
            run_id=run.run_id,
            role=AgentRole.CONTROLLER,
            name="controller",
        ),
        Agent(
            run_id=run.run_id,
            role=AgentRole.ORCHESTRATOR,
            name="orchestrator",
            surface_id="surface:1",
        ),
        Agent(
            run_id=run.run_id,
            role=AgentRole.WORKER,
            name="worker-fix",
            surface_id="surface:2",
        ),
    ):
        store.save_agent(agent)
        fs.create_inbox(agent.name)

    prompt_builder = PromptBuilder(str(fs.outbox), str(fs.inbox))
    prompt_builder.write_protocol_files(
        fs.base,
        store.get_agents(run.run_id),
        template_dir=TEMPLATES_ROOT / "bugfix",
    )
    runtime = AgentRuntime(
        store=store,
        event_log=event_log,
        fs=fs,
        cmux=cmux,
        prompt_builder=prompt_builder,
        run_id=run.run_id,
        workspace_id=run.workspace_id,
        template_dir=TEMPLATES_ROOT / "bugfix",
        provider_config={
            "worker-review": {"provider": "claude", "flags": "--effort max"},
        },
    )
    broker = MessageBroker(
        store=store,
        event_log=event_log,
        fs=fs,
        cmux=cmux,
        prompt_builder=prompt_builder,
        run_id=run.run_id,
        workspace_id=run.workspace_id,
        runtime=runtime,
    )

    _write_artifact(
        fs.outbox / "01-dispatch.json",
        {
            "type": "dispatch",
            "sender": "orchestrator",
            "recipient": "worker-fix",
            "message": "Patch the login timeout race",
        },
    )
    _write_artifact(
        fs.outbox / "02-result.json",
        {
            "type": "result",
            "sender": "worker-fix",
            "recipient": "orchestrator",
            "message": "Implemented the timeout guard",
        },
    )
    _write_artifact(
        fs.outbox / "03-spawn.json",
        {
            "type": "control",
            "sender": "orchestrator",
            "recipient": "controller",
            "message": "Need a review worker before final handoff",
            "action": "spawn_agent",
            "agent": {"name": "worker-review"},
        },
    )

    watcher = ArtifactWatcher(fs.outbox, broker)
    watcher._process_existing()

    assert list(fs.outbox.iterdir()) == []
    assert sorted(path.name for path in fs.processed.iterdir() if path.is_file()) == [
        "01-dispatch.json",
        "02-result.json",
        "03-spawn.json",
    ]
    assert list(fs.failed.iterdir()) == []

    worker_messages = _read_inbox(fs.inbox / "worker-fix")
    assert len(worker_messages) == 1
    assert worker_messages[0]["task"] == "Patch the login timeout race"
    assert worker_messages[0]["artifact_format"]["sender"] == "worker-fix"

    orchestrator_results = {
        delivery["result"]
        for delivery in _read_inbox(fs.inbox / "orchestrator")
    }
    assert orchestrator_results == {
        "Implemented the timeout guard",
        "spawned worker-review (claude)",
    }

    spawned = store.get_agent_by_name(run.run_id, "worker-review")
    assert spawned is not None
    assert spawned.role == AgentRole.WORKER
    assert spawned.surface_id == "surface:3"
    assert (fs.inbox / "worker-review").is_dir()
    assert "Agent Card" in (fs.base / "WORKER-REVIEW.md").read_text(encoding="utf-8")

    messages = store.get_messages(run.run_id)
    assert len(messages) == 3
    assert [message.status for message in messages] == [
        MessageStatus.DELIVERED,
        MessageStatus.DELIVERED,
        MessageStatus.DELIVERED,
    ]
    assert [message.type for message in messages] == [
        MessageType.DISPATCH,
        MessageType.RESULT,
        MessageType.RESULT,
    ]

    send_texts = _calls(cmux, "send_text")
    assert any(
        call["surface_id"] == "surface:2"
        and "Patch the login timeout race" in call["text"]
        and "WORKER-FIX.md" in call["text"]
        for call in send_texts
    )
    assert any(
        call["surface_id"] == "surface:1"
        and "Implemented the timeout guard" in call["text"]
        for call in send_texts
    )
    assert any(
        call["surface_id"] == "surface:3" and call["text"] == "claude --effort max\n"
        for call in send_texts
    )
    assert any(
        call["surface_id"] == "surface:3"
        and "WORKER-REVIEW.md" in call["text"]
        and "docs/templates/gap.md" in call["text"]
        for call in send_texts
    )
    assert any(
        call["surface_id"] == "surface:1"
        and "spawned worker-review (claude)" in call["text"]
        for call in send_texts
    )

    assert len(_calls(cmux, "send_key")) == 4
    assert {call["surface_id"] for call in _calls(cmux, "trigger_flash")} == {
        "surface:1",
        "surface:2",
    }

    event_names = [event["event"] for event in event_log.read_all(run.run_id)]
    assert event_names.count("artifact.detected") == 3
    assert event_names.count("message.delivered") == 3
    assert event_names.count("agent.registered") == 1
    assert "artifact.validation_failed" not in event_names


def test_watcher_moves_failed_artifacts_without_delivery_or_injection(tmp_path):
    fs = AgentFileSystem(tmp_path / ".agent")
    fs.init()
    store = StateStore(fs.db_path)
    event_log = EventLog(fs.event_log_path)
    cmux = FakeCmux(
        surfaces={"surface:1"},
        new_surface_result=CmuxResult(
            ok=False,
            stdout="",
            stderr="cmux new-surface unavailable",
        ),
    )

    run = Run(
        run_id="run-1",
        status=RunStatus.RUNNING,
        workspace_id="workspace:1",
    )
    store.save_run(run)
    for agent in (
        Agent(
            run_id=run.run_id,
            role=AgentRole.CONTROLLER,
            name="controller",
        ),
        Agent(
            run_id=run.run_id,
            role=AgentRole.ORCHESTRATOR,
            name="orchestrator",
            surface_id="surface:1",
        ),
        Agent(
            run_id=run.run_id,
            role=AgentRole.WORKER,
            name="worker-fix",
            surface_id="surface:2",
        ),
    ):
        store.save_agent(agent)
        fs.create_inbox(agent.name)

    prompt_builder = PromptBuilder(str(fs.outbox), str(fs.inbox))
    runtime = AgentRuntime(
        store=store,
        event_log=event_log,
        fs=fs,
        cmux=cmux,
        prompt_builder=prompt_builder,
        run_id=run.run_id,
        workspace_id=run.workspace_id,
        template_dir=TEMPLATES_ROOT / "bugfix",
    )
    broker = MessageBroker(
        store=store,
        event_log=event_log,
        fs=fs,
        cmux=cmux,
        prompt_builder=prompt_builder,
        run_id=run.run_id,
        workspace_id=run.workspace_id,
        runtime=runtime,
    )

    (fs.outbox / "01-malformed.json").write_text("{not-json", encoding="utf-8")
    _write_artifact(
        fs.outbox / "02-missing-field.json",
        {
            "type": "dispatch",
            "sender": "orchestrator",
            "recipient": "worker-fix",
        },
    )
    _write_artifact(
        fs.outbox / "03-unknown-recipient.json",
        {
            "type": "dispatch",
            "sender": "orchestrator",
            "recipient": "worker-missing",
            "message": "Route this nowhere",
        },
    )
    _write_artifact(
        fs.outbox / "04-inactive-surface.json",
        {
            "type": "dispatch",
            "sender": "orchestrator",
            "recipient": "worker-fix",
            "message": "This worker tab is closed",
        },
    )
    _write_artifact(
        fs.outbox / "05-spawn-failure.json",
        {
            "type": "control",
            "sender": "orchestrator",
            "recipient": "controller",
            "message": "Need another worker",
            "action": "spawn_agent",
            "agent": {"name": "worker-review"},
        },
    )

    watcher = ArtifactWatcher(fs.outbox, broker)
    watcher._process_existing()

    assert list(fs.outbox.iterdir()) == []
    assert sorted(path.name for path in fs.failed.iterdir()) == [
        "01-malformed.json",
        "02-missing-field.json",
        "03-unknown-recipient.json",
        "04-inactive-surface.json",
        "05-spawn-failure.json",
    ]
    assert store.get_messages(run.run_id) == []
    assert store.get_agent_by_name(run.run_id, "worker-review") is None
    assert _read_inbox(fs.inbox / "orchestrator") == []
    assert _read_inbox(fs.inbox / "worker-fix") == []

    assert _calls(cmux, "send_text") == []
    assert _calls(cmux, "send_key") == []
    assert _calls(cmux, "trigger_flash") == []
    assert _calls(cmux, "notify") == []
    assert len(_calls(cmux, "new_surface")) == 1

    events = event_log.read_all(run.run_id)
    event_names = [event["event"] for event in events]
    reasons = [
        event["data"]["reason"]
        for event in events
        if event["event"] == "artifact.validation_failed"
    ]
    assert event_names.count("artifact.detected") == 3
    assert event_names.count("artifact.validation_failed") == 5
    assert "message.delivered" not in event_names
    assert any("artifact parse failed" in reason for reason in reasons)
    assert any("필수 필드 누락" in reason for reason in reasons)
    assert "미등록 recipient: worker-missing" in reasons
    assert "비활성 recipient: worker-fix" in reasons
    assert "cmux new-surface unavailable" in reasons
