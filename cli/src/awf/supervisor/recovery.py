"""Strict recovery-checkpoint wire contract shared by Supervisor boundaries."""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence, Tuple


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_BASE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,254}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_RECOVERY_KIND = "awf-supervisor-recovery-checkpoint"
_RECOVERY_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "job_id",
        "generation",
        "origin_agent_id",
        "origin_environment",
        "native",
        "worker_descriptors",
        "handles",
        "workspace_manifest_sha256",
        "repos",
        "cross_node_eligible",
    }
)
_NATIVE_FIELDS = frozenset(
    {"batch_fingerprint", "state", "coordinator_session_id"}
)
_WORKER_FIELDS = frozenset({"name", "sha256"})
_HANDLE_FIELDS = frozenset({"task_id", "agent_uri", "history_uri"})
_REPO_FIELDS = frozenset({"repo", "base", "head", "remote_ref", "clean", "pushed"})
_RESUMABLE_NATIVE_STATES = frozenset({"interrupted", "resuming"})


class RecoveryCheckpointError(ValueError):
    """An untrusted checkpoint violates the immutable recovery wire contract."""


def normalize_recovery_checkpoint(
    value: Any,
    *,
    job_id: str,
    checkpoint_generation: int,
    repo_refs: Optional[Sequence[Tuple[str, str]]] = None,
) -> dict[str, Any]:
    """Validate and copy one canonical, resumable recovery checkpoint.

    ``checkpoint_generation`` is the generation at which the checkpoint was
    produced.  Supplying expected repository references additionally enforces
    their exact order, which binds a checkpoint to its job contract.
    """

    _require_identifier(job_id, "expected job_id")
    if type(checkpoint_generation) is not int or checkpoint_generation < 0:
        raise RecoveryCheckpointError("recovery checkpoint generation is invalid")
    if not isinstance(value, Mapping) or set(value) != _RECOVERY_FIELDS:
        raise RecoveryCheckpointError("recovery checkpoint fields are invalid")
    if (
        value["schema_version"] != 1
        or value["kind"] != _RECOVERY_KIND
        or value["job_id"] != job_id
        or type(value["generation"]) is not int
        or value["generation"] != checkpoint_generation
    ):
        raise RecoveryCheckpointError("recovery checkpoint identity is invalid")
    _require_identifier(value["origin_agent_id"], "origin agent identity")
    if value["origin_environment"] not in {"local", "aws"}:
        raise RecoveryCheckpointError("recovery checkpoint origin environment is invalid")
    _require_sha256(value["workspace_manifest_sha256"], "workspace manifest digest")
    if type(value["cross_node_eligible"]) is not bool:
        raise RecoveryCheckpointError("recovery checkpoint cross-node eligibility is invalid")

    native = _normalize_native(value["native"])
    workers = _normalize_workers(value["worker_descriptors"])
    handles = _normalize_handles(value["handles"])
    repos = _normalize_repos(value["repos"], repo_refs=repo_refs)
    return {
        "schema_version": 1,
        "kind": _RECOVERY_KIND,
        "job_id": job_id,
        "generation": checkpoint_generation,
        "origin_agent_id": value["origin_agent_id"],
        "origin_environment": value["origin_environment"],
        "native": native,
        "worker_descriptors": workers,
        "handles": handles,
        "workspace_manifest_sha256": value["workspace_manifest_sha256"],
        "repos": repos,
        "cross_node_eligible": value["cross_node_eligible"],
    }


def _normalize_native(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _NATIVE_FIELDS:
        raise RecoveryCheckpointError("recovery checkpoint native state is invalid")
    _require_sha256(value["batch_fingerprint"], "native batch fingerprint")
    if value["state"] not in _RESUMABLE_NATIVE_STATES:
        raise RecoveryCheckpointError("recovery checkpoint native state is not resumable")
    _require_identifier(value["coordinator_session_id"], "native coordinator session identity")
    return {
        "batch_fingerprint": value["batch_fingerprint"],
        "state": value["state"],
        "coordinator_session_id": value["coordinator_session_id"],
    }


def _normalize_workers(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) != 1:
        raise RecoveryCheckpointError("recovery checkpoint worker descriptor is invalid")
    worker = value[0]
    if not isinstance(worker, Mapping) or set(worker) != _WORKER_FIELDS:
        raise RecoveryCheckpointError("recovery checkpoint worker descriptor is invalid")
    if worker["name"] != "SupervisorJob":
        raise RecoveryCheckpointError("recovery checkpoint worker descriptor is invalid")
    _require_sha256(worker["sha256"], "worker descriptor digest")
    return [{"name": "SupervisorJob", "sha256": worker["sha256"]}]


def _normalize_handles(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _HANDLE_FIELDS:
        raise RecoveryCheckpointError("recovery checkpoint handles are invalid")
    _require_identifier(value["task_id"], "task handle")
    _require_handle(value["agent_uri"], "agent://", "agent handle")
    _require_handle(value["history_uri"], "history://", "history handle")
    return {
        "task_id": value["task_id"],
        "agent_uri": value["agent_uri"],
        "history_uri": value["history_uri"],
    }


def _normalize_repos(
    value: Any, *, repo_refs: Optional[Sequence[Tuple[str, str]]]
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise RecoveryCheckpointError("recovery checkpoint repositories are invalid")
    expected_refs = _normalize_expected_repo_refs(repo_refs)
    if expected_refs is not None and len(value) != len(expected_refs):
        raise RecoveryCheckpointError("recovery checkpoint repository count is invalid")

    repos: list[dict[str, Any]] = []
    seen = set()
    for index, row in enumerate(value):
        if not isinstance(row, Mapping) or set(row) != _REPO_FIELDS:
            raise RecoveryCheckpointError("recovery checkpoint repository is invalid")
        repo = row["repo"]
        base = row["base"]
        if not isinstance(repo, str) or _REPO.fullmatch(repo) is None or repo in seen:
            raise RecoveryCheckpointError("recovery checkpoint repository identity is invalid")
        if not isinstance(base, str) or _BASE.fullmatch(base) is None:
            raise RecoveryCheckpointError("recovery checkpoint repository identity is invalid")
        _require_commit(row["head"], "repository head")
        if row["remote_ref"] != "refs/heads/{}".format(base):
            raise RecoveryCheckpointError("recovery checkpoint repository ref is invalid")
        if type(row["clean"]) is not bool or type(row["pushed"]) is not bool:
            raise RecoveryCheckpointError("recovery checkpoint repository flags are invalid")
        if expected_refs is not None and (repo, base) != expected_refs[index]:
            raise RecoveryCheckpointError("recovery checkpoint repository order is invalid")
        seen.add(repo)
        repos.append(
            {
                "repo": repo,
                "base": base,
                "head": row["head"],
                "remote_ref": row["remote_ref"],
                "clean": row["clean"],
                "pushed": row["pushed"],
            }
        )
    return repos


def _normalize_expected_repo_refs(
    value: Optional[Sequence[Tuple[str, str]]]
) -> Optional[Tuple[Tuple[str, str], ...]]:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise RecoveryCheckpointError("expected recovery repository references are invalid")
    refs = []
    names = set()
    for reference in value:
        if (
            not isinstance(reference, tuple)
            or len(reference) != 2
            or not isinstance(reference[0], str)
            or not isinstance(reference[1], str)
        ):
            raise RecoveryCheckpointError("expected recovery repository reference is invalid")
        repo, base = reference
        if _REPO.fullmatch(repo) is None or _BASE.fullmatch(base) is None or repo in names:
            raise RecoveryCheckpointError("expected recovery repository reference is invalid")
        names.add(repo)
        refs.append((repo, base))
    return tuple(refs)


def _require_identifier(value: Any, field: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise RecoveryCheckpointError("recovery checkpoint {} is invalid".format(field))


def _require_sha256(value: Any, field: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise RecoveryCheckpointError("recovery checkpoint {} is invalid".format(field))


def _require_commit(value: Any, field: str) -> None:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise RecoveryCheckpointError("recovery checkpoint {} is invalid".format(field))


def _require_handle(value: Any, prefix: str, field: str) -> None:
    if not isinstance(value, str) or not value.startswith(prefix):
        raise RecoveryCheckpointError("recovery checkpoint {} is invalid".format(field))
    _require_identifier(value[len(prefix) :], field)


__all__ = ["RecoveryCheckpointError", "normalize_recovery_checkpoint"]
