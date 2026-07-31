"""Durable SQLite outbox and idempotent command ledger for the Supervisor."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Optional, Tuple, Union

from awf.supervisor.contracts import SupervisorEvent, validate_contract


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS event_outbox (
    job_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_id, generation, sequence)
);
CREATE TABLE IF NOT EXISTS command_ledger (
    command_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('claimed', 'completed', 'failed')),
    result_json TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_sequence (
    job_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    next_sequence INTEGER NOT NULL,
    PRIMARY KEY (job_id, generation)
);
"""

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS event_outbox (
        job_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        sequence INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (job_id, generation, sequence)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS command_ledger (
        command_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('claimed', 'completed', 'failed')),
        result_json TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_sequence (
        job_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        next_sequence INTEGER NOT NULL,
        PRIMARY KEY (job_id, generation)
    )
    """,
)

_TERMINAL_COMMAND_STATUSES = frozenset(("completed", "failed"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _require_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("{} must be a non-empty identifier".format(field))
    return value


def _require_non_negative_integer(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("{} must be a non-negative integer".format(field))
    return value


def _encode_json(value: Any, field: str) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("{} must be JSON-serializable".format(field)) from error


def _decode_json(value: Any, field: str) -> Any:
    if not isinstance(value, str):
        raise ValueError("stored {} must be JSON text".format(field))
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("stored {} is not valid JSON".format(field)) from error


class SupervisorStore:
    """Persist Supervisor events and command execution state in one SQLite file."""

    def __init__(self, path: Union[str, os.PathLike[str]]) -> None:
        self._path = os.fspath(path)
        self._initialize_schema()

    def allocate_sequence(self, job_id: str, generation: int) -> int:
        """Allocate the next sequence number for one job generation atomically."""
        job_id = _require_identifier(job_id, "job_id")
        generation = _require_non_negative_integer(generation, "generation")

        with self._transaction("BEGIN IMMEDIATE") as connection:
            return self._allocate_sequence(connection, job_id, generation)

    def enqueue_next_event(
        self,
        job_id: str,
        generation: int,
        factory: Callable[[int], SupervisorEvent],
    ) -> SupervisorEvent:
        """Construct, validate, and enqueue the next event in one transaction."""
        job_id = _require_identifier(job_id, "job_id")
        generation = _require_non_negative_integer(generation, "generation")
        if not callable(factory):
            raise ValueError("event factory must be callable")

        with self._transaction("BEGIN IMMEDIATE") as connection:
            sequence = self._allocate_sequence(connection, job_id, generation)
            event = factory(sequence)
            payload, event_job_id, event_generation, event_sequence = self._event_payload(event)
            if (event_job_id, event_generation, event_sequence) != (
                job_id,
                generation,
                sequence,
            ):
                raise ValueError("event factory returned a mismatched event identity")
            payload_json = _encode_json(payload, "event payload")
            connection.execute(
                """
                INSERT INTO event_outbox (
                    job_id, generation, sequence, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, generation, sequence, payload_json, _now()),
            )
            return event

    def _allocate_sequence(
        self, connection: sqlite3.Connection, job_id: str, generation: int
    ) -> int:
        row = connection.execute(
            """
            SELECT next_sequence
            FROM job_sequence
            WHERE job_id = ? AND generation = ?
            """,
            (job_id, generation),
        ).fetchone()
        if row is None:
            sequence = 1
            connection.execute(
                """
                INSERT INTO job_sequence (job_id, generation, next_sequence)
                VALUES (?, ?, ?)
                """,
                (job_id, generation, sequence + 1),
            )
            return sequence

        sequence = row["next_sequence"]
        if type(sequence) is not int or sequence < 1:
            raise ValueError("stored next_sequence must be a positive integer")
        connection.execute(
            """
            UPDATE job_sequence
            SET next_sequence = ?
            WHERE job_id = ? AND generation = ?
            """,
            (sequence + 1, job_id, generation),
        )
        return sequence

    def enqueue_event(self, event: SupervisorEvent) -> None:
        """Validate and append an event to the durable outbox."""
        payload, job_id, generation, sequence = self._event_payload(event)
        payload_json = _encode_json(payload, "event payload")

        with self._transaction("BEGIN") as connection:
            connection.execute(
                """
                INSERT INTO event_outbox (
                    job_id, generation, sequence, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, generation, sequence, payload_json, _now()),
            )

    def pending_events(self, limit: int) -> list[SupervisorEvent]:
        """Return pending events in their deterministic outbox order."""
        limit = _require_non_negative_integer(limit, "limit")

        with self._transaction("BEGIN") as connection:
            rows = connection.execute(
                """
                SELECT job_id, generation, sequence, payload_json
                FROM event_outbox
                ORDER BY job_id ASC, generation ASC, sequence ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._event_from_row(row) for row in rows]

    def ack_event(self, job_id: str, generation: int, sequence: int) -> None:
        """Remove exactly one durable event after successful delivery."""
        job_id = _require_identifier(job_id, "job_id")
        generation = _require_non_negative_integer(generation, "generation")
        sequence = _require_non_negative_integer(sequence, "sequence")

        with self._transaction("BEGIN") as connection:
            connection.execute(
                """
                DELETE FROM event_outbox
                WHERE job_id = ? AND generation = ? AND sequence = ?
                """,
                (job_id, generation, sequence),
            )

    def claim_command(self, command_id: str, job_id: str, generation: int) -> bool:
        """Record a command claim once, returning whether this call won the claim."""
        command_id = _require_identifier(command_id, "command_id")
        job_id = _require_identifier(job_id, "job_id")
        generation = _require_non_negative_integer(generation, "generation")

        with self._transaction("BEGIN IMMEDIATE") as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO command_ledger (
                    command_id, job_id, generation, status, result_json, updated_at
                ) VALUES (?, ?, ?, 'claimed', NULL, ?)
                """,
                (command_id, job_id, generation, _now()),
            )
            return cursor.rowcount == 1

    def release_command_claim(
        self, command_id: str, job_id: str, generation: int
    ) -> bool:
        """Delete exactly the still-claimed ledger row created for a retryable receipt."""
        command_id = _require_identifier(command_id, "command_id")
        job_id = _require_identifier(job_id, "job_id")
        generation = _require_non_negative_integer(generation, "generation")

        with self._transaction("BEGIN IMMEDIATE") as connection:
            cursor = connection.execute(
                """
                DELETE FROM command_ledger
                WHERE command_id = ? AND job_id = ? AND generation = ?
                  AND status = 'claimed'
                """,
                (command_id, job_id, generation),
            )
            return cursor.rowcount == 1

    def complete_command(self, command_id: str, result: Any) -> None:
        """Write a claimed command's terminal successful result exactly once."""
        self._record_terminal_command(command_id, "completed", result)

    def fail_command(self, command_id: str, result: Any) -> None:
        """Write a claimed command's terminal failure result exactly once."""
        self._record_terminal_command(command_id, "failed", result)

    def get_command(self, command_id: str) -> Optional[Mapping[str, Any]]:
        """Return an immutable decoded command record, if it exists."""
        command_id = _require_identifier(command_id, "command_id")

        with self._transaction("BEGIN") as connection:
            row = connection.execute(
                """
                SELECT command_id, job_id, generation, status, result_json, updated_at
                FROM command_ledger
                WHERE command_id = ?
                """,
                (command_id,),
            ).fetchone()
            if row is None:
                return None
            return self._command_from_row(row)

    def _initialize_schema(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def _transaction(self, begin_statement: str) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute(begin_statement)
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _event_payload(self, event: SupervisorEvent) -> Tuple[Mapping[str, Any], str, int, int]:
        if not isinstance(event, SupervisorEvent):
            raise ValueError("event must be a SupervisorEvent")
        payload = event.to_dict()
        validate_contract("event", payload)
        job_id = _require_identifier(payload["job_id"], "event.job_id")
        generation = _require_non_negative_integer(
            payload["generation"], "event.generation"
        )
        sequence = _require_non_negative_integer(payload["sequence"], "event.sequence")
        return payload, job_id, generation, sequence

    def _event_from_row(self, row: sqlite3.Row) -> SupervisorEvent:
        payload = _decode_json(row["payload_json"], "event payload")
        try:
            event = SupervisorEvent.from_dict(payload)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("stored event payload violates the event contract") from error

        job_id = _require_identifier(event.job_id, "stored event.job_id")
        generation = _require_non_negative_integer(
            event.generation, "stored event.generation"
        )
        sequence = _require_non_negative_integer(event.sequence, "stored event.sequence")
        row_job_id = _require_identifier(row["job_id"], "stored event key job_id")
        row_generation = _require_non_negative_integer(
            row["generation"], "stored event key generation"
        )
        row_sequence = _require_non_negative_integer(
            row["sequence"], "stored event key sequence"
        )
        if (job_id, generation, sequence) != (
            row_job_id,
            row_generation,
            row_sequence,
        ):
            raise ValueError("stored event key does not match its payload")
        return event

    def _record_terminal_command(
        self, command_id: str, terminal_status: str, result: Any
    ) -> None:
        command_id = _require_identifier(command_id, "command_id")
        if terminal_status not in _TERMINAL_COMMAND_STATUSES:
            raise ValueError("invalid terminal command status")

        with self._transaction("BEGIN IMMEDIATE") as connection:
            row = connection.execute(
                """
                SELECT status
                FROM command_ledger
                WHERE command_id = ?
                """,
                (command_id,),
            ).fetchone()
            if row is None:
                return

            status = row["status"]
            if status in _TERMINAL_COMMAND_STATUSES:
                return
            if status != "claimed":
                raise ValueError("stored command has an invalid status")

            result_json = _encode_json(result, "command result")
            cursor = connection.execute(
                """
                UPDATE command_ledger
                SET status = ?, result_json = ?, updated_at = ?
                WHERE command_id = ? AND status = 'claimed'
                """,
                (terminal_status, result_json, _now(), command_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("claimed command changed before it could be finalized")

    def _command_from_row(self, row: sqlite3.Row) -> Mapping[str, Any]:
        command_id = _require_identifier(row["command_id"], "stored command_id")
        job_id = _require_identifier(row["job_id"], "stored command job_id")
        generation = _require_non_negative_integer(
            row["generation"], "stored command generation"
        )
        status = row["status"]
        if not isinstance(status, str) or status not in {
            "claimed",
            "completed",
            "failed",
        }:
            raise ValueError("stored command has an invalid status")
        updated_at = row["updated_at"]
        if not isinstance(updated_at, str) or not updated_at:
            raise ValueError("stored command has an invalid updated_at timestamp")

        result_json = row["result_json"]
        if status == "claimed":
            if result_json is not None:
                raise ValueError("claimed command must not have a result")
            result = None
        else:
            if result_json is None:
                raise ValueError("terminal command is missing its result")
            result = _decode_json(result_json, "command result")

        return MappingProxyType(
            {
                "command_id": command_id,
                "job_id": job_id,
                "generation": generation,
                "status": status,
                "result": result,
                "updated_at": updated_at,
            }
        )


__all__ = ("SupervisorStore",)
