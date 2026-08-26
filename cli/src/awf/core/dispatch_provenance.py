from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(item) for item in value]
        return str(value)
    return value


def _text(metadata: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _is_ascii_graphic(value: str) -> bool:
    return bool(value) and all(0x21 <= ord(character) <= 0x7E for character in value)


_NATIVE_CHECKPOINT_STATES = frozenset(
    {"prepared", "resuming", "completed", "interrupted", "ambiguous"}
)
_NATIVE_TERMINAL_STATUSES = frozenset(
    {"completed", "success", "ok", "failed", "error", "cancelled", "canceled"}
)

_CANCELLATION_AUDIT_STATES = (
    "requested",
    "acknowledged",
    "final",
    "partial",
    "unresolved",
)
_CANCELLED_STATUSES = frozenset({"cancelled", "canceled"})
_EVIDENCE_AGENT_STATUSES = _NATIVE_TERMINAL_STATUSES | frozenset({"timed_out"})
_EVIDENCE_DISPATCH_STATUSES = _NATIVE_CHECKPOINT_STATES | frozenset(
    {"failed", "timed_out"}
)


def _evidence_status(value: Any, *, allowed: frozenset[str]) -> str:
    if not isinstance(value, str):
        return "unknown"
    normalized = value.strip().lower()
    return normalized if normalized in allowed else "unknown"



def _known_bool(value: Any) -> bool | None:
    return value if type(value) is bool else None


def _safe_cancellation_audit(
    value: Any,
    *,
    status: str | None = None,
) -> dict[str, bool | None]:
    """Keep only cancellation state transitions; discard diagnostic bodies."""
    raw = value if isinstance(value, Mapping) else {}
    audit = {
        state: _known_bool(raw.get(state))
        for state in _CANCELLATION_AUDIT_STATES
    }
    if status in _CANCELLED_STATUSES and audit["final"] is None:
        audit["final"] = True
    return audit


def _partial_result_status(
    metadata: Mapping[str, Any],
    cancellation: Mapping[str, bool | None],
) -> bool | None:
    for key in ("partial", "partial_result", "partial_result_preserved"):
        value = _known_bool(metadata.get(key))
        if value is not None:
            return value
    return cancellation.get("partial")


def _patch_scope_status(metadata: Mapping[str, Any]) -> str:
    validation = metadata.get("write_scope_validation")
    if isinstance(validation, Mapping):
        valid = _known_bool(validation.get("valid"))
        applied = _known_bool(validation.get("applied"))
        if valid is False:
            return "invalid"
        if valid is True and applied is True:
            return "valid"
        if valid is True and applied is False:
            return "not_applied"
    if _text(metadata, "patch_path"):
        return "unverified"
    return "unknown"


_USAGE_ALIASES = {
    "input_tokens": (
        "input_tokens",
        "inputTokens",
        "prompt_tokens",
        "promptTokens",
    ),
    "output_tokens": (
        "output_tokens",
        "outputTokens",
        "completion_tokens",
        "completionTokens",
    ),
    "total_tokens": ("total_tokens", "totalTokens", "tokens"),
    "cache_read_tokens": ("cache_read_tokens", "cacheReadTokens"),
    "cache_write_tokens": ("cache_write_tokens", "cacheWriteTokens"),
    "duration_ms": ("duration_ms", "durationMs"),
    "cost_usd": ("cost_usd", "costUsd", "cost", "usd"),
}
_COST_ALIASES = {
    "cost_usd": ("cost_usd", "costUsd", "cost", "usd", "total_usd")
}
_FOLLOWUP_OUTCOMES = frozenset({"delivered", "failed"})
_FOLLOWUP_REASON_CODES = frozenset({"registry_unavailable", "delivery_failed"})
_CORRELATION_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _nonnegative_finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _normalized_numeric_fields(
    value: Any,
    aliases: Mapping[str, tuple[str, ...]],
) -> dict[str, int | float] | None:
    if not isinstance(value, Mapping):
        return None
    normalized: dict[str, int | float] = {}
    for field, names in aliases.items():
        raw = next(
            (value.get(name) for name in names if value.get(name) is not None),
            None,
        )
        number = _nonnegative_finite_number(raw)
        if number is not None:
            normalized[field] = number
    return normalized or None


def _reported_usage(value: Any) -> dict[str, int | float] | None:
    """Extract finite worker-reported usage without arbitrary metadata."""
    return _normalized_numeric_fields(value, _USAGE_ALIASES)


def _normalized_cost(value: Any) -> dict[str, int | float] | None:
    if not isinstance(value, Mapping):
        number = _nonnegative_finite_number(value)
        return {"cost_usd": number} if number is not None else None
    return _normalized_numeric_fields(value, _COST_ALIASES)


def _correlation_identifier(value: Any) -> str | None:
    return (
        value
        if isinstance(value, str) and _CORRELATION_IDENTIFIER.fullmatch(value)
        else None
    )


def _safe_followup_evidence(value: Any) -> dict[str, list[dict[str, Any]]] | None:
    """Retain only bounded follow-up correlation fields, never tool bodies."""
    if not isinstance(value, Mapping):
        return None
    evidence: dict[str, list[dict[str, Any]]] = {}
    for kind in ("hub", "read", "task"):
        records = value.get(kind)
        if not isinstance(records, list):
            continue
        normalized_records: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            index = record.get("index")
            outcome = record.get("outcome")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 0
                or not isinstance(outcome, str)
                or outcome not in _FOLLOWUP_OUTCOMES
            ):
                continue
            normalized: dict[str, Any] = {"index": index, "outcome": outcome}
            if kind == "hub":
                target_task_id = _correlation_identifier(
                    record.get("target_task_id")
                )
                if target_task_id is not None:
                    normalized["target_task_id"] = target_task_id
                reason_code = record.get("reason_code")
                if (
                    isinstance(reason_code, str)
                    and reason_code in _FOLLOWUP_REASON_CODES
                ):
                    normalized["reason_code"] = reason_code
            elif kind == "read":
                path = record.get("path")
                if _is_opaque_uri(path, "history://"):
                    normalized["history_uri"] = path
            normalized_records.append(normalized)
        evidence[kind] = normalized_records
    return evidence


def _cancellation_summary(
    records: Iterable[Mapping[str, Any]],
) -> dict[str, bool | None]:
    summary: dict[str, bool | None] = {}
    materialized = list(records)
    for state in _CANCELLATION_AUDIT_STATES:
        values = [
            _known_bool(record.get("cancellation", {}).get(state))
            for record in materialized
            if isinstance(record.get("cancellation"), Mapping)
        ]
        known = [value for value in values if value is not None]
        summary[state] = (
            True
            if any(value is True for value in known)
            else False
            if known and len(known) == len(materialized)
            else None
        )
    return summary


def _read_workflow_state_for_evidence(root: Path) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(
            (root / ".workflow" / "state.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _workflow_correlation(root: Path) -> dict[str, str | int | None]:
    """Capture bounded workflow correlation without making provenance writes depend on state."""
    unknown = {"workflow_id": None, "phase": None, "attempt": None}
    payload = _read_workflow_state_for_evidence(root)
    if payload is None:
        return unknown
    workflow_id = payload.get("id")
    phase = payload.get("currentPhase")
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        return unknown
    if not isinstance(phase, str) or not phase.strip():
        return {"workflow_id": workflow_id, "phase": None, "attempt": None}
    phases = payload.get("phases")
    phase_info = phases.get(phase) if isinstance(phases, Mapping) else None
    retries = phase_info.get("retries") if isinstance(phase_info, Mapping) else None
    attempt = (
        retries + 1
        if isinstance(retries, int) and not isinstance(retries, bool) and retries >= 0
        else None
    )
    return {"workflow_id": workflow_id, "phase": phase, "attempt": attempt}


def _is_lower_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_opaque_uri(value: Any, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and _is_ascii_graphic(value[len(prefix) :])
    )


def _native_required_bool(
    path: Path, payload: Mapping[str, Any], field: str
) -> bool:
    if field not in payload:
        raise ValueError(f"invalid OMP native checkpoint {path}: missing {field}")
    value = payload[field]
    if type(value) is not bool:
        raise ValueError(f"invalid OMP native checkpoint {path}: invalid {field}")
    return value


def _safe_schema_validation(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    if type(value.get("valid")) is bool:
        result["valid"] = value["valid"]
    status = value.get("status")
    if isinstance(status, str) and status in {
        "valid",
        "invalid",
        "pass",
        "fail",
        "not_run",
        "skipped",
        "failed",
    }:
        result["status"] = status
    mode = value.get("mode")
    if isinstance(mode, str) and mode in {"strict", "permissive"}:
        result["mode"] = mode
    return result or None


_CORRELATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")


def _safe_correlation_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _CORRELATION_ID.fullmatch(value) else None


def _safe_conclusion(value: Any) -> tuple[str | None, str]:
    raw = str(value or "")
    normalized = raw.strip().upper()
    verdict = (
        "PASS"
        if normalized.startswith("PASS")
        else "FAIL"
        if normalized.startswith("FAIL")
        else None
    )
    return verdict, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_steering_evidence(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    inspected = value.get("inspected_completed")
    raw_wait_calls = value.get("wait_calls")
    inspected_ids = (
        [
            safe
            for item in inspected
            if (safe := _safe_correlation_id(item)) is not None
        ]
        if isinstance(inspected, list)
        else []
    )
    return {
        "reported": value.get("reported") is True,
        "wait_calls": (
            raw_wait_calls
            if isinstance(raw_wait_calls, int)
            and not isinstance(raw_wait_calls, bool)
            and raw_wait_calls >= 0
            else 0
        ),
        "inspected_completed": inspected_ids,
        "message_sent": value.get("message_sent") is True,
        "message_target": _safe_correlation_id(value.get("message_target")),
        "message_kind": (
            value.get("message_kind")
            if value.get("message_kind") in {"corrective", "blocker"}
            else None
        ),
    }


def _declared_status(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"completed", "complete", "success", "succeeded", "ok"}:
        return "completed"
    if normalized in {"timed_out", "timeout", "timed-out"}:
        return "timed_out"
    return "failed"


def _validation_failed(metadata: Mapping[str, Any], key: str) -> bool:
    value = metadata.get(key)
    return isinstance(value, Mapping) and value.get("valid") is False


def _status(agent: Any, metadata: Mapping[str, Any]) -> str:
    if bool(getattr(agent, "timed_out", False)):
        return "timed_out"
    if (
        int(getattr(agent, "returncode", 1)) != 0
        or bool(getattr(agent, "parse_error", False))
        or _validation_failed(metadata, "schema_validation")
        or _validation_failed(metadata, "write_scope_validation")
    ):
        return "failed"
    declared = _declared_status(_text(metadata, "status"))
    return declared or "completed"


def _record_for_agent(agent: Any) -> dict[str, Any]:
    stdout = str(getattr(agent, "stdout", "") or "")
    raw_metadata = getattr(agent, "metadata", {}) or {}
    metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
    session_id = _text(metadata, "coordinator_session_id", "session_id")
    task_id = _text(metadata, "task_id")
    parent_task_id = _text(metadata, "parent_task_id")
    successor_task_id = _text(metadata, "successor_task_id")
    worker_usage = metadata.get("worker_usage")
    has_worker_usage = isinstance(worker_usage, Mapping) and any(
        value is not None for value in worker_usage.values()
    )
    worker_reported_usage = _reported_usage(worker_usage)
    runtime_usage = worker_usage if has_worker_usage else metadata.get("usage")
    lineage = {
        "parent_run_id": _text(metadata, "parent_run_id"),
        "parent_task_id": parent_task_id,
        "original_task_id": _text(metadata, "original_task_id"),
        "successor_task_id": successor_task_id,
        "followup_kind": _text(metadata, "followup_kind", "delivery"),
    }
    status = _status(agent, metadata)
    cancellation = _safe_cancellation_audit(
        metadata.get("cancellation_audit", metadata.get("cancellation")),
        status=_text(metadata, "status", "task_status") or status,
    )
    partial_result = _partial_result_status(metadata, cancellation)
    declared_status = _text(metadata, "status")
    normalized_declared_status = _declared_status(declared_status)
    conclusion, conclusion_sha256 = _safe_conclusion(
        getattr(agent, "conclusion", "")
    )
    return {
        "role": str(getattr(agent, "role", "")),
        "provider": str(getattr(agent, "provider_name", "")),
        "status": status,
        "returncode": int(getattr(agent, "returncode", 1)),
        "timed_out": bool(getattr(agent, "timed_out", False)),
        "parse_error": bool(getattr(agent, "parse_error", False)),
        "elapsed_sec": round(float(getattr(agent, "elapsed_sec", 0.0)), 3),
        "conclusion": conclusion,
        "conclusion_sha256": conclusion_sha256,
        "output_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        "coordination_surface": _text(metadata, "coordination_surface"),
        "coordinator_session_id": session_id,
        "session_persisted": bool(metadata.get("session_persisted", False)),
        "task_id": task_id,
        "agent_uri": _text(metadata, "agent_uri"),
        "history_uri": _text(metadata, "history_uri"),
        "schema_validation": _safe_schema_validation(metadata.get("schema_validation")),
        "write_scope_validation": _safe_schema_validation(
            metadata.get("write_scope_validation")
        ),
        "cancellation": cancellation,
        "partial_result": partial_result,
        "patch_scope_status": _patch_scope_status(metadata),
        "lineage": lineage,
        "followup_evidence": _safe_followup_evidence(
            metadata.get("followup_evidence")
        ),
        "steering_evidence": _safe_steering_evidence(
            metadata.get("steering_evidence")
        ),
        "declared_status": declared_status,
        "declared_status_matches_evidence": (
            normalized_declared_status == status
            if normalized_declared_status is not None
            else None
        ),
        "metadata": {
            "backend": _text(metadata, "backend"),
            "session_id": session_id,
            "execution_mode": _text(metadata, "execution_mode"),
            "session_persisted": bool(metadata.get("session_persisted", False)),
            "task_id": task_id,
            "agent_uri": _text(metadata, "agent_uri"),
            "history_uri": _text(metadata, "history_uri"),
            "provider": _text(metadata, "provider"),
            "model": _text(metadata, "model"),
        },
        "runtime": {
            "provider": _text(metadata, "provider"),
            "model": _text(metadata, "model"),
            "cost": _normalized_cost(metadata.get("cost")),
            "usage": _reported_usage(runtime_usage),
            "coordinator_usage": _reported_usage(metadata.get("usage")),
            "worker_reported_usage": worker_reported_usage,
        },
    }


def _atomic_write(target: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.stem}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(encoded)
        temp_path = Path(handle.name)
    os.replace(temp_path, target)


def write_omp_dispatch_provenance(
    repo_root: str | os.PathLike[str],
    *,
    strategy: str,
    mode: str,
    agents: Iterable[Any],
    elapsed_sec: float,
    parent_run_id: str | None = None,
    parent_task_id: str | None = None,
    message_sha256: str | None = None,
) -> Path | None:
    """Write one redacted OMP dispatch record under workflow artifacts.

    Only correlation handles, runtime facts, and SHA-256 digests are retained.
    Prompt, follow-up message, response, and arbitrary metadata bodies are
    deliberately excluded.
    """
    root = Path(repo_root).resolve()
    workflow_dir = root / ".workflow"
    if not workflow_dir.is_dir():
        return None

    records = [_record_for_agent(agent) for agent in agents]
    sessions = sorted(
        {
            str(record["coordinator_session_id"])
            for record in records
            if record.get("coordinator_session_id")
        }
    )
    now = datetime.now(timezone.utc)
    run_id = f"omp-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
    payload = {
        "schema_version": 2,
        "run_id": run_id,
        "created_at": now.isoformat(),
        "backend": "omp",
        "strategy": strategy,
        "mode": mode,
        "status": (
            "completed"
            if records and all(record["status"] == "completed" for record in records)
            else "failed"
        ),
        "elapsed_sec": round(elapsed_sec, 3),
        "coordinator_session_id": sessions[0] if len(sessions) == 1 else None,
        "parent_run_id": parent_run_id,
        "parent_task_id": parent_task_id,
        "message_sha256": message_sha256,
        "workflow": _workflow_correlation(root),
        "cancellation": _cancellation_summary(records),
        "partial_result": (
            True
            if any(record.get("partial_result") is True for record in records)
            else False
            if records and all(record.get("partial_result") is False for record in records)
            else None
        ),
        "agents": records,
    }

    target_dir = workflow_dir / "artifacts" / "dispatch"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{run_id}.json"
    _atomic_write(target, payload)
    try:
        from awf.core.operational_metrics import record_omp_evidence_summary

        record_omp_evidence_summary(root, summarize_omp_evidence(root))
    except Exception:
        # Provenance is mandatory, but its operational telemetry is observational.
        pass
    return target


def _native_checkpoint_record(
    path: Path, payload: Mapping[str, Any]
) -> dict[str, Any]:
    version = payload.get("version")
    if type(version) is not int or version != 1:
        raise ValueError(f"invalid OMP native checkpoint {path}: unsupported version")

    fingerprint = payload.get("batch_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        raise ValueError(f"invalid OMP native checkpoint {path}: missing batch_fingerprint")
    if not _is_lower_sha256(fingerprint):
        raise ValueError(f"invalid OMP native checkpoint {path}: invalid batch_fingerprint")

    workers = payload.get("workers")
    if not isinstance(workers, list) or not workers:
        raise ValueError(f"invalid OMP native checkpoint {path}: missing workers")

    if "descriptor_hashes" not in payload:
        raise ValueError(f"invalid OMP native checkpoint {path}: missing descriptor_hashes")
    descriptor_hashes = payload["descriptor_hashes"]
    if not isinstance(descriptor_hashes, list) or not all(
        _is_lower_sha256(descriptor) for descriptor in descriptor_hashes
    ):
        raise ValueError(f"invalid OMP native checkpoint {path}: invalid descriptor_hashes")
    if len(descriptor_hashes) != len(workers):
        raise ValueError(
            f"invalid OMP native checkpoint {path}: descriptor_hashes cardinality"
        )

    if "worker_names" not in payload:
        raise ValueError(f"invalid OMP native checkpoint {path}: missing worker_names")
    worker_names = payload["worker_names"]
    if not isinstance(worker_names, list) or not all(
        isinstance(name, str) for name in worker_names
    ):
        raise ValueError(f"invalid OMP native checkpoint {path}: invalid worker_names")
    if len(worker_names) != len(workers):
        raise ValueError(
            f"invalid OMP native checkpoint {path}: worker_names cardinality"
        )

    if "attempt" not in payload:
        raise ValueError(f"invalid OMP native checkpoint {path}: missing attempt")
    attempt = payload["attempt"]
    if type(attempt) is not int or attempt < 1:
        raise ValueError(f"invalid OMP native checkpoint {path}: invalid attempt")

    session_persistence_requested = _native_required_bool(
        path, payload, "session_persistence_requested"
    )
    session_persisted = _native_required_bool(path, payload, "session_persisted")
    resumable = _native_required_bool(path, payload, "resumable")

    if "steering_evidence" not in payload:
        raise ValueError(f"invalid OMP native checkpoint {path}: missing steering_evidence")
    steering_evidence = payload["steering_evidence"]
    if steering_evidence is not None and not isinstance(steering_evidence, Mapping):
        raise ValueError(f"invalid OMP native checkpoint {path}: invalid steering_evidence")

    state = payload.get("state")
    if not isinstance(state, str) or state not in _NATIVE_CHECKPOINT_STATES:
        raise ValueError(f"invalid OMP native checkpoint {path}: invalid state")

    if resumable and not session_persisted:
        raise ValueError(
            f"invalid OMP native checkpoint {path}: resumable checkpoint requires persisted session"
        )
    if session_persisted and not session_persistence_requested:
        raise ValueError(
            f"invalid OMP native checkpoint {path}: session persistence contradiction"
        )

    session_id = payload.get("coordinator_session_id")
    if session_persisted:
        if not isinstance(session_id, str) or not _is_ascii_graphic(session_id):
            raise ValueError(
                f"invalid OMP native checkpoint {path}: missing coordinator_session_id"
            )
    elif session_id is not None:
        raise ValueError(
            f"invalid OMP native checkpoint {path}: coordinator session contradiction"
        )

    if state == "prepared":
        if session_persisted or resumable or steering_evidence is not None:
            raise ValueError(
                f"invalid OMP native checkpoint {path}: prepared checkpoint state contradiction"
            )
    elif state == "resuming":
        if (
            not session_persistence_requested
            or not session_persisted
            or not resumable
            or steering_evidence is not None
        ):
            raise ValueError(
                f"invalid OMP native checkpoint {path}: resuming checkpoint state contradiction"
            )
    else:
        if not isinstance(steering_evidence, Mapping):
            raise ValueError(f"invalid OMP native checkpoint {path}: invalid steering_evidence")
        if state == "completed":
            if resumable:
                raise ValueError(
                    f"invalid OMP native checkpoint {path}: completed checkpoint cannot be resumable"
                )
        elif state == "interrupted":
            if not (
                (session_persisted and resumable)
                or (
                    not session_persistence_requested
                    and not session_persisted
                    and not resumable
                )
            ):
                raise ValueError(
                    f"invalid OMP native checkpoint {path}: interrupted checkpoint state contradiction"
                )
        elif resumable or (not session_persisted and not session_persistence_requested):
            raise ValueError(
                f"invalid OMP native checkpoint {path}: ambiguous checkpoint state contradiction"
            )

    records: list[dict[str, Any]] = []
    task_ids: set[str] = set()
    for position, worker in enumerate(workers):
        if not isinstance(worker, Mapping):
            raise ValueError(
                f"invalid OMP native checkpoint {path}: worker {position} is not an object"
            )
        if "index" not in worker:
            raise ValueError(
                f"invalid OMP native checkpoint {path}: worker {position} missing index"
            )
        index = worker["index"]
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index != position
        ):
            raise ValueError(
                f"invalid OMP native checkpoint {path}: worker {position} has invalid index"
            )

        if "descriptor_sha256" not in worker:
            raise ValueError(
                f"invalid OMP native checkpoint {path}: worker {position} missing descriptor_sha256"
            )
        descriptor_sha256 = worker["descriptor_sha256"]
        if not _is_lower_sha256(descriptor_sha256):
            raise ValueError(
                f"invalid OMP native checkpoint {path}: worker {position} has invalid descriptor_sha256"
            )
        if descriptor_sha256 != descriptor_hashes[position]:
            raise ValueError(
                f"invalid OMP native checkpoint {path}: worker {position} descriptor mismatch"
            )

        name = worker.get("name")
        if not isinstance(name, str) or not name or name != name.strip():
            raise ValueError(
                f"invalid OMP native checkpoint {path}: worker {position} has invalid name"
            )
        if name != worker_names[position]:
            raise ValueError(
                f"invalid OMP native checkpoint {path}: worker_names ordering"
            )

        task_id = worker.get("task_id")
        agent_uri = worker.get("agent_uri")
        history_uri = worker.get("history_uri")
        status = worker.get("status")
        if state == "prepared":
            if (
                task_id is not None
                or agent_uri is not None
                or history_uri is not None
                or status is not None
            ):
                raise ValueError(
                    f"invalid OMP native checkpoint {path}: prepared worker {position} has finalized fields"
                )
        else:
            if state == "completed" and "task_id" not in worker:
                raise ValueError(
                    f"invalid OMP native checkpoint {path}: worker {position} has no task ID"
                )
            if task_id is None:
                if resumable:
                    raise ValueError(
                        f"invalid OMP native checkpoint {path}: resumable worker {position} missing task ID"
                    )
                if state == "completed":
                    raise ValueError(
                        f"invalid OMP native checkpoint {path}: worker {position} has invalid task ID"
                    )
                if agent_uri is not None or history_uri is not None:
                    raise ValueError(
                        f"invalid OMP native checkpoint {path}: worker {position} has handles without task ID"
                    )
            else:
                if not isinstance(task_id, str) or not _is_ascii_graphic(task_id):
                    raise ValueError(
                        f"invalid OMP native checkpoint {path}: worker {position} has invalid task ID"
                    )
                if agent_uri is None or history_uri is None:
                    if state == "completed":
                        raise ValueError(
                            f"invalid OMP native checkpoint {path}: completed worker {position} missing handles"
                        )
                    raise ValueError(
                        f"invalid OMP native checkpoint {path}: worker {position} missing handles"
                    )
                if not _is_opaque_uri(agent_uri, "agent://"):
                    raise ValueError(
                        f"invalid OMP native checkpoint {path}: worker {position} has invalid agent_uri"
                    )
                if not _is_opaque_uri(history_uri, "history://"):
                    raise ValueError(
                        f"invalid OMP native checkpoint {path}: worker {position} has invalid history_uri"
                    )
                if task_id in task_ids:
                    raise ValueError(
                        f"invalid OMP native checkpoint {path}: duplicate task ID {task_id}"
                    )
                task_ids.add(task_id)

            if status is not None and (
                not isinstance(status, str)
                or not status
                or status != status.strip()
            ):
                raise ValueError(
                    f"invalid OMP native checkpoint {path}: worker {position} has invalid status"
                )
            if state == "completed" and status not in _NATIVE_TERMINAL_STATUSES:
                raise ValueError(
                    f"invalid OMP native checkpoint {path}: completed worker {position} is not terminal"
                )

        records.append(
            {
                "worker_index": index,
                "name": name,
                "task_id": task_id or "",
                "agent_uri": agent_uri or "",
                "history_uri": history_uri or "",
                "status": status or "",
                "session_persisted": session_persisted,
                "coordinator_session_id": session_id,
            }
        )
    cancelled = any(
        str(record.get("status") or "").lower() in _CANCELLED_STATUSES
        for record in records
    )


    return {
        "schema_version": 2,
        "backend": "omp",
        "source_kind": "omp_native_batch",
        "run_id": path.stem,
        "status": state,
        "coordinator_session_id": session_id,
        "workflow": {"workflow_id": None, "phase": None, "attempt": attempt},
        "checkpoint": {
            "state": state,
            "resumable": resumable,
            "session_persisted": session_persisted,
        },
        "cancellation": _safe_cancellation_audit(
            {},
            status="cancelled" if cancelled else None,
        ),
        "partial_result": None,
        "agents": records,
    }


def _load_record(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid OMP provenance file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"not a supported OMP provenance record: {path}")
    if payload.get("kind") == "omp_native_batch":
        if "backend" in payload or "schema_version" in payload:
            raise ValueError(
                f"invalid OMP native checkpoint {path}: conflicting discriminator"
            )
        return _native_checkpoint_record(path, payload)
    if payload.get("backend") == "omp" and payload.get("schema_version") == 2:
        return payload
    raise ValueError(f"not a supported OMP provenance record: {path}")


def lookup_omp_provenance(
    repo_root: str | os.PathLike[str],
    reference: str | os.PathLike[str],
) -> tuple[Path, dict[str, Any]]:
    """Resolve exactly one OMP provenance file by path or recorded run ID."""
    root = Path(repo_root).resolve()
    ref = Path(reference)
    explicit = ref if ref.is_absolute() else root / ref
    if explicit.is_file():
        return explicit.resolve(), _load_record(explicit.resolve())

    dispatch_dir = root / ".workflow" / "artifacts" / "dispatch"
    if not ref.is_absolute() and ref.parent == Path("."):
        named = dispatch_dir / ref.name
        if named.is_file():
            return named.resolve(), _load_record(named.resolve())
    candidates: list[tuple[Path, dict[str, Any]]] = []
    if dispatch_dir.is_dir():
        for path in sorted(dispatch_dir.glob("*.json")):
            try:
                payload = _load_record(path)
            except ValueError:
                continue
            if str(payload.get("run_id") or "") == str(reference):
                candidates.append((path.resolve(), payload))
    if not candidates:
        raise FileNotFoundError(f"OMP provenance not found: {reference}")
    if len(candidates) > 1:
        paths = ", ".join(str(path) for path, _ in candidates)
        raise ValueError(f"ambiguous OMP provenance run ID {reference!r}: {paths}")
    return candidates[0]


def _evidence_schema_status(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {
            "mode": "unknown",
            "status": "unknown",
            "strict_status": "unknown",
        }
    mode = value.get("mode")
    normalized_mode = mode if mode in {"strict", "permissive"} else "unknown"
    valid = _known_bool(value.get("valid"))
    status = "valid" if valid is True else "invalid" if valid is False else "unknown"
    strict_status = (
        status
        if normalized_mode == "strict"
        else "not_strict"
        if normalized_mode == "permissive"
        else "unknown"
    )
    return {
        "mode": normalized_mode,
        "status": status,
        "strict_status": strict_status,
    }


def _evidence_correlation(payload: Mapping[str, Any]) -> dict[str, str | int | None]:
    raw = payload.get("workflow")
    if not isinstance(raw, Mapping):
        raw = {}
    workflow_id = raw.get("workflow_id")
    phase = raw.get("phase")
    attempt = raw.get("attempt")
    return {
        "workflow_id": (
            workflow_id if isinstance(workflow_id, str) and workflow_id.strip() else None
        ),
        "phase": phase if isinstance(phase, str) and phase.strip() else None,
        "attempt": (
            attempt
            if isinstance(attempt, int)
            and not isinstance(attempt, bool)
            and attempt >= 1
            else None
        ),
    }


def _evidence_lineage(value: Any) -> dict[str, str | None]:
    raw = value if isinstance(value, Mapping) else {}
    return {
        key: _text(raw, key)
        for key in (
            "parent_run_id",
            "parent_task_id",
            "original_task_id",
            "successor_task_id",
            "followup_kind",
        )
    }


def _evidence_agent(record: Mapping[str, Any]) -> dict[str, Any]:
    runtime = record.get("runtime")
    runtime_map = runtime if isinstance(runtime, Mapping) else {}
    metadata = record.get("metadata")
    metadata_map = metadata if isinstance(metadata, Mapping) else {}
    normalized_status = _evidence_status(
        record.get("status"),
        allowed=_EVIDENCE_AGENT_STATUSES,
    )
    cancellation = _safe_cancellation_audit(
        record.get("cancellation"),
        status=normalized_status,
    )
    partial = _known_bool(record.get("partial_result"))
    if partial is None:
        partial = cancellation["partial"]
    patch_scope = record.get("patch_scope_status")
    if patch_scope not in {"valid", "invalid", "not_applied", "unverified"}:
        patch_scope = _patch_scope_status(metadata_map)
    worker_usage = _reported_usage(runtime_map.get("worker_reported_usage"))
    model = _text(runtime_map, "model") or _text(metadata_map, "model") or "unknown"
    role = record.get("role") or record.get("name")
    return {
        "role": role if isinstance(role, str) and role.strip() else "unknown",
        "status": normalized_status,
        "timeout": (
            "timed_out"
            if record.get("timed_out") is True
            else "not_timed_out"
            if record.get("timed_out") is False
            else "unknown"
        ),
        "cancellation": cancellation,
        "partial_result": (
            "partial"
            if partial is True
            else "not_partial"
            if partial is False
            else "unknown"
        ),
        "schema": _evidence_schema_status(record.get("schema_validation")),
        "patch_scope_status": patch_scope,
        "model": model,
        "reported_usage": {
            "source": "omp_worker_reported",
            "status": "reported" if worker_usage is not None else "unknown",
            "values": worker_usage,
        },
        "lineage": _evidence_lineage(record.get("lineage")),
    }


def _validate_evidence_record(payload: Mapping[str, Any]) -> bool:
    """Validate only the redacted fields consumed by the read-only panel."""
    run_id = payload.get("run_id")
    agents = payload.get("agents")
    dispatch_status = _evidence_status(
        payload.get("status"),
        allowed=_EVIDENCE_DISPATCH_STATUSES,
    )
    if (
        not isinstance(run_id, str)
        or not run_id.strip()
        or not isinstance(agents, list)
        or dispatch_status == "unknown"
    ):
        return False
    native_incomplete = (
        payload.get("source_kind") == "omp_native_batch"
        and dispatch_status != "completed"
    )
    for agent in agents:
        if not isinstance(agent, Mapping):
            return False
        role = agent.get("role") or agent.get("name")
        status = agent.get("status")
        if (
            not isinstance(role, str)
            or not role.strip()
            or not isinstance(status, str)
            or (
                status.strip()
                and _evidence_status(status, allowed=_EVIDENCE_AGENT_STATUSES)
                == "unknown"
            )
            or (not status.strip() and not native_incomplete)
        ):
            return False
    return True


def _phase_primary_estimated_usage(state: Mapping[str, Any]) -> dict[str, Any]:
    telemetry = state.get("telemetry")
    phases = telemetry.get("phases") if isinstance(telemetry, Mapping) else None
    by_phase: list[dict[str, int | float | str]] = []
    totals: dict[str, int | float] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
    }
    if isinstance(phases, Mapping):
        for phase, raw in sorted(phases.items()):
            if not isinstance(phase, str) or not isinstance(raw, Mapping):
                continue
            input_tokens = _nonnegative_finite_number(raw.get("input_tokens"))
            output_tokens = _nonnegative_finite_number(raw.get("output_tokens"))
            cost_usd = _nonnegative_finite_number(raw.get("cost_usd"))
            if not any(
                value is not None
                for value in (input_tokens, output_tokens, cost_usd)
            ):
                continue
            item: dict[str, int | float | str] = {"phase": phase}
            if input_tokens is not None:
                item["input_tokens"] = input_tokens
                totals["input_tokens"] += input_tokens
            if output_tokens is not None:
                item["output_tokens"] = output_tokens
                totals["output_tokens"] += output_tokens
            if cost_usd is not None:
                item["cost_usd"] = cost_usd
                totals["cost_usd"] += cost_usd
            by_phase.append(item)
    return {
        "source": "phase_primary_estimated",
        "status": "estimated" if by_phase else "unknown",
        "by_phase": by_phase,
        "totals": totals if by_phase else None,
    }


def _omp_worker_reported_usage(dispatches: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    totals: dict[str, int | float] = {}
    worker_count = 0
    reported_worker_count = 0
    for dispatch in dispatches:
        agents = dispatch.get("agents")
        if not isinstance(agents, list):
            continue
        for agent in agents:
            if not isinstance(agent, Mapping):
                continue
            worker_count += 1
            usage = agent.get("reported_usage")
            values = usage.get("values") if isinstance(usage, Mapping) else None
            if not isinstance(values, Mapping):
                continue
            reported_worker_count += 1
            for field, value in values.items():
                number = _nonnegative_finite_number(value)
                if number is not None:
                    totals[field] = totals.get(field, 0) + number
    return {
        "source": "omp_worker_reported",
        "status": "reported" if reported_worker_count else "unknown",
        "reported_worker_count": reported_worker_count,
        "unknown_worker_count": worker_count - reported_worker_count,
        "totals": totals if reported_worker_count else None,
    }


def _provenance_reference(root: Path, path: Path) -> dict[str, str | None]:
    try:
        path_value = path.resolve().relative_to(root).as_posix()
    except ValueError:
        path_value = None
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        digest = None
    return {"path": path_value, "sha256": digest}


def summarize_omp_evidence(
    repo_root: str | os.PathLike[str],
    state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a redacted, read-only OMP evidence panel summary.

    Provenance is evidence rather than a gate. Missing dispatch artifacts remain
    ``unknown`` and malformed artifacts make the panel ``blocked``; neither
    condition is represented as a passing result.
    """
    root = Path(repo_root).resolve()
    workflow_state = (
        state
        if isinstance(state, Mapping)
        else _read_workflow_state_for_evidence(root) or {}
    )
    current_correlation: dict[str, str | int | None] = {
        "workflow_id": workflow_state.get("id")
        if isinstance(workflow_state.get("id"), str)
        else None,
        "phase": workflow_state.get("currentPhase")
        if isinstance(workflow_state.get("currentPhase"), str)
        else None,
        "attempt": None,
    }
    current_phase = current_correlation["phase"]
    phase_info = (
        workflow_state.get("phases", {}).get(current_phase)
        if isinstance(workflow_state.get("phases"), Mapping)
        and isinstance(current_phase, str)
        else None
    )
    if isinstance(phase_info, Mapping):
        retries = phase_info.get("retries")
        if isinstance(retries, int) and not isinstance(retries, bool) and retries >= 0:
            current_correlation["attempt"] = retries + 1

    summary: dict[str, Any] = {
        "status": "unknown",
        "workflow": current_correlation,
        "dispatches": [],
        "usage": {
            "phase_primary_estimated": _phase_primary_estimated_usage(workflow_state),
            "omp_worker_reported": {
                "source": "omp_worker_reported",
                "status": "unknown",
                "reported_worker_count": 0,
                "unknown_worker_count": 0,
                "totals": None,
            },
        },
        "diagnostics": [],
    }
    dispatch_dir = root / ".workflow" / "artifacts" / "dispatch"
    if not dispatch_dir.is_dir():
        summary["diagnostics"].append("dispatch_artifacts_missing")
        return summary

    paths = sorted(dispatch_dir.glob("*.json"))
    if not paths:
        summary["diagnostics"].append("omp_provenance_missing")
        return summary

    dispatches: list[dict[str, Any]] = []
    blocked = False
    for path in paths:
        reference = _provenance_reference(root, path)
        try:
            payload = _load_record(path)
        except ValueError:
            blocked = True
            dispatches.append(
                {
                    "status": "blocked",
                    "provenance": reference,
                    "diagnostic": "invalid_provenance",
                }
            )
            continue
        if not _validate_evidence_record(payload):
            blocked = True
            dispatches.append(
                {
                    "status": "blocked",
                    "provenance": reference,
                    "diagnostic": "redacted_record_invalid",
                }
            )
            continue
        agents = [
            _evidence_agent(agent)
            for agent in payload["agents"]
            if isinstance(agent, Mapping)
        ]
        strict_evidence_invalid = any(
            (
                agent["schema"]["mode"] == "strict"
                and agent["schema"]["strict_status"] != "valid"
            )
            or agent["patch_scope_status"] == "invalid"
            for agent in agents
        )
        if strict_evidence_invalid:
            blocked = True
        checkpoint = payload.get("checkpoint")
        checkpoint_map = checkpoint if isinstance(checkpoint, Mapping) else {}
        dispatches.append(
            {
                "status": _evidence_status(
                    payload.get("status"),
                    allowed=_EVIDENCE_DISPATCH_STATUSES,
                ),
                "evidence_status": (
                    "blocked" if strict_evidence_invalid else "available"
                ),
                "dispatch_run_id": str(payload["run_id"]),
                "correlation": _evidence_correlation(payload),
                "checkpoint": {
                    "state": (
                        checkpoint_map.get("state")
                        if isinstance(checkpoint_map.get("state"), str)
                        else "unknown"
                    ),
                    "resumable": _known_bool(checkpoint_map.get("resumable")),
                    "session_persisted": _known_bool(
                        checkpoint_map.get("session_persisted")
                    ),
                },
                "lineage": _evidence_lineage(
                    {
                        "parent_run_id": payload.get("parent_run_id"),
                        "parent_task_id": payload.get("parent_task_id"),
                    }
                ),
                "cancellation": _safe_cancellation_audit(
                    payload.get("cancellation"),
                    status=str(payload.get("status") or ""),
                ),
                "partial_result": (
                    "partial"
                    if payload.get("partial_result") is True
                    else "not_partial"
                    if payload.get("partial_result") is False
                    else "unknown"
                ),
                "provenance": reference,
                "agents": agents,
            }
        )

    summary["dispatches"] = dispatches
    summary["usage"]["omp_worker_reported"] = _omp_worker_reported_usage(dispatches)
    summary["status"] = "blocked" if blocked else "available"
    return summary
