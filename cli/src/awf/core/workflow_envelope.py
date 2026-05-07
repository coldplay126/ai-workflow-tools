from __future__ import annotations

from typing import Any


VALID_WORKER_STATUSES = {"completed", "escaped", "failed"}


def is_enveloped_worker_result(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    status = str(payload.get("status", "") or "").strip()
    if status not in VALID_WORKER_STATUSES:
        return False
    return "result" in payload


def wrap_legacy_worker_result(payload: dict[str, Any], *, phase: str, provider: str) -> dict[str, Any]:
    return {
        "status": "completed",
        "phase": phase,
        "provider": provider,
        "result": payload,
        "escape": None,
        "meta": {
            "format_version": 1,
            "wrapped_legacy_result": True,
        },
    }


def normalize_worker_result(payload: dict[str, Any], *, phase: str, provider: str) -> dict[str, Any]:
    if is_enveloped_worker_result(payload):
        normalized = dict(payload)
        normalized.setdefault("phase", phase)
        normalized.setdefault("provider", provider)
        normalized.setdefault("escape", None)
        meta = normalized.get("meta")
        normalized["meta"] = dict(meta) if isinstance(meta, dict) else {}
        normalized["meta"].setdefault("format_version", 1)
        if not isinstance(normalized.get("result"), dict):
            normalized["result"] = {}
        return normalized
    return wrap_legacy_worker_result(payload, phase=phase, provider=provider)
