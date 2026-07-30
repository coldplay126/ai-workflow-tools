"""Behavioral regression tests for the fenced AWF Supervisor state machine.

The JSON fixture is the language-neutral transition authority. These tests use
only the public Supervisor contracts and state-machine boundary functions.
"""

from __future__ import annotations

import json
from dataclasses import replace
from importlib import resources
from typing import Any, Dict, Iterable, Mapping, Tuple

import pytest

from awf.supervisor.contracts import (
    JobState,
    RequestedTarget,
    SupervisorErrorCode,
    SupervisorJob,
    validate_contract,
)
from awf.supervisor.state_machine import (
    LeaseConflict,
    TransitionEvidence,
    UnsafeTransition,
    assert_owner_write,
    assert_transition,
)


NOW = "2026-07-30T12:00:00Z"
LEASE_AFTER_NOW = "2026-07-30T12:01:30Z"
LEASE_BEFORE_NOW = "2026-07-30T11:59:00Z"
LOCAL_AGENT_ID = "local-agent-1"
AWS_AGENT_ID = "aws-agent-01"


def load_state_machine_fixture() -> Mapping[str, Any]:
    """Load and fail closed on an unsupported fixture major version."""
    payload = json.loads(
        resources.files("awf.supervisor.fixtures")
        .joinpath("state-machine-v1.json")
        .read_text(encoding="utf-8")
    )
    validate_state_machine_fixture(payload)
    return payload


def validate_state_machine_fixture(payload: Mapping[str, Any]) -> None:
    """Validate the versioned fixture envelope before using its vectors."""
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ValueError("unsupported state-machine fixture schema_version")

    required_fields = {
        "schema_version",
        "now",
        "job_template",
        "checkpoint",
        "allowed_transitions",
        "allowed_transition_vectors",
        "rejected_transition_vectors",
    }
    missing = required_fields.difference(payload)
    if missing:
        raise ValueError("invalid state-machine fixture: missing {}".format(sorted(missing)))

    transitions = payload["allowed_transitions"]
    if not isinstance(transitions, Mapping):
        raise ValueError("invalid state-machine fixture: allowed_transitions")
    state_names = {state.value for state in JobState}
    if set(transitions) != state_names:
        raise ValueError("invalid state-machine fixture: incomplete transition table")
    if any(
        not isinstance(targets, list)
        or len(targets) != len(set(targets))
        or not set(targets).issubset(state_names)
        for targets in transitions.values()
    ):
        raise ValueError("invalid state-machine fixture: invalid transition table")

    for vector in transition_vectors(payload):
        required_vector_fields = {"name", "current", "proposed", "evidence", "allowed"}
        if not required_vector_fields.issubset(vector):
            raise ValueError("invalid state-machine fixture: malformed transition vector")
        if (
            not isinstance(vector["current"], Mapping)
            or not isinstance(vector["proposed"], Mapping)
            or not isinstance(vector["evidence"], Mapping)
            or type(vector["allowed"]) is not bool
        ):
            raise ValueError("invalid state-machine fixture: malformed transition vector")


def transition_vectors(payload: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    yield from payload["allowed_transition_vectors"]
    yield from payload["rejected_transition_vectors"]


FIXTURE = load_state_machine_fixture()
NON_TERMINAL_STATES = tuple(
    state
    for state in JobState
    if state not in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}
)


def checkpoint_fixture(generation: int = 7) -> Dict[str, Any]:
    checkpoint = dict(FIXTURE["checkpoint"])
    checkpoint["artifact_uri"] = checkpoint["artifact_uri"].replace(
        "/7/", "/{}/".format(generation)
    )
    return checkpoint


def job_fixture(
    *, state: JobState = JobState.QUEUED, generation: int = 7, **updates: Any
) -> SupervisorJob:
    payload = dict(FIXTURE["job_template"])
    payload.update({"state": state.value, "generation": generation})
    payload.update(updates)
    return SupervisorJob.from_dict(payload)


def job_from_vector(vector: Mapping[str, Any]) -> SupervisorJob:
    payload = dict(FIXTURE["job_template"])
    payload.update(vector)
    return SupervisorJob.from_dict(payload)

def unvalidated_rejected_job_from_vector(
    vector: Mapping[str, Any],
) -> SupervisorJob:
    """Construct a rejected proposed candidate for entry-point validation."""
    payload = dict(FIXTURE["job_template"])
    payload.update(vector)
    checkpoint = payload.get("checkpoint")
    return SupervisorJob(
        schema_version=payload["schema_version"],
        job_id=payload["job_id"],
        workflow_id=payload["workflow_id"],
        state=JobState(payload["state"]),
        desired_state=payload["desired_state"],
        approval_required=payload["approval_required"],
        requested_target=RequestedTarget(payload["requested_target"]),
        owner_agent_id=payload.get("owner_agent_id"),
        lease_expires_at=payload.get("lease_expires_at"),
        generation=payload["generation"],
        attempt=payload["attempt"],
        repo_refs=tuple((item["repo"], item["base"]) for item in payload["repo_refs"]),
        required_capabilities=tuple(payload["required_capabilities"]),
        checkpoint=None if checkpoint is None else dict(checkpoint),
        created_at=payload["created_at"],
        updated_at=payload["updated_at"],
    )


def evidence_from_vector(payload: Mapping[str, Any]) -> TransitionEvidence:
    evidence = dict(payload)
    error_code = evidence.get("failure_error_code")
    if error_code is not None:
        evidence["failure_error_code"] = SupervisorErrorCode(error_code)
    return TransitionEvidence(**evidence)


def vector_ids(vector: Mapping[str, Any]) -> str:
    return str(vector["name"])


def fixture_state_pairs() -> Tuple[Tuple[JobState, JobState, bool], ...]:
    transitions = FIXTURE["allowed_transitions"]
    return tuple(
        (current, proposed, proposed.value in transitions[current.value])
        for current in JobState
        for proposed in JobState
    )


def pair_id(pair: Tuple[JobState, JobState, bool]) -> str:
    current, proposed, allowed = pair
    outcome = "allowed" if allowed else "rejected"
    return "{}_{}_{}".format(current.value.lower(), proposed.value.lower(), outcome)


def transition_pair(
    current_state: JobState, proposed_state: JobState
) -> Tuple[SupervisorJob, SupervisorJob, TransitionEvidence]:
    current = job_fixture(state=current_state)
    proposed = job_fixture(state=proposed_state)
    evidence = TransitionEvidence()

    if proposed_state is JobState.CLAIMED:
        proposed = job_fixture(
            state=JobState.CLAIMED,
            generation=current.generation + 1,
            owner_agent_id=LOCAL_AGENT_ID,
            lease_expires_at=LEASE_AFTER_NOW,
        )

    if current_state is JobState.PAUSED and proposed_state is JobState.CLAIMED:
        checkpoint = checkpoint_fixture(generation=current.generation)
        current = job_fixture(
            state=JobState.PAUSED,
            generation=current.generation,
            checkpoint=checkpoint,
        )
        proposed = job_fixture(
            state=JobState.CLAIMED,
            generation=current.generation + 1,
            owner_agent_id=LOCAL_AGENT_ID,
            lease_expires_at=LEASE_AFTER_NOW,
            checkpoint=checkpoint,
        )
        evidence = TransitionEvidence(
            checkpoint_verified=True,
            execution_stopped=True,
            recovery_origin_matches=True,
        )
    elif current_state is JobState.RUNNING and proposed_state is JobState.PAUSED:
        checkpoint = checkpoint_fixture(generation=current.generation)
        current = job_fixture(
            state=JobState.RUNNING,
            generation=current.generation,
            checkpoint=checkpoint,
        )
        proposed = job_fixture(
            state=JobState.PAUSED,
            generation=current.generation,
            checkpoint=checkpoint,
        )
        evidence = TransitionEvidence(
            checkpoint_verified=True,
            execution_stopped=True,
        )
    elif (
        current_state
        in {JobState.PREPARING, JobState.RUNNING, JobState.WAITING_APPROVAL}
        and proposed_state is JobState.RECOVERY_REQUIRED
    ):
        evidence = TransitionEvidence(execution_stopped=True, cleanup_refused=True)
    elif proposed_state is JobState.FAILED:
        evidence = TransitionEvidence(
            failure_error_code=SupervisorErrorCode.TERMINAL_EXECUTION,
            failure_is_retryable=False,
            execution_stopped=True,
            cleanup_completed=True,
        )
    elif proposed_state is JobState.CANCELLED:
        evidence = TransitionEvidence(
            execution_stopped=True,
            cleanup_completed=True,
        )

    return current, proposed, evidence


def test_fixture_reader_rejects_unsupported_schema_version() -> None:
    unsupported = dict(FIXTURE)
    unsupported["schema_version"] = 2

    with pytest.raises(ValueError, match="unsupported"):
        validate_state_machine_fixture(unsupported)


def test_fixture_records_are_schema_valid_supervisor_jobs() -> None:
    validate_contract("job", FIXTURE["job_template"])
    for vector in FIXTURE["allowed_transition_vectors"]:
        job_from_vector(vector["current"])
        job_from_vector(vector["proposed"])
    for vector in FIXTURE["rejected_transition_vectors"]:
        job_from_vector(vector["current"])


def test_fixture_vectors_match_the_declared_transition_table() -> None:
    transitions = FIXTURE["allowed_transitions"]
    for vector in transition_vectors(FIXTURE):
        current_state = vector["current"]["state"]
        proposed_state = vector["proposed"]["state"]
        table_allows_edge = proposed_state in transitions[current_state]
        if vector["allowed"]:
            assert table_allows_edge


@pytest.mark.parametrize(
    "vector", FIXTURE["allowed_transition_vectors"], ids=vector_ids
)
def test_allowed_fixture_transitions(vector: Mapping[str, Any]) -> None:
    assert_transition(
        current=job_from_vector(vector["current"]),
        proposed=job_from_vector(vector["proposed"]),
        evidence=evidence_from_vector(vector["evidence"]),
        now=FIXTURE["now"],
    )


@pytest.mark.parametrize(
    "vector", FIXTURE["rejected_transition_vectors"], ids=vector_ids
)
def test_rejected_fixture_transitions(vector: Mapping[str, Any]) -> None:
    with pytest.raises(UnsafeTransition):
        assert_transition(
            current=job_from_vector(vector["current"]),
            proposed=unvalidated_rejected_job_from_vector(vector["proposed"]),
            evidence=evidence_from_vector(vector["evidence"]),
            now=FIXTURE["now"],
        )


@pytest.mark.parametrize("pair", fixture_state_pairs(), ids=pair_id)
def test_state_machine_matches_fixture_adjacency(
    pair: Tuple[JobState, JobState, bool],
) -> None:
    current_state, proposed_state, allowed = pair
    current, proposed, evidence = transition_pair(current_state, proposed_state)

    if allowed:
        assert_transition(current, proposed, evidence=evidence, now=NOW)
    else:
        with pytest.raises(UnsafeTransition):
            assert_transition(current, proposed, evidence=evidence, now=NOW)


@pytest.mark.parametrize("state", NON_TERMINAL_STATES)
def test_non_terminal_failure_requires_complete_termination_evidence(
    state: JobState,
) -> None:
    current = job_fixture(state=state)
    proposed = job_fixture(state=JobState.FAILED)
    complete = TransitionEvidence(
        failure_error_code=SupervisorErrorCode.TERMINAL_EXECUTION,
        failure_is_retryable=False,
        execution_stopped=True,
        cleanup_completed=True,
    )

    assert_transition(current, proposed, evidence=complete, now=NOW)

    for incomplete in (
        replace(complete, failure_error_code=None),
        replace(complete, failure_is_retryable=True),
        replace(complete, execution_stopped=False),
        replace(complete, cleanup_completed=False),
    ):
        with pytest.raises(UnsafeTransition):
            assert_transition(current, proposed, evidence=incomplete, now=NOW)


@pytest.mark.parametrize("state", NON_TERMINAL_STATES)
def test_non_terminal_cancellation_requires_stopped_execution_and_cleanup(
    state: JobState,
) -> None:
    current = job_fixture(state=state)
    proposed = job_fixture(state=JobState.CANCELLED)
    complete = TransitionEvidence(execution_stopped=True, cleanup_completed=True)

    assert_transition(current, proposed, evidence=complete, now=NOW)

    for incomplete in (
        replace(complete, execution_stopped=False),
        replace(complete, cleanup_completed=False),
    ):
        with pytest.raises(UnsafeTransition):
            assert_transition(current, proposed, evidence=incomplete, now=NOW)


def test_paused_claim_requires_verified_checkpoint_and_exactly_next_generation() -> None:
    checkpoint = checkpoint_fixture()
    current = job_fixture(
        state=JobState.PAUSED,
        generation=7,
        checkpoint=checkpoint,
    )
    proposed = job_fixture(
        state=JobState.CLAIMED,
        generation=8,
        owner_agent_id=LOCAL_AGENT_ID,
        lease_expires_at=LEASE_AFTER_NOW,
        checkpoint=checkpoint,
    )
    retained = TransitionEvidence(
        checkpoint_verified=True,
        execution_stopped=True,
        recovery_origin_matches=True,
    )

    with pytest.raises(UnsafeTransition, match="generation"):
        assert_transition(
            current,
            replace(proposed, generation=current.generation),
            evidence=retained,
            now=NOW,
        )
    with pytest.raises(UnsafeTransition, match="generation"):
        assert_transition(
            current,
            replace(proposed, generation=current.generation + 2),
            evidence=retained,
            now=NOW,
        )

    assert_transition(current, proposed, evidence=retained, now=NOW)


def test_cross_node_paused_claim_requires_verified_commit_boundary() -> None:
    checkpoint = checkpoint_fixture()
    current = job_fixture(
        state=JobState.PAUSED,
        generation=7,
        checkpoint=checkpoint,
    )
    proposed = job_fixture(
        state=JobState.CLAIMED,
        generation=8,
        requested_target="aws",
        owner_agent_id=AWS_AGENT_ID,
        lease_expires_at=LEASE_AFTER_NOW,
        checkpoint=checkpoint,
    )
    without_commit_boundary = TransitionEvidence(
        checkpoint_verified=True,
        execution_stopped=True,
        recovery_origin_matches=False,
    )

    with pytest.raises(UnsafeTransition, match="commit boundary"):
        assert_transition(
            current,
            proposed,
            evidence=without_commit_boundary,
            now=NOW,
        )

    assert_transition(
        current,
        proposed,
        evidence=replace(without_commit_boundary, commit_boundary_verified=True),
        now=NOW,
    )


def test_running_without_verified_checkpoint_cannot_pause() -> None:
    current = job_fixture(state=JobState.RUNNING)
    proposed = job_fixture(state=JobState.PAUSED)

    with pytest.raises(UnsafeTransition, match="checkpoint"):
        assert_transition(
            current,
            proposed,
            evidence=TransitionEvidence(execution_stopped=True),
            now=NOW,
        )


@pytest.mark.parametrize(
    "state", (JobState.PREPARING, JobState.RUNNING, JobState.WAITING_APPROVAL)
)
def test_recovery_required_requires_stopped_execution_and_explicit_reason(
    state: JobState,
) -> None:
    current = job_fixture(state=state)
    proposed = job_fixture(state=JobState.RECOVERY_REQUIRED)

    assert_transition(
        current,
        proposed,
        evidence=TransitionEvidence(execution_stopped=True, recovery_unsafe=True),
        now=NOW,
    )

    for incomplete in (
        TransitionEvidence(recovery_unsafe=True),
        TransitionEvidence(execution_stopped=True),
    ):
        with pytest.raises(UnsafeTransition):
            assert_transition(current, proposed, evidence=incomplete, now=NOW)


def test_owner_write_rejects_stale_generation_wrong_owner_and_expired_lease() -> None:
    with pytest.raises(LeaseConflict, match="generation"):
        assert_owner_write(
            expected_agent_id=LOCAL_AGENT_ID,
            expected_generation=5,
            actual_agent_id=LOCAL_AGENT_ID,
            actual_generation=4,
            lease_expires_at=LEASE_AFTER_NOW,
            now=NOW,
        )
    with pytest.raises(LeaseConflict, match="owner"):
        assert_owner_write(
            expected_agent_id=LOCAL_AGENT_ID,
            expected_generation=5,
            actual_agent_id=AWS_AGENT_ID,
            actual_generation=5,
            lease_expires_at=LEASE_AFTER_NOW,
            now=NOW,
        )
    with pytest.raises(LeaseConflict, match="lease"):
        assert_owner_write(
            expected_agent_id=LOCAL_AGENT_ID,
            expected_generation=5,
            actual_agent_id=LOCAL_AGENT_ID,
            actual_generation=5,
            lease_expires_at=LEASE_BEFORE_NOW,
            now=NOW,
        )


def test_owner_write_rejects_boolean_generations() -> None:
    with pytest.raises((LeaseConflict, ValueError)):
        assert_owner_write(
            expected_agent_id=LOCAL_AGENT_ID,
            expected_generation=True,
            actual_agent_id=LOCAL_AGENT_ID,
            actual_generation=1,
            lease_expires_at=LEASE_AFTER_NOW,
            now=NOW,
        )
    with pytest.raises((LeaseConflict, ValueError)):
        assert_owner_write(
            expected_agent_id=LOCAL_AGENT_ID,
            expected_generation=1,
            actual_agent_id=LOCAL_AGENT_ID,
            actual_generation=True,
            lease_expires_at=LEASE_AFTER_NOW,
            now=NOW,
        )


def test_transition_rejects_boolean_generation_different_job_and_non_utc_now() -> None:
    current = job_fixture(state=JobState.QUEUED, generation=3)
    proposed = job_fixture(
        state=JobState.CLAIMED,
        generation=4,
        owner_agent_id=LOCAL_AGENT_ID,
        lease_expires_at=LEASE_AFTER_NOW,
    )

    with pytest.raises(ValueError):
        assert_transition(
            current,
            replace(proposed, generation=True),
            evidence=TransitionEvidence(),
            now=NOW,
        )
    with pytest.raises(UnsafeTransition, match="job"):
        assert_transition(
            current,
            replace(proposed, job_id="other-job"),
            evidence=TransitionEvidence(),
            now=NOW,
        )
    with pytest.raises(ValueError, match="UTC"):
        assert_transition(
            current,
            proposed,
            evidence=TransitionEvidence(),
            now="2026-07-30T12:00:00+01:00",
        )

def test_transition_rejects_subclass_that_serializes_a_different_state() -> None:
    class ClaimedJobThatSerializesAsSucceeded(SupervisorJob):
        def to_dict(self) -> Dict[str, Any]:
            payload = super().to_dict()
            payload["state"] = JobState.SUCCEEDED.value
            return payload

    current = job_fixture(state=JobState.QUEUED, generation=3)
    proposed = ClaimedJobThatSerializesAsSucceeded.from_dict(
        job_fixture(
            state=JobState.CLAIMED,
            generation=4,
            owner_agent_id=LOCAL_AGENT_ID,
            lease_expires_at=LEASE_AFTER_NOW,
        ).to_dict()
    )

    with pytest.raises(UnsafeTransition):
        assert_transition(current, proposed, evidence=TransitionEvidence(), now=NOW)


def test_claim_rejects_an_expired_proposed_lease() -> None:
    current = job_fixture(state=JobState.QUEUED, generation=3)
    proposed = job_fixture(
        state=JobState.CLAIMED,
        generation=4,
        owner_agent_id=LOCAL_AGENT_ID,
        lease_expires_at=LEASE_BEFORE_NOW,
    )

    with pytest.raises(UnsafeTransition, match="lease"):
        assert_transition(
            current,
            proposed,
            evidence=TransitionEvidence(),
            now=NOW,
        )


@pytest.mark.parametrize(
    "current_state, proposed_state",
    tuple(
        (state, proposed)
        for state in JobState
        for proposed in FIXTURE["allowed_transitions"][state.value]
        if proposed != JobState.CLAIMED.value
    ),
)
def test_non_ownership_edges_preserve_generation(
    current_state: JobState, proposed_state: str
) -> None:
    current, proposed, evidence = transition_pair(
        current_state,
        JobState(proposed_state),
    )

    assert_transition(current, proposed, evidence=evidence, now=NOW)

    with pytest.raises(UnsafeTransition, match="generation"):
        assert_transition(
            current,
            replace(proposed, generation=current.generation + 1),
            evidence=evidence,
            now=NOW,
        )
