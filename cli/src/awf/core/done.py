"""Parent-only, deterministic workflow Done confirmation.

This module deliberately records an explicit human decision only.  It never
runs providers or OMP, creates or merges pull requests, cleans up worktrees, or
infers deployment health.  ``actor`` is an audit label, not an authorization
credential: parent-only authority is enforced by excluding Done from delegated
workflow execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import stat
import unicodedata
from typing import Any
from urllib.parse import urlparse

from awf.core import state as workflow_state
from awf.core.paths import find_repo_root


_CONFIRMATION_SCHEMA_VERSION = 1
_CONFIRMATION_FILENAME = "confirmation.json"
_MAX_ARTIFACT_BYTES = 64 * 1024
_MAX_ACTOR_CHARS = 256
_MAX_WORKFLOW_ID_CHARS = 256
_MAX_PR_URL_CHARS = 2048
_RESERVED_ACTORS = frozenset(
    {
        "agent",
        "automation",
        "claude",
        "codex",
        "omp",
        "openai",
        "provider",
        "system",
        "worker",
    }
)
_SENSITIVE_TEXT = re.compile(
    r"(?:api[_-]?key|password|secret|token|credential|authorization)\s*(?:=|:)|"
    r"-----BEGIN [A-Z ]+-----|\bsk-[A-Za-z0-9_-]{16,}",
    re.IGNORECASE,
)
_GITHUB_PATH = re.compile(
    r"/([A-Za-z0-9][A-Za-z0-9_.-]{0,99})/"
    r"([A-Za-z0-9][A-Za-z0-9_.-]{0,99})/pull/([1-9][0-9]*)\Z"
)
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_CONFIRMATION_FIELDS = frozenset(
    {
        "schema_version",
        "workflow_id",
        "decision",
        "actor",
        "pr_url",
        "recorded_at",
    }
)


class DoneConfirmationError(ValueError):
    """Stable, sanitized error code for Done confirmation failures."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DoneConfirmationResult:
    decision: str
    status: str
    current_phase: str
    pr_url: str | None
    reused: bool = False

    def as_payload(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "status": self.status,
            "current_phase": self.current_phase,
            "pr_url": self.pr_url,
            "reused": self.reused,
        }


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_text(value: object, *, limit: int, code: str) -> str:
    if not isinstance(value, str):
        raise DoneConfirmationError(code)
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized or len(normalized) > limit or _SENSITIVE_TEXT.search(normalized):
        raise DoneConfirmationError(code)
    return normalized


def _validate_pr_url(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DoneConfirmationError("pr_url_invalid")
    normalized = unicodedata.normalize("NFKC", value)
    if (
        not normalized
        or normalized != value
        or len(normalized) > _MAX_PR_URL_CHARS
        or any(character.isspace() for character in normalized)
        or _SENSITIVE_TEXT.search(normalized)
    ):
        raise DoneConfirmationError("pr_url_invalid")
    try:
        parsed = urlparse(normalized)
        port = parsed.port
    except ValueError:
        raise DoneConfirmationError("pr_url_invalid") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
        or _GITHUB_PATH.fullmatch(parsed.path) is None
    ):
        raise DoneConfirmationError("pr_url_invalid")
    return normalized


def _validate_request(
    decision: object,
    actor: object,
    pr_url: object,
) -> tuple[str, str, str | None]:
    if decision not in {"complete", "hold"}:
        raise DoneConfirmationError("decision_invalid")
    normalized_actor = _normalize_text(actor, limit=_MAX_ACTOR_CHARS, code="actor_invalid")
    if normalized_actor.casefold() in _RESERVED_ACTORS:
        raise DoneConfirmationError("actor_reserved")
    normalized_pr_url = _validate_pr_url(pr_url)
    if decision == "hold" and normalized_pr_url is not None:
        raise DoneConfirmationError("pr_url_invalid")
    return decision, normalized_actor, normalized_pr_url


def _nofollow_flag(code: str) -> int:
    flag = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(flag, int) or flag == 0:
        raise DoneConfirmationError(code)
    return flag


def _open_artifacts_directory(root: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | _nofollow_flag(
        "confirmation_artifacts_unavailable"
    )
    try:
        descriptor = os.open(root / ".workflow" / "artifacts", flags)
    except OSError as error:
        raise DoneConfirmationError("confirmation_artifacts_unavailable") from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        os.close(descriptor)
        raise DoneConfirmationError("confirmation_artifacts_unavailable")
    return descriptor


def _read_regular_file(directory_fd: int, name: str, *, missing_ok: bool) -> bytes | None:
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | _nofollow_flag("confirmation_artifact_invalid"),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            if missing_ok:
                return None
            raise DoneConfirmationError("confirmation_artifact_missing") from None
        except OSError as error:
            raise DoneConfirmationError("confirmation_artifact_invalid") from error
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_size > _MAX_ARTIFACT_BYTES
        ):
            raise DoneConfirmationError("confirmation_artifact_invalid")
        content = bytearray()
        while True:
            chunk = os.read(descriptor, min(64 * 1024, _MAX_ARTIFACT_BYTES + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > _MAX_ARTIFACT_BYTES:
                raise DoneConfirmationError("confirmation_artifact_invalid")
        return bytes(content)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate JSON key")
        payload[key] = value
    return payload


def _parse_confirmation_artifact(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _CONFIRMATION_FIELDS:
        raise DoneConfirmationError("confirmation_artifact_invalid")
    if payload.get("schema_version") != _CONFIRMATION_SCHEMA_VERSION:
        raise DoneConfirmationError("confirmation_artifact_invalid")
    if payload.get("decision") != "complete":
        raise DoneConfirmationError("confirmation_artifact_invalid")
    try:
        workflow_id = _normalize_text(
            payload.get("workflow_id"),
            limit=_MAX_WORKFLOW_ID_CHARS,
            code="confirmation_artifact_invalid",
        )
        actor = _normalize_text(
            payload.get("actor"),
            limit=_MAX_ACTOR_CHARS,
            code="confirmation_artifact_invalid",
        )
        pr_url = _validate_pr_url(payload.get("pr_url"))
    except DoneConfirmationError:
        raise DoneConfirmationError("confirmation_artifact_invalid") from None
    if actor.casefold() in _RESERVED_ACTORS:
        raise DoneConfirmationError("confirmation_artifact_invalid")
    recorded_at = payload.get("recorded_at")
    if not isinstance(recorded_at, str) or _TIMESTAMP.fullmatch(recorded_at) is None:
        raise DoneConfirmationError("confirmation_artifact_invalid")
    try:
        parsed_recorded_at = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    except ValueError:
        raise DoneConfirmationError("confirmation_artifact_invalid") from None
    if parsed_recorded_at.tzinfo is None or parsed_recorded_at.utcoffset() != timezone.utc.utcoffset(parsed_recorded_at):
        raise DoneConfirmationError("confirmation_artifact_invalid")
    return {
        "schema_version": _CONFIRMATION_SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "decision": "complete",
        "actor": actor,
        "pr_url": pr_url,
        "recorded_at": recorded_at,
    }


def _read_confirmation_artifact(root: Path) -> dict[str, object] | None:
    directory_fd = _open_artifacts_directory(root)
    try:
        raw = _read_regular_file(directory_fd, _CONFIRMATION_FILENAME, missing_ok=True)
    finally:
        os.close(directory_fd)
    if raw is None:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise DoneConfirmationError("confirmation_artifact_invalid") from None
    return _parse_confirmation_artifact(payload)


def _write_confirmation_artifact(root: Path, payload: dict[str, object]) -> None:
    directory_fd = _open_artifacts_directory(root)
    temporary_name = f".{_CONFIRMATION_FILENAME}.{secrets.token_hex(16)}.tmp"
    descriptor: int | None = None
    created = False
    try:
        try:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | _nofollow_flag("confirmation_write_failed"),
                0o600,
                dir_fd=directory_fd,
            )
            created = True
        except OSError as error:
            raise DoneConfirmationError("confirmation_write_failed") from error
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise DoneConfirmationError("confirmation_write_failed")
        content = _canonical_json_bytes(payload) + b"\n"
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise DoneConfirmationError("confirmation_write_failed")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            _CONFIRMATION_FILENAME,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except OSError as error:
        raise DoneConfirmationError("confirmation_write_failed") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        os.close(directory_fd)


def _state_parts(state: object) -> tuple[dict[str, Any], dict[str, Any], list[Any]]:
    if not isinstance(state, dict):
        raise DoneConfirmationError("state_invalid")
    phases = state.get("phases")
    gates = state.get("gates")
    history = state.get("history")
    if not isinstance(phases, dict) or not isinstance(gates, dict) or not isinstance(history, list):
        raise DoneConfirmationError("state_invalid")
    return phases, gates, history


def _require_g6(gates: dict[str, Any]) -> None:
    g6 = gates.get("G6")
    if not isinstance(g6, dict) or g6.get("passed") is not True:
        raise DoneConfirmationError("g6_not_passed")


def _workflow_id(state: dict[str, Any]) -> str:
    try:
        return _normalize_text(
            state.get("id"),
            limit=_MAX_WORKFLOW_ID_CHARS,
            code="state_invalid",
        )
    except DoneConfirmationError:
        raise DoneConfirmationError("state_invalid") from None


def _require_current_done(state: dict[str, Any], phases: dict[str, Any], gates: dict[str, Any]) -> dict[str, Any]:
    _require_g6(gates)
    if state.get("currentPhase") != "done":
        raise DoneConfirmationError("done_phase_not_current")
    test = phases.get("test")
    done = phases.get("done")
    if not isinstance(test, dict) or test.get("status") != "completed":
        raise DoneConfirmationError("test_state_invalid")
    if not isinstance(done, dict) or done.get("status") not in {"pending", "in_progress"}:
        raise DoneConfirmationError("done_state_invalid")
    return done


def _is_completed_confirmation(
    state: dict[str, Any],
    phases: dict[str, Any],
    gates: dict[str, Any],
    artifact: dict[str, object],
    workflow_id: str,
) -> bool:
    done = phases.get("done")
    return (
        artifact.get("workflow_id") == workflow_id
        and artifact.get("decision") == "complete"
        and isinstance(done, dict)
        and done.get("status") == "completed"
        and state.get("currentPhase") == "completed"
        and isinstance(gates.get("G6"), dict)
        and gates["G6"].get("passed") is True
    )


def _append_history(
    history: list[Any],
    *,
    action: str,
    timestamp: str,
    actor: str,
    pr_url: str | None,
) -> None:
    history.append(
        {
            "phase": "done",
            "action": action,
            "timestamp": timestamp,
            "actor": actor,
            "pr_url": pr_url,
            "details": "explicit parent Done decision recorded",
        }
    )


def _has_matching_hold(history: list[Any], actor: str) -> bool:
    if not history:
        return False
    event = history[-1]
    return (
        isinstance(event, dict)
        and event.get("phase") == "done"
        and event.get("action") == "held"
        and event.get("actor") == actor
        and event.get("pr_url") is None
    )


def _record_completed_state(
    state: dict[str, Any],
    *,
    actor: str,
    pr_url: str | None,
    timestamp: str,
    action: str,
) -> None:
    phases, _, history = _state_parts(state)
    done = phases["done"]
    done["status"] = "completed"
    done["confirmedAt"] = timestamp
    done["actor"] = actor
    done["prUrl"] = pr_url
    state["currentPhase"] = "completed"
    _append_history(
        history,
        action=action,
        timestamp=timestamp,
        actor=actor,
        pr_url=pr_url,
    )


def apply_done_confirmation(
    explicit_root: str | None,
    *,
    decision: object,
    actor: object,
    pr_url: object = None,
) -> DoneConfirmationResult:
    """Record an explicit parent Done decision after the current G6 result.

    ``complete`` writes strict ``confirmation.json`` before atomically committing
    the associated state/history transition.  If interrupted after the artifact
    write, only the exact same decision can recover the pending state.  ``hold``
    intentionally leaves Done pending and records a state/history audit event;
    it never creates a final confirmation artifact.
    """

    decision, actor, pr_url = _validate_request(decision, actor, pr_url)
    try:
        root = find_repo_root(explicit_root)
    except Exception:
        raise DoneConfirmationError("repo_root_invalid") from None
    state_path = root / ".workflow" / "state.json"

    try:
        with workflow_state._workflow_state_lock(state_path), workflow_state._workflow_state_transaction(root) as directory_fd:
            state = workflow_state._read_workflow_state_from_directory(directory_fd)
            phases, gates, history = _state_parts(state)
            workflow_id = _workflow_id(state)
            existing_artifact = _read_confirmation_artifact(root)

            if existing_artifact is not None and existing_artifact.get("workflow_id") != workflow_id:
                raise DoneConfirmationError("confirmation_artifact_conflict")
            if (
                existing_artifact is not None
                and _is_completed_confirmation(state, phases, gates, existing_artifact, workflow_id)
            ):
                return DoneConfirmationResult(
                    decision="complete",
                    status="completed",
                    current_phase="completed",
                    pr_url=existing_artifact["pr_url"],
                    reused=True,
                )

            done_state = _require_current_done(state, phases, gates)
            if existing_artifact is not None:
                if (
                    decision != "complete"
                    or existing_artifact.get("actor") != actor
                    or existing_artifact.get("pr_url") != pr_url
                ):
                    raise DoneConfirmationError("confirmation_artifact_conflict")
                recorded_at = str(existing_artifact["recorded_at"])
                _record_completed_state(
                    state,
                    actor=actor,
                    pr_url=pr_url,
                    timestamp=recorded_at,
                    action="confirmation_recovered",
                )
                workflow_state._write_workflow_state_unlocked(root, state, directory_fd)
                return DoneConfirmationResult(
                    decision="complete",
                    status="completed",
                    current_phase="completed",
                    pr_url=pr_url,
                    reused=True,
                )

            if decision == "hold":
                if _has_matching_hold(history, actor):
                    return DoneConfirmationResult(
                        decision="hold",
                        status="held",
                        current_phase="done",
                        pr_url=None,
                        reused=True,
                    )
                timestamp = _now_iso()
                done_state["status"] = "pending"
                _append_history(
                    history,
                    action="held",
                    timestamp=timestamp,
                    actor=actor,
                    pr_url=None,
                )
                workflow_state._write_workflow_state_unlocked(root, state, directory_fd)
                return DoneConfirmationResult(
                    decision="hold",
                    status="held",
                    current_phase="done",
                    pr_url=None,
                )

            timestamp = _now_iso()
            artifact = {
                "schema_version": _CONFIRMATION_SCHEMA_VERSION,
                "workflow_id": workflow_id,
                "decision": "complete",
                "actor": actor,
                "pr_url": pr_url,
                "recorded_at": timestamp,
            }
            _write_confirmation_artifact(root, artifact)
            _record_completed_state(
                state,
                actor=actor,
                pr_url=pr_url,
                timestamp=timestamp,
                action="confirmed",
            )
            workflow_state._write_workflow_state_unlocked(root, state, directory_fd)
            return DoneConfirmationResult(
                decision="complete",
                status="completed",
                current_phase="completed",
                pr_url=pr_url,
            )
    except DoneConfirmationError:
        raise
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise DoneConfirmationError("confirmation_failed") from None
