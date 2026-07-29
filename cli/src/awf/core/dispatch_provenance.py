from __future__ import annotations

import hashlib
import json
import os
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


def _safe_schema_validation(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _json_safe(value[key])
            for key in ("valid", "status", "mode")
            if key in value
        }
    if isinstance(value, (str, bool)) or value is None:
        return value
    return str(value)


def _safe_steering_evidence(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    inspected = value.get("inspected_completed")
    raw_wait_calls = value.get("wait_calls")
    return {
        "reported": value.get("reported") is True,
        "wait_calls": (
            raw_wait_calls
            if isinstance(raw_wait_calls, int)
            and not isinstance(raw_wait_calls, bool)
            and raw_wait_calls >= 0
            else 0
        ),
        "inspected_completed": (
            [str(name) for name in inspected if isinstance(name, str)]
            if isinstance(inspected, list)
            else []
        ),
        "message_sent": value.get("message_sent") is True,
        "message_target": _text(value, "message_target"),
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
    runtime_usage = worker_usage if has_worker_usage else metadata.get("usage")
    lineage = {
        "parent_run_id": _text(metadata, "parent_run_id"),
        "parent_task_id": parent_task_id,
        "original_task_id": _text(metadata, "original_task_id"),
        "successor_task_id": successor_task_id,
        "followup_kind": _text(metadata, "followup_kind", "delivery"),
    }
    status = _status(agent, metadata)
    declared_status = _text(metadata, "status")
    normalized_declared_status = _declared_status(declared_status)
    return {
        "role": str(getattr(agent, "role", "")),
        "provider": str(getattr(agent, "provider_name", "")),
        "status": status,
        "returncode": int(getattr(agent, "returncode", 1)),
        "timed_out": bool(getattr(agent, "timed_out", False)),
        "parse_error": bool(getattr(agent, "parse_error", False)),
        "elapsed_sec": round(float(getattr(agent, "elapsed_sec", 0.0)), 3),
        "conclusion": str(getattr(agent, "conclusion", "")),
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
        "lineage": lineage,
        "followup_evidence": _json_safe(metadata.get("followup_evidence")),
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
            "cost": _json_safe(metadata.get("cost")),
            "usage": _json_safe(runtime_usage),
            "coordinator_usage": _json_safe(metadata.get("usage")),
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
        "agents": records,
    }

    target_dir = workflow_dir / "artifacts" / "dispatch"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{run_id}.json"
    _atomic_write(target, payload)
    return target


def _load_record(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid OMP provenance file {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("backend") != "omp":
        raise ValueError(f"not an OMP provenance record: {path}")
    return payload


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
