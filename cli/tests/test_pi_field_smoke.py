from __future__ import annotations

import json
from datetime import datetime, timezone

from awf.core.pi_field_smoke import (
    ARTIFACT_SCHEMA,
    pi_field_smoke_latest_path,
    read_pi_field_smoke_summary,
    write_pi_field_smoke_result,
)


def test_write_and_read_pi_field_smoke_summary(tmp_path) -> None:
    payload = {
        "schema": "awf_pi_field_smoke_v1",
        "ok": False,
        "reason": "provider_quota_exhausted",
        "pi_command_source": "PATH",
        "pi_command": "/bin/pi",
        "billing_context": "anthropic_extra_usage",
        "next_action": "Enable Extra Usage.",
        "diagnosis": {
            "kind": "provider_quota_exhausted",
            "summary": "Quota exhausted.",
            "next_action": "Enable Extra Usage.",
        },
    }

    path = write_pi_field_smoke_result(
        tmp_path,
        payload,
        recorded_at="2026-05-10T00:00:00+00:00",
    )

    assert path == pi_field_smoke_latest_path(tmp_path)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope["schema"] == ARTIFACT_SCHEMA
    summary = read_pi_field_smoke_summary(
        tmp_path,
        now=datetime(2026, 5, 10, 12, tzinfo=timezone.utc),
    )
    assert summary["status"] == "found"
    assert summary["recorded_at"] == "2026-05-10T00:00:00+00:00"
    assert summary["stale"] is False
    assert summary["age_hours"] == 12.0
    assert summary["ok"] is False
    assert summary["reason"] == "provider_quota_exhausted"
    assert summary["diagnosis_kind"] == "provider_quota_exhausted"
    assert summary["billing_context"] == "anthropic_extra_usage"
    assert summary["next_action"] == "Enable Extra Usage."


def test_read_pi_field_smoke_summary_handles_missing_and_invalid(tmp_path) -> None:
    missing = read_pi_field_smoke_summary(tmp_path)
    assert missing["status"] == "missing"

    path = pi_field_smoke_latest_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not-json\n", encoding="utf-8")

    invalid = read_pi_field_smoke_summary(tmp_path)
    assert invalid["status"] == "invalid"
    assert "could not be parsed" in invalid["detail"]


def test_read_pi_field_smoke_summary_marks_old_results_stale(tmp_path) -> None:
    write_pi_field_smoke_result(
        tmp_path,
        {
            "schema": "awf_pi_field_smoke_v1",
            "ok": True,
            "reason": "dispatch_ok",
        },
        recorded_at="2026-05-08T00:00:00+00:00",
    )

    summary = read_pi_field_smoke_summary(
        tmp_path,
        now=datetime(2026, 5, 10, 12, tzinfo=timezone.utc),
    )

    assert summary["stale"] is True
    assert summary["stale_reason"] == "older_than_threshold"
    assert summary["age_hours"] == 60.0
