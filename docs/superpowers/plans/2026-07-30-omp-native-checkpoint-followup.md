# OMP Native Checkpoint Follow-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow `awf agents followup-omp` to select and resume workers directly from `omp-native-*.json` checkpoints while preserving schema-v2 lineage output.

**Architecture:** Extend the provenance loader with an in-memory native-checkpoint normalization layer. Keep target selection in `commands/agents.py`: schema-v2 records match their stored role, while native records match the deterministic `omp_worker_name(index, role)`. All successful follow-ups continue to write schema-v2 provenance.

**Tech Stack:** Python 3.11+, pytest, AWF `dispatch_provenance`, AWF OMP runner metadata.

---

### Task 1: Normalize native OMP checkpoints

**Files:**
- Modify: `cli/src/awf/core/dispatch_provenance.py:253-293`
- Test: `cli/tests/test_omp_followup_provenance.py`

- [ ] **Step 1: Add the native checkpoint fixture and failing loader test**

Add this helper near `_write_parent`:

```python
def _write_native_checkpoint(
    tmp_path: Path,
    *,
    task_id: str = "Awf000Implementer",
    session_persisted: bool = True,
) -> Path:
    target = tmp_path / ".workflow" / "artifacts" / "dispatch" / "omp-native-batch-1.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "omp_native_batch",
                "batch_fingerprint": "batch-1",
                "coordinator_session_id": "session-native-1",
                "session_persisted": session_persisted,
                "state": "completed",
                "workers": [
                    {
                        "index": 0,
                        "name": task_id,
                        "task_id": task_id,
                        "agent_uri": f"agent://{task_id}",
                        "history_uri": f"history://{task_id}",
                        "status": "completed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return target
```

Add the test:

```python
def test_lookup_omp_provenance_normalizes_native_checkpoint(tmp_path: Path):
    checkpoint = _write_native_checkpoint(tmp_path)

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
```

- [ ] **Step 2: Run the loader test and confirm RED**

Run:

```bash
uv run --project cli pytest cli/tests/test_omp_followup_provenance.py::test_lookup_omp_provenance_normalizes_native_checkpoint -q
```

Expected: FAIL because `_load_record` rejects payloads without `backend: omp`.

- [ ] **Step 3: Implement strict native checkpoint normalization**

Add before `_load_record`:

```python
def _native_checkpoint_record(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("version") != 1:
        raise ValueError(f"invalid OMP native checkpoint {path}: unsupported version")
    fingerprint = str(payload.get("batch_fingerprint") or "").strip()
    session_id = str(payload.get("coordinator_session_id") or "").strip()
    workers = payload.get("workers")
    if not fingerprint:
        raise ValueError(f"invalid OMP native checkpoint {path}: missing batch_fingerprint")
    if not session_id:
        raise ValueError(
            f"invalid OMP native checkpoint {path}: missing coordinator_session_id"
        )
    if not isinstance(workers, list) or not workers:
        raise ValueError(f"invalid OMP native checkpoint {path}: missing workers")

    records: list[dict[str, Any]] = []
    for position, worker in enumerate(workers):
        if not isinstance(worker, Mapping):
            raise ValueError(
                f"invalid OMP native checkpoint {path}: worker {position} is not an object"
            )
        index = worker.get("index", position)
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ValueError(
                f"invalid OMP native checkpoint {path}: worker {position} has invalid index"
            )
        task_id = str(worker.get("task_id") or worker.get("name") or "").strip()
        if not task_id:
            raise ValueError(
                f"invalid OMP native checkpoint {path}: worker {position} has no task ID"
            )
        records.append(
            {
                "worker_index": index,
                "name": str(worker.get("name") or task_id),
                "task_id": task_id,
                "agent_uri": str(worker.get("agent_uri") or ""),
                "history_uri": str(worker.get("history_uri") or ""),
                "status": str(worker.get("status") or ""),
                "session_persisted": payload.get("session_persisted") is True,
                "coordinator_session_id": session_id,
            }
        )

    return {
        "schema_version": 2,
        "backend": "omp",
        "source_kind": "omp_native_batch",
        "run_id": path.stem,
        "status": str(payload.get("state") or ""),
        "coordinator_session_id": session_id,
        "agents": records,
    }
```

Replace `_load_record` with:

```python
def _load_record(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid OMP provenance file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"not a supported OMP provenance record: {path}")
    if payload.get("backend") == "omp" and payload.get("schema_version") == 2:
        return payload
    if payload.get("kind") == "omp_native_batch":
        return _native_checkpoint_record(path, payload)
    raise ValueError(f"not a supported OMP provenance record: {path}")
```

- [ ] **Step 4: Add malformed checkpoint boundary tests**

Add:

```python
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
```

- [ ] **Step 5: Run Task 1 tests and commit**

Run:

```bash
uv run --project cli pytest cli/tests/test_omp_followup_provenance.py -q
```

Expected: all tests in the file pass.

Commit:

```bash
git add cli/src/awf/core/dispatch_provenance.py cli/tests/test_omp_followup_provenance.py
git commit -m "feat: load native OMP checkpoints as provenance"
```

### Task 2: Select native workers by role and task ID

**Files:**
- Modify: `cli/src/awf/commands/agents.py:13-137`
- Test: `cli/tests/test_omp_followup_provenance.py`

- [ ] **Step 1: Add failing target-selection tests**

Add:

```python
def test_find_followup_target_matches_native_worker_by_role(tmp_path: Path):
    checkpoint = _write_native_checkpoint(tmp_path)

    path, payload, record = agents_command._find_followup_target(
        tmp_path,
        run_reference=str(checkpoint),
        role="implementer",
        task_id=None,
    )

    assert path == checkpoint.resolve()
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
    assert record["task_id"] == "Awf000Implementer"


def test_find_followup_target_rejects_wrong_native_role(tmp_path: Path):
    checkpoint = _write_native_checkpoint(tmp_path)

    with pytest.raises(FileNotFoundError, match="role 'reviewer' not found"):
        agents_command._find_followup_target(
            tmp_path,
            run_reference=str(checkpoint),
            role="reviewer",
            task_id=None,
        )
```

- [ ] **Step 2: Run the selection tests and confirm RED**

Run:

```bash
uv run --project cli pytest \
  cli/tests/test_omp_followup_provenance.py::test_find_followup_target_matches_native_worker_by_role \
  cli/tests/test_omp_followup_provenance.py::test_find_followup_target_matches_native_worker_by_task_id \
  cli/tests/test_omp_followup_provenance.py::test_find_followup_target_rejects_wrong_native_role -q
```

Expected: role and task-ID tests fail because native records have no stored role and `_provenance_records` ignores native checkpoints.

- [ ] **Step 3: Reuse the canonical worker-name function and include native records**

Extend imports:

```python
from awf.runners.omp import (
    OmpRunnerConfig,
    _terminate_process_group,
    omp_worker_name,
    parse_omp_json_stream,
    parse_omp_task_events,
)
```

Replace `_provenance_records` with:

```python
def _provenance_records(repo_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    dispatch_dir = repo_root / ".workflow" / "artifacts" / "dispatch"
    records: list[tuple[Path, dict[str, Any]]] = []
    if not dispatch_dir.is_dir():
        return records
    for path in sorted(dispatch_dir.glob("*.json")):
        try:
            resolved_path, payload = lookup_omp_provenance(repo_root, path)
        except (FileNotFoundError, OSError, ValueError):
            continue
        records.append((resolved_path, payload))
    return records
```

Add:

```python
def _record_matches_role(
    payload: Mapping[str, Any], record: Mapping[str, Any], role: str
) -> bool:
    if payload.get("source_kind") != "omp_native_batch":
        return record.get("role") == role
    index = record.get("worker_index")
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        return False
    expected = omp_worker_name(index, role)
    return expected in {record.get("name"), record.get("task_id")}
```

Change the `run_reference` match expression to:

```python
matches = [
    record
    for record in payload.get("agents", [])
    if isinstance(record, dict) and _record_matches_role(payload, record, role)
]
```

- [ ] **Step 4: Add duplicate native task-ID and non-persisted-session tests**

Add:

```python
def test_followup_task_id_rejects_duplicate_native_checkpoint(tmp_path: Path):
    first = _write_native_checkpoint(tmp_path)
    second = first.with_name("omp-native-batch-2.json")
    second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(ValueError, match="ambiguous"):
        agents_command._find_followup_target(
            tmp_path,
            run_reference=None,
            role=None,
            task_id="Awf000Implementer",
        )


def test_native_checkpoint_requires_persisted_session(tmp_path: Path):
    checkpoint = _write_native_checkpoint(tmp_path, session_persisted=False)
    _, payload, record = agents_command._find_followup_target(
        tmp_path,
        run_reference=str(checkpoint),
        role="implementer",
        task_id=None,
    )

    with pytest.raises(ValueError, match="persisted coordinator session"):
        agents_command._require_actionable_target(payload, record)
```

- [ ] **Step 5: Run Task 2 tests and commit**

Run:

```bash
uv run --project cli pytest cli/tests/test_omp_followup_provenance.py -q
```

Expected: all tests pass.

Commit:

```bash
git add cli/src/awf/commands/agents.py cli/tests/test_omp_followup_provenance.py
git commit -m "feat: follow up native OMP checkpoint workers"
```

### Task 3: Verify CLI lineage and live behavior

**Files:**
- Modify: `cli/tests/test_omp_followup_provenance.py`

- [ ] **Step 1: Add a CLI-level native checkpoint test**

Add the complete network-free CLI test:

```python
def test_followup_command_accepts_native_checkpoint_and_persists_lineage(
    tmp_path: Path, monkeypatch, capsys
):
    checkpoint = _write_native_checkpoint(tmp_path)
    (tmp_path / ".workflow" / "provider-config.json").write_text(
        json.dumps({"dispatch": {"omp": {"command": "repo-omp"}}}),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_resume(**kwargs):
        captured.update(kwargs)
        return (
            subprocess.CompletedProcess(
                ["omp-fixture"], 0, stdout=_direct_evidence("Awf000Implementer"), stderr=""
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
        message="native checkpoint follow-up",
        message_file=None,
        json=True,
    )

    assert agents_command.run_agents_followup_omp(args) == 0
    assert captured["session_id"] == "session-native-1"
    summary = json.loads(capsys.readouterr().out)
    child = json.loads(
        Path(summary["provenance_path"]).read_text(encoding="utf-8")
    )
    assert child["parent_run_id"] == checkpoint.stem
    assert child["parent_task_id"] == "Awf000Implementer"
    assert child["schema_version"] == 2
```

- [ ] **Step 2: Run the focused follow-up suite**

Run:

```bash
uv run --project cli pytest cli/tests/test_omp_followup_provenance.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run the complete AWF regression suite**

Run:

```bash
uv run --project cli pytest cli/tests
```

Expected: no failures; live tests remain skipped unless explicitly enabled.

- [ ] **Step 4: Run a temporary live fixture**

In a temporary git repository with `.workflow/provider-config.json` using `dispatch.surface_preference=omp`:

1. Run an AWF OMP native `implementer` worker with persisted session enabled.
2. Pass its generated `omp-native-<fingerprint>.json` directly to:

```bash
awf agents followup-omp \
  --repo-root <fixture> \
  --run <fixture>/.workflow/artifacts/dispatch/omp-native-<fingerprint>.json \
  --role implementer \
  --message "Read RESULT.txt without editing and verify its exact content." \
  --json
```

Expected:

- exit code 0
- `status: completed`
- `delivery: direct` or `successor`
- child provenance `parent_run_id` equals the native checkpoint stem
- child provenance `parent_task_id` equals the selected native worker task ID
- `RESULT.txt` unchanged

- [ ] **Step 5: Commit verification coverage**

```bash
git add cli/tests/test_omp_followup_provenance.py
git commit -m "test: cover native checkpoint follow-up lineage"
```
