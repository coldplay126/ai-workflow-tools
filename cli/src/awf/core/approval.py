"""Deterministic, parent-only workflow approval transitions.

Approval is deliberately kept outside provider execution.  This module accepts an
explicit human decision, validates the reviewed planning snapshot, and records
the resulting G3 transition without treating worker output as approval.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import unicodedata
from typing import Any

from awf.core.paths import find_repo_root
from awf.core.planning_options import (
    PlanningOptionsError,
    resolve_planning_options_policy,
    validate_planning_options_provenance,
)
from awf.core import state as workflow_state


_APPROVAL_SCHEMA_VERSION = 2
_PLANNING_SEAL_SCHEMA_VERSION = 1
_APPROVAL_FILENAME = "approval.json"
_SCOPE_ARTIFACTS = ("spec.md", "plan.md", "tasks.md")
_PLANNING_ARTIFACTS = (
    "constitution.md",
    "spec.md",
    "plan.md",
    "tasks.md",
    "test-criteria.md",
    "allowed-files.json",
)
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
_MAX_ACTOR_CHARS = 256
_MAX_REASON_CHARS = 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SENSITIVE_TEXT = re.compile(
    r"(?:api[_-]?key|password|secret|token|credential|authorization)\s*(?:=|:)|"
    r"-----BEGIN [A-Z ]+-----|\bsk-[A-Za-z0-9_-]{16,}",
    re.IGNORECASE,
)
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
_APPROVAL_FIELDS = frozenset(
    {
        "schema_version",
        "decision",
        "actor",
        "reason",
        "scope_hash",
        "scope_artifacts",
        "planning_seal",
        "plan_provenance",
        "recorded_at",
    }
)
_PLANNING_SEAL_FIELDS = frozenset({"identity", "artifacts"})


class ApprovalError(ValueError):
    """A stable, safe approval failure code suitable for CLI output."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ApprovalResult:
    decision: str
    status: str
    current_phase: str
    scope_hash: str | None = None
    reused: bool = False

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "decision": self.decision,
            "status": self.status,
            "current_phase": self.current_phase,
            "reused": self.reused,
        }
        if self.scope_hash is not None:
            payload["scope_hash"] = self.scope_hash
        return payload



@dataclass(frozen=True)
class ApprovedPlanningSeal:
    """Exact G3 planning identity validated against the current artifacts."""

    scope_hash: str
    scope_artifacts: dict[str, str]
    planning_seal: dict[str, object]
    plan_provenance: dict[str, str]


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
        raise ApprovalError(code)
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized or len(normalized) > limit or _SENSITIVE_TEXT.search(normalized):
        raise ApprovalError(code)
    return normalized


def _validate_request(
    decision: object,
    actor: object,
    reason: object,
) -> tuple[str, str, str]:
    if decision not in {"approve", "revise", "reject"}:
        raise ApprovalError("decision_invalid")
    normalized_actor = _normalize_text(actor, limit=_MAX_ACTOR_CHARS, code="actor_invalid")
    if normalized_actor.casefold() in _RESERVED_ACTORS:
        raise ApprovalError("actor_not_human")
    if reason is None:
        normalized_reason = ""
    else:
        normalized_reason = _normalize_text(
            reason,
            limit=_MAX_REASON_CHARS,
            code="reason_invalid",
        )
    if decision in {"revise", "reject"} and not normalized_reason:
        raise ApprovalError("reason_required")
    return decision, normalized_actor, normalized_reason


def _open_artifacts_directory(root: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(root / ".workflow" / "artifacts", flags)
    except OSError as error:
        raise ApprovalError("approval_artifacts_unavailable") from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        os.close(descriptor)
        raise ApprovalError("approval_artifacts_unavailable")
    return descriptor


def _read_regular_file(directory_fd: int, name: str, *, missing_code: str) -> bytes:
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            raise ApprovalError(missing_code) from None
        except OSError as error:
            raise ApprovalError("approval_artifact_invalid") from error
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_size > _MAX_ARTIFACT_BYTES
        ):
            raise ApprovalError("approval_artifact_invalid")
        content = bytearray()
        while True:
            chunk = os.read(descriptor, min(64 * 1024, _MAX_ARTIFACT_BYTES + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > _MAX_ARTIFACT_BYTES:
                raise ApprovalError("approval_artifact_invalid")
        return bytes(content)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _planning_seal_from_artifacts(artifacts: dict[str, str]) -> dict[str, object]:
    return {
        "identity": hashlib.sha256(
            _canonical_json_bytes(
                {
                    "schema_version": _PLANNING_SEAL_SCHEMA_VERSION,
                    "artifacts": artifacts,
                }
            )
        ).hexdigest(),
        "artifacts": dict(artifacts),
    }


def _planning_snapshot(
    root: Path,
) -> tuple[str, dict[str, str], dict[str, object]]:
    directory_fd = _open_artifacts_directory(root)
    try:
        planning_artifacts = {
            name: hashlib.sha256(
                _read_regular_file(
                    directory_fd,
                    name,
                    missing_code="planning_artifact_missing",
                )
            ).hexdigest()
            for name in _PLANNING_ARTIFACTS
        }
    finally:
        os.close(directory_fd)
    scope_artifacts = {
        name: planning_artifacts[name] for name in _SCOPE_ARTIFACTS
    }
    scope_hash = hashlib.sha256(
        _canonical_json_bytes(
            {
                "schema_version": _PLANNING_SEAL_SCHEMA_VERSION,
                "artifacts": scope_artifacts,
            }
        )
    ).hexdigest()
    return (
        scope_hash,
        scope_artifacts,
        _planning_seal_from_artifacts(planning_artifacts),
    )


def _approval_snapshot(
    root: Path,
) -> tuple[str, dict[str, str], dict[str, object], dict[str, str]]:
    scope_hash, scope_artifacts, planning_seal = _planning_snapshot(root)
    return scope_hash, scope_artifacts, planning_seal, _plan_provenance(root)


def _plan_provenance(root: Path) -> dict[str, str]:
    try:
        policy = resolve_planning_options_policy(root)
    except PlanningOptionsError as error:
        if error.code in {"artifact_missing", "provenance_missing"}:
            raise ApprovalError("plan_provenance_missing") from None
        raise ApprovalError("plan_provenance_invalid") from None

    if not policy.required:
        return {"status": "not_required"}
    if policy.artifact is None:
        raise ApprovalError("plan_provenance_missing")
    try:
        sealed, detail = validate_planning_options_provenance(
            root,
            policy.artifact.artifact_hash,
        )
    except PlanningOptionsError:
        raise ApprovalError("plan_provenance_invalid") from None
    if not sealed:
        if detail in {"provenance_missing", "artifact_missing"}:
            raise ApprovalError("plan_provenance_missing")
        raise ApprovalError("plan_provenance_changed")
    return {
        "status": "sealed",
        "planning_options_hash": policy.artifact.artifact_hash,
    }


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate JSON key")
        payload[key] = value
    return payload


def _normalize_planning_seal(value: object, *, code: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _PLANNING_SEAL_FIELDS:
        raise ApprovalError(code)
    identity = value.get("identity")
    artifacts = value.get("artifacts")
    if (
        not isinstance(identity, str)
        or _SHA256.fullmatch(identity) is None
        or not isinstance(artifacts, dict)
        or set(artifacts) != set(_PLANNING_ARTIFACTS)
        or any(
            not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
            for digest in artifacts.values()
        )
    ):
        raise ApprovalError(code)
    normalized_artifacts = {
        name: str(artifacts[name]) for name in _PLANNING_ARTIFACTS
    }
    normalized = _planning_seal_from_artifacts(normalized_artifacts)
    if normalized["identity"] != identity:
        raise ApprovalError(code)
    return normalized


def _parse_approval_artifact(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _APPROVAL_FIELDS:
        raise ApprovalError("approval_artifact_invalid")
    if payload.get("schema_version") != _APPROVAL_SCHEMA_VERSION:
        raise ApprovalError("approval_artifact_invalid")
    if payload.get("decision") != "approve":
        raise ApprovalError("approval_artifact_invalid")
    try:
        actor = _normalize_text(payload.get("actor"), limit=_MAX_ACTOR_CHARS, code="approval_artifact_invalid")
        reason_value = payload.get("reason")
        reason = "" if reason_value == "" else _normalize_text(
            reason_value,
            limit=_MAX_REASON_CHARS,
            code="approval_artifact_invalid",
        )
    except ApprovalError:
        raise ApprovalError("approval_artifact_invalid") from None
    if actor.casefold() in _RESERVED_ACTORS:
        raise ApprovalError("approval_artifact_invalid")
    scope_hash = payload.get("scope_hash")
    scope_artifacts = payload.get("scope_artifacts")
    planning_seal = payload.get("planning_seal")
    plan_provenance = payload.get("plan_provenance")
    recorded_at = payload.get("recorded_at")
    if (
        not isinstance(scope_hash, str)
        or _SHA256.fullmatch(scope_hash) is None
        or not isinstance(scope_artifacts, dict)
        or set(scope_artifacts) != set(_SCOPE_ARTIFACTS)
        or any(
            not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
            for digest in scope_artifacts.values()
        )
        or not isinstance(recorded_at, str)
        or not recorded_at
    ):
        raise ApprovalError("approval_artifact_invalid")
    try:
        parsed_recorded_at = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    except ValueError:
        raise ApprovalError("approval_artifact_invalid") from None
    if parsed_recorded_at.tzinfo is None:
        raise ApprovalError("approval_artifact_invalid")
    normalized_seal = _normalize_planning_seal(
        planning_seal,
        code="approval_artifact_invalid",
    )
    seal_artifacts = normalized_seal["artifacts"]
    if not isinstance(seal_artifacts, dict):
        raise ApprovalError("approval_artifact_invalid")
    expected_scope_artifacts = {
        name: seal_artifacts[name] for name in _SCOPE_ARTIFACTS
    }
    expected_scope_hash = hashlib.sha256(
        _canonical_json_bytes(
            {
                "schema_version": _PLANNING_SEAL_SCHEMA_VERSION,
                "artifacts": expected_scope_artifacts,
            }
        )
    ).hexdigest()
    if scope_artifacts != expected_scope_artifacts or scope_hash != expected_scope_hash:
        raise ApprovalError("approval_artifact_invalid")
    if plan_provenance == {"status": "not_required"}:
        normalized_provenance: dict[str, str] = {"status": "not_required"}
    elif (
        isinstance(plan_provenance, dict)
        and set(plan_provenance) == {"status", "planning_options_hash"}
        and plan_provenance.get("status") == "sealed"
        and isinstance(plan_provenance.get("planning_options_hash"), str)
        and _SHA256.fullmatch(str(plan_provenance["planning_options_hash"])) is not None
    ):
        normalized_provenance = {
            "status": "sealed",
            "planning_options_hash": str(plan_provenance["planning_options_hash"]),
        }
    else:
        raise ApprovalError("approval_artifact_invalid")
    return {
        "schema_version": _APPROVAL_SCHEMA_VERSION,
        "decision": "approve",
        "actor": actor,
        "reason": reason,
        "scope_hash": scope_hash,
        "scope_artifacts": dict(scope_artifacts),
        "planning_seal": normalized_seal,
        "plan_provenance": normalized_provenance,
        "recorded_at": recorded_at,
    }


def _read_approval_artifact(root: Path) -> dict[str, object] | None:
    directory_fd = _open_artifacts_directory(root)
    try:
        try:
            raw = _read_regular_file(
                directory_fd,
                _APPROVAL_FILENAME,
                missing_code="approval_artifact_missing",
            )
        except ApprovalError as error:
            if error.code == "approval_artifact_missing":
                return None
            raise
    finally:
        os.close(directory_fd)
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise ApprovalError("approval_artifact_invalid") from None
    return _parse_approval_artifact(payload)


def _write_approval_artifact(root: Path, payload: dict[str, object]) -> None:
    directory_fd = _open_artifacts_directory(root)
    temporary_name = f".{_APPROVAL_FILENAME}.{secrets.token_hex(16)}.tmp"
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
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
            created = True
        except OSError as error:
            raise ApprovalError("approval_write_failed") from error
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ApprovalError("approval_write_failed")
        content = _canonical_json_bytes(payload) + b"\n"
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise ApprovalError("approval_write_failed")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            _APPROVAL_FILENAME,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except OSError as error:
        raise ApprovalError("approval_write_failed") from error
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


def _remove_approval_artifact_if_matches(root: Path, payload: dict[str, object]) -> None:
    """Remove only the artifact this transaction attempted to publish."""


    directory_fd = _open_artifacts_directory(root)
    try:
        try:
            raw = _read_regular_file(
                directory_fd,
                _APPROVAL_FILENAME,
                missing_code="approval_artifact_missing",
            )
        except ApprovalError as error:
            if error.code == "approval_artifact_missing":
                return
            raise ApprovalError("approval_rollback_failed") from None
        try:
            current = _parse_approval_artifact(
                json.loads(
                    raw.decode("utf-8"),
                    object_pairs_hook=_reject_duplicate_keys,
                )
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            raise ApprovalError("approval_rollback_failed") from None
        if current != payload:
            raise ApprovalError("approval_rollback_failed")
        os.unlink(_APPROVAL_FILENAME, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except ApprovalError:
        raise
    except OSError:
        raise ApprovalError("approval_rollback_failed") from None
    finally:
        os.close(directory_fd)


def _state_parts(state: object) -> tuple[dict[str, Any], dict[str, Any], list[Any]]:
    if not isinstance(state, dict):
        raise ApprovalError("state_invalid")
    phases = state.get("phases")
    gates = state.get("gates")
    history = state.get("history")
    if not isinstance(phases, dict) or not isinstance(gates, dict) or not isinstance(history, list):
        raise ApprovalError("state_invalid")
    return phases, gates, history


def _require_g2(gates: dict[str, Any]) -> None:
    g2 = gates.get("G2")
    if not isinstance(g2, dict) or g2.get("passed") is not True:
        raise ApprovalError("g2_not_passed")


def _require_pending_approval(
    state: dict[str, Any],
    phases: dict[str, Any],
    gates: dict[str, Any],
) -> None:
    _require_g2(gates)
    if state.get("currentPhase") != "approve":
        raise ApprovalError("approval_phase_not_current")
    review = phases.get("review")
    approval = phases.get("approve")
    g3 = gates.get("G3")
    if not isinstance(review, dict) or review.get("status") != "completed":
        raise ApprovalError("review_state_invalid")
    if not isinstance(approval, dict) or approval.get("status") != "pending":
        raise ApprovalError("approval_state_invalid")
    if (
        not isinstance(g3, dict)
        or g3.get("passed") is not None
        or g3.get("scope_hash") is not None
    ):
        raise ApprovalError("g3_state_invalid")


def _approval_matches(
    artifact: dict[str, object],
    *,
    scope_hash: str,
    scope_artifacts: dict[str, str],
    planning_seal: dict[str, object],
    plan_provenance: dict[str, str],
) -> bool:
    return (
        artifact["scope_hash"] == scope_hash
        and artifact["scope_artifacts"] == scope_artifacts
        and artifact["planning_seal"] == planning_seal
        and artifact["plan_provenance"] == plan_provenance
    )


def _validated_approved_planning_seal(
    root: Path,
    state: object,
) -> ApprovedPlanningSeal:
    if not isinstance(state, dict):
        raise ApprovalError("approval_state_invalid")
    gates = state.get("gates")
    g3 = gates.get("G3") if isinstance(gates, dict) else None
    if not isinstance(g3, dict) or g3.get("passed") is not True:
        raise ApprovalError("g3_not_passed")

    artifact = _read_approval_artifact(root)
    if artifact is None:
        raise ApprovalError("approval_artifact_missing")
    for field in (
        "scope_hash",
        "scope_artifacts",
        "planning_seal",
        "plan_provenance",
    ):
        if g3.get(field) != artifact[field]:
            raise ApprovalError("approval_seal_invalid")

    try:
        scope_hash, scope_artifacts, planning_seal, plan_provenance = _approval_snapshot(
            root
        )
    except ApprovalError:
        raise ApprovalError("approval_seal_changed") from None
    if not _approval_matches(
        artifact,
        scope_hash=scope_hash,
        scope_artifacts=scope_artifacts,
        planning_seal=planning_seal,
        plan_provenance=plan_provenance,
    ):
        raise ApprovalError("approval_seal_changed")
    seal_identity = planning_seal.get("identity")
    seal_artifacts = planning_seal.get("artifacts")
    if not isinstance(seal_identity, str) or not isinstance(seal_artifacts, dict):
        raise ApprovalError("approval_seal_changed")
    return ApprovedPlanningSeal(
        scope_hash=scope_hash,
        scope_artifacts=dict(scope_artifacts),
        planning_seal={
            "identity": seal_identity,
            "artifacts": dict(seal_artifacts),
        },
        plan_provenance=dict(plan_provenance),
    )


def validate_approved_planning_seal(
    repo_root: Path,
    *,
    state: object | None = None,
) -> ApprovedPlanningSeal:
    """Fail closed unless G3's saved six-artifact seal remains exact."""

    root = Path(repo_root)
    if state is not None:
        return _validated_approved_planning_seal(root, state)
    state_path = root / ".workflow" / "state.json"
    try:
        with workflow_state._workflow_state_lock(
            state_path
        ), workflow_state._workflow_state_transaction(root) as directory_fd:
            current_state = workflow_state._read_workflow_state_from_directory(
                directory_fd
            )
            return _validated_approved_planning_seal(root, current_state)
    except ApprovalError:
        raise
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise ApprovalError("approval_state_invalid") from None


def _reason_hash(reason: str) -> str | None:
    return hashlib.sha256(reason.encode("utf-8")).hexdigest() if reason else None


def _append_history(
    history: list[Any],
    *,
    action: str,
    timestamp: str,
    actor: str,
    details: str,
    reason: str,
    scope_hash: str | None = None,
) -> None:
    item: dict[str, object] = {
        "phase": "approve",
        "action": action,
        "timestamp": timestamp,
        "actor": actor,
        "details": details,
        "reason_sha256": _reason_hash(reason),
    }
    if scope_hash is not None:
        item["scope_hash"] = scope_hash
    history.append(item)


def _record_approved_state(
    state: dict[str, Any],
    *,
    actor: str,
    scope_hash: str,
    scope_artifacts: dict[str, str],
    planning_seal: dict[str, object],
    plan_provenance: dict[str, str],
    recorded_at: str,
    reason: str,
    action: str,
) -> None:
    phases, gates, history = _state_parts(state)
    approval = phases["approve"]
    approval["status"] = "completed"
    approval["approvedAt"] = recorded_at
    approval["actor"] = actor
    gates["G3"] = {
        "passed": True,
        "scope_hash": scope_hash,
        "scope_artifacts": dict(scope_artifacts),
        "planning_seal": dict(planning_seal),
        "plan_provenance": dict(plan_provenance),
        "approvedAt": recorded_at,
        "actor": actor,
    }
    state["currentPhase"] = "impl"
    _append_history(
        history,
        action=action,
        timestamp=recorded_at,
        actor=actor,
        details="parent approval recorded",
        reason=reason,
        scope_hash=scope_hash,
    )


def _record_revision_state(
    state: dict[str, Any], *, actor: str, reason: str, timestamp: str) -> None:
    phases, gates, history = _state_parts(state)
    current_index = workflow_state.PHASE_ORDER.index("plan")
    for phase in workflow_state.PHASE_ORDER[current_index:]:
        phase_state = phases.get(phase)
        if not isinstance(phase_state, dict):
            phase_state = {}
            phases[phase] = phase_state
        workflow_state._clear_phase_runtime_markers(phase_state)
        for key in ("startedAt", "skippedAt", "skipReason", "rejectedAt"):
            phase_state.pop(key, None)
        phase_state["status"] = "pending"
        phase_state["retries"] = 0
        phase_state["executions"] = 0
        gate_id = workflow_state.PHASE_GATE.get(phase)
        if gate_id is not None:
            gates[gate_id] = workflow_state._initial_skipped_gate_state(gate_id)
    state["currentPhase"] = "plan"
    loop = state.setdefault("loop", {})
    if not isinstance(loop, dict):
        raise ApprovalError("state_invalid")
    loop["replanCount"] = int(loop.get("replanCount", 0) or 0) + 1
    loop.setdefault("maxReplans", 3)
    loop.setdefault("history", []).append(
        {
            "fromPhase": "approve",
            "toPhase": "plan",
            "decision": "replan",
            "reason": "approval_revise",
            "at": timestamp,
        }
    )
    _append_history(
        history,
        action="revised",
        timestamp=timestamp,
        actor=actor,
        details="parent requested plan revision",
        reason=reason,
    )


def _record_rejected_state(
    state: dict[str, Any], *, actor: str, reason: str, timestamp: str) -> None:
    phases, gates, history = _state_parts(state)
    approval = phases["approve"]
    approval["status"] = "rejected"
    approval["rejectedAt"] = timestamp
    approval["actor"] = actor
    gates["G3"] = {
        "passed": False,
        "scope_hash": None,
        "rejectedAt": timestamp,
        "actor": actor,
    }
    state["currentPhase"] = "rejected"
    loop = state.setdefault("loop", {})
    if not isinstance(loop, dict):
        raise ApprovalError("state_invalid")
    loop.setdefault("history", []).append(
        {
            "fromPhase": "approve",
            "toPhase": None,
            "decision": "reject",
            "reason": "approval_reject",
            "at": timestamp,
        }
    )
    _append_history(
        history,
        action="rejected",
        timestamp=timestamp,
        actor=actor,
        details="parent rejected workflow approval",
        reason=reason,
    )


def apply_approval(
    explicit_root: str | None,
    *,
    decision: object,
    actor: object,
    reason: object = None,
) -> ApprovalResult:
    """Apply one explicit parent approval decision to a reviewed workflow.

    New approvals bind the state and artifact to one six-artifact snapshot.
    The snapshot is checked again immediately before each durable authority
    write; a failed state commit removes the artifact this invocation created.
    """

    decision, actor, reason = _validate_request(decision, actor, reason)
    try:
        root = find_repo_root(explicit_root)
    except Exception:
        raise ApprovalError("repo_root_invalid") from None

    state_path = root / ".workflow" / "state.json"
    try:
        with workflow_state._workflow_state_lock(
            state_path
        ), workflow_state._workflow_state_transaction(root) as directory_fd:
            state = workflow_state._read_workflow_state_from_directory(directory_fd)
            phases, gates, _ = _state_parts(state)
            _require_g2(gates)
            g3 = gates.get("G3")
            approval_state = phases.get("approve")
            existing_artifact = _read_approval_artifact(root)

            if decision == "approve":
                (
                    entry_scope_hash,
                    entry_scope_artifacts,
                    entry_planning_seal,
                    entry_plan_provenance,
                ) = _approval_snapshot(root)
                if (
                    isinstance(g3, dict)
                    and g3.get("passed") is True
                    and existing_artifact is not None
                    and all(
                        g3.get(field) == existing_artifact[field]
                        for field in (
                            "scope_hash",
                            "scope_artifacts",
                            "planning_seal",
                            "plan_provenance",
                        )
                    )
                    and isinstance(approval_state, dict)
                    and approval_state.get("status") == "completed"
                    and state.get("currentPhase")
                    in {"impl", "verify", "test", "done", "completed"}
                    and _approval_matches(
                        existing_artifact,
                        scope_hash=entry_scope_hash,
                        scope_artifacts=entry_scope_artifacts,
                        planning_seal=entry_planning_seal,
                        plan_provenance=entry_plan_provenance,
                    )
                ):
                    return ApprovalResult(
                        decision="approve",
                        status="approved",
                        current_phase=str(state["currentPhase"]),
                        scope_hash=entry_scope_hash,
                        reused=True,
                    )
            else:
                entry_scope_hash = ""
                entry_scope_artifacts = {}
                entry_planning_seal = {}
                entry_plan_provenance = {}

            _require_pending_approval(state, phases, gates)
            if decision == "approve" and existing_artifact is not None and not _approval_matches(
                existing_artifact,
                scope_hash=entry_scope_hash,
                scope_artifacts=entry_scope_artifacts,
                planning_seal=entry_planning_seal,
                plan_provenance=entry_plan_provenance,
            ):
                raise ApprovalError("approval_artifact_conflict")

            timestamp = _now_iso()
            if decision == "approve":
                (
                    commit_scope_hash,
                    commit_scope_artifacts,
                    commit_planning_seal,
                    commit_plan_provenance,
                ) = _approval_snapshot(root)
                if (
                    commit_scope_hash != entry_scope_hash
                    or commit_scope_artifacts != entry_scope_artifacts
                    or commit_planning_seal != entry_planning_seal
                    or commit_plan_provenance != entry_plan_provenance
                ):
                    raise ApprovalError("approval_identity_changed")

                if existing_artifact is not None:
                    if (
                        existing_artifact["actor"] != actor
                        or existing_artifact["reason"] != reason
                    ):
                        raise ApprovalError("approval_artifact_conflict")
                    (
                        final_scope_hash,
                        final_scope_artifacts,
                        final_planning_seal,
                        final_plan_provenance,
                    ) = _approval_snapshot(root)
                    if (
                        final_scope_hash != commit_scope_hash
                        or final_scope_artifacts != commit_scope_artifacts
                        or final_planning_seal != commit_planning_seal
                        or final_plan_provenance != commit_plan_provenance
                    ):
                        raise ApprovalError("approval_identity_changed")
                    recorded_at = str(existing_artifact["recorded_at"])
                    _record_approved_state(
                        state,
                        actor=actor,
                        scope_hash=commit_scope_hash,
                        scope_artifacts=commit_scope_artifacts,
                        planning_seal=commit_planning_seal,
                        plan_provenance=commit_plan_provenance,
                        recorded_at=recorded_at,
                        reason=reason,
                        action="approval_recovered",
                    )
                    workflow_state._write_workflow_state_unlocked(root, state, directory_fd)
                    return ApprovalResult(
                        decision="approve",
                        status="approved",
                        current_phase="impl",
                        scope_hash=commit_scope_hash,
                        reused=True,
                    )

                artifact = {
                    "schema_version": _APPROVAL_SCHEMA_VERSION,
                    "decision": "approve",
                    "actor": actor,
                    "reason": reason,
                    "scope_hash": commit_scope_hash,
                    "scope_artifacts": commit_scope_artifacts,
                    "planning_seal": commit_planning_seal,
                    "plan_provenance": commit_plan_provenance,
                    "recorded_at": timestamp,
                }
                try:
                    _write_approval_artifact(root, artifact)
                    (
                        final_scope_hash,
                        final_scope_artifacts,
                        final_planning_seal,
                        final_plan_provenance,
                    ) = _approval_snapshot(root)
                    if (
                        final_scope_hash != commit_scope_hash
                        or final_scope_artifacts != commit_scope_artifacts
                        or final_planning_seal != commit_planning_seal
                        or final_plan_provenance != commit_plan_provenance
                    ):
                        raise ApprovalError("approval_identity_changed")
                    _record_approved_state(
                        state,
                        actor=actor,
                        scope_hash=commit_scope_hash,
                        scope_artifacts=commit_scope_artifacts,
                        planning_seal=commit_planning_seal,
                        plan_provenance=commit_plan_provenance,
                        recorded_at=timestamp,
                        reason=reason,
                        action="approved",
                    )
                    workflow_state._write_workflow_state_unlocked(root, state, directory_fd)
                except Exception:
                    _remove_approval_artifact_if_matches(root, artifact)
                    raise
                return ApprovalResult(
                    decision="approve",
                    status="approved",
                    current_phase="impl",
                    scope_hash=commit_scope_hash,
                )

            if existing_artifact is not None:
                raise ApprovalError("approval_artifact_conflict")
            if decision == "revise":
                _record_revision_state(state, actor=actor, reason=reason, timestamp=timestamp)
                workflow_state._write_workflow_state_unlocked(root, state, directory_fd)
                return ApprovalResult(
                    decision="revise",
                    status="revised",
                    current_phase="plan",
                )

            _record_rejected_state(state, actor=actor, reason=reason, timestamp=timestamp)
            workflow_state._write_workflow_state_unlocked(root, state, directory_fd)
            return ApprovalResult(
                decision="reject",
                status="rejected",
                current_phase="rejected",
            )
    except ApprovalError:
        raise
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise ApprovalError("approval_failed") from None
