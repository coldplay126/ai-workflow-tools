# AWF Supervisor Core Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the versioned Supervisor contracts, fenced state machine, durable outbox, authenticated HTTP client, and user-facing Supervisor CLI to `ai-workflow-tools`.

**Architecture:** Keep cloud persistence behind a small `SupervisorClient` port. The core package owns JSON validation, state transitions, generation fencing, and local durability but has no AWS resource code. CLI handlers serialize versioned requests and can be tested against an in-process HTTP fixture.

**Tech Stack:** Python 3.9+, `dataclasses`, `enum`, `sqlite3`, `jsonschema`, `urllib`, `botocore`, `pytest`

---

## Preconditions

- Work in an isolated `ai-workflow-tools` worktree based on the commit containing `docs/superpowers/specs/2026-07-30-awf-supervisor-control-plane-design.md`.
- Run `uv sync --project cli`.
- Baseline command: `uv run --project cli pytest cli/tests -q`.
- Expected baseline at plan creation: `991 passed, 3 skipped, 10 deselected`.
- This plan does not start an agent process and does not create AWS resources. Those are separate plans.

## File map

- `cli/src/awf/supervisor/contracts.py`: enums, dataclasses, schema loading, validation, serialization.
- `cli/src/awf/supervisor/state_machine.py`: allowed transitions and owner/generation/lease guards.
- `cli/src/awf/supervisor/store.py`: SQLite event outbox and command idempotency ledger.
- `cli/src/awf/supervisor/client.py`: HTTP transport, SigV4 signing, API error normalization.
- `cli/src/awf/supervisor/config.py`: Supervisor configuration resolution.
- `cli/src/awf/supervisor/schemas/*.json`: version 1 wire contracts.
- `cli/src/awf/supervisor/fixtures/state-machine-v1.json`: language-neutral transition and recovery vectors.
- `cli/src/awf/commands/supervisor.py`: submit/status/watch/cancel/approve/reject/agents handlers.
- `cli/src/awf/cli.py`: argparse surface only.
- `cli/tests/test_supervisor_*.py`: observable contract tests.

### Task 1: Define and package version 1 wire contracts

**Files:**
- Create: `cli/src/awf/supervisor/__init__.py`
- Create: `cli/src/awf/supervisor/contracts.py`
- Create: `cli/src/awf/supervisor/schemas/__init__.py`
- Create: `cli/src/awf/supervisor/schemas/job-v1.json`
- Create: `cli/src/awf/supervisor/schemas/agent-v1.json`
- Create: `cli/src/awf/supervisor/schemas/event-v1.json`
- Create: `cli/src/awf/supervisor/schemas/command-v1.json`
- Modify: `cli/pyproject.toml`
- Test: `cli/tests/test_supervisor_contracts.py`

- [ ] **Step 1: Write failing schema and round-trip tests**

```python
from awf.supervisor.contracts import (
    AgentEnvironment,
    JobState,
    RequestedTarget,
    SupervisorJob,
    validate_contract,
)


def test_job_contract_round_trip_preserves_fencing_fields() -> None:
    job = SupervisorJob.new(
        workflow_id="2026-07-30-login-contract",
        requested_target=RequestedTarget.AUTO,
        repo_refs=(("blip-server", "main"),),
        required_capabilities=("git", "omp"),
        now="2026-07-30T12:00:00Z",
        job_id="job-1",
    )
    payload = job.to_dict()
    validate_contract("job", payload)
    assert payload["schema_version"] == 1
    assert payload["state"] == JobState.QUEUED.value
    assert payload["generation"] == 0
    assert "prompt" not in payload
    assert "prompt_artifact_uri" not in payload
    assert "prompt_sha256" not in payload


def test_job_contract_requires_a_lease_for_an_owner() -> None:
    payload = job_fixture(owner_agent_id="local-1", lease_expires_at=None)
    with pytest.raises(ValueError, match="lease_expires_at"):
        validate_contract("job", payload)


def test_job_contract_forbids_a_lease_without_an_owner() -> None:
    payload = job_fixture(owner_agent_id=None, lease_expires_at="2026-07-30T12:01:00Z")
    with pytest.raises(ValueError, match="lease_expires_at"):
        validate_contract("job", payload)


def test_contract_rejects_unknown_major_schema() -> None:
    payload = {"schema_version": 2, "job_id": "job-1"}
    with pytest.raises(ValueError, match="unsupported job schema_version"):
        validate_contract("job", payload)
```

Also cover:

- missing `job_id`, `workflow_id`, `state`, `desired_state`, `approval_required`, `generation`, timestamps, and each required agent/event/command field
- duplicate repo names, non-ASCII or whitespace-containing identifiers, malformed artifact/checkpoint SHA-256 or S3 URI, URI/digest disagreement checked by Python after JSON Schema validation, and `active_jobs > max_concurrency`
- every unknown job state, requested target, agent environment/status, Supervisor event type, and command type
- both owner/lease conditional failures above, a non-null owner with a valid RFC 3339 lease, and an ownerless job with a null or absent lease
- rejection of `prompt`, `prompt_artifact_uri`, `prompt_sha256`, and internal `dispatch_command_id` on a public job envelope; prompt text is accepted only by the explicit submit request below
- event metadata rejection for every unknown key and for `run_id`, `task_id`, raw native `data`, `text`, `result`, `tool`, `path`, `prompt`, `model_output`, and source-code fields
- acceptance of every closed `summary` value and rejection of arbitrary caller summary text, including prompt/model-output-shaped text
- one exhaustive 24-entry `ExecutionEvent` mapping test and an agent-event request test that accepts the full stored shape only when `source` exactly equals the authenticated agent ID, rejecting a mismatched source before persistence

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
uv run --project cli pytest cli/tests/test_supervisor_contracts.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'awf.supervisor'`.

- [ ] **Step 3: Add exact JSON schemas**

Each schema must use Draft 2020-12, `additionalProperties: false`, and `schema_version: {"const": 1}`. The five packaged contract entries are exactly `agent-v1.json`, `command-v1.json`, `event-v1.json`, `job-v1.json`, and `state-machine-v1.json`; do not add a prompt schema or replace the state-machine fixture.

Use the identifier pattern `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` and repository-name pattern `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`. Artifact URIs have three closed patterns whose `{job_id}` segment uses the identifier pattern, `{generation}` is decimal digits, and `{sha256}` is lowercase 64-hex: `^s3://[A-Za-z0-9.-]+/artifacts/checkpoints/{job_id}/{generation}/{sha256}\.json$`, the same path with `provenance`, and the same path with `redacted-results`. Do not permit `/jobs/`, arbitrary artifact prefixes, or a URI whose final digest differs from its paired SHA-256. All timestamps use JSON Schema `format: "date-time"` and are validated with `FormatChecker`.

`job-v1.json` must require exactly `schema_version`, `job_id`, `workflow_id`, `state`, `desired_state`, `approval_required`, `requested_target`, `generation`, `attempt`, `repo_refs`, `required_capabilities`, `created_at`, and `updated_at`; it may additionally contain `owner_agent_id`, `lease_expires_at`, and `checkpoint`. Version 1 is deliberately fail-safe: `approval_required` is server-owned and always literal `true`; it is absent from submit requests and cannot be disabled by a capability or caller field. Its values are:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://awf.local/contracts/supervisor/job-v1.json",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "job_id", "workflow_id", "state", "desired_state", "approval_required", "requested_target", "generation", "attempt", "repo_refs", "required_capabilities", "created_at", "updated_at"],
  "properties": {
    "schema_version": {"const": 1},
    "job_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"},
    "workflow_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"},
    "state": {"enum": ["QUEUED", "CLAIMED", "PREPARING", "RUNNING", "WAITING_APPROVAL", "PAUSED", "SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED", "STALE", "RECOVERY_REQUIRED"]},
    "desired_state": {"enum": ["RUNNING", "CANCELLED", "PAUSED"]},
    "approval_required": {"const": true},
    "requested_target": {"enum": ["auto", "local", "aws"]},
    "owner_agent_id": {"type": ["string", "null"], "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"},
    "generation": {"type": "integer", "minimum": 0},
    "lease_expires_at": {"type": ["string", "null"], "format": "date-time"},
    "attempt": {"type": "integer", "minimum": 0},
    "repo_refs": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["repo", "base"],
        "properties": {
          "repo": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"},
          "base": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,254}$"}
        }
      }
    },
    "required_capabilities": {
      "type": "array",
      "minItems": 1,
      "uniqueItems": true,
      "items": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"}
    },
    "checkpoint": {
      "oneOf": [
        {"type": "null"},
        {
          "type": "object",
          "additionalProperties": false,
          "required": ["kind", "artifact_uri", "sha256"],
          "properties": {
            "kind": {"const": "awf-omp-native"},
            "artifact_uri": {"type": "string", "pattern": "^s3://[A-Za-z0-9.-]+/artifacts/checkpoints/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}/[0-9]+/[0-9a-f]{64}\\.json$"},
            "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}
          }
        }
      ]
    },
    "created_at": {"type": "string", "format": "date-time"},
    "updated_at": {"type": "string", "format": "date-time"}
  },
  "allOf": [
    {
      "if": {
        "required": ["owner_agent_id"],
        "properties": {"owner_agent_id": {"type": "string"}}
      },
      "then": {
        "required": ["lease_expires_at"],
        "properties": {"lease_expires_at": {"type": "string", "format": "date-time"}}
      },
      "else": {
        "properties": {"lease_expires_at": {"type": "null"}}
      }
    }
  ]
}
```

The job envelope never contains a prompt reference or digest. The submit endpoint alone accepts `prompt`; after allocating `job_id`, the Control Plane stores it only as the SSE-KMS object `prompts/{job_id}.txt` and sets its lowercase SHA-256 only in S3 user metadata `x-amz-meta-sha256`. Agent prompt retrieval deterministically derives that key from `job_id` and verifies the S3 metadata digest. Neither value is copied to DynamoDB, a `SupervisorJob`, event data, CLI output, or logs.

`agent-v1.json` must require exactly `schema_version`, `agent_id`, `environment`, `status`, `last_heartbeat_at`, `max_concurrency`, `active_jobs`, `capabilities`, `repos`, and `version`. `environment` is exactly `["local", "aws"]`; `status` is exactly `["ONLINE", "DRAINING", "OFFLINE"]`; `max_concurrency` is an integer at least 1; `active_jobs` is a non-negative integer; `capabilities` and `repos` are unique arrays of identifier-pattern strings; and `version` is an `additionalProperties: false` object requiring non-empty ASCII `awf` and `omp` strings (maximum 128 characters each). Python validation must additionally reject `active_jobs > max_concurrency`.

`event-v1.json` must require exactly `schema_version`, `job_id`, `generation`, `sequence`, `type`, `timestamp`, `source`, and `data`. `generation` is a non-negative integer, `sequence` is an integer at least 1, and `source` is an agent identifier. Its `type` enum is exactly:

```text
TASK_STARTED, TASK_COMPLETED, TASK_FAILED, ESCAPE_TRIGGERED,
ORCHESTRATOR_DECIDED, STAGE_STARTED, STAGE_COMPLETED, PHASE_STARTED,
PHASE_COMPLETED, WORKER_SPAWNED, WORKER_PROGRESS, WORKER_COMPLETED,
ARTIFACT_CREATED, ARTIFACT_UPDATED, PROVIDER_OUTPUT, PROVIDER_TOOL_CALL,
GATE_EVALUATED, HEARTBEAT, PROGRESS_UPDATE, MULTI_AGENT_STARTED,
AGENT_COMPLETED, JUDGE_VERDICT, TEAM_TURN_STARTED, TEAM_TURN_COMPLETED
```

`data` is a required `additionalProperties: false` object whose only optional properties are `phase`, `status_code`, `return_code`, `summary`, `terminal_status`, `retryable`, `artifact_uri`, `artifact_sha256`, `provenance_uri`, `provenance_sha256`, `checkpoint_uri`, `checkpoint_sha256`, `error_code`, `stopped_at`, and `cleanup_completed`. `phase` matches the identifier pattern; `status_code` matches `^[A-Z][A-Z0-9_]{0,63}$`; `return_code` is an integer; `terminal_status` is exactly one of `SUCCEEDED`, `FAILED`, or `CANCELLED`; `retryable` and `cleanup_completed` are booleans; `summary` is the closed enum `["task_started", "task_completed", "task_failed", "escape_triggered", "orchestrator_decided", "stage_started", "stage_completed", "phase_started", "phase_completed", "worker_spawned", "worker_progress", "worker_completed", "artifact_created", "artifact_updated", "provider_output_suppressed", "provider_tool_call_suppressed", "gate_evaluated", "heartbeat", "progress_update", "multi_agent_started", "agent_completed", "judge_verdict", "team_turn_started", "team_turn_completed"]`; `artifact_uri` uses only the `artifacts/redacted-results` pattern, `provenance_uri` only `artifacts/provenance`, and `checkpoint_uri` only `artifacts/checkpoints`; each URI's terminal digest must equal its paired `*_sha256`, which matches `^[0-9a-f]{64}$`. `error_code` is exactly one of `TRANSIENT`, `AUTH_REQUIRED`, `POLICY_DENIED`, `CONFLICT`, `CORRUPT_ARTIFACT`, `UNSAFE_RECOVERY`, or `TERMINAL_EXECUTION`; and `stopped_at` is a date-time. Add conditional schema branches: `terminal_status: SUCCEEDED` requires `return_code: 0`, both provenance fields, and both redacted-result artifact fields; `FAILED` requires `retryable: false`, `error_code`, `stopped_at`, and `cleanup_completed: true`; `CANCELLED` requires `stopped_at` and `cleanup_completed: true` and forbids `error_code`; non-terminal events forbid `terminal_status`, `retryable`, `stopped_at`, and `cleanup_completed`. Cleanup proof is always flat—there is no `cleanup` object—and there is no free-form `result`. The converter selects `summary` only from that static enum by mapped type (`PROVIDER_OUTPUT` and `PROVIDER_TOOL_CALL` use their `_suppressed` values); it never receives caller or native-event summary text. The outgoing agent event request carries all stored fields, including `source`; the Control Plane rejects it unless `source` exactly equals the authenticated local agent ID or configured AWS-principal agent ID, then persists that canonical value. It is therefore an allowlisted but non-authoritative identity field. No request or stored event may include raw prompt, source code, model output, execution `run_id`, `task_id`, raw native event `data`, local filesystem paths, tool payloads, or caller-provided text.

`command-v1.json` must require exactly `schema_version`, `command_id`, `job_id`, `generation`, and `type`. `command_id` is a lowercase canonical UUID4 matching `^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`; `job_id` uses the identifier pattern; `generation` is a non-negative integer; and the version-1 `type` enum is exactly `["EXECUTE"]`. Cancellation and approval/rejection are observed through the current job `desired_state`; they are not free-form command payloads. Event identity is `(job_id, generation, sequence)` and command identity is `(job_id, generation, command_id)`.

- [ ] **Step 4: Implement typed contracts and validation**

```python
class JobState(str, Enum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    STALE = "STALE"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class RequestedTarget(str, Enum):
    AUTO = "auto"
    LOCAL = "local"
    AWS = "aws"


class AgentEnvironment(str, Enum):
    LOCAL = "local"
    AWS = "aws"


class AgentStatus(str, Enum):
    ONLINE = "ONLINE"
    DRAINING = "DRAINING"
    OFFLINE = "OFFLINE"


class SupervisorErrorCode(str, Enum):
    TRANSIENT = "TRANSIENT"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    POLICY_DENIED = "POLICY_DENIED"
    CONFLICT = "CONFLICT"
    CORRUPT_ARTIFACT = "CORRUPT_ARTIFACT"
    UNSAFE_RECOVERY = "UNSAFE_RECOVERY"
    TERMINAL_EXECUTION = "TERMINAL_EXECUTION"


class SupervisorEventType(str, Enum):
    TASK_STARTED = "TASK_STARTED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    ESCAPE_TRIGGERED = "ESCAPE_TRIGGERED"
    ORCHESTRATOR_DECIDED = "ORCHESTRATOR_DECIDED"
    STAGE_STARTED = "STAGE_STARTED"
    STAGE_COMPLETED = "STAGE_COMPLETED"
    PHASE_STARTED = "PHASE_STARTED"
    PHASE_COMPLETED = "PHASE_COMPLETED"
    WORKER_SPAWNED = "WORKER_SPAWNED"
    WORKER_PROGRESS = "WORKER_PROGRESS"
    WORKER_COMPLETED = "WORKER_COMPLETED"
    ARTIFACT_CREATED = "ARTIFACT_CREATED"
    ARTIFACT_UPDATED = "ARTIFACT_UPDATED"
    PROVIDER_OUTPUT = "PROVIDER_OUTPUT"
    PROVIDER_TOOL_CALL = "PROVIDER_TOOL_CALL"
    GATE_EVALUATED = "GATE_EVALUATED"
    HEARTBEAT = "HEARTBEAT"
    PROGRESS_UPDATE = "PROGRESS_UPDATE"
    MULTI_AGENT_STARTED = "MULTI_AGENT_STARTED"
    AGENT_COMPLETED = "AGENT_COMPLETED"
    JUDGE_VERDICT = "JUDGE_VERDICT"
    TEAM_TURN_STARTED = "TEAM_TURN_STARTED"
    TEAM_TURN_COMPLETED = "TEAM_TURN_COMPLETED"


class CommandType(str, Enum):
    EXECUTE = "EXECUTE"


EXECUTION_EVENT_TYPE_MAP: Mapping[EventType, SupervisorEventType] = {
    EventType.TASK_STARTED: SupervisorEventType.TASK_STARTED,
    EventType.TASK_COMPLETED: SupervisorEventType.TASK_COMPLETED,
    EventType.TASK_FAILED: SupervisorEventType.TASK_FAILED,
    EventType.ESCAPE_TRIGGERED: SupervisorEventType.ESCAPE_TRIGGERED,
    EventType.ORCHESTRATOR_DECIDED: SupervisorEventType.ORCHESTRATOR_DECIDED,
    EventType.STAGE_STARTED: SupervisorEventType.STAGE_STARTED,
    EventType.STAGE_COMPLETED: SupervisorEventType.STAGE_COMPLETED,
    EventType.PHASE_STARTED: SupervisorEventType.PHASE_STARTED,
    EventType.PHASE_COMPLETED: SupervisorEventType.PHASE_COMPLETED,
    EventType.WORKER_SPAWNED: SupervisorEventType.WORKER_SPAWNED,
    EventType.WORKER_PROGRESS: SupervisorEventType.WORKER_PROGRESS,
    EventType.WORKER_COMPLETED: SupervisorEventType.WORKER_COMPLETED,
    EventType.ARTIFACT_CREATED: SupervisorEventType.ARTIFACT_CREATED,
    EventType.ARTIFACT_UPDATED: SupervisorEventType.ARTIFACT_UPDATED,
    EventType.PROVIDER_OUTPUT: SupervisorEventType.PROVIDER_OUTPUT,
    EventType.PROVIDER_TOOL_CALL: SupervisorEventType.PROVIDER_TOOL_CALL,
    EventType.GATE_EVALUATED: SupervisorEventType.GATE_EVALUATED,
    EventType.HEARTBEAT: SupervisorEventType.HEARTBEAT,
    EventType.PROGRESS_UPDATE: SupervisorEventType.PROGRESS_UPDATE,
    EventType.MULTI_AGENT_STARTED: SupervisorEventType.MULTI_AGENT_STARTED,
    EventType.AGENT_COMPLETED: SupervisorEventType.AGENT_COMPLETED,
    EventType.JUDGE_VERDICT: SupervisorEventType.JUDGE_VERDICT,
    EventType.TEAM_TURN_STARTED: SupervisorEventType.TEAM_TURN_STARTED,
    EventType.TEAM_TURN_COMPLETED: SupervisorEventType.TEAM_TURN_COMPLETED,
}


_SCHEMA_FILES = {
    "job": "job-v1.json",
    "agent": "agent-v1.json",
    "event": "event-v1.json",
    "command": "command-v1.json",
}


def validate_contract(kind: str, payload: Mapping[str, Any]) -> None:
    if kind not in _SCHEMA_FILES:
        raise ValueError(f"unknown supervisor contract: {kind}")
    version = payload.get("schema_version")
    if type(version) is not int or version != 1:
        raise ValueError(f"unsupported {kind} schema_version: {version!r}")
    schema = json.loads(
        resources.files("awf.supervisor.schemas")
        .joinpath(_SCHEMA_FILES[kind])
        .read_text(encoding="utf-8")
    )
    errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload), key=lambda item: list(item.path))
    if errors:
        raise ValueError(f"invalid {kind} contract: {errors[0].message}")
```

Use frozen dataclasses with `to_dict()` and `from_dict()` methods for `SupervisorJob`, `SupervisorAgent`, `SupervisorEvent`, and `SupervisorCommand`; their fields mirror the corresponding schema exactly. Import `EventType` from `awf.core.events` and require `set(EXECUTION_EVENT_TYPE_MAP) == set(EventType)` in a regression test so adding a native event cannot silently omit a Supervisor mapping. The converter uses the mapping for `type`, copies only `timestamp` from `ExecutionEvent`, takes `job_id`, `generation`, and locally allocated `sequence` from the Supervisor context, and sets `source` only from the configured authenticated agent ID—not `ExecutionEvent.source`. It accepts only independently constructed schema-valid metadata for `data`; it never serializes `ExecutionEvent.run_id`, `task_id`, `source`, or native `data`. Reject duplicate repo names in Python after JSON Schema validation because JSON Schema `uniqueItems` cannot enforce uniqueness by one object property.

- [ ] **Step 5: Package schema files and run GREEN**

Add:

```toml
[tool.setuptools.package-data]
"awf.supervisor.schemas" = ["*.json"]
"awf.supervisor.fixtures" = ["*.json"]
```

The focused test must inspect the installed wheel resources and assert the package contains exactly the four `schemas/*-v1.json` files plus `fixtures/state-machine-v1.json`; this preserves the five-entry shared-contract manifest consumed by the Control Plane.

Run:

```bash
uv run --project cli pytest cli/tests/test_supervisor_contracts.py -q
```

Expected: all contract tests pass.

- [ ] **Step 6: Commit**

```bash
git add cli/pyproject.toml cli/src/awf/supervisor cli/tests/test_supervisor_contracts.py
git commit -m "feat: add supervisor wire contracts"
```

### Task 2: Implement the fenced state machine

**Files:**
- Create: `cli/src/awf/supervisor/state_machine.py`
- Create: `cli/src/awf/supervisor/fixtures/__init__.py`
- Create: `cli/src/awf/supervisor/fixtures/state-machine-v1.json`
- Modify: `cli/pyproject.toml`
- Test: `cli/tests/test_supervisor_state_machine.py`

- [ ] **Step 1: Write table-driven failing transition tests**

```python
@pytest.mark.parametrize("vector", load_state_machine_fixture()["allowed_transition_vectors"])
def test_allowed_transitions(vector: Mapping[str, Any]) -> None:
    assert_transition(
        current=job_from_vector(vector["current"]),
        proposed=job_from_vector(vector["proposed"]),
        evidence=evidence_from_vector(vector["evidence"]),
        now="2026-07-30T12:00:00Z",
    )


@pytest.mark.parametrize("state", NON_TERMINAL_STATES)
def test_non_terminal_failure_requires_complete_termination_evidence(state: JobState) -> None:
    current = job_fixture(state=state, generation=7)
    proposed = job_fixture(state=JobState.FAILED, generation=7)
    complete = TransitionEvidence(
        checkpoint_verified=False,
        execution_stopped=True,
        cleanup_completed=True,
        failure_error_code=SupervisorErrorCode.TERMINAL_EXECUTION,
        failure_is_retryable=False,
    )
    assert_transition(current, proposed, evidence=complete, now=NOW)
    for incomplete in (
        replace(complete, failure_is_retryable=True),
        replace(complete, execution_stopped=False),
        replace(complete, cleanup_completed=False),
    ):
        with pytest.raises(UnsafeTransition):
            assert_transition(current, proposed, evidence=incomplete, now=NOW)


def test_paused_claim_requires_verified_checkpoint_and_exactly_next_generation() -> None:
    current = job_fixture(
        state=JobState.PAUSED,
        generation=7,
        owner_agent_id=None,
        lease_expires_at=None,
        checkpoint=verified_checkpoint_fixture(generation=7),
    )
    same_generation = job_fixture(
        state=JobState.CLAIMED,
        generation=7,
        owner_agent_id="local-1",
        lease_expires_at="2026-07-30T12:01:30Z",
        checkpoint=verified_checkpoint_fixture(generation=7),
    )
    retained = TransitionEvidence(
        checkpoint_verified=True,
        execution_stopped=True,
        recovery_origin_matches=True,
    )
    with pytest.raises(UnsafeTransition, match="generation"):
        assert_transition(current, same_generation, evidence=retained, now=NOW)
    assert_transition(
        current,
        replace(same_generation, generation=8),
        evidence=retained,
        now=NOW,
    )


def test_cross_node_paused_claim_requires_verified_commit_boundary() -> None:
    current = job_fixture(
        state=JobState.PAUSED,
        generation=7,
        owner_agent_id=None,
        lease_expires_at=None,
        checkpoint=verified_checkpoint_fixture(generation=7),
    )
    proposed = job_fixture(
        state=JobState.CLAIMED,
        generation=8,
        owner_agent_id="aws-agent-01",
        lease_expires_at="2026-07-30T12:01:30Z",
        checkpoint=verified_checkpoint_fixture(generation=7),
    )
    with pytest.raises(UnsafeTransition, match="commit boundary"):
        assert_transition(
            current,
            proposed,
            evidence=TransitionEvidence(
                checkpoint_verified=True,
                execution_stopped=True,
                recovery_origin_matches=False,
            ),
            now=NOW,
        )
    assert_transition(
        current,
        proposed,
        evidence=TransitionEvidence(
            checkpoint_verified=True,
            execution_stopped=True,
            recovery_origin_matches=False,
            commit_boundary_verified=True,
        ),
        now=NOW,
    )


def test_running_without_verified_checkpoint_cannot_pause() -> None:
    with pytest.raises(UnsafeTransition, match="checkpoint"):
        assert_transition(
            job_fixture(state=JobState.RUNNING, generation=7),
            job_fixture(state=JobState.PAUSED, generation=7),
            evidence=TransitionEvidence(execution_stopped=True),
            now=NOW,
        )


def test_stale_owner_is_fenced() -> None:
    with pytest.raises(LeaseConflict, match="generation"):
        assert_owner_write(
            expected_agent_id="local-1",
            expected_generation=5,
            actual_agent_id="local-1",
            actual_generation=4,
            lease_expires_at="2026-07-30T12:01:00Z",
            now="2026-07-30T12:00:00Z",
        )
```

Define `NON_TERMINAL_STATES` as every `JobState` except `SUCCEEDED`, `FAILED`, and `CANCELLED`. Cover every state in the enum, terminal-state immutability, expired lease, wrong owner, bool-as-int generation rejection, and generation increment on every ownership acquisition. Add allowed `QUEUED → CLAIMED` and `PAUSED → CLAIMED` vectors with `proposed.generation == current.generation + 1`, plus negative same-generation and skipped-generation vectors for both edges. Also add a negative vector for each non-terminal `→ FAILED` edge that independently omits the non-retryable error, stopped evidence, or cleanup evidence; a negative vector for each non-terminal `→ CANCELLED` edge that omits stopped or cleanup evidence; and paused-claim negatives for missing checkpoint, missing owner, and expired/missing lease.

Drive the transition parametrization and recovery cases from `importlib.resources.files("awf.supervisor.fixtures").joinpath("state-machine-v1.json")`; the checked-in JSON is the language-neutral authority later consumed by the Node Control Plane.

- [ ] **Step 2: Confirm RED**

```bash
uv run --project cli pytest cli/tests/test_supervisor_state_machine.py -q
```

Expected: import failure for `awf.supervisor.state_machine`.

- [ ] **Step 3: Implement explicit transition tables and guards**

```python
_ALLOWED: Dict[JobState, FrozenSet[JobState]] = {
    JobState.QUEUED: frozenset({JobState.CLAIMED, JobState.BLOCKED, JobState.CANCELLED, JobState.FAILED}),
    JobState.CLAIMED: frozenset({JobState.PREPARING, JobState.QUEUED, JobState.CANCELLED, JobState.FAILED}),
    JobState.PREPARING: frozenset({JobState.RUNNING, JobState.RECOVERY_REQUIRED, JobState.FAILED, JobState.CANCELLED}),
    JobState.RUNNING: frozenset({JobState.WAITING_APPROVAL, JobState.PAUSED, JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.STALE, JobState.RECOVERY_REQUIRED}),
    JobState.WAITING_APPROVAL: frozenset({JobState.RUNNING, JobState.RECOVERY_REQUIRED, JobState.CANCELLED, JobState.FAILED}),
    JobState.PAUSED: frozenset({JobState.CLAIMED, JobState.CANCELLED, JobState.FAILED}),
    JobState.BLOCKED: frozenset({JobState.QUEUED, JobState.CANCELLED, JobState.FAILED}),
    JobState.STALE: frozenset({JobState.PAUSED, JobState.RECOVERY_REQUIRED, JobState.FAILED, JobState.CANCELLED}),
    JobState.RECOVERY_REQUIRED: frozenset({JobState.PAUSED, JobState.FAILED, JobState.CANCELLED}),
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
}


@dataclass(frozen=True)
class TransitionEvidence:
    checkpoint_verified: bool = False
    execution_stopped: bool = False
    cleanup_completed: bool = False
    failure_error_code: Optional[SupervisorErrorCode] = None
    cleanup_refused: bool = False
    recovery_unsafe: bool = False
    recovery_origin_matches: Optional[bool] = None
    commit_boundary_verified: bool = False
    failure_is_retryable: Optional[bool] = None
```

`assert_transition(current: SupervisorJob, proposed: SupervisorJob, *, evidence: TransitionEvidence, now: str) -> None` is the only transition entry point. It verifies a single unchanged job ID, an allowed state edge, schema-valid current and proposed jobs, and an aware-UTC RFC 3339 `now`. Every ownership-acquiring edge whose target is `CLAIMED` requires a non-null proposed owner, a lease after `now`, and `proposed.generation == current.generation + 1`; same or skipped generations are rejected. `PAUSED → CLAIMED` additionally requires `checkpoint_verified`, `execution_stopped`, preservation of the prior-generation checkpoint URI/digest, and exactly one recovery branch: `recovery_origin_matches=True` for the originating agent in the same environment, or `recovery_origin_matches=False` plus `commit_boundary_verified=True` for a different agent/environment. The commit-boundary evidence means every repo is clean, its normalized remote ref resolves to the recorded immutable commit, and no source-bearing uncommitted state exists. All edges not acquiring an owner preserve generation.

For `RUNNING → PAUSED`, require `checkpoint_verified=True`, `execution_stopped=True`, and a schema-valid checkpoint URI/digest pair. For `PREPARING`, `RUNNING`, or `WAITING_APPROVAL → RECOVERY_REQUIRED`, require `execution_stopped=True` and evidence that cleanup was refused, the recovery checkpoint was unavailable/invalid, retained-native recovery was unsafe, or commit-boundary recovery failed its clean/pushed/ref-to-commit verification; never infer any evidence from a status string. These recovery edges preserve owner/generation for the fenced event write and carry no terminal fields.

For every non-terminal `→ FAILED` edge, `assert_transition` requires all of: `failure_error_code` set to one of the non-transient `SupervisorErrorCode` values, `failure_is_retryable is False`, `execution_stopped is True`, and `cleanup_completed is True`. `execution_stopped=True` means an execution-stop record for work that started or an explicit no-execution record for `QUEUED`/`BLOCKED`; neither condition may be inferred. The corresponding event carries exactly `terminal_status: "FAILED"`, `retryable: false`, `error_code`, `stopped_at`, and `cleanup_completed: true` plus the mapped closed summary. `TRANSIENT`, a missing error code, and any retryable error are rejected. Every non-terminal `→ CANCELLED` edge similarly requires stopped and cleanup evidence and emits flat `terminal_status: "CANCELLED"`, `stopped_at`, and `cleanup_completed: true`; it does not carry an error code. No state helper may terminalize a job by state pair alone.

`state-machine-v1.json` must contain `schema_version: 1`, the complete `_ALLOWED` adjacency list above, and vector objects with `current`, `proposed`, `evidence`, and `allowed` fields. It must include the three expired-running recovery vectors (verified checkpoint, no checkpoint, and process-not-stopped), allowed and same/skipped-generation vectors for both `QUEUED → CLAIMED` and `PAUSED → CLAIMED`, retained-origin and verified-commit-boundary cross-node PAUSED claims, rejection of a different-origin PAUSED claim without commit-boundary evidence, paused-claim missing-checkpoint/owner/lease vectors, allowed `PREPARING|RUNNING|WAITING_APPROVAL → RECOVERY_REQUIRED` vectors with stopped-process plus cleanup-refusal/unsafe-recovery evidence and corresponding negatives, an allowed failure vector for each non-terminal state, the three incomplete-failure variants for each such state, and an allowed and each incomplete-cancellation vector for every non-terminal state. Reject unsupported fixture versions and reject an implementation whose derived table or vector outcomes differ from the fixture. The package-data stanza in Task 1 already packages this fixture as the fifth shared entry.


- [ ] **Step 4: Run GREEN**

```bash
uv run --project cli pytest cli/tests/test_supervisor_state_machine.py -q
```

Expected: all state-machine tests pass.

- [ ] **Step 5: Commit**

```bash
git add cli/src/awf/supervisor/state_machine.py cli/src/awf/supervisor/fixtures cli/pyproject.toml cli/tests/test_supervisor_state_machine.py
git commit -m "feat: enforce supervisor lease fencing"
```

### Task 3: Add the durable event outbox and command ledger

**Files:**
- Create: `cli/src/awf/supervisor/store.py`
- Test: `cli/tests/test_supervisor_store.py`

- [ ] **Step 1: Write failing persistence and idempotency tests**

```python
def test_outbox_survives_reopen_and_ack_is_exact(tmp_path: Path) -> None:
    db = tmp_path / "supervisor.db"
    store = SupervisorStore(db)
    event = event_fixture(job_id="job-1", generation=2, sequence=1)
    store.enqueue_event(event)

    reopened = SupervisorStore(db)
    assert reopened.pending_events(limit=10) == [event]
    reopened.ack_event("job-1", 2, 1)
    assert reopened.pending_events(limit=10) == []


def test_duplicate_command_is_not_executed_twice(tmp_path: Path) -> None:
    store = SupervisorStore(tmp_path / "supervisor.db")
    assert store.claim_command("cmd-1", "job-1", 3) is True
    assert store.claim_command("cmd-1", "job-1", 3) is False
```

Also test monotonic per-job sequence allocation, rollback on duplicate event key, terminal command result recording, and concurrent SQLite writers using two store instances.

- [ ] **Step 2: Confirm RED**

```bash
uv run --project cli pytest cli/tests/test_supervisor_store.py -q
```

Expected: import failure for `SupervisorStore`.

- [ ] **Step 3: Implement SQLite schema and atomic operations**

```python
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS event_outbox (
    job_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_id, generation, sequence)
);
CREATE TABLE IF NOT EXISTS command_ledger (
    command_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('claimed', 'completed', 'failed')),
    result_json TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_sequence (
    job_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    next_sequence INTEGER NOT NULL,
    PRIMARY KEY (job_id, generation)
);
"""
```

Use `BEGIN IMMEDIATE` for sequence allocation and command claim. Validate every payload with `validate_contract` before insert and after load. Set connection timeout to five seconds and enable foreign keys.

- [ ] **Step 4: Run GREEN**

```bash
uv run --project cli pytest cli/tests/test_supervisor_store.py -q
```

Expected: all store tests pass.

- [ ] **Step 5: Commit**

```bash
git add cli/src/awf/supervisor/store.py cli/tests/test_supervisor_store.py
git commit -m "feat: persist supervisor outbox and commands"
```

### Task 4: Add Supervisor configuration and authenticated client transport

**Files:**
- Create: `cli/src/awf/supervisor/config.py`
- Create: `cli/src/awf/supervisor/client.py`
- Modify: `cli/src/awf/core/config.py`
- Modify: `cli/pyproject.toml`
- Test: `cli/tests/test_supervisor_client.py`
- Test: `cli/tests/test_supervisor_config.py`

- [ ] **Step 1: Write failing config precedence and API normalization tests**

```python
def test_supervisor_config_env_overrides_user_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    write_user_config(tmp_path, api_url="https://old.example", region="us-east-1")
    monkeypatch.setenv("AWF_SUPERVISOR_API_URL", "https://api.example")
    monkeypatch.setenv("AWF_SUPERVISOR_REGION", "ap-northeast-2")
    config = load_supervisor_config()
    assert config.api_url == "https://api.example"
    assert config.region == "ap-northeast-2"


def test_client_maps_generation_conflict() -> None:
    transport = FixtureTransport(status=409, payload={"code": "GENERATION_CONFLICT", "message": "stale generation"})
    with pytest.raises(SupervisorConflict, match="stale generation"):
        SupervisorClient(transport).cancel_job("job-1", generation=3)
```

Cover TLS-only URL validation, trailing slash normalization, body size limit, malformed JSON, 401/403/404/409/429/5xx mapping, request ID propagation, and non-retry of POST without an idempotency key. Add a recording-transport test that calls `submit_job` with a supplied UUID4 and asserts the path is `/v1/admin/jobs`, `Idempotency-Key` is that UUID, and the JSON body equals exactly `{"schema_version": 1, "workflow_id": "...", "requested_target": "auto", "repo_refs": [{"repo": "blip-server", "base": "main"}], "required_capabilities": ["git", "omp", "github"], "prompt": "Fix the login contract.\n"}`—with no job ID, owner, lease, checkpoint, prompt URI, or prompt digest. Test generated UUID4 keys, malformed/non-v4 supplied keys, and validation of the returned job envelope independently.

- [ ] **Step 2: Confirm RED**

```bash
uv run --project cli pytest cli/tests/test_supervisor_config.py cli/tests/test_supervisor_client.py -q
```

Expected: import failures for the new modules.

- [ ] **Step 3: Add botocore and exact config defaults**

Add `"botocore>=1.40,<2"` to main dependencies. Add this default block to `AwfConfig.defaults()`:

```python
{
"supervisor": {
    "api_url": "",
    "region": "ap-northeast-2",
    "profile": "",
    "poll_interval_seconds": 2,
    "request_timeout_seconds": 30,
},
}
```

`load_supervisor_config` reads user TOML first, project TOML second through existing `load_awf_config`, then applies `AWF_SUPERVISOR_API_URL`, `AWF_SUPERVISOR_REGION`, and `AWS_PROFILE`.

- [ ] **Step 4: Implement the transport port and SigV4 transport**

```python
import uuid

from typing import Any, Mapping, Optional, Protocol, Sequence


class Transport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> HttpResponse: ...


class SigV4Transport:
    def __init__(self, config: SupervisorConfig) -> None:
        self._config = config
        session = botocore.session.Session(profile=config.profile or None)
        self._credentials = session.get_credentials()
        if self._credentials is None:
            raise SupervisorAuthRequired("AWS SSO credentials are unavailable")

    def request(self, method: str, path: str, *, payload=None, headers=None) -> HttpResponse:
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        url = f"{self._config.api_url}{path}"
        request = AWSRequest(method=method, url=url, data=body, headers={"Content-Type": "application/json", **dict(headers or {})})
        SigV4Auth(self._credentials.get_frozen_credentials(), "execute-api", self._config.region).add_auth(request)
        prepared = request.prepare()
        return _send_prepared(prepared, timeout=self._config.request_timeout_seconds)


class SupervisorClient:
    def submit_job(
        self,
        *,
        workflow_id: str,
        requested_target: RequestedTarget,
        repo_refs: Sequence[RepoRef],
        required_capabilities: Sequence[str],
        prompt: str,
        idempotency_key: Optional[str] = None,
    ) -> SupervisorJob:
        key = _validate_uuid4(idempotency_key) if idempotency_key is not None else str(uuid.uuid4())
        payload = {
            "schema_version": 1,
            "workflow_id": workflow_id,
            "requested_target": requested_target.value,
            "repo_refs": [repo_ref.to_dict() for repo_ref in repo_refs],
            "required_capabilities": list(required_capabilities),
            "prompt": prompt,
        }
        response = self._transport.request(
            "POST",
            "/v1/admin/jobs",
            payload=payload,
            headers={"Idempotency-Key": key},
        )
        return SupervisorJob.from_dict(_successful_json(response))
```

All `SupervisorClient` methods use Python 3.9-compatible annotations: import `Optional` from `typing` rather than PEP 604 `X | Y` unions. `submit_job` serializes only the exact six-key admin request above. It creates a UUID4 `Idempotency-Key` unless its optional `idempotency_key` argument supplies a validated canonical UUID4; it never retries a POST without that header. Decision methods always create their own UUID4 key. Validate successful response contracts before returning them. The client does not upload a prompt artifact or calculate an envelope prompt digest.

- [ ] **Step 5: Run GREEN and refresh the lockfile**

```bash
uv lock --project cli
uv run --project cli pytest cli/tests/test_supervisor_config.py cli/tests/test_supervisor_client.py -q
```

Expected: all client and config tests pass.

- [ ] **Step 6: Commit**

```bash
git add cli/pyproject.toml cli/uv.lock cli/src/awf/core/config.py cli/src/awf/supervisor/config.py cli/src/awf/supervisor/client.py cli/tests/test_supervisor_config.py cli/tests/test_supervisor_client.py
git commit -m "feat: add supervisor API client"
```

### Task 5: Add the user-facing Supervisor CLI

**Files:**
- Create: `cli/src/awf/commands/supervisor.py`
- Modify: `cli/src/awf/cli.py`
- Modify: `cli/README.md`
- Test: `cli/tests/test_supervisor_cli.py`
- Test: `cli/tests/test_docs_semantic_audit.py`

- [ ] **Step 1: Write failing parser and handler tests**

```python
def test_submit_requires_prompt_repo_and_workflow_id() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "supervisor", "submit",
        "--workflow-id", "2026-07-30-login-contract",
        "--repo", "blip-server:main",
        "--prompt-file", "task.txt",
        "--target", "auto",
        "--json",
    ])
    assert args.supervisor_command == "submit"
    assert args.workflow_id == "2026-07-30-login-contract"
    assert args.repo == ["blip-server:main"]


def test_submit_uses_complete_deterministic_harness_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompt = tmp_path / "task.txt"
    prompt.write_text("Fix the login contract.\n", encoding="utf-8")
    fake = RecordingSupervisorClient(job_fixture(state="QUEUED"))
    key = "5a2c1e31-cf45-4fe4-ae47-f40d3eb90837"
    monkeypatch.setenv("AWF_SUPERVISOR_E2E_HARNESS", "1")
    monkeypatch.setattr(supervisor_command, "_client", lambda args: fake)
    argv = [
        "supervisor", "submit",
        "--workflow-id", "2026-07-30-login-contract",
        "--repo", "blip-server:main",
        "--require-capability", "github",
        "--prompt-file", str(prompt),
        "--idempotency-key", key,
        "--json",
    ]
    assert main(argv) == 0
    assert main(argv) == 0
    assert fake.submissions == [
        {
            "workflow_id": "2026-07-30-login-contract",
            "requested_target": RequestedTarget.AUTO,
            "repo_refs": [RepoRef(repo="blip-server", base="main")],
            "required_capabilities": ["git", "omp", "github"],
            "prompt": "Fix the login contract.\n",
            "idempotency_key": key,
        },
        {
            "workflow_id": "2026-07-30-login-contract",
            "requested_target": RequestedTarget.AUTO,
            "repo_refs": [RepoRef(repo="blip-server", base="main")],
            "required_capabilities": ["git", "omp", "github"],
            "prompt": "Fix the login contract.\n",
            "idempotency_key": key,
        },
    ]
    output_lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["schema_version"] for line in output_lines] == [1, 1]
```

Also test parser rejection without `--workflow-id`; repo parsing with branches containing `/`; the required `git` and `omp` capability defaults; duplicate/invalid `--require-capability`; prompt maximum 64 KiB; empty prompt rejection; text output; watch terminal exit; Ctrl-C exit 130; cancel/approve/reject generation requirement; fixed approve→`APPROVE/CONTINUE` and reject→`REJECT/CANCEL` request bodies; and API error exit codes. Test that `--idempotency-key` rejects a malformed/non-v4 UUID and is rejected unless `AWF_SUPERVISOR_E2E_HARNESS=1`; normal submissions omit the option and receive the client-generated UUID4.

- [ ] **Step 2: Confirm RED**

```bash
uv run --project cli pytest cli/tests/test_supervisor_cli.py cli/tests/test_docs_semantic_audit.py -q
```

Expected: parser rejects `supervisor` and the known-command audit fails after only one side is changed.

- [ ] **Step 3: Add the exact argparse surface**

Add `"supervisor"` to `KNOWN_COMMANDS`. Register:

```text
awf supervisor submit --workflow-id WORKFLOW_ID --repo REPO:BASE... (--prompt TEXT | --prompt-file PATH) [--require-capability CAPABILITY]... [--target auto|local|aws] [--idempotency-key UUID4] [--json]
awf supervisor status JOB_ID [--json]
awf supervisor watch JOB_ID [--interval 1..60] [--json]
awf supervisor cancel JOB_ID --generation N [--json]
awf supervisor approve JOB_ID --generation N [--json]
awf supervisor reject JOB_ID --generation N [--json]
awf supervisor agents [--json]
```

Use an argparse mutually exclusive group for `--prompt` and `--prompt-file`. `--workflow-id` is required and is the literal `workflow_id` request field; no ambient repository or `.workflow/state.json` value is inferred. `--repo` values produce `repo_refs`, `--target` produces `requested_target`, and `_load_prompt` produces the `prompt` string rather than a file reference. Start `required_capabilities` with exactly `["git", "omp"]`, append each `--require-capability` in order, and reject duplicates or an invalid identifier before invoking the client. `schema_version` is always literal integer `1`.
The approve handler sends exactly `{generation, decision: "APPROVE", requested_action: "CONTINUE"}` and reject sends exactly `{generation, decision: "REJECT", requested_action: "CANCEL"}`. There is no free-form action option in version 1.

`--idempotency-key` is a harness-only option for deterministic replay: accept only a canonical UUID4 and only when `AWF_SUPERVISOR_E2E_HARNESS=1`; otherwise raise an argparse usage error before reading the prompt or making a request. Pass its value unchanged to `SupervisorClient.submit_job`. Do not add agent runtime commands in this task.

- [ ] **Step 4: Implement handlers with stable exit codes**

```python
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_CONFLICT = 4
EXIT_REMOTE = 5


def run_supervisor_submit(args: argparse.Namespace) -> int:
    try:
        prompt = _load_prompt(args)
        repo_refs = [_parse_repo_ref(value) for value in args.repo]
        required_capabilities = _required_capabilities(args.require_capability)
        idempotency_key = _harness_idempotency_key(args.idempotency_key)
        job = _client(args).submit_job(
            workflow_id=args.workflow_id,
            prompt=prompt,
            repo_refs=repo_refs,
            required_capabilities=required_capabilities,
            requested_target=RequestedTarget(args.target),
            idempotency_key=idempotency_key,
        )
    except SupervisorAuthRequired as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_AUTH
    except SupervisorConflict as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFLICT
    except (OSError, ValueError, SupervisorRemoteError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REMOTE
    _print_job(job, as_json=bool(args.json))
    return EXIT_OK
```

`watch` polls until a terminal or operator-action state. It prints each state only when `(state, generation, updated_at)` changes. JSON mode prints one JSON object per line.

- [ ] **Step 5: Update CLI documentation and run GREEN**

Document configuration keys, AWS SSO prerequisite, command examples, exit codes, the required `--workflow-id`, the default and optional required capabilities, and the fact that `submit` does not upload local source files. State that the prompt is sent only in the six-field submit request; the Control Plane, not the CLI, persists it to the deterministic encrypted S3 key. Document `--idempotency-key` only as the `AWF_SUPERVISOR_E2E_HARNESS=1` deterministic-test interface, not as a normal operator retry mechanism.

Run:

```bash
uv run --project cli pytest cli/tests/test_supervisor_cli.py cli/tests/test_docs_semantic_audit.py -q
```

Expected: all focused tests pass and `KNOWN_COMMANDS` exactly matches argparse.

- [ ] **Step 6: Commit**

```bash
git add cli/src/awf/commands/supervisor.py cli/src/awf/cli.py cli/README.md cli/tests/test_supervisor_cli.py cli/tests/test_docs_semantic_audit.py
git commit -m "feat: add supervisor CLI commands"
```

### Task 6: Run contract packaging and regression verification

**Files:**
- Modify only if a failure proves a defect in files introduced by Tasks 1-5.

- [ ] **Step 1: Build the wheel and inspect packaged schemas**

```bash
uv build --project cli
python3 - <<'PY'
from pathlib import Path
from zipfile import ZipFile

wheel = max(Path("cli/dist").glob("awf_cli-*.whl"), key=lambda path: path.stat().st_mtime)
expected = {
    "awf/supervisor/schemas/agent-v1.json",
    "awf/supervisor/schemas/command-v1.json",
    "awf/supervisor/schemas/event-v1.json",
    "awf/supervisor/schemas/job-v1.json",
    "awf/supervisor/fixtures/state-machine-v1.json",
}
with ZipFile(wheel) as archive:
    actual = {
        name for name in archive.namelist()
        if name.startswith("awf/supervisor/schemas/") and name.endswith("-v1.json")
        or name == "awf/supervisor/fixtures/state-machine-v1.json"
    }
assert actual == expected, (actual, expected)
PY
```

Expected: the wheel contains exactly the four schema files and the state-machine fixture—no prompt schema and no missing manifest entry.

- [ ] **Step 2: Run the Supervisor suite**

```bash
uv run --project cli pytest cli/tests/test_supervisor_contracts.py cli/tests/test_supervisor_state_machine.py cli/tests/test_supervisor_store.py cli/tests/test_supervisor_config.py cli/tests/test_supervisor_client.py cli/tests/test_supervisor_cli.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run the full AWF suite**

```bash
uv run --project cli pytest cli/tests -q
```

Expected: no failures; only the repository's documented skips and deselections remain.

- [ ] **Step 4: Verify CLI help**

```bash
uv run --project cli awf supervisor --help
uv run --project cli awf supervisor submit --help
```

Expected: the exact commands and arguments from Task 5 appear, with no agent runtime commands.

- [ ] **Step 5: Commit any verification-only fix, otherwise leave the branch unchanged**

If verification required a code change, repeat the focused failing test before the fix and commit only that fix:

```bash
git add -u -- cli
git commit -m "fix: correct supervisor core contract"
```

Do not create an empty commit.
