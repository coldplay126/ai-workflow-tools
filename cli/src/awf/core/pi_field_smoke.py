from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from awf.core.operational_metrics import operations_root


ARTIFACT_SCHEMA = "awf_pi_field_smoke_latest_v1"
PI_FIELD_SMOKE_DIR = "pi-field-smoke"
LATEST_RESULT = "latest.json"
STALE_AFTER_HOURS = 24


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def pi_field_smoke_latest_path(repo_root: str | Path) -> Path:
    return operations_root(repo_root) / PI_FIELD_SMOKE_DIR / LATEST_RESULT


def write_pi_field_smoke_result(
    repo_root: str | Path,
    payload: dict[str, Any],
    *,
    recorded_at: str | None = None,
) -> Path:
    target = pi_field_smoke_latest_path(repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "schema": ARTIFACT_SCHEMA,
        "recorded_at": recorded_at or _now_iso(),
        "payload": payload,
    }
    tmp = target.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(target)
    return target


def read_pi_field_smoke_summary(
    repo_root: str | Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    path = pi_field_smoke_latest_path(repo_root)
    base: dict[str, Any] = {
        "status": "missing",
        "path": str(path),
    }
    if not path.is_file():
        return base
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            **base,
            "status": "invalid",
            "detail": f"latest Pi field smoke result could not be parsed: {exc}",
        }
    if not isinstance(data, dict):
        return {
            **base,
            "status": "invalid",
            "detail": "latest Pi field smoke result is not an object",
        }
    if data.get("schema") != ARTIFACT_SCHEMA:
        return {
            **base,
            "status": "invalid",
            "schema": data.get("schema"),
            "detail": (
                "unsupported Pi field smoke artifact schema: "
                f"{data.get('schema')!r}"
            ),
        }
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return {
            **base,
            "status": "invalid",
            "schema": data.get("schema"),
            "recorded_at": data.get("recorded_at"),
            "detail": "latest Pi field smoke payload is not an object",
        }

    recorded_at = data.get("recorded_at")
    recorded_dt = _parse_iso_datetime(recorded_at)
    age_hours = None
    stale = True
    stale_reason = "recorded_at_missing_or_invalid"
    if recorded_dt is not None:
        effective_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        age_seconds = max((effective_now - recorded_dt).total_seconds(), 0)
        age_hours = round(age_seconds / 3600, 2)
        stale = age_hours > STALE_AFTER_HOURS
        stale_reason = "older_than_threshold" if stale else "fresh"

    diagnosis = (
        payload.get("diagnosis")
        if isinstance(payload.get("diagnosis"), dict)
        else {}
    )
    return {
        **base,
        "status": "found",
        "schema": data.get("schema"),
        "recorded_at": recorded_at,
        "stale": stale,
        "stale_reason": stale_reason,
        "age_hours": age_hours,
        "stale_after_hours": STALE_AFTER_HOURS,
        "ok": bool(payload.get("ok")),
        "reason": payload.get("reason"),
        "diagnosis_kind": diagnosis.get("kind"),
        "billing_context": (
            payload.get("billing_context") or diagnosis.get("billing_context")
        ),
        "next_action": payload.get("next_action") or diagnosis.get("next_action"),
        "pi_command_source": payload.get("pi_command_source"),
        "pi_command": payload.get("pi_command"),
    }
