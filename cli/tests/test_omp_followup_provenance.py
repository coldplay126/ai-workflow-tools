from __future__ import annotations

import hashlib
import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from awf.commands import agents as agents_command
from awf.core.agent_runner import AgentResult
from awf.core.dispatch_provenance import (
    lookup_omp_provenance,
    write_omp_dispatch_provenance,
)
from awf.runners.omp import OmpRunnerConfig


def _agent(*, session_persisted: bool = True) -> AgentResult:
    return AgentResult(
        provider_name="omp:fixture",
        role="reviewer",
        stdout="sensitive response body",
        stderr="",
        returncode=0,
        elapsed_sec=0.5,
        metadata={
            "backend": "omp",
            "coordination_surface": "native",
            "coordinator_session_id": "session-1",
            "session_persisted": session_persisted,
            "task_id": "task-1",
            "agent_uri": "agent://task-1",
            "history_uri": "history://task-1",
            "status": "completed",
            "provider": "fixture",
            "model": "fixture-model",
            "usage": {"total_tokens": 3},
            "schema_validation": {"valid": True},
            "prompt": "secret prompt body",
        },
    )


def _write_parent(tmp_path: Path, *, session_persisted: bool = True) -> Path:
    (tmp_path / ".workflow").mkdir(exist_ok=True)
    path = write_omp_dispatch_provenance(
        tmp_path,
        strategy="parallel",
        mode="cross",
        agents=[_agent(session_persisted=session_persisted)],
        elapsed_sec=0.5,
    )
    assert path is not None
    return path

def _write_native_checkpoint(
    tmp_path: Path,
    *,
    task_id: str = "Awf000Implementer",
    session_persisted: bool = True,
) -> Path:
    target = tmp_path / ".workflow" / "artifacts" / "dispatch" / "omp-native-batch-1.json"
    fingerprint = "a" * 64
    descriptor_sha256 = "b" * 64
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "omp_native_batch",
                "batch_fingerprint": fingerprint,
                "descriptor_hashes": [descriptor_sha256],
                "worker_names": [task_id],
                "state": "completed",
                "attempt": 1,
                "session_persistence_requested": session_persisted,
                "session_persisted": session_persisted,
                "resumable": False,
                "coordinator_session_id": (
                    "session-native-1" if session_persisted else None
                ),
                "workers": [
                    {
                        "index": 0,
                        "name": task_id,
                        "descriptor_sha256": descriptor_sha256,
                        "task_id": task_id,
                        "agent_uri": f"agent://{task_id}",
                        "history_uri": f"history://{task_id}",
                        "status": "completed",
                    }
                ],
                "steering_evidence": {
                    "reported": False,
                    "wait_calls": 0,
                    "inspected_completed": [],
                    "message_sent": False,
                    "message_target": None,
                    "message_kind": None,
                },
            }
        ),
        encoding="utf-8",
    )
    return target






def _direct_evidence(task_id: str = "task-1") -> str:
    events = [
        {
            "type": "tool_execution_start",
            "toolName": "hub",
            "toolCallId": "hub-1",
            "args": {"op": "send", "to": task_id, "message": "secret"},
        },
        {
            "type": "tool_execution_end",
            "toolName": "hub",
            "toolCallId": "hub-1",
            "isError": False,
            "result": {
                "details": {
                    "receipts": [{"to": task_id, "outcome": "delivered"}]
                }
            },
        },
    ]
    return "\n".join(json.dumps(event) for event in events)


def _successor_evidence(
    task_id: str = "task-1",
    successor_id: str = "task-successor",
) -> str:
    events = [
        {
            "type": "tool_execution_start",
            "toolName": "hub",
            "toolCallId": "hub-1",
            "args": {"op": "send", "to": task_id, "message": "secret"},
        },
        {
            "type": "tool_execution_end",
            "toolName": "hub",
            "toolCallId": "hub-1",
            "isError": True,
            "result": {
                "isError": True,
                "details": {
                    "receipts": [
                        {
                            "to": task_id,
                            "outcome": "failed",
                            "error": f'Unknown agent "{task_id}"',
                        }
                    ]
                },
            },
        },
        {
            "type": "tool_execution_start",
            "toolName": "read",
            "toolCallId": "read-1",
            "args": {"path": f"history://{task_id}"},
        },
        {
            "type": "tool_execution_end",
            "toolName": "read",
            "toolCallId": "read-1",
            "isError": False,
            "result": {"content": [{"type": "text", "text": "secret history"}]},
        },
        {
            "type": "tool_execution_start",
            "toolName": "task",
            "toolCallId": "task-1",
            "args": {"tasks": [{"name": "Successor", "task": "secret prompt"}]},
        },
        {
            "type": "tool_execution_end",
            "toolName": "task",
            "toolCallId": "task-1",
            "isError": False,
            "result": {
                "details": {
                    "progress": [
                        {
                            "index": 0,
                            "id": successor_id,
                            "status": "completed",
                        }
                    ]
                }
            },
        },
    ]
    return "\n".join(json.dumps(event) for event in events)


def test_v2_provenance_persists_handles_lineage_and_only_hashes_bodies(tmp_path: Path):
    path = _write_parent(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    record = payload["agents"][0]

    assert payload["schema_version"] == 2
    assert payload["coordinator_session_id"] == "session-1"
    assert record["task_id"] == "task-1"
    assert record["agent_uri"] == "agent://task-1"
    assert record["history_uri"] == "history://task-1"
    assert record["status"] == "completed"
    assert record["schema_validation"] == {"valid": True}
    assert record["output_sha256"] == hashlib.sha256(
        b"sensitive response body"
    ).hexdigest()
    encoded = path.read_text(encoding="utf-8")
    assert "sensitive response body" not in encoded
    assert "secret prompt body" not in encoded


def test_lookup_omp_provenance_reports_not_found_and_duplicate_run_id(tmp_path: Path):
    path = _write_parent(tmp_path)
    source = path.read_bytes()
    payload = json.loads(source)
    resolved_path, resolved = lookup_omp_provenance(tmp_path, path.name)
    assert resolved_path == path.resolve()
    assert resolved == payload
    assert path.read_bytes() == source
    with pytest.raises(FileNotFoundError, match="not found"):
        lookup_omp_provenance(tmp_path, "missing-run")

    duplicate = path.with_name("duplicate.json")
    duplicate.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="ambiguous"):
        lookup_omp_provenance(tmp_path, payload["run_id"])


def test_lookup_omp_provenance_normalizes_native_checkpoint(tmp_path: Path):
    checkpoint = _write_native_checkpoint(tmp_path)
    source = checkpoint.read_bytes()

    path, payload = lookup_omp_provenance(tmp_path, checkpoint)

    assert path == checkpoint.resolve()
    assert payload["schema_version"] == 2
    assert payload["backend"] == "omp"
    assert payload["source_kind"] == "omp_native_batch"
    assert payload["run_id"] == checkpoint.stem
    assert payload["coordinator_session_id"] == "session-native-1"
    assert payload["agents"] == [
        {
            "worker_index": 0,
            "name": "Awf000Implementer",
            "task_id": "Awf000Implementer",
            "agent_uri": "agent://Awf000Implementer",
            "history_uri": "history://Awf000Implementer",
            "status": "completed",
            "session_persisted": True,
            "coordinator_session_id": "session-native-1",
        }
    ]
    assert checkpoint.read_bytes() == source




@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("batch_fingerprint", "missing batch_fingerprint"),
        ("coordinator_session_id", "missing coordinator_session_id"),
        ("workers", "missing workers"),
    ],
)
def test_lookup_omp_provenance_rejects_malformed_native_checkpoint(
    tmp_path: Path, field: str, message: str
):
    checkpoint = _write_native_checkpoint(tmp_path)
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload.pop(field)
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        lookup_omp_provenance(tmp_path, checkpoint)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("version", True, "unsupported version"),
        ("version", 1.0, "unsupported version"),
        ("batch_fingerprint", "", "missing batch_fingerprint"),
        ("batch_fingerprint", True, "missing batch_fingerprint"),
        ("batch_fingerprint", {}, "missing batch_fingerprint"),
        ("coordinator_session_id", "", "missing coordinator_session_id"),
        ("coordinator_session_id", True, "missing coordinator_session_id"),
        ("coordinator_session_id", {}, "missing coordinator_session_id"),
    ],
)
def test_lookup_omp_provenance_rejects_invalid_native_checkpoint_scalars(
    tmp_path: Path, field: str, value: object, message: str
):
    checkpoint = _write_native_checkpoint(tmp_path)
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload[field] = value
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError, match=rf"invalid OMP native checkpoint .*: {message}"
    ):
        lookup_omp_provenance(tmp_path, checkpoint)


@pytest.mark.parametrize(
    ("field", "value", "remove", "message"),
    [
        ("index", None, True, "worker 0 missing index"),
        ("index", 1, False, "worker 0 has invalid index"),
        ("task_id", None, True, "worker 0 has no task ID"),
        ("task_id", None, False, "worker 0 has invalid task ID"),
        ("task_id", True, False, "worker 0 has invalid task ID"),
        ("task_id", {}, False, "worker 0 has invalid task ID"),
    ],
)
def test_lookup_omp_provenance_rejects_invalid_native_worker_fields(
    tmp_path: Path, field: str, value: object, remove: bool, message: str
):
    checkpoint = _write_native_checkpoint(tmp_path)
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    worker = payload["workers"][0]
    if remove:
        worker.pop(field)
    else:
        worker[field] = value
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError, match=rf"invalid OMP native checkpoint .*: {message}"
    ):
        lookup_omp_provenance(tmp_path, checkpoint)


def test_lookup_omp_provenance_rejects_duplicate_native_worker_task_ids(
    tmp_path: Path,
):
    checkpoint = _write_native_checkpoint(tmp_path)
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    duplicate = {
        **payload["workers"][0],
        "index": 1,
        "name": "Awf001Implementer",
        "descriptor_sha256": "c" * 64,
    }
    payload["workers"].append(duplicate)
    payload["descriptor_hashes"].append(duplicate["descriptor_sha256"])
    payload["worker_names"].append(duplicate["name"])
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=r"invalid OMP native checkpoint .*: duplicate task ID",
    ):
        lookup_omp_provenance(tmp_path, checkpoint)


def test_lookup_omp_provenance_allows_null_interrupted_worker_status(tmp_path: Path):
    checkpoint = _write_native_checkpoint(tmp_path)
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload.update(
        {
            "state": "interrupted",
            "session_persisted": True,
            "resumable": True,
            "coordinator_session_id": "session-native-1",
            "steering_evidence": {},
        }
    )
    payload["workers"][0]["status"] = None
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    _, normalized = lookup_omp_provenance(tmp_path, checkpoint)

    assert normalized["agents"][0]["status"] == ""






@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", "", "worker 0 has invalid name"),
        ("name", " ", "worker 0 has invalid name"),
        ("name", True, "worker 0 has invalid name"),
        ("name", {}, "worker 0 has invalid name"),
        ("agent_uri", True, "worker 0 has invalid agent_uri"),
        ("agent_uri", {}, "worker 0 has invalid agent_uri"),
        ("agent_uri", "history://task-1", "worker 0 has invalid agent_uri"),
        ("agent_uri", None, "completed worker 0 missing handles"),
        ("history_uri", True, "worker 0 has invalid history_uri"),
        ("history_uri", {}, "worker 0 has invalid history_uri"),
        ("history_uri", "agent://task-1", "worker 0 has invalid history_uri"),
        ("history_uri", None, "completed worker 0 missing handles"),
        ("status", "", "worker 0 has invalid status"),
        ("status", " ", "worker 0 has invalid status"),
        ("status", " completed ", "worker 0 has invalid status"),
        ("status", True, "worker 0 has invalid status"),
        ("status", {}, "worker 0 has invalid status"),
        ("status", None, "completed worker 0 is not terminal"),
    ],
)
def test_lookup_omp_provenance_rejects_invalid_native_worker_metadata(
    tmp_path: Path, field: str, value: object, message: str
):
    checkpoint = _write_native_checkpoint(tmp_path)
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload["workers"][0][field] = value
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError, match=rf"invalid OMP native checkpoint .*: {message}"
    ):
        lookup_omp_provenance(tmp_path, checkpoint)


@pytest.mark.parametrize(
    "status",
    ["completed", "success", "ok", "failed", "error", "cancelled", "canceled"],
)
def test_lookup_omp_provenance_preserves_terminal_native_worker_status(
    tmp_path: Path, status: str
):
    checkpoint = _write_native_checkpoint(tmp_path)
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload["workers"][0]["status"] = status
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    _, normalized = lookup_omp_provenance(tmp_path, checkpoint)

    assert normalized["agents"][0]["status"] == status


@pytest.mark.parametrize(
    "task_id",
    [
        "Awf 000Implementer",
        "Awf\n000Implementer",
        "Awf\t000Implementer",
        "Awf\x1f000Implementer",
    ],
)
def test_lookup_omp_provenance_rejects_unsafe_native_worker_task_ids(
    tmp_path: Path, task_id: str
):
    checkpoint = _write_native_checkpoint(tmp_path)
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    worker = payload["workers"][0]
    worker["task_id"] = task_id
    worker["agent_uri"] = None
    worker["history_uri"] = None
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=r"invalid OMP native checkpoint .*: worker 0 has invalid task ID",
    ):
        lookup_omp_provenance(tmp_path, checkpoint)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "agent_uri",
            "agent://Awf000Implementer\nsuffix",
            "worker 0 has invalid agent_uri",
        ),
        (
            "agent_uri",
            "agent://Awf000Implementer suffix",
            "worker 0 has invalid agent_uri",
        ),
        (
            "history_uri",
            "history://Awf000Implementer\nsuffix",
            "worker 0 has invalid history_uri",
        ),
        (
            "history_uri",
            "history://Awf000Implementer suffix",
            "worker 0 has invalid history_uri",
        ),
    ],
)
def test_lookup_omp_provenance_rejects_unsafe_native_worker_handles(
    tmp_path: Path, field: str, value: str, message: str
):
    checkpoint = _write_native_checkpoint(tmp_path)
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload["workers"][0][field] = value
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError, match=rf"invalid OMP native checkpoint .*: {message}"
    ):
        lookup_omp_provenance(tmp_path, checkpoint)


@pytest.mark.parametrize(
    ("task_id", "agent_uri", "history_uri"),
    [
        (
            "Awf000Implementer",
            "agent://Awf000Implementer",
            "history://Awf000Implementer",
        ),
        ("task-1", "agent://opaque-agent", "history://opaque-history"),
    ],
)
def test_lookup_omp_provenance_normalizes_valid_native_worker_handles(
    tmp_path: Path,
    task_id: str,
    agent_uri: str | None,
    history_uri: str | None,
):
    checkpoint = _write_native_checkpoint(tmp_path)
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    worker = payload["workers"][0]
    worker["name"] = task_id
    payload["worker_names"][0] = task_id
    worker["task_id"] = task_id
    worker["agent_uri"] = agent_uri
    worker["history_uri"] = history_uri
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    _, normalized = lookup_omp_provenance(tmp_path, checkpoint)
    record = normalized["agents"][0]

    assert record["task_id"] == task_id
    assert record["agent_uri"] == (agent_uri or "")
    assert record["history_uri"] == (history_uri or "")


@pytest.mark.parametrize(
    ("state", "updates"),
    [
        (
            "prepared",
            {
                "session_persisted": False,
                "resumable": False,
                "coordinator_session_id": None,
                "steering_evidence": None,
            },
        ),
        (
            "resuming",
            {
                "session_persisted": True,
                "resumable": True,
                "coordinator_session_id": "session-native-1",
                "steering_evidence": None,
            },
        ),
        ("completed", {}),
        (
            "interrupted",
            {
                "session_persisted": True,
                "resumable": True,
                "coordinator_session_id": "session-native-1",
                "steering_evidence": {},
            },
        ),
        (
            "ambiguous",
            {
                "session_persisted": True,
                "resumable": False,
                "coordinator_session_id": "session-native-1",
                "steering_evidence": {},
            },
        ),
    ],
)
def test_lookup_omp_provenance_accepts_valid_native_checkpoint_states(
    tmp_path: Path, state: str, updates: dict[str, object]
):
    checkpoint = _write_native_checkpoint(tmp_path)
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload["state"] = state
    payload.update(updates)
    if state == "prepared":
        payload["workers"][0].update(
            {
                "task_id": None,
                "agent_uri": None,
                "history_uri": None,
                "status": None,
            }
        )
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    _, normalized = lookup_omp_provenance(tmp_path, checkpoint)

    assert normalized["status"] == state
    if state == "prepared":
        record = normalized["agents"][0]
        assert record["task_id"] == ""
        assert record["agent_uri"] == ""
        assert record["history_uri"] == ""
        assert record["status"] == ""
        with pytest.raises(ValueError, match="requires a persisted coordinator session"):
            agents_command._require_actionable_target(normalized, record)


@pytest.mark.parametrize(
    ("state", "steering_evidence"),
    [("resuming", None), ("interrupted", {})],
)
def test_lookup_omp_provenance_rejects_resumable_worker_without_task_id(
    tmp_path: Path, state: str, steering_evidence: object
):
    checkpoint = _write_native_checkpoint(tmp_path)
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload.update(
        {
            "state": state,
            "session_persisted": True,
            "resumable": True,
            "coordinator_session_id": "session-native-1",
            "steering_evidence": steering_evidence,
        }
    )
    payload["workers"][0].update(
        {"task_id": None, "agent_uri": None, "history_uri": None}
    )
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=r"invalid OMP native checkpoint .*: resumable worker 0 missing task ID",
    ):
        lookup_omp_provenance(tmp_path, checkpoint)


@pytest.mark.parametrize("state", [None, True, {}, "", "failed"])
def test_lookup_omp_provenance_rejects_invalid_native_checkpoint_state(
    tmp_path: Path, state: object
):
    checkpoint = _write_native_checkpoint(tmp_path)
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload["state"] = state
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=r"invalid OMP native checkpoint .*: invalid state",
    ):
        lookup_omp_provenance(tmp_path, checkpoint)


def test_lookup_omp_provenance_rejects_conflicting_native_discriminator(
    tmp_path: Path,
):
    checkpoint = _write_native_checkpoint(tmp_path)
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload.update({"backend": "omp", "schema_version": 2})
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=r"invalid OMP native checkpoint .*: conflicting discriminator",
    ):
        lookup_omp_provenance(tmp_path, checkpoint)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("descriptor_hashes", "missing descriptor_hashes"),
        ("worker_names", "missing worker_names"),
        ("attempt", "missing attempt"),
        (
            "session_persistence_requested",
            "missing session_persistence_requested",
        ),
        ("session_persisted", "missing session_persisted"),
        ("resumable", "missing resumable"),
        ("steering_evidence", "missing steering_evidence"),
        ("worker_descriptor_sha256", "worker 0 missing descriptor_sha256"),
    ],
)
def test_lookup_omp_provenance_rejects_missing_native_checkpoint_identity_fields(
    tmp_path: Path, field: str, message: str
):
    checkpoint = _write_native_checkpoint(tmp_path)
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    if field == "worker_descriptor_sha256":
        payload["workers"][0].pop("descriptor_sha256")
    else:
        payload.pop(field)
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError, match=rf"invalid OMP native checkpoint .*: {message}"
    ):
        lookup_omp_provenance(tmp_path, checkpoint)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("batch_fingerprint", "A" * 64, "invalid batch_fingerprint"),
        ("descriptor_hashes", ["g" * 64], "invalid descriptor_hashes"),
        (
            "worker_descriptor_sha256",
            "c" * 63,
            "worker 0 has invalid descriptor_sha256",
        ),
    ],
)
def test_lookup_omp_provenance_rejects_invalid_native_checkpoint_sha256_fields(
    tmp_path: Path, field: str, value: object, message: str
):
    checkpoint = _write_native_checkpoint(tmp_path)
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    if field == "worker_descriptor_sha256":
        payload["workers"][0]["descriptor_sha256"] = value
    else:
        payload[field] = value
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError, match=rf"invalid OMP native checkpoint .*: {message}"
    ):
        lookup_omp_provenance(tmp_path, checkpoint)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("descriptor_cardinality", "descriptor_hashes cardinality"),
        ("worker_names_ordering", "worker_names ordering"),
        ("worker_descriptor_mismatch", "worker 0 descriptor mismatch"),
    ],
)
def test_lookup_omp_provenance_rejects_incoherent_native_checkpoint_identity(
    tmp_path: Path, case: str, message: str
):
    checkpoint = _write_native_checkpoint(tmp_path)
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    if case == "descriptor_cardinality":
        payload["descriptor_hashes"] = []
    elif case == "worker_names_ordering":
        second = {
            **payload["workers"][0],
            "index": 1,
            "name": "Awf001Implementer",
            "descriptor_sha256": "c" * 64,
            "task_id": "Awf001Implementer",
            "agent_uri": "agent://Awf001Implementer",
            "history_uri": "history://Awf001Implementer",
        }
        payload["workers"].append(second)
        payload["descriptor_hashes"].append(second["descriptor_sha256"])
        payload["worker_names"].append(second["name"])
        payload["worker_names"].reverse()
    else:
        payload["workers"][0]["descriptor_sha256"] = "c" * 64
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError, match=rf"invalid OMP native checkpoint .*: {message}"
    ):
        lookup_omp_provenance(tmp_path, checkpoint)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"resumable": True}, "completed checkpoint cannot be resumable"),
        (
            {"session_persistence_requested": False},
            "session persistence contradiction",
        ),
        ({"session_persisted": False}, "coordinator session contradiction"),
        (
            {
                "state": "interrupted",
                "session_persistence_requested": True,
                "session_persisted": False,
                "resumable": True,
                "coordinator_session_id": None,
            },
            "resumable checkpoint requires persisted session",
        ),
    ],
)
def test_lookup_omp_provenance_rejects_contradictory_native_checkpoint_session(
    tmp_path: Path, updates: dict[str, object], message: str
):
    checkpoint = _write_native_checkpoint(tmp_path)
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload.update(updates)
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError, match=rf"invalid OMP native checkpoint .*: {message}"
    ):
        lookup_omp_provenance(tmp_path, checkpoint)

def test_followup_task_id_rejects_ambiguous_exact_provenance(tmp_path: Path):
    _write_parent(tmp_path)
    _write_parent(tmp_path)

    with pytest.raises(ValueError, match="ambiguous"):
        agents_command._find_followup_target(
            tmp_path,
            run_reference=None,
            role=None,
            task_id="task-1",
        )



def test_find_followup_target_matches_native_worker_by_role(tmp_path: Path):
    checkpoint = _write_native_checkpoint(tmp_path)

    path, payload, record = agents_command._find_followup_target(
        tmp_path,
        run_reference=str(checkpoint),
        role="implementer",
        task_id=None,
    )

    assert path == checkpoint.resolve()
    assert payload["source_kind"] == "omp_native_batch"
    assert payload["run_id"] == checkpoint.stem
    assert record["task_id"] == "Awf000Implementer"


def test_find_followup_target_matches_native_worker_by_task_id(tmp_path: Path):
    checkpoint = _write_native_checkpoint(tmp_path)

    path, payload, record = agents_command._find_followup_target(
        tmp_path,
        run_reference=None,
        role=None,
        task_id="Awf000Implementer",
    )

    assert path == checkpoint.resolve()
    assert payload["source_kind"] == "omp_native_batch"
    assert payload["run_id"] == checkpoint.stem
    assert record["task_id"] == "Awf000Implementer"


def test_find_followup_target_rejects_wrong_native_role(tmp_path: Path):
    checkpoint = _write_native_checkpoint(tmp_path)

    with pytest.raises(FileNotFoundError) as error:
        agents_command._find_followup_target(
            tmp_path,
            run_reference=str(checkpoint),
            role="reviewer",
            task_id=None,
        )

    assert str(error.value) == (
        "role 'reviewer' not found in OMP provenance 'omp-native-batch-1'"
    )


def test_followup_task_id_rejects_duplicate_native_checkpoint(tmp_path: Path):
    first = _write_native_checkpoint(tmp_path)
    second = first.with_name("omp-native-batch-2.json")
    second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(ValueError) as error:
        agents_command._find_followup_target(
            tmp_path,
            run_reference=None,
            role=None,
            task_id="Awf000Implementer",
        )

    assert str(error.value) == (
        "OMP task ID is ambiguous across provenance records: Awf000Implementer"
    )


def test_native_checkpoint_requires_persisted_session(tmp_path: Path):
    checkpoint = _write_native_checkpoint(tmp_path, session_persisted=False)
    path, payload, record = agents_command._find_followup_target(
        tmp_path,
        run_reference=str(checkpoint),
        role="implementer",
        task_id=None,
    )

    assert path == checkpoint.resolve()
    assert payload["source_kind"] == "omp_native_batch"
    assert payload["run_id"] == checkpoint.stem
    assert record["task_id"] == "Awf000Implementer"

    with pytest.raises(ValueError) as error:
        agents_command._require_actionable_target(payload, record)

    assert str(error.value) == (
        "OMP follow-up requires a persisted coordinator session; "
        "the selected provenance record is not resumable"
    )


def test_build_omp_resume_command_is_exact():
    config = OmpRunnerConfig(command="omp-bin", extra_args=("--quiet",))
    assert agents_command._build_omp_resume_command(
        "session-1", "@/tmp/follow-up.txt", config
    ) == [
        "omp-bin",
        "--quiet",
        "--mode",
        "json",
        "-r",
        "session-1",
        "-p",
        "@/tmp/follow-up.txt",
    ]


def test_followup_result_preserves_direct_task_metadata(monkeypatch):
    monkeypatch.setattr(
        agents_command,
        "parse_omp_json_stream",
        lambda *_args, **_kwargs: (
            '{"delivery":"direct","status":"completed"}',
            {"provider": "fixture", "session_id": "session-1"},
            1,
            2,
        ),
    )
    monkeypatch.setattr(agents_command, "parse_omp_task_events", lambda _text: [])
    result = agents_command._followup_result(
        completed=subprocess.CompletedProcess(
            ["omp"], 0, stdout=_direct_evidence(), stderr=""
        ),
        elapsed_sec=0.2,
        coordinator_session_id="session-1",
        parent_run_id="run-1",
        parent_task_id="task-1",
        parent_agent_uri="agent://task-1",
        parent_history_uri="history://task-1",
    )
    assert result.returncode == 0
    assert result.metadata["followup_kind"] == "direct"
    assert result.metadata["task_id"] == "task-1"
    assert result.metadata["parent_task_id"] == "task-1"
    assert result.metadata["successor_task_id"] is None


def test_followup_result_uses_event_task_id_for_successor_lineage(monkeypatch):
    monkeypatch.setattr(
        agents_command,
        "parse_omp_json_stream",
        lambda *_args, **_kwargs: (
            '{"delivery":"successor","status":"completed","task_id":"model-fake"}',
            {"provider": "fixture", "session_id": "session-1"},
            1,
            2,
        ),
    )
    result = agents_command._followup_result(
        completed=subprocess.CompletedProcess(
            ["omp"], 0, stdout=_successor_evidence(), stderr=""
        ),
        elapsed_sec=0.2,
        coordinator_session_id="session-1",
        parent_run_id="run-1",
        parent_task_id="task-1",
        parent_agent_uri="agent://task-1",
        parent_history_uri="history://task-1",
    )
    assert result.returncode == 0
    assert result.metadata["followup_kind"] == "successor"
    assert result.metadata["task_id"] == "task-successor"
    assert result.metadata["task_id"] != "model-fake"
    assert result.metadata["successor_task_id"] == "task-successor"
    assert result.metadata["original_task_id"] == "task-1"
    assert result.metadata["agent_uri"] == "agent://task-successor"


def test_followup_command_resumes_session_and_persists_redacted_child(
    tmp_path: Path, monkeypatch, capsys
):
    parent_path = _write_parent(tmp_path)
    parent = json.loads(parent_path.read_text(encoding="utf-8"))
    (tmp_path / ".workflow" / "provider-config.json").write_text(
        json.dumps(
            {
                "dispatch": {
                    "omp": {
                        "command": "repo-omp",
                        "extra_args": ["--repo-flag"],
                        "no_session": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_resume(**kwargs):
        captured.update(kwargs)
        return (
            subprocess.CompletedProcess(
                ["omp-fixture"], 0, stdout=_direct_evidence(), stderr=""
            ),
            0.2,
        )

    monkeypatch.setenv("AWF_OMP_COMMAND", "wrong-env-omp")
    monkeypatch.setattr(agents_command, "_run_omp_resume", fake_resume)
    monkeypatch.setattr(
        agents_command,
        "parse_omp_json_stream",
        lambda *_args, **_kwargs: (
            '{"delivery":"direct","status":"completed"}',
            {"provider": "fixture", "session_id": "session-1"},
            1,
            2,
        ),
    )
    monkeypatch.setattr(agents_command, "parse_omp_task_events", lambda _text: [])
    args = Namespace(
        repo_root=str(tmp_path),
        run=parent["run_id"],
        role="reviewer",
        task_id=None,
        message="sensitive follow-up message",
        message_file=None,
        json=True,
    )
    assert agents_command.run_agents_followup_omp(args) == 0
    assert captured["session_id"] == "session-1"
    assert captured["repo_root"] == tmp_path
    assert "hub send" in str(captured["prompt"])
    assert "task-1" in str(captured["prompt"])
    config = captured["config"]
    assert isinstance(config, OmpRunnerConfig)
    assert config.command == "repo-omp"
    assert config.extra_args == ("--repo-flag",)

    summary = json.loads(capsys.readouterr().out)
    child_path = Path(summary["provenance_path"])
    child = json.loads(child_path.read_text(encoding="utf-8"))
    assert child["parent_run_id"] == parent["run_id"]
    assert child["parent_task_id"] == "task-1"
    assert child["message_sha256"] == hashlib.sha256(
        b"sensitive follow-up message"
    ).hexdigest()
    child_text = child_path.read_text(encoding="utf-8")
    assert "sensitive follow-up message" not in child_text
    assert '{"delivery":"direct"' not in child_text

def test_followup_command_resumes_native_checkpoint_and_persists_v2_child(
    tmp_path: Path, monkeypatch, capsys
):
    checkpoint = _write_native_checkpoint(tmp_path)
    checkpoint_payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    checkpoint_payload["workers"][0].update(
        {
            "name": "Awf000Reviewer",
            "task_id": "Awf000Reviewer",
            "agent_uri": "agent://Awf000Reviewer",
            "history_uri": "history://Awf000Reviewer",
        }
    )
    checkpoint_payload["worker_names"][0] = "Awf000Reviewer"
    implementer_descriptor_hash = hashlib.sha256(
        b"Awf001Implementer descriptor"
    ).hexdigest()
    checkpoint_payload["workers"].append(
        {
            "index": 1,
            "name": "Awf001Implementer",
            "task_id": "Awf001Implementer",
            "agent_uri": "agent://Awf001Implementer",
            "history_uri": "history://Awf001Implementer",
            "status": "completed",
            "descriptor_sha256": implementer_descriptor_hash,
        }
    )
    checkpoint_payload["worker_names"].append("Awf001Implementer")
    checkpoint_payload["descriptor_hashes"].append(implementer_descriptor_hash)
    checkpoint.write_text(json.dumps(checkpoint_payload), encoding="utf-8")

    (tmp_path / ".workflow" / "provider-config.json").write_text(
        json.dumps({"dispatch": {"omp": {"command": "repo-omp"}}}),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_resume(**kwargs):
        captured.update(kwargs)
        return (
            subprocess.CompletedProcess(
                ["omp-fixture"],
                0,
                stdout=_direct_evidence("Awf001Implementer"),
                stderr="",
            ),
            0.2,
        )

    monkeypatch.setattr(agents_command, "_run_omp_resume", fake_resume)
    monkeypatch.setattr(
        agents_command,
        "parse_omp_json_stream",
        lambda *_args, **_kwargs: (
            '{"delivery":"direct","status":"completed"}',
            {"provider": "fixture", "session_id": "session-native-1"},
            1,
            2,
        ),
    )
    monkeypatch.setattr(agents_command, "parse_omp_task_events", lambda _text: [])
    args = Namespace(
        repo_root=str(tmp_path),
        run=str(checkpoint),
        role="implementer",
        task_id=None,
        message="resume native checkpoint worker",
        message_file=None,
        json=True,
    )

    assert agents_command.run_agents_followup_omp(args) == 0
    assert captured["session_id"] == "session-native-1"
    prompt = str(captured["prompt"])
    assert "Awf001Implementer" in prompt
    assert "Awf000Reviewer" not in prompt


    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "completed"
    assert summary["delivery"] == "direct"
    assert summary["task_id"] == "Awf001Implementer"
    assert summary["parent_run_id"] == checkpoint.stem
    assert summary["parent_task_id"] == "Awf001Implementer"
    child_path = Path(summary["provenance_path"])
    child = json.loads(child_path.read_text(encoding="utf-8"))
    assert child["status"] == "completed"
    assert len(child["agents"]) == 1
    child_agent = child["agents"][0]
    assert child_agent["task_id"] == "Awf001Implementer"
    assert child_agent["metadata"]["session_id"] == "session-native-1"
    assert child_agent["status"] == "completed"
    lineage = child_agent["lineage"]
    assert lineage["parent_run_id"] == checkpoint.stem
    assert lineage["parent_task_id"] == "Awf001Implementer"
    assert lineage["original_task_id"] == "Awf001Implementer"
    assert lineage["successor_task_id"] is None
    assert lineage["followup_kind"] == "direct"
    assert child["schema_version"] == 2
    assert child["parent_run_id"] == checkpoint.stem
    assert child["parent_task_id"] == "Awf001Implementer"


def test_followup_command_fails_before_spawn_without_persisted_session(
    tmp_path: Path, monkeypatch, capsys
):
    parent_path = _write_parent(tmp_path, session_persisted=False)
    parent = json.loads(parent_path.read_text(encoding="utf-8"))

    def unexpected_run(**_kwargs):
        raise AssertionError("subprocess must not start")

    monkeypatch.setattr(agents_command, "_run_omp_resume", unexpected_run)
    args = Namespace(
        repo_root=str(tmp_path),
        run=parent["run_id"],
        role="reviewer",
        task_id=None,
        message="follow up",
        message_file=None,
        json=False,
    )
    assert agents_command.run_agents_followup_omp(args) == 1
    assert "persisted coordinator session" in capsys.readouterr().err
