"""Behavioral tests for the durable AWF Supervisor event outbox and command ledger."""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from awf.supervisor.contracts import SupervisorEvent, SupervisorEventType
from awf.supervisor.store import SupervisorStore


NOW = "2026-07-30T12:00:00Z"


def event_fixture(
    *,
    job_id: str = "job-1",
    generation: int = 2,
    sequence: int = 1,
) -> SupervisorEvent:
    return SupervisorEvent(
        schema_version=1,
        job_id=job_id,
        generation=generation,
        sequence=sequence,
        type=SupervisorEventType.TASK_STARTED,
        timestamp=NOW,
        source="local-agent-1",
        data={"summary": "task_started"},
    )


def test_outbox_survives_reopen_and_ack_is_exact(tmp_path: Path) -> None:
    db = tmp_path / "supervisor.db"
    events = [
        event_fixture(job_id="job-1", generation=2, sequence=1),
        event_fixture(job_id="job-1", generation=2, sequence=2),
        event_fixture(job_id="job-1", generation=3, sequence=1),
        event_fixture(job_id="job-2", generation=2, sequence=1),
    ]

    store = SupervisorStore(db)
    for event in events:
        store.enqueue_event(event)

    reopened = SupervisorStore(db)
    assert reopened.pending_events(limit=10) == events

    reopened.ack_event("job-1", 2, 1)

    assert reopened.pending_events(limit=10) == events[1:]


def test_sequence_allocation_is_monotonic_per_job_and_generation(tmp_path: Path) -> None:
    db = tmp_path / "supervisor.db"
    store = SupervisorStore(db)

    assert store.allocate_sequence("job-1", 2) == 1
    assert store.allocate_sequence("job-1", 2) == 2
    assert store.allocate_sequence("job-1", 3) == 1
    assert store.allocate_sequence("job-2", 2) == 1

    reopened = SupervisorStore(db)
    assert reopened.allocate_sequence("job-1", 2) == 3


def test_atomic_enqueue_does_not_burn_a_sequence_when_factory_raises(
    tmp_path: Path,
) -> None:
    store = SupervisorStore(tmp_path / "supervisor.db")

    def broken_factory(_sequence: int) -> SupervisorEvent:
        raise RuntimeError("event construction failed")

    with pytest.raises(RuntimeError, match="construction"):
        store.enqueue_next_event("job-1", 2, broken_factory)

    event = store.enqueue_next_event(
        "job-1", 2, lambda sequence: event_fixture(sequence=sequence)
    )
    assert event.sequence == 1
    assert store.pending_events(limit=10) == [event]


def test_atomic_enqueue_does_not_burn_a_sequence_when_insert_fails(
    tmp_path: Path,
) -> None:
    db = tmp_path / "supervisor.db"
    store = SupervisorStore(db)
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_event_outbox_insert
            BEFORE INSERT ON event_outbox
            BEGIN
                SELECT RAISE(ABORT, 'simulated insert failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="simulated insert failure"):
        store.enqueue_next_event(
            "job-1", 2, lambda sequence: event_fixture(sequence=sequence)
        )

    with sqlite3.connect(db) as connection:
        connection.execute("DROP TRIGGER reject_event_outbox_insert")

    event = store.enqueue_next_event(
        "job-1", 2, lambda sequence: event_fixture(sequence=sequence)
    )
    assert event.sequence == 1
    assert store.pending_events(limit=10) == [event]


def test_duplicate_event_rolls_back_without_corrupting_sequence_or_outbox(
    tmp_path: Path,
) -> None:
    store = SupervisorStore(tmp_path / "supervisor.db")
    event = event_fixture(sequence=store.allocate_sequence("job-1", 2))
    store.enqueue_event(event)

    with pytest.raises(sqlite3.IntegrityError):
        store.enqueue_event(event)

    assert store.pending_events(limit=10) == [event]
    assert store.allocate_sequence("job-1", 2) == 2


def test_command_claim_is_idempotent_across_store_instances(tmp_path: Path) -> None:
    db = tmp_path / "supervisor.db"
    store = SupervisorStore(db)

    assert store.claim_command("cmd-1", "job-1", 3) is True

    reopened = SupervisorStore(db)
    assert reopened.claim_command("cmd-1", "job-1", 3) is False

    record = reopened.get_command("cmd-1")
    assert record is not None
    assert record["command_id"] == "cmd-1"
    assert record["job_id"] == "job-1"
    assert record["generation"] == 3
    assert record["status"] == "claimed"
    assert record["result"] is None



def test_release_command_claim_deletes_only_the_matching_unfinished_claim(
    tmp_path: Path,
) -> None:
    store = SupervisorStore(tmp_path / "supervisor.db")
    assert store.claim_command("cmd-1", "job-1", 3) is True

    assert store.release_command_claim("cmd-1", "job-1", 2) is False
    assert store.get_command("cmd-1")["status"] == "claimed"

    assert store.release_command_claim("cmd-1", "job-1", 3) is True
    assert store.get_command("cmd-1") is None

    assert store.claim_command("cmd-1", "job-1", 3) is True
    store.complete_command("cmd-1", {"outcome": "completed"})
    assert store.release_command_claim("cmd-1", "job-1", 3) is False
    assert store.get_command("cmd-1")["status"] == "completed"

@pytest.mark.parametrize(
    ("record_method", "overwrite_method", "expected_status"),
    [
        ("complete_command", "fail_command", "completed"),
        ("fail_command", "complete_command", "failed"),
    ],
)
def test_terminal_command_results_are_recorded_and_cannot_be_overwritten(
    tmp_path: Path,
    record_method: str,
    overwrite_method: str,
    expected_status: str,
) -> None:
    store = SupervisorStore(tmp_path / "supervisor.db")
    initial_result = {"outcome": expected_status, "attempt": 1}
    overwritten_result = {"outcome": "overwritten", "attempt": 2}

    assert store.claim_command("cmd-1", "job-1", 3) is True
    getattr(store, record_method)("cmd-1", initial_result)

    terminal_record = store.get_command("cmd-1")
    assert terminal_record is not None
    assert terminal_record["status"] == expected_status
    assert terminal_record["result"] == initial_result

    getattr(store, overwrite_method)("cmd-1", overwritten_result)

    assert store.get_command("cmd-1") == terminal_record


def test_invalid_event_is_rejected_before_it_reaches_the_outbox(tmp_path: Path) -> None:
    store = SupervisorStore(tmp_path / "supervisor.db")
    invalid_event = SupervisorEvent(
        schema_version=1,
        job_id="job-1",
        generation=2,
        sequence=1,
        type=SupervisorEventType.TASK_STARTED,
        timestamp=NOW,
        source="local-agent-1",
        data={"summary": "not-an-allowed-summary"},
    )

    with pytest.raises(ValueError):
        store.enqueue_event(invalid_event)

    assert store.pending_events(limit=10) == []


def test_corrupted_stored_event_payload_is_rejected_when_loaded(tmp_path: Path) -> None:
    db = tmp_path / "supervisor.db"
    store = SupervisorStore(db)
    store.enqueue_event(event_fixture())

    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE event_outbox SET payload_json = ?",
            (json.dumps({"schema_version": 1}),),
        )

    reopened = SupervisorStore(db)
    with pytest.raises(ValueError):
        reopened.pending_events(limit=10)


def test_pending_events_have_deterministic_order_and_honor_limit(tmp_path: Path) -> None:
    store = SupervisorStore(tmp_path / "supervisor.db")
    events = [
        event_fixture(job_id="job-b", generation=0, sequence=1),
        event_fixture(job_id="job-a", generation=1, sequence=1),
        event_fixture(job_id="job-a", generation=0, sequence=2),
        event_fixture(job_id="job-a", generation=0, sequence=1),
    ]
    for event in events:
        store.enqueue_event(event)

    assert store.pending_events(limit=2) == [events[3], events[2]]


def _run_concurrently(
    db: Path,
    operation: Callable[[SupervisorStore], Any],
) -> list[Any]:
    barrier = threading.Barrier(2)

    def invoke() -> Any:
        store = SupervisorStore(db)
        barrier.wait()
        return operation(store)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(invoke) for _ in range(2)]
        return [future.result(timeout=5) for future in futures]


def test_two_store_instances_make_sequence_allocation_and_claim_atomic(
    tmp_path: Path,
) -> None:
    db = tmp_path / "supervisor.db"
    SupervisorStore(db)

    allocated = _run_concurrently(
        db,
        lambda store: store.allocate_sequence("job-1", 2),
    )
    claimed = _run_concurrently(
        db,
        lambda store: store.claim_command("cmd-1", "job-1", 2),
    )

    assert sorted(allocated) == [1, 2]
    assert sorted(claimed) == [False, True]
