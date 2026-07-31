"""Long-running, fenced Supervisor agent lifecycle and safe idle inspection."""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import tempfile
import time
from urllib.parse import quote
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from awf.supervisor.client import SupervisorAuthRequired, SupervisorConflict
from awf.supervisor.contracts import AgentEnvironment, SupervisorCommand, SupervisorEvent
from awf.supervisor.executor import AcceptedLease, ExecutionResult, SupervisorJobExecutor
from awf.supervisor.runtime_paths import RuntimePaths
from awf.supervisor.store import SupervisorStore
from awf.supervisor.transport import CommandDelivery, CommandSource, LeaseApi
from awf.supervisor.workspace import WorkspaceAdapter


_BUILT_IN_CAPABILITIES = ("git", "omp")
_ACTIVE_LEASE_FIELDS = frozenset(
    ("job_id", "generation", "agent_id", "acquired_at", "lease_expires_at")
)
_MAX_SHUTDOWN_FLUSH_ATTEMPTS = 8


class IdleState(str, Enum):
    """Conservative result used by idle-status callers and host schedulers."""

    SAFE = "safe"
    BUSY = "busy"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class IdleStatus:
    """The runtime's state paths and fail-closed current-idleness decision."""

    state: IdleState
    active_lease_path: Path
    store_path: Path

    @property
    def exit_code(self) -> int:
        if self.state is IdleState.SAFE:
            return 0
        if self.state is IdleState.BUSY:
            return 3
        return 4


@dataclass(frozen=True)
class _ActiveLease:
    job_id: str
    generation: int
    acquired_at: str
    lease_expires_at: str


class LeaseControl:
    """Native OMP control hook that renews the current owner lease on every tick."""

    poll_interval_sec = 1.0

    def __init__(self, runtime: "SupervisorAgentRuntime") -> None:
        self._runtime = runtime

    def on_tick(self) -> Optional[str]:
        if self._runtime.stopping:
            return "service_stopping"
        try:
            self._runtime._renew_active_lease()
        except SupervisorConflict:
            return "lease_lost"
        except SupervisorAuthRequired:
            self._runtime._stopping = True
            return "control_plane_unavailable"
        except Exception:
            # The native batch must stop rather than continue with an unknown fence.
            return "control_plane_unavailable"
        return None


class SupervisorAgentRuntime:
    """Own one supervisor command at a time with a durable event outbox."""

    def __init__(
        self,
        *,
        paths: RuntimePaths,
        store: SupervisorStore,
        workspace: WorkspaceAdapter,
        executor: SupervisorJobExecutor,
        source: CommandSource,
        lease_api: LeaseApi,
        agent_id: str,
        environment: AgentEnvironment,
        version: Mapping[str, str],
        heartbeat_interval_sec: float = 30.0,
        command_wait_seconds: int = 20,
        shutdown_deadline_sec: float = 10.0,
        initial_backoff_sec: float = 0.25,
        max_backoff_sec: float = 5.0,
        now: Optional[Callable[[], datetime]] = None,
        monotonic: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        install_signal_handler: bool = True,
    ) -> None:
        if heartbeat_interval_sec <= 0:
            raise ValueError("heartbeat_interval_sec must be positive")
        if command_wait_seconds <= 0:
            raise ValueError("command_wait_seconds must be positive")
        if shutdown_deadline_sec < 0:
            raise ValueError("shutdown_deadline_sec must be non-negative")
        if initial_backoff_sec <= 0 or max_backoff_sec < initial_backoff_sec:
            raise ValueError("invalid bounded backoff configuration")
        if not agent_id:
            raise ValueError("agent_id must not be empty")
        if environment not in (AgentEnvironment.LOCAL, AgentEnvironment.AWS):
            raise ValueError("environment must be local or aws")

        self.paths = paths
        self.store = store
        # Older compatible stores did not expose their path; runtime wiring must.
        if not hasattr(store, "path"):
            setattr(store, "path", paths.store_path)
        self.workspace = workspace
        self.executor = executor
        self.source = source
        self.lease_api = lease_api
        self.agent_id = agent_id
        self.environment = environment
        self.version = dict(version)
        self.active_lease_path = paths.active_lease_path
        self._heartbeat_interval_sec = heartbeat_interval_sec
        self._command_wait_seconds = command_wait_seconds
        self._shutdown_deadline_sec = shutdown_deadline_sec
        self._initial_backoff_sec = initial_backoff_sec
        self._max_backoff_sec = max_backoff_sec
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._last_heartbeat_at: Optional[float] = None
        self._stopping = False
        self._active_lease = self._hydrate_active_lease()
        self._pending_deliveries: Dict[str, CommandDelivery] = {}
        self._pending_results: Dict[str, ExecutionResult] = {}
        self._accepted_terminal_job_generations: set[Tuple[str, int]] = set()
        self._backoff_sec = initial_backoff_sec
        self.active_control = LeaseControl(self)
        if install_signal_handler:
            self._install_signal_handler()

    @property
    def stopping(self) -> bool:
        return self._stopping

    def supports_required_capabilities(self, required: Sequence[str]) -> bool:
        """Return whether a job requires only server-advertised built-ins.

        Approval is deliberately absent: it is an unconditional job policy, never a
        target-selection capability.
        """

        return all(type(item) is str and item in _BUILT_IN_CAPABILITIES for item in required)

    def run(self, *, max_polls: Optional[int] = None) -> int:
        """Run bounded command polls; errors are isolated to their delivery boundary."""

        polls = 0
        while not self._stopping and (max_polls is None or polls < max_polls):
            self._send_heartbeat_if_due()
            self._flush_outbox()
            if self._stopping:
                break
            try:
                delivery = self.source.next_command(wait_seconds=self._command_wait_seconds)
            except SupervisorAuthRequired:
                self._stopping = True
                break
            except Exception:
                self._backoff()
                polls += 1
                continue
            polls += 1
            self._backoff_sec = self._initial_backoff_sec
            if delivery is None:
                continue
            self._handle_delivery(delivery)
        return self.shutdown() if self._stopping else 0

    def shutdown(self) -> int:
        """Stop acquisition and make bounded best effort to deliver existing outbox rows."""

        self._stopping = True
        deadline = self._monotonic() + self._shutdown_deadline_sec
        attempts = 0
        while attempts < _MAX_SHUTDOWN_FLUSH_ATTEMPTS and self._monotonic() <= deadline:
            attempts += 1
            if self._flush_outbox():
                if not self._has_pending_events():
                    self._finish_pending_deliveries()
                    return 0
            if self._has_pending_events():
                self._backoff()
            else:
                self._finish_pending_deliveries()
                return 0
        # A marker/outbox is recovery evidence.  Never clear it during a forced exit.
        return 1 if self._has_pending_events() or self.active_lease_path.exists() else 0

    def handle_signal(self, signum: int, frame: Any) -> None:
        """SIGTERM handler: stop acquisition and ask native OMP to end safely."""

        if signum == signal.SIGTERM:
            self._stopping = True

    def write_active_lease(self, accepted: AcceptedLease) -> None:
        """Atomically persist exactly the fence data required for recovery."""

        lease = _ActiveLease(
            job_id=_lease_text(accepted, "job_id"),
            generation=_lease_generation(accepted),
            acquired_at=_lease_text(accepted, "acquired_at"),
            lease_expires_at=_lease_text(accepted, "lease_expires_at"),
        )
        self._write_lease(lease)
        self._active_lease = lease

    def clear_active_lease(self) -> None:
        """Delete recovery state only after terminal acceptance and empty outbox."""

        try:
            self.active_lease_path.unlink()
        except FileNotFoundError:
            pass
        else:
            _fsync_directory(self.active_lease_path.parent)
        self._active_lease = None

    def flush_until_idle(self) -> None:
        """Flush the ordered outbox once per pass until it stalls or becomes empty."""

        while self._has_pending_events() and self._flush_outbox():
            pass
        if not self._has_pending_events():
            self._finish_pending_deliveries()

    def idle_status(self) -> IdleStatus:
        """Check marker and actual SQLite outbox without creating or repairing either."""

        marker_state = self._marker_state()
        if marker_state is IdleState.UNKNOWN:
            return self._idle(IdleState.UNKNOWN)
        try:
            pending = _outbox_has_pending_event(self.paths.store_path)
        except Exception:
            return self._idle(IdleState.UNKNOWN)
        if marker_state is IdleState.BUSY or pending:
            return self._idle(IdleState.BUSY)
        return self._idle(IdleState.SAFE)

    def _handle_delivery(self, delivery: CommandDelivery) -> None:
        command = delivery.command
        try:
            existing = self.store.get_command(command.command_id)
        except Exception:
            delivery.release()
            return
        if existing is not None:
            self._handle_known_delivery(delivery)
            return
        try:
            inspected = self.executor.inspect_claim(command, agent_id=self.agent_id)
        except SupervisorAuthRequired:
            self._stopping = True
            delivery.release()
            return
        except Exception:
            delivery.release()
            self._backoff()
            return
        if not self.supports_required_capabilities(inspected.required_capabilities):
            delivery.release()
            return
        try:
            claimed = self.store.claim_command(
                command.command_id, command.job_id, command.generation
            )
        except Exception:
            delivery.release()
            return
        if not claimed:
            self._handle_known_delivery(delivery)
            return

        try:
            accepted = self.executor.accept_claim(command, agent_id=self.agent_id)
        except SupervisorAuthRequired:
            self._release_unaccepted_claim(command)
            self._stopping = True
            delivery.release()
            return
        except Exception:
            self._release_unaccepted_claim(command)
            delivery.release()
            self._backoff()
            return

        try:
            self.write_active_lease(accepted)
            self._set_executor_control()
            result = self.executor.execute_accepted(
                command, accepted, agent_id=self.agent_id
            )
        except SupervisorAuthRequired:
            self._stopping = True
            delivery.release()
            return
        except Exception:
            delivery.release()
            return

        self._pending_deliveries[command.command_id] = delivery
        self._pending_results[command.command_id] = result
        self.flush_until_idle()

    def _release_unaccepted_claim(self, command: SupervisorCommand) -> None:
        """Free this new ledger claim only before marker or execution side effects."""
        try:
            self.store.release_command_claim(
                command.command_id, command.job_id, command.generation
            )
        except Exception:
            # The unreleased claim is safer than assuming another writer did not win.
            pass

    def _handle_known_delivery(self, delivery: CommandDelivery) -> None:
        """Reconcile ledger/outbox state and never re-enter workspace or OMP execution."""

        record = self.store.get_command(delivery.command.command_id)
        if record is None:
            delivery.release()
            return
        status = record.get("status")
        if status in ("completed", "failed"):
            delivery.ack()
            return
        if status != "claimed":
            delivery.release()
            return
        command_key = (delivery.command.job_id, delivery.command.generation)
        saw_final_completion = command_key in self._accepted_terminal_job_generations
        while True:
            try:
                pending = self.store.pending_events(limit=100)
            except Exception:
                delivery.release()
                return
            if not pending:
                break
            saw_final_completion = saw_final_completion or any(
                event.job_id == delivery.command.job_id
                and event.generation == delivery.command.generation
                and _is_final_completion_event(event)
                for event in pending
            )
            if not self._flush_outbox():
                delivery.release()
                return
        if not saw_final_completion:
            # Durable non-terminal progress does not authorize receipt completion.
            delivery.release()
            return
        self.store.complete_command(
            delivery.command.command_id,
            {
                "reconciled": True,
                "job_id": delivery.command.job_id,
                "generation": delivery.command.generation,
            },
        )
        self._accepted_terminal_job_generations.discard(command_key)
        if self._active_lease_matches(delivery.command):
            self.clear_active_lease()
        delivery.ack()

    def _send_heartbeat_if_due(self) -> None:
        now = self._monotonic()
        if self._last_heartbeat_at is not None and (
            now - self._last_heartbeat_at < self._heartbeat_interval_sec
        ):
            return
        try:
            self.lease_api.heartbeat(
                agent_id=self.agent_id,
                capabilities=_BUILT_IN_CAPABILITIES,
                repos=self._available_repositories(),
                max_concurrency=1,
                active_jobs=1 if self._active_lease is not None else 0,
                version=self.version,
            )
        except SupervisorAuthRequired:
            self._stopping = True
            return
        except Exception:
            self._backoff()
            return
        self._last_heartbeat_at = now

    def _flush_outbox(self) -> bool:
        """Append and ack in durable order, stopping at the first failed request."""

        try:
            events = self.store.pending_events(limit=100)
        except Exception:
            return False
        for event in events:
            try:
                self.lease_api.append_event(event, agent_id=self.agent_id)
            except SupervisorAuthRequired:
                self._stopping = True
                return False
            except Exception:
                self._backoff()
                return False
            if _is_final_completion_event(event):
                self._accepted_terminal_job_generations.add(
                    (event.job_id, event.generation)
                )
            try:
                self.store.ack_event(event.job_id, event.generation, event.sequence)
            except Exception:
                # A delivery accepted before local ack is safely retryable by key.
                return False
        if events:
            self._backoff_sec = self._initial_backoff_sec
        return True

    def _renew_active_lease(self) -> None:
        lease = self._active_lease
        if lease is None:
            raise SupervisorConflict("no active lease")
        renewed = self.lease_api.renew(
            job_id=lease.job_id, generation=lease.generation, agent_id=self.agent_id
        )
        expiry = getattr(renewed, "lease_expires_at", None)
        if type(expiry) is not str or not expiry:
            raise ValueError("renewal response did not contain lease expiry")
        renewed_lease = _ActiveLease(
            job_id=lease.job_id,
            generation=lease.generation,
            acquired_at=lease.acquired_at,
            lease_expires_at=expiry,
        )
        self._write_lease(renewed_lease)
        self._active_lease = renewed_lease

    def _finish_pending_deliveries(self) -> None:
        if self._has_pending_events():
            return
        for command_id, delivery in tuple(self._pending_deliveries.items()):
            result = self._pending_results.get(command_id)
            if (
                result is None
                or not result.terminal_state_accepted
                or not _result_is_finally_complete(result)
            ):
                continue
            self.store.complete_command(command_id, result.to_dict())
            self.clear_active_lease()
            delivery.ack()
            del self._pending_deliveries[command_id]
            del self._pending_results[command_id]

    def _has_pending_events(self) -> bool:
        try:
            return bool(self.store.pending_events(limit=1))
        except Exception:
            return True

    def _available_repositories(self) -> Tuple[str, ...]:
        root = self.paths.repo_root
        try:
            root_real = root.resolve(strict=True)
            children = tuple(root.iterdir())
        except OSError:
            return ()
        names = []
        for child in children:
            try:
                if not child.is_dir():
                    continue
                real_child = child.resolve(strict=True)
                if _is_under(real_child, root_real):
                    names.append(child.name)
            except OSError:
                continue
        return tuple(sorted(names))

    def _hydrate_active_lease(self) -> Optional[_ActiveLease]:
        """Load only a complete marker fenced to this runtime's agent identity."""
        try:
            raw = self.active_lease_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ValueError("active lease marker cannot be read") from error
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as error:
            raise ValueError("active lease marker is invalid") from error
        if not _valid_marker(value):
            raise ValueError("active lease marker is invalid")
        if value["agent_id"] != self.agent_id:
            raise ValueError("active lease marker belongs to a different agent")
        return _ActiveLease(
            job_id=value["job_id"],
            generation=value["generation"],
            acquired_at=value["acquired_at"],
            lease_expires_at=value["lease_expires_at"],
        )

    def _marker_state(self) -> IdleState:
        try:
            raw = self.active_lease_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return IdleState.SAFE
        except OSError:
            return IdleState.UNKNOWN
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return IdleState.UNKNOWN
        if not _valid_marker(value):
            return IdleState.UNKNOWN
        return IdleState.BUSY

    def _write_lease(self, lease: _ActiveLease) -> None:
        self.active_lease_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {
            "job_id": lease.job_id,
            "generation": lease.generation,
            "agent_id": self.agent_id,
            "acquired_at": lease.acquired_at,
            "lease_expires_at": lease.lease_expires_at,
        }
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".{}-".format(self.active_lease_path.name),
            suffix=".tmp",
            dir=str(self.active_lease_path.parent),
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(payload, handle, allow_nan=False, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary), str(self.active_lease_path))
            os.chmod(str(self.active_lease_path), 0o600)
            _fsync_directory(self.active_lease_path.parent)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _active_lease_matches(self, command: SupervisorCommand) -> bool:
        lease = self._active_lease
        return lease is not None and (lease.job_id, lease.generation) == (
            command.job_id,
            command.generation,
        )

    def _set_executor_control(self) -> None:
        setter = getattr(self.executor, "set_run_control", None)
        if callable(setter):
            setter(self.active_control)

    def _backoff(self) -> None:
        delay = self._backoff_sec
        self._backoff_sec = min(self._max_backoff_sec, delay * 2)
        self._sleep(delay)

    def _idle(self, state: IdleState) -> IdleStatus:
        return IdleStatus(
            state=state,
            active_lease_path=self.active_lease_path,
            store_path=self.paths.store_path,
        )

    def _install_signal_handler(self) -> None:
        try:
            signal.signal(signal.SIGTERM, self.handle_signal)
        except (ValueError, OSError):
            # Tests and non-main threads can use handle_signal directly.
            pass


def _lease_text(accepted: AcceptedLease, field: str) -> str:
    value = getattr(accepted, field, None)
    if type(value) is not str or not value:
        raise ValueError("accepted lease {} must be non-empty text".format(field))
    return value


def _lease_generation(accepted: AcceptedLease) -> int:
    value = getattr(accepted, "generation", None)
    if type(value) is not int or value < 0:
        raise ValueError("accepted lease generation must be non-negative integer")
    return value


def _valid_marker(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != _ACTIVE_LEASE_FIELDS:
        return False
    return (
        type(value["job_id"]) is str
        and bool(value["job_id"])
        and type(value["generation"]) is int
        and value["generation"] >= 0
        and type(value["agent_id"]) is str
        and bool(value["agent_id"])
        and type(value["acquired_at"]) is str
        and bool(value["acquired_at"])
        and type(value["lease_expires_at"]) is str
        and bool(value["lease_expires_at"])
    )


def _result_is_finally_complete(result: ExecutionResult) -> bool:
    value = getattr(result, "terminal_state", None)
    state = getattr(value, "value", value)
    return state in ("SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED")



def _is_final_completion_event(event: SupervisorEvent) -> bool:
    return event.data.get("terminal_status") in ("SUCCEEDED", "FAILED", "CANCELLED")

def _outbox_has_pending_event(path: Path) -> bool:
    if not path.is_file():
        raise OSError("supervisor database does not exist")
    uri = "file:{}?mode=ro".format(quote(path.as_posix(), safe="/"))
    connection = sqlite3.connect(uri, uri=True, timeout=0.0, isolation_level=None)
    try:
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute("SELECT 1 FROM event_outbox LIMIT 1").fetchone()
        return row is not None
    finally:
        connection.close()


def _is_under(child: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(child), str(root))) == str(root)
    except ValueError:
        return False


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["IdleState", "IdleStatus", "LeaseControl", "SupervisorAgentRuntime"]
