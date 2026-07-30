"""Fail-closed Supervisor job state transitions and owner-write fencing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, FrozenSet, Optional

from awf.supervisor.contracts import JobState, SupervisorErrorCode, SupervisorJob, validate_contract


_ALLOWED: Dict[JobState, FrozenSet[JobState]] = {
    JobState.QUEUED: frozenset(
        {JobState.CLAIMED, JobState.BLOCKED, JobState.CANCELLED, JobState.FAILED}
    ),
    JobState.CLAIMED: frozenset(
        {JobState.PREPARING, JobState.QUEUED, JobState.CANCELLED, JobState.FAILED}
    ),
    JobState.PREPARING: frozenset(
        {
            JobState.RUNNING,
            JobState.RECOVERY_REQUIRED,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.RUNNING: frozenset(
        {
            JobState.WAITING_APPROVAL,
            JobState.PAUSED,
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.STALE,
            JobState.RECOVERY_REQUIRED,
        }
    ),
    JobState.WAITING_APPROVAL: frozenset(
        {
            JobState.RUNNING,
            JobState.RECOVERY_REQUIRED,
            JobState.CANCELLED,
            JobState.FAILED,
        }
    ),
    JobState.PAUSED: frozenset(
        {JobState.CLAIMED, JobState.CANCELLED, JobState.FAILED}
    ),
    JobState.BLOCKED: frozenset(
        {JobState.QUEUED, JobState.CANCELLED, JobState.FAILED}
    ),
    JobState.STALE: frozenset(
        {
            JobState.PAUSED,
            JobState.RECOVERY_REQUIRED,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.RECOVERY_REQUIRED: frozenset(
        {JobState.PAUSED, JobState.FAILED, JobState.CANCELLED}
    ),
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
}


_RFC3339_UTC = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)
_CHECKPOINT_GENERATION = re.compile(
    r"^s3://[^/]+/artifacts/checkpoints/[^/]+/(?P<generation>[0-9]+)/[0-9a-f]{64}\.json$"
)
_OWNER_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class UnsafeTransition(ValueError):
    """Raised when a proposed Supervisor job transition lacks required proof."""


class LeaseConflict(UnsafeTransition):
    """Raised when an owner write no longer holds the job's current lease."""


@dataclass(frozen=True)
class TransitionEvidence:
    """Explicit durable proofs required by exceptional Supervisor transitions."""

    checkpoint_verified: bool = False
    execution_stopped: bool = False
    cleanup_completed: bool = False
    failure_error_code: Optional[SupervisorErrorCode] = None
    cleanup_refused: bool = False
    recovery_unsafe: bool = False
    recovery_origin_matches: Optional[bool] = None
    commit_boundary_verified: bool = False
    failure_is_retryable: Optional[bool] = None


def _parse_utc_rfc3339(value: str, *, field: str) -> datetime:
    """Return an aware UTC datetime from a canonical RFC 3339 UTC string."""

    if type(value) is not str or _RFC3339_UTC.fullmatch(value) is None:
        raise ValueError("{} must be an aware UTC RFC3339 timestamp".format(field))

    normalized = "{}+00:00".format(value[:-1]) if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("{} must be an aware UTC RFC3339 timestamp".format(field)) from error

    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("{} must be an aware UTC RFC3339 timestamp".format(field))
    return parsed.astimezone(timezone.utc)


def _require_evidence_types(evidence: TransitionEvidence) -> None:
    if not isinstance(evidence, TransitionEvidence):
        raise ValueError("evidence must be TransitionEvidence")

    for field in (
        "checkpoint_verified",
        "execution_stopped",
        "cleanup_completed",
        "cleanup_refused",
        "recovery_unsafe",
        "commit_boundary_verified",
    ):
        if type(getattr(evidence, field)) is not bool:
            raise UnsafeTransition("{} evidence must be boolean".format(field))

    for field in ("recovery_origin_matches", "failure_is_retryable"):
        value = getattr(evidence, field)
        if value is not None and type(value) is not bool:
            raise UnsafeTransition("{} evidence must be boolean or null".format(field))

    if (
        evidence.failure_error_code is not None
        and not isinstance(evidence.failure_error_code, SupervisorErrorCode)
    ):
        raise UnsafeTransition("failure_error_code must be a SupervisorErrorCode")


def _validate_job(job: SupervisorJob, *, role: str) -> None:
    if type(job) is not SupervisorJob:
        raise UnsafeTransition("{} job must be exactly SupervisorJob".format(role))
    payload = job.to_dict()
    try:
        validate_contract("job", payload)
    except ValueError as error:
        raise UnsafeTransition(
            "{} job fails public contract validation: {}".format(role, error)
        ) from error


def _require_immutable_identity(
    current: SupervisorJob, proposed: SupervisorJob
) -> None:
    if current.job_id != proposed.job_id:
        raise UnsafeTransition("job ID must not change during a transition")

    current_identity = (
        current.schema_version,
        current.workflow_id,
        current.desired_state,
        current.approval_required,
        current.repo_refs,
        current.required_capabilities,
        current.created_at,
    )
    proposed_identity = (
        proposed.schema_version,
        proposed.workflow_id,
        proposed.desired_state,
        proposed.approval_required,
        proposed.repo_refs,
        proposed.required_capabilities,
        proposed.created_at,
    )
    if current_identity != proposed_identity:
        raise UnsafeTransition("immutable job identity must not change during a transition")


def _checkpoint_generation(job: SupervisorJob) -> Optional[int]:
    checkpoint = job.checkpoint
    if checkpoint is None:
        return None
    match = _CHECKPOINT_GENERATION.fullmatch(checkpoint["artifact_uri"])
    if match is None:
        return None
    return int(match["generation"])


def _require_next_generation(current: SupervisorJob, proposed: SupervisorJob) -> None:
    if proposed.generation != current.generation + 1:
        raise UnsafeTransition("ownership acquisition requires exactly next generation")


def _require_claim_lease(proposed: SupervisorJob, now: datetime) -> None:
    if proposed.owner_agent_id is None:
        raise UnsafeTransition("ownership acquisition requires an owner")
    if proposed.lease_expires_at is None:
        raise UnsafeTransition("ownership acquisition requires a lease")

    try:
        lease_expires_at = _parse_utc_rfc3339(
            proposed.lease_expires_at, field="proposed lease"
        )
    except ValueError as error:
        raise UnsafeTransition(str(error)) from error
    if lease_expires_at <= now:
        raise UnsafeTransition("ownership acquisition lease must be after now")


def _require_paused_claim_proofs(
    current: SupervisorJob, proposed: SupervisorJob, evidence: TransitionEvidence
) -> None:
    if not evidence.checkpoint_verified:
        raise UnsafeTransition("paused claim requires a verified checkpoint")
    if not evidence.execution_stopped:
        raise UnsafeTransition("paused claim requires stopped execution")
    if current.checkpoint is None or proposed.checkpoint is None:
        raise UnsafeTransition("paused claim requires a retained checkpoint")

    current_checkpoint = current.checkpoint
    proposed_checkpoint = proposed.checkpoint
    if (
        current_checkpoint["artifact_uri"] != proposed_checkpoint["artifact_uri"]
        or current_checkpoint["sha256"] != proposed_checkpoint["sha256"]
    ):
        raise UnsafeTransition("paused claim must preserve checkpoint URI and digest")
    if _checkpoint_generation(current) != current.generation:
        raise UnsafeTransition("paused claim checkpoint must retain the prior generation")
    if _checkpoint_generation(proposed) != current.generation:
        raise UnsafeTransition("paused claim checkpoint must retain the prior generation")

    if evidence.recovery_origin_matches is True:
        if evidence.commit_boundary_verified:
            raise UnsafeTransition("paused claim must use exactly one recovery branch")
        return
    if evidence.recovery_origin_matches is False and evidence.commit_boundary_verified:
        return
    if evidence.recovery_origin_matches is False:
        raise UnsafeTransition("cross-node paused claim requires a commit boundary")
    raise UnsafeTransition("paused claim requires an explicit recovery origin")


def _require_running_pause_proofs(
    proposed: SupervisorJob, evidence: TransitionEvidence
) -> None:
    if not evidence.checkpoint_verified or proposed.checkpoint is None:
        raise UnsafeTransition("running pause requires a verified checkpoint")
    if not evidence.execution_stopped:
        raise UnsafeTransition("running pause requires stopped execution")
    if _checkpoint_generation(proposed) != proposed.generation:
        raise UnsafeTransition("running pause checkpoint must match the generation")


def _require_recovery_required_proofs(
    current: SupervisorJob, proposed: SupervisorJob, evidence: TransitionEvidence
) -> None:
    if not evidence.execution_stopped:
        raise UnsafeTransition("recovery-required transition requires stopped execution")
    if not (evidence.cleanup_refused or evidence.recovery_unsafe):
        raise UnsafeTransition("recovery-required transition requires explicit recovery evidence")
    if (
        current.owner_agent_id != proposed.owner_agent_id
        or current.lease_expires_at != proposed.lease_expires_at
    ):
        raise UnsafeTransition("recovery-required transition must preserve owner and lease")


def _require_failed_proofs(evidence: TransitionEvidence) -> None:
    if evidence.failure_error_code is None:
        raise UnsafeTransition("failed transition requires a non-transient error code")
    if evidence.failure_error_code is SupervisorErrorCode.TRANSIENT:
        raise UnsafeTransition("failed transition rejects transient error codes")
    if evidence.failure_is_retryable is not False:
        raise UnsafeTransition("failed transition requires a non-retryable error")
    if not evidence.execution_stopped:
        raise UnsafeTransition("failed transition requires stopped execution")
    if not evidence.cleanup_completed:
        raise UnsafeTransition("failed transition requires completed cleanup")


def _require_cancelled_proofs(evidence: TransitionEvidence) -> None:
    if not evidence.execution_stopped:
        raise UnsafeTransition("cancelled transition requires stopped execution")
    if not evidence.cleanup_completed:
        raise UnsafeTransition("cancelled transition requires completed cleanup")
    if evidence.failure_error_code is not None or evidence.failure_is_retryable is not None:
        raise UnsafeTransition("cancelled transition must not carry failure evidence")


def assert_owner_write(
    *,
    expected_agent_id: str,
    expected_generation: int,
    actual_agent_id: str,
    actual_generation: int,
    lease_expires_at: str,
    now: str,
) -> None:
    """Fence an owner write against the currently persisted owner and lease."""

    if type(expected_generation) is not int or type(actual_generation) is not int:
        raise LeaseConflict("owner write generation must be an integer")
    if expected_generation != actual_generation:
        raise LeaseConflict("owner write generation does not match")
    if (
        type(expected_agent_id) is not str
        or type(actual_agent_id) is not str
        or _OWNER_AGENT_ID.fullmatch(expected_agent_id) is None
        or _OWNER_AGENT_ID.fullmatch(actual_agent_id) is None
    ):
        raise LeaseConflict("owner write has an invalid owner")
    if expected_agent_id != actual_agent_id:
        raise LeaseConflict("owner write owner does not match")

    try:
        current_time = _parse_utc_rfc3339(now, field="now")
        lease_time = _parse_utc_rfc3339(lease_expires_at, field="lease")
    except ValueError as error:
        raise LeaseConflict(str(error)) from error
    if lease_time <= current_time:
        raise LeaseConflict("owner write lease has expired")


def assert_transition(
    current: SupervisorJob,
    proposed: SupervisorJob,
    *,
    evidence: TransitionEvidence,
    now: str,
) -> None:
    """Validate one fenced state transition without mutating either job."""

    _validate_job(current, role="current")
    _validate_job(proposed, role="proposed")
    _require_evidence_types(evidence)
    current_time = _parse_utc_rfc3339(now, field="now")
    _require_immutable_identity(current, proposed)

    if proposed.state not in _ALLOWED[current.state]:
        raise UnsafeTransition(
            "{} cannot transition to {}".format(current.state.value, proposed.state.value)
        )

    if proposed.state is JobState.CLAIMED:
        _require_next_generation(current, proposed)
        _require_claim_lease(proposed, current_time)
        if current.state is JobState.PAUSED:
            _require_paused_claim_proofs(current, proposed, evidence)
    elif proposed.generation != current.generation:
        raise UnsafeTransition("non-ownership transition must preserve generation")

    if current.state is JobState.RUNNING and proposed.state is JobState.PAUSED:
        _require_running_pause_proofs(proposed, evidence)

    if (
        current.state
        in {JobState.PREPARING, JobState.RUNNING, JobState.WAITING_APPROVAL}
        and proposed.state is JobState.RECOVERY_REQUIRED
    ):
        _require_recovery_required_proofs(current, proposed, evidence)

    if proposed.state is JobState.FAILED:
        _require_failed_proofs(evidence)
    elif proposed.state is JobState.CANCELLED:
        _require_cancelled_proofs(evidence)


__all__ = [
    "LeaseConflict",
    "TransitionEvidence",
    "UnsafeTransition",
    "assert_owner_write",
    "assert_transition",
]
