"""Behavioral contracts for the durable Supervisor agent runtime."""
from __future__ import annotations

import json
import os
import signal
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

from awf.supervisor.agent import IdleState, SupervisorAgentRuntime
from awf.supervisor.contracts import (
    AgentEnvironment,
    CommandType,
    JobState,
    SupervisorCommand,
    SupervisorEvent,
    SupervisorEventType,
    SupervisorJob,
)
from awf.supervisor.runtime_paths import RuntimePaths
from awf.supervisor.store import SupervisorStore


NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


@dataclass
class AcceptedLeaseStub:
    job_id: str
    generation: int
    acquired_at: str
    lease_expires_at: str


@dataclass
class ExecutionResultStub:
    terminal_state_accepted: bool
    terminal_state: str = "SUCCEEDED"

    @property
    def state(self) -> str:
        return self.terminal_state

    def to_dict(self) -> Dict[str, Any]:
        return {
            "terminal_state": self.terminal_state,
            "terminal_state_accepted": self.terminal_state_accepted,
        }


class RecordingDelivery:
    def __init__(self, command: SupervisorCommand) -> None:
        self.command = command
        self.acks = 0
        self.releases = 0

    def ack(self) -> None:
        self.acks += 1

    def release(self) -> None:
        self.releases += 1


class RecordingSource:
    def __init__(self, deliveries: Sequence[RecordingDelivery]) -> None:
        self._deliveries = list(deliveries)
        self.polls: List[int] = []

    def next_command(self, *, wait_seconds: int) -> Optional[RecordingDelivery]:
        self.polls.append(wait_seconds)
        if self._deliveries:
            return self._deliveries.pop(0)
        return None


class RecordingLeaseApi:
    def __init__(self, *, append_failures: int = 0) -> None:
        self.append_failures = append_failures
        self.appended: List[int] = []
        self.heartbeats: List[Mapping[str, Any]] = []
        self.renew_calls: List[Mapping[str, Any]] = []
        self.next_lease_expiry = "2026-07-30T12:10:00Z"

    def heartbeat(self, **kwargs: Any) -> None:
        self.heartbeats.append(kwargs)

    def append_event(self, event: SupervisorEvent, *, agent_id: str) -> None:
        if self.append_failures:
            self.append_failures -= 1
            raise OSError("control plane unavailable")
        self.appended.append(event.sequence)

    def renew(self, *, job_id: str, generation: int, agent_id: str) -> Any:
        self.renew_calls.append(
            {"job_id": job_id, "generation": generation, "agent_id": agent_id}
        )
        return SimpleNamespace(lease_expires_at=self.next_lease_expiry)


class RecordingExecutor:
    def __init__(
        self,
        store: SupervisorStore,
        *,
        fail_accept: bool = False,
        accept_failures: int = 0,
        crash: bool = False,
        terminal_accepted: bool = True,
        terminal_state: str = "SUCCEEDED",
        inspected_capabilities: Sequence[str] = ("git", "omp"),
    ) -> None:
        self._store = store
        self.fail_accept = fail_accept
        self.accept_failures = accept_failures
        self.crash = crash
        self.terminal_accepted = terminal_accepted
        self.terminal_state = terminal_state
        self.inspected_capabilities = tuple(inspected_capabilities)
        self.inspect_calls = 0
        self.accept_calls = 0
        self.execute_calls = 0
        self.controls: List[Any] = []

    def set_run_control(self, control: Any) -> None:
        self.controls.append(control)
    def inspect_claim(
        self, command: SupervisorCommand, *, agent_id: str
    ) -> SupervisorJob:
        self.inspect_calls += 1
        return SupervisorJob.from_dict(
            {
                "schema_version": 1,
                "job_id": command.job_id,
                "workflow_id": "workflow-1",
                "state": JobState.CLAIMED.value,
                "desired_state": "RUNNING",
                "approval_required": True,
                "requested_target": "local",
                "owner_agent_id": agent_id,
                "lease_expires_at": "2026-07-30T12:05:00Z",
                "generation": command.generation,
                "attempt": 0,
                "repo_refs": [{"repo": "api", "base": "main"}],
                "required_capabilities": list(self.inspected_capabilities),
                "checkpoint": None,
                "created_at": "2026-07-30T12:00:00Z",
                "updated_at": "2026-07-30T12:00:00Z",
            }
        )

    def accept_claim(
        self, command: SupervisorCommand, *, agent_id: str
    ) -> AcceptedLeaseStub:
        self.accept_calls += 1
        if self.fail_accept:
            raise OSError("claim endpoint unavailable")
        if self.accept_failures:
            self.accept_failures -= 1
            raise OSError("claim endpoint unavailable")
        return AcceptedLeaseStub(
            job_id=command.job_id,
            generation=command.generation,
            acquired_at="2026-07-30T12:00:00Z",
            lease_expires_at="2026-07-30T12:05:00Z",
        )

    def execute_accepted(
        self,
        command: SupervisorCommand,
        accepted_lease: AcceptedLeaseStub,
        *,
        agent_id: str,
    ) -> ExecutionResultStub:
        self.execute_calls += 1
        if self.crash:
            raise RuntimeError("native batch crashed")
        sequence = self._store.allocate_sequence(command.job_id, command.generation)
        self._store.enqueue_event(
            SupervisorEvent(
                schema_version=1,
                job_id=command.job_id,
                generation=command.generation,
                sequence=sequence,
                type=SupervisorEventType.TASK_COMPLETED,
                timestamp="2026-07-30T12:00:01Z",
                source=agent_id,
                data={"summary": "task_completed"},
            )
        )
        return ExecutionResultStub(self.terminal_accepted, self.terminal_state)


class WorkspaceStub:
    def __init__(self, state_root: Path) -> None:
        self.state_root = state_root


def command_fixture() -> SupervisorCommand:
    return SupervisorCommand(
        schema_version=1,
        command_id="cmd-1",
        job_id="job-1",
        generation=1,
        type=CommandType.EXECUTE,
    )


def paths_fixture(tmp_path: Path) -> RuntimePaths:
    state_root = tmp_path / "state"
    repo_root = tmp_path / "repos"
    repo_root.mkdir()
    return RuntimePaths(
        state_root=state_root,
        store_path=state_root / "supervisor.db",
        active_lease_path=state_root / "active-lease.json",
        repo_root=repo_root,
    )


def agent_fixture(
    tmp_path: Path,
    *,
    append_failures: int = 0,
    executor: Optional[RecordingExecutor] = None,
    source: Optional[RecordingSource] = None,
    lease_api: Optional[RecordingLeaseApi] = None,
    heartbeat_interval_sec: float = 30.0,
) -> SupervisorAgentRuntime:
    paths = paths_fixture(tmp_path)
    paths.state_root.mkdir()
    store = SupervisorStore(paths.store_path)
    actual_executor = executor or RecordingExecutor(store)
    delivery = RecordingDelivery(command_fixture())
    return SupervisorAgentRuntime(
        paths=paths,
        store=store,
        workspace=WorkspaceStub(paths.state_root),
        executor=actual_executor,
        source=source or RecordingSource([delivery]),
        lease_api=lease_api or RecordingLeaseApi(append_failures=append_failures),
        agent_id="local-mac-01",
        environment=AgentEnvironment.LOCAL,
        version={"awf": "test"},
        heartbeat_interval_sec=heartbeat_interval_sec,
        now=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )


def test_aws_paths_are_shared_by_store_workspace_runtime_and_idle_status(
    tmp_path: Path,
) -> None:
    paths = paths_fixture(tmp_path)
    paths = RuntimePaths(
        state_root=paths.state_root,
        store_path=paths.store_path,
        active_lease_path=tmp_path / "agent-state" / "supervisor-active-lease.json",
        repo_root=paths.repo_root,
    )
    paths.state_root.mkdir()
    runtime = SupervisorAgentRuntime(
        paths=paths,
        store=SupervisorStore(paths.store_path),
        workspace=WorkspaceStub(paths.state_root),
        executor=RecordingExecutor(SupervisorStore(paths.store_path)),
        source=RecordingSource([]),
        lease_api=RecordingLeaseApi(),
        agent_id="aws-agent-01",
        environment=AgentEnvironment.AWS,
        version={"awf": "test"},
        now=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )

    assert runtime.store.path == paths.store_path
    assert runtime.workspace.state_root == paths.state_root
    assert runtime.active_lease_path == paths.active_lease_path
    assert runtime.idle_status().active_lease_path == runtime.active_lease_path


def test_active_lease_is_retained_until_terminal_event_is_accepted_and_outbox_is_empty(
    tmp_path: Path,
) -> None:
    runtime = agent_fixture(tmp_path, append_failures=1)
    delivery = runtime.source._deliveries[0]

    runtime.run(max_polls=1)

    assert runtime.active_lease_path.is_file()
    assert runtime.store.pending_events(limit=10)
    assert delivery.acks == 0

    runtime.flush_until_idle()

    assert runtime.store.pending_events(limit=10) == []
    assert not runtime.active_lease_path.exists()
    assert delivery.acks == 1


def test_paused_result_keeps_active_lease_after_accepted_events_flush(tmp_path: Path) -> None:
    paths = paths_fixture(tmp_path)
    paths.state_root.mkdir()
    store = SupervisorStore(paths.store_path)
    executor = RecordingExecutor(store, terminal_state="PAUSED")
    delivery = RecordingDelivery(command_fixture())
    runtime = SupervisorAgentRuntime(
        paths=paths,
        store=store,
        workspace=WorkspaceStub(paths.state_root),
        executor=executor,
        source=RecordingSource([delivery]),
        lease_api=RecordingLeaseApi(),
        agent_id="local-mac-01",
        environment=AgentEnvironment.LOCAL,
        version={"awf": "test"},
        now=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )

    runtime.run(max_polls=1)

    assert store.pending_events(limit=1) == []
    assert runtime.active_lease_path.is_file()
    assert delivery.acks == 0


def test_active_lease_marker_contains_only_recovery_fields_and_is_replaced_atomically(
    tmp_path: Path,
) -> None:
    runtime = agent_fixture(tmp_path)
    accepted = AcceptedLeaseStub(
        job_id="job-1",
        generation=1,
        acquired_at="2026-07-30T12:00:00Z",
        lease_expires_at="2026-07-30T12:05:00Z",
    )

    runtime.write_active_lease(accepted)

    assert json.loads(runtime.active_lease_path.read_text(encoding="utf-8")) == {
        "job_id": "job-1",
        "generation": 1,
        "agent_id": "local-mac-01",
        "acquired_at": "2026-07-30T12:00:00Z",
        "lease_expires_at": "2026-07-30T12:05:00Z",
    }
    assert not list(runtime.active_lease_path.parent.glob("*.tmp"))


def test_lease_control_renews_and_replaces_marker_expiry(tmp_path: Path) -> None:
    lease_api = RecordingLeaseApi()
    runtime = agent_fixture(tmp_path, lease_api=lease_api)
    runtime.write_active_lease(
        AcceptedLeaseStub(
            "job-1", 1, "2026-07-30T12:00:00Z", "2026-07-30T12:05:00Z"
        )
    )

    assert runtime.active_control.on_tick() is None

    assert lease_api.renew_calls == [
        {"job_id": "job-1", "generation": 1, "agent_id": "local-mac-01"}
    ]
    assert json.loads(runtime.active_lease_path.read_text(encoding="utf-8"))["lease_expires_at"] == "2026-07-30T12:10:00Z"


def test_heartbeat_is_interval_bounded_and_discovers_only_direct_safe_repositories(
    tmp_path: Path,
) -> None:
    runtime = agent_fixture(tmp_path, heartbeat_interval_sec=20.0)
    (runtime.paths.repo_root / "zeta").mkdir()
    (runtime.paths.repo_root / "alpha").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (runtime.paths.repo_root / "escape").symlink_to(outside, target_is_directory=True)

    runtime._send_heartbeat_if_due()
    runtime._send_heartbeat_if_due()

    heartbeat = runtime.lease_api.heartbeats[0]
    assert len(runtime.lease_api.heartbeats) == 1
    assert heartbeat["capabilities"] == ("git", "omp")
    assert heartbeat["repos"] == ("alpha", "zeta")
    assert heartbeat["max_concurrency"] == 1
    assert heartbeat["active_jobs"] == 0
    assert "approval" not in heartbeat["capabilities"]
    assert heartbeat["version"] == {"awf": "test"}


@pytest.mark.parametrize(
    "environment", [AgentEnvironment.LOCAL, AgentEnvironment.AWS]
)
def test_required_capability_check_rejects_unadvertised_capability_before_accept(
    tmp_path: Path, environment: AgentEnvironment
) -> None:
    runtime = agent_fixture(tmp_path)
    runtime.environment = environment

    assert runtime.supports_required_capabilities(("git", "omp"))
    assert not runtime.supports_required_capabilities(("git", "omp", "approval"))
    assert not runtime.supports_required_capabilities(("git", "network"))



def test_unsupported_capability_releases_before_ledger_claim_or_remote_accept(
    tmp_path: Path,
) -> None:
    paths = paths_fixture(tmp_path)
    paths.state_root.mkdir()
    store = SupervisorStore(paths.store_path)
    executor = RecordingExecutor(store, inspected_capabilities=("git", "omp", "network"))
    delivery = RecordingDelivery(command_fixture())
    runtime = SupervisorAgentRuntime(
        paths=paths,
        store=store,
        workspace=WorkspaceStub(paths.state_root),
        executor=executor,
        source=RecordingSource([delivery]),
        lease_api=RecordingLeaseApi(),
        agent_id="local-mac-01",
        environment=AgentEnvironment.LOCAL,
        version={"awf": "test"},
        now=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )

    runtime.run(max_polls=1)

    assert executor.inspect_calls == 1
    assert executor.accept_calls == 0
    assert store.get_command("cmd-1") is None
    assert delivery.acks == 0
    assert delivery.releases == 1

def test_transient_command_source_failure_uses_bounded_exponential_backoff(
    tmp_path: Path,
) -> None:
    class UnavailableSource:
        def next_command(self, *, wait_seconds: int) -> Optional[RecordingDelivery]:
            raise OSError("control plane unavailable")

    paths = paths_fixture(tmp_path)
    paths.state_root.mkdir()
    store = SupervisorStore(paths.store_path)
    delays: List[float] = []
    runtime = SupervisorAgentRuntime(
        paths=paths,
        store=store,
        workspace=WorkspaceStub(paths.state_root),
        executor=RecordingExecutor(store),
        source=UnavailableSource(),
        lease_api=RecordingLeaseApi(),
        agent_id="local-mac-01",
        environment=AgentEnvironment.LOCAL,
        version={"awf": "test"},
        initial_backoff_sec=0.25,
        max_backoff_sec=0.5,
        now=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=delays.append,
    )

    runtime.run(max_polls=4)

    assert delays == [0.25, 0.5, 0.5, 0.5]

def test_remote_acceptance_failure_releases_new_claim_for_successful_redelivery(
    tmp_path: Path,
) -> None:
    paths = paths_fixture(tmp_path)
    paths.state_root.mkdir()
    store = SupervisorStore(paths.store_path)
    executor = RecordingExecutor(store, accept_failures=1)
    first_delivery = RecordingDelivery(command_fixture())
    redelivery = RecordingDelivery(command_fixture())
    runtime = SupervisorAgentRuntime(
        paths=paths,
        store=store,
        workspace=WorkspaceStub(paths.state_root),
        executor=executor,
        source=RecordingSource([first_delivery, redelivery]),
        lease_api=RecordingLeaseApi(),
        agent_id="local-mac-01",
        environment=AgentEnvironment.LOCAL,
        version={"awf": "test"},
        now=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )

    runtime.run(max_polls=2)

    assert executor.inspect_calls == 2
    assert executor.accept_calls == 2
    assert executor.execute_calls == 1
    assert first_delivery.acks == 0
    assert first_delivery.releases == 1
    assert redelivery.acks == 1
    assert redelivery.releases == 0
    assert store.get_command("cmd-1")["status"] == "completed"


def test_execution_crash_never_resets_claim_after_marker_or_reexecutes_delivery(
    tmp_path: Path,
) -> None:
    paths = paths_fixture(tmp_path)
    paths.state_root.mkdir()
    store = SupervisorStore(paths.store_path)
    executor = RecordingExecutor(store, crash=True)
    first_delivery = RecordingDelivery(command_fixture())
    redelivery = RecordingDelivery(command_fixture())
    runtime = SupervisorAgentRuntime(
        paths=paths,
        store=store,
        workspace=WorkspaceStub(paths.state_root),
        executor=executor,
        source=RecordingSource([first_delivery, redelivery]),
        lease_api=RecordingLeaseApi(),
        agent_id="local-mac-01",
        environment=AgentEnvironment.LOCAL,
        version={"awf": "test"},
        now=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )

    runtime.run(max_polls=2)

    assert executor.accept_calls == 1
    assert executor.execute_calls == 1
    assert first_delivery.acks == 0
    assert first_delivery.releases == 1
    assert redelivery.acks == 0
    assert redelivery.releases == 1
    assert runtime.active_lease_path.is_file()
    assert store.get_command("cmd-1")["status"] == "claimed"


def test_outbox_flush_preserves_sequence_order_and_stops_on_control_plane_outage(
    tmp_path: Path,
) -> None:
    runtime = agent_fixture(tmp_path)
    store = runtime.store
    for sequence in (2, 1):
        store.enqueue_event(
            SupervisorEvent(
                schema_version=1,
                job_id="job-1",
                generation=1,
                sequence=sequence,
                type=SupervisorEventType.TASK_STARTED,
                timestamp="2026-07-30T12:00:00Z",
                source="local-mac-01",
                data={"summary": "task_started"},
            )
        )
    runtime.lease_api.append_failures = 1

    runtime._flush_outbox()

    assert runtime.lease_api.appended == []
    assert [event.sequence for event in store.pending_events(limit=10)] == [1, 2]

    runtime._flush_outbox()

    assert runtime.lease_api.appended == [1, 2]
    assert store.pending_events(limit=10) == []


def test_startup_hydrates_only_a_valid_active_lease_owned_by_this_agent(
    tmp_path: Path,
) -> None:
    paths = paths_fixture(tmp_path)
    paths.state_root.mkdir()
    paths.active_lease_path.write_text(
        json.dumps(
            {
                "job_id": "job-1",
                "generation": 1,
                "agent_id": "local-mac-01",
                "acquired_at": "2026-07-30T12:00:00Z",
                "lease_expires_at": "2026-07-30T12:05:00Z",
            }
        ),
        encoding="utf-8",
    )
    store = SupervisorStore(paths.store_path)
    runtime = SupervisorAgentRuntime(
        paths=paths,
        store=store,
        workspace=WorkspaceStub(paths.state_root),
        executor=RecordingExecutor(store),
        source=RecordingSource([]),
        lease_api=RecordingLeaseApi(),
        agent_id="local-mac-01",
        environment=AgentEnvironment.LOCAL,
        version={"awf": "test"},
        now=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )

    assert runtime._active_lease is not None
    assert runtime._active_lease.job_id == "job-1"
    assert runtime._active_lease.generation == 1
    assert runtime._active_lease.acquired_at == "2026-07-30T12:00:00Z"
    assert runtime._active_lease.lease_expires_at == "2026-07-30T12:05:00Z"


@pytest.mark.parametrize(
    "marker",
    [
        {
            "job_id": "job-1",
            "generation": 1,
            "agent_id": "other-agent",
            "acquired_at": "2026-07-30T12:00:00Z",
            "lease_expires_at": "2026-07-30T12:05:00Z",
        },
        "{",
    ],
)
def test_startup_rejects_mismatched_or_corrupt_active_lease_marker(
    tmp_path: Path, marker: Any
) -> None:
    paths = paths_fixture(tmp_path)
    paths.state_root.mkdir()
    paths.active_lease_path.write_text(
        marker if isinstance(marker, str) else json.dumps(marker), encoding="utf-8"
    )
    store = SupervisorStore(paths.store_path)

    with pytest.raises(ValueError):
        SupervisorAgentRuntime(
            paths=paths,
            store=store,
            workspace=WorkspaceStub(paths.state_root),
            executor=RecordingExecutor(store),
            source=RecordingSource([]),
            lease_api=RecordingLeaseApi(),
            agent_id="local-mac-01",
            environment=AgentEnvironment.LOCAL,
            version={"awf": "test"},
            now=lambda: NOW,
            monotonic=lambda: 0.0,
            sleep=lambda _: None,
        )


def test_restarted_agent_reconciles_claimed_delivery_without_second_native_batch(
    tmp_path: Path,
) -> None:
    paths = paths_fixture(tmp_path)
    paths.state_root.mkdir()
    paths.active_lease_path.write_text(
        json.dumps(
            {
                "job_id": "job-1",
                "generation": 1,
                "agent_id": "local-mac-01",
                "acquired_at": "2026-07-30T12:00:00Z",
                "lease_expires_at": "2026-07-30T12:05:00Z",
            }
        ),
        encoding="utf-8",
    )
    store = SupervisorStore(paths.store_path)
    assert store.claim_command("cmd-1", "job-1", 1)
    store.enqueue_event(
        SupervisorEvent(
            schema_version=1,
            job_id="job-1",
            generation=1,
            sequence=1,
            type=SupervisorEventType.TASK_COMPLETED,
            timestamp="2026-07-30T12:00:01Z",
            source="local-mac-01",
            data={
                "summary": "task_completed",
                "terminal_status": "SUCCEEDED",
                "return_code": 0,
                "artifact_uri": "s3://bucket/artifacts/redacted-results/job-1/1/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json",
                "artifact_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "provenance_uri": "s3://bucket/artifacts/provenance/job-1/1/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.json",
                "provenance_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            },
        )
    )
    delivery = RecordingDelivery(command_fixture())
    executor = RecordingExecutor(store)
    runtime = SupervisorAgentRuntime(
        paths=paths,
        store=store,
        workspace=WorkspaceStub(paths.state_root),
        executor=executor,
        source=RecordingSource([delivery]),
        lease_api=RecordingLeaseApi(),
        agent_id="local-mac-01",
        environment=AgentEnvironment.LOCAL,
        version={"awf": "test"},
        now=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _: None,
    )

    runtime.run(max_polls=1)

    assert executor.execute_calls == 0
    assert store.get_command("cmd-1")["status"] == "completed"
    assert delivery.acks == 1
    assert store.pending_events(limit=1) == []
    assert not runtime.active_lease_path.exists()
    assert runtime.idle_status().state is IdleState.SAFE


def test_restarted_claim_with_only_progress_event_is_released_not_acknowledged(
    tmp_path: Path,
) -> None:
    runtime = agent_fixture(tmp_path)
    delivery = runtime.source._deliveries[0]
    assert runtime.store.claim_command("cmd-1", "job-1", 1)
    runtime.store.enqueue_event(
        SupervisorEvent(
            schema_version=1,
            job_id="job-1",
            generation=1,
            sequence=1,
            type=SupervisorEventType.TASK_STARTED,
            timestamp="2026-07-30T12:00:00Z",
            source="local-mac-01",
            data={"summary": "task_started"},
        )
    )

    runtime.run(max_polls=1)

    assert runtime.executor.execute_calls == 0
    assert runtime.store.get_command("cmd-1")["status"] == "claimed"
    assert delivery.acks == 0
    assert delivery.releases == 1


def test_idle_status_is_safe_only_when_marker_absent_and_real_outbox_empty(
    tmp_path: Path,
) -> None:
    runtime = agent_fixture(tmp_path)

    status = runtime.idle_status()

    assert status.state is IdleState.SAFE
    assert status.active_lease_path == runtime.active_lease_path
    assert status.store_path == runtime.paths.store_path


def test_idle_status_is_busy_for_pending_event_without_marker(tmp_path: Path) -> None:
    runtime = agent_fixture(tmp_path)
    runtime.store.enqueue_event(
        SupervisorEvent(
            schema_version=1,
            job_id="job-1",
            generation=1,
            sequence=1,
            type=SupervisorEventType.TASK_STARTED,
            timestamp="2026-07-30T12:00:00Z",
            source="local-mac-01",
            data={"summary": "task_started"},
        )
    )

    assert runtime.idle_status().state is IdleState.BUSY


@pytest.mark.parametrize("marker", ["{", "[]", '{"job_id":"job-1"}'])
def test_idle_status_is_unknown_for_malformed_active_lease(tmp_path: Path, marker: str) -> None:
    runtime = agent_fixture(tmp_path)
    runtime.active_lease_path.write_text(marker, encoding="utf-8")

    assert runtime.idle_status().state is IdleState.UNKNOWN


def test_idle_status_is_busy_when_marker_and_outbox_exist(tmp_path: Path) -> None:
    runtime = agent_fixture(tmp_path)
    runtime.write_active_lease(
        AcceptedLeaseStub("job-1", 1, "2026-07-30T12:00:00Z", "2026-07-30T12:05:00Z")
    )
    runtime.store.enqueue_event(
        SupervisorEvent(
            schema_version=1,
            job_id="job-1",
            generation=1,
            sequence=1,
            type=SupervisorEventType.TASK_STARTED,
            timestamp="2026-07-30T12:00:00Z",
            source="local-mac-01",
            data={"summary": "task_started"},
        )
    )

    assert runtime.idle_status().state is IdleState.BUSY


@pytest.mark.parametrize(
    "database_kind", ["corrupt", "missing_schema", "unreadable"]
)
def test_idle_status_fails_closed_for_corrupt_missing_or_unreadable_database(
    tmp_path: Path, database_kind: str
) -> None:
    runtime = agent_fixture(tmp_path)
    if database_kind == "unreadable":
        runtime.paths.store_path.chmod(0)
    else:
        runtime.store.path.unlink()
        if database_kind == "corrupt":
            runtime.paths.store_path.write_bytes(b"not sqlite")
        else:
            sqlite3.connect(runtime.paths.store_path).close()

    assert runtime.idle_status().state is IdleState.UNKNOWN


def test_idle_status_fails_closed_when_real_database_is_locked(tmp_path: Path) -> None:
    runtime = agent_fixture(tmp_path)
    connection = sqlite3.connect(runtime.paths.store_path, timeout=0.0)
    connection.execute("PRAGMA locking_mode=EXCLUSIVE")
    connection.execute("BEGIN EXCLUSIVE")
    try:
        assert runtime.idle_status().state is IdleState.UNKNOWN
    finally:
        connection.rollback()
        connection.close()


def test_sigterm_stops_acquisition_and_preserves_marker_when_outbox_cannot_flush(
    tmp_path: Path,
) -> None:
    runtime = agent_fixture(tmp_path, append_failures=100)
    runtime.write_active_lease(
        AcceptedLeaseStub("job-1", 1, "2026-07-30T12:00:00Z", "2026-07-30T12:05:00Z")
    )
    runtime.store.enqueue_event(
        SupervisorEvent(
            schema_version=1,
            job_id="job-1",
            generation=1,
            sequence=1,
            type=SupervisorEventType.TASK_STARTED,
            timestamp="2026-07-30T12:00:00Z",
            source="local-mac-01",
            data={"summary": "task_started"},
        )
    )

    runtime.handle_signal(signal.SIGTERM, None)

    assert runtime.stopping
    assert runtime.active_control.on_tick() == "service_stopping"
    assert runtime.shutdown() == 1
    assert runtime.active_lease_path.is_file()
    assert runtime.store.pending_events(limit=1)
