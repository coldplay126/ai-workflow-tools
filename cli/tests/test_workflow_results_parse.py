"""Unit tests for workflow_results._parse_result_json.

Targets the §1.3 stream-json parsing path documented in
docs/gaps/2026-05-13-blip-gem-cycle-operational-issues.md — claude code
streams events line-by-line and ends with a `{"type": "result"}` envelope;
the worker's structured payload lives inside that envelope's `result` field.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf.core.workflow_results import _parse_result_json


def _write(tmp_path: Path, content: str) -> str:
    path = tmp_path / "result.json"
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_single_json_document(tmp_path: Path) -> None:
    payload = {"status": "completed", "result": {"verdict": "PASS"}}
    parsed = _parse_result_json(_write(tmp_path, json.dumps(payload)))
    assert parsed == payload


def test_json_embedded_in_prose(tmp_path: Path) -> None:
    body = (
        "Some preamble text from the model.\n"
        '{"status": "completed", "result": {"verdict": "PASS"}}\n'
        "trailing thought"
    )
    parsed = _parse_result_json(_write(tmp_path, body))
    assert parsed["status"] == "completed"
    assert parsed["result"]["verdict"] == "PASS"


def test_stream_json_with_json_result(tmp_path: Path) -> None:
    """claude code stream-json: final result event carries the worker payload."""
    inner = json.dumps({"status": "completed", "result": {"verdict": "PASS", "findings": []}})
    events = [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"text": "..."}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "thinking"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": inner, "duration_ms": 1234},
    ]
    raw = "\n".join(json.dumps(e) for e in events)
    parsed = _parse_result_json(_write(tmp_path, raw))
    assert parsed["status"] == "completed"
    assert parsed["result"]["verdict"] == "PASS"


def test_stream_json_picks_last_result_event(tmp_path: Path) -> None:
    """Multiple result events (rare but defensive): last one wins."""
    first = json.dumps({"status": "completed", "result": {"verdict": "FAIL"}})
    final = json.dumps({"status": "completed", "result": {"verdict": "PASS"}})
    events = [
        {"type": "result", "is_error": False, "result": first},
        {"type": "system", "subtype": "noise"},
        {"type": "result", "is_error": False, "result": final},
    ]
    raw = "\n".join(json.dumps(e) for e in events)
    parsed = _parse_result_json(_write(tmp_path, raw))
    assert parsed["result"]["verdict"] == "PASS"


def test_stream_json_result_with_surrounding_prose(tmp_path: Path) -> None:
    """Worker may emit JSON wrapped in markdown fences inside the result field."""
    inner = (
        "Here is the verdict:\n```json\n"
        + json.dumps({"status": "completed", "result": {"verdict": "PASS"}})
        + "\n```\n"
    )
    events = [
        {"type": "result", "is_error": False, "result": inner},
    ]
    raw = "\n".join(json.dumps(e) for e in events)
    parsed = _parse_result_json(_write(tmp_path, raw))
    assert parsed["status"] == "completed"


def test_stream_json_partial_lines_are_ignored(tmp_path: Path) -> None:
    """Malformed lines mixed with valid stream-json events must not crash."""
    inner = json.dumps({"status": "completed", "result": {"verdict": "PASS"}})
    raw = "\n".join([
        "not-json-at-all",
        json.dumps({"type": "stream_event", "event": {"type": "content_block_delta"}}),
        "{partial broken",
        json.dumps({"type": "result", "is_error": False, "result": inner}),
    ])
    parsed = _parse_result_json(_write(tmp_path, raw))
    assert parsed["status"] == "completed"


def test_unparseable_raises(tmp_path: Path) -> None:
    with pytest.raises((ValueError, json.JSONDecodeError)):
        _parse_result_json(_write(tmp_path, "no json here at all"))
