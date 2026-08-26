"""Safe registration of completed Autoresearch evidence.

This module deliberately does not execute an optimisation loop.  It accepts a
small, strictly validated result envelope that an external worker (such as OMP)
has already completed, binds it to the currently approved implementation plan,
and atomically records only provenance.  Metrics remain at their verified
repository-relative path and are never copied into workflow artifacts.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
from typing import Iterator, Literal, Optional, Union

from awf.core import state as workflow_state
from awf.core.approval import (
    ApprovalError,
    ApprovedPlanningSeal,
    validate_approved_planning_seal,
)


AUTORESEARCH_SCHEMA_VERSION = 1
AUTORESEARCH_ARTIFACT_RELATIVE_PATH = ".workflow/artifacts/autoresearch-run.json"

EXIT_SUCCESS = 0
EXIT_BLOCKED = 2
EXIT_REPLAN_REQUIRED = 3

_MAX_ENVELOPE_BYTES = 64 * 1024
_MAX_STATE_BYTES = 256 * 1024
_MAX_ALLOWED_FILES_BYTES = 256 * 1024
_MAX_ARTIFACT_BYTES = 64 * 1024
_MAX_METRICS_BYTES = 8 * 1024 * 1024
_MAX_RUN_ID_BYTES = 128
_MAX_SCORE_NAME_BYTES = 64
_MAX_CANDIDATE_REF_BYTES = 256
_MAX_PATH_BYTES = 1024
_MAX_CHANGED_FILES = 512

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
_SCORE_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")

_PAYLOAD_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "goal_digest",
        "score_name",
        "score_direction",
        "baseline_score",
        "final_score",
        "kept_candidate_ref",
        "planning_options_hash",
        "scope_hash",
        "metrics_path",
        "metrics_hash",
        "completed_at",
        "changed_files",
    }
)

# This is intentionally a small public description of the on-disk envelope.
# The validator below additionally rejects duplicate JSON keys and non-finite
# Python numbers, which JSON Schema alone cannot reliably enforce.
AUTORESEARCH_RUN_JSON_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": sorted(_PAYLOAD_FIELDS),
    "properties": {
        "schema_version": {"const": AUTORESEARCH_SCHEMA_VERSION},
        "run_id": {"type": "string", "minLength": 1, "maxLength": _MAX_RUN_ID_BYTES},
        "goal_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "score_name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$", "maxLength": _MAX_SCORE_NAME_BYTES},
        "score_direction": {"enum": ["maximize", "minimize"]},
        "baseline_score": {"type": "number"},
        "final_score": {"type": "number"},
        "kept_candidate_ref": {"type": "string", "minLength": 1, "maxLength": _MAX_CANDIDATE_REF_BYTES},
        "planning_options_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "scope_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "metrics_path": {"type": "string", "minLength": 1, "maxLength": _MAX_PATH_BYTES},
        "metrics_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "completed_at": {"type": "string", "format": "date-time"},
        "changed_files": {
            "type": "array",
            "minItems": 1,
            "maxItems": _MAX_CHANGED_FILES,
            "items": {"type": "string", "minLength": 1, "maxLength": _MAX_PATH_BYTES},
            "uniqueItems": True,
        },
    },
}


class AutoresearchValidationError(ValueError):
    """Stable validation failure that never includes untrusted content."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AutoresearchRun:
    """Normalized, validated provenance for one completed Autoresearch run."""

    schema_version: int
    run_id: str
    goal_digest: str
    score_name: str
    score_direction: Literal["maximize", "minimize"]
    baseline_score: Union[int, float]
    final_score: Union[int, float]
    kept_candidate_ref: str
    planning_options_hash: str
    scope_hash: str
    metrics_path: str
    metrics_hash: str
    completed_at: str
    changed_files: tuple[str, ...]


@dataclass(frozen=True)
class AutoresearchRegistrationResult:
    """Deterministic registration outcome for a thin CLI adapter."""

    status: Literal["written", "reuse", "blocked", "replan_required"]
    exit_code: int
    reason: str
    artifact_path: Optional[str]


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate JSON key")
        payload[key] = value
    return payload


def _canonical_repo_root(repo_root: Path) -> Path:
    try:
        supplied = Path(repo_root)
        metadata = os.lstat(supplied)
        if stat.S_ISLNK(metadata.st_mode):
            raise AutoresearchValidationError("repo_root_invalid")
        root = supplied.resolve(strict=True)
        metadata = os.lstat(root)
    except AutoresearchValidationError:
        raise
    except OSError:
        raise AutoresearchValidationError("repo_root_invalid") from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise AutoresearchValidationError("repo_root_invalid")
    return root


def _bounded_string(value: object, maximum_bytes: int) -> str:
    if not isinstance(value, str):
        raise AutoresearchValidationError("payload_invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise AutoresearchValidationError("payload_invalid") from None
    if not value or len(encoded) > maximum_bytes or "\x00" in value:
        raise AutoresearchValidationError("payload_invalid")
    return value


def _parse_hash(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise AutoresearchValidationError("payload_invalid")
    return value


def _parse_score(value: object) -> Union[int, float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AutoresearchValidationError("payload_invalid")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise AutoresearchValidationError("payload_invalid")
    return value


def _parse_completed_at(value: object) -> str:
    timestamp = _bounded_string(value, 32)
    if _TIMESTAMP.fullmatch(timestamp) is None:
        raise AutoresearchValidationError("payload_invalid")
    try:
        parsed = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError:
        raise AutoresearchValidationError("payload_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise AutoresearchValidationError("payload_invalid")
    canonical_seconds = parsed.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    canonical_microseconds = parsed.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")
    if timestamp not in {canonical_seconds, canonical_microseconds}:
        raise AutoresearchValidationError("payload_invalid")
    return timestamp


def _parse_repo_relative_path(value: object) -> str:
    path = _bounded_string(value, _MAX_PATH_BYTES)
    if "\\" in path:
        raise AutoresearchValidationError("payload_invalid")
    candidate = PurePosixPath(path)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or candidate.as_posix() != path
    ):
        raise AutoresearchValidationError("payload_invalid")
    return path


def _parse_changed_files(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > _MAX_CHANGED_FILES:
        raise AutoresearchValidationError("payload_invalid")
    files = tuple(_parse_repo_relative_path(item) for item in value)
    if len(set(files)) != len(files):
        raise AutoresearchValidationError("payload_invalid")
    return tuple(sorted(files))


def _parse_raw_payload(payload: object) -> dict[str, object]:
    if isinstance(payload, bytes):
        if len(payload) > _MAX_ENVELOPE_BYTES:
            raise AutoresearchValidationError("payload_invalid")
        try:
            decoded = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise AutoresearchValidationError("payload_invalid") from None
    elif isinstance(payload, str):
        try:
            if len(payload.encode("utf-8")) > _MAX_ENVELOPE_BYTES:
                raise AutoresearchValidationError("payload_invalid")
        except UnicodeEncodeError:
            raise AutoresearchValidationError("payload_invalid") from None
        decoded = payload
    elif isinstance(payload, dict):
        return payload
    else:
        raise AutoresearchValidationError("payload_invalid")
    try:
        parsed = json.loads(decoded, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise AutoresearchValidationError("payload_invalid") from None
    if not isinstance(parsed, dict):
        raise AutoresearchValidationError("payload_invalid")
    return parsed


def validate_autoresearch_run(payload: object) -> AutoresearchRun:
    """Validate an exact result envelope without reading the repository."""

    raw = _parse_raw_payload(payload)
    if set(raw) != _PAYLOAD_FIELDS:
        raise AutoresearchValidationError("payload_invalid")
    schema_version = raw["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != AUTORESEARCH_SCHEMA_VERSION
    ):
        raise AutoresearchValidationError("payload_invalid")

    run_id = _bounded_string(raw["run_id"], _MAX_RUN_ID_BYTES)
    score_name = _bounded_string(raw["score_name"], _MAX_SCORE_NAME_BYTES)
    kept_candidate_ref = _bounded_string(
        raw["kept_candidate_ref"], _MAX_CANDIDATE_REF_BYTES
    )
    if (
        _IDENTIFIER.fullmatch(run_id) is None
        or _SCORE_NAME.fullmatch(score_name) is None
        or _IDENTIFIER.fullmatch(kept_candidate_ref) is None
    ):
        raise AutoresearchValidationError("payload_invalid")

    score_direction = raw["score_direction"]
    if score_direction not in {"maximize", "minimize"}:
        raise AutoresearchValidationError("payload_invalid")

    run = AutoresearchRun(
        schema_version=schema_version,
        run_id=run_id,
        goal_digest=_parse_hash(raw["goal_digest"]),
        score_name=score_name,
        score_direction=score_direction,
        baseline_score=_parse_score(raw["baseline_score"]),
        final_score=_parse_score(raw["final_score"]),
        kept_candidate_ref=kept_candidate_ref,
        planning_options_hash=_parse_hash(raw["planning_options_hash"]),
        scope_hash=_parse_hash(raw["scope_hash"]),
        metrics_path=_parse_repo_relative_path(raw["metrics_path"]),
        metrics_hash=_parse_hash(raw["metrics_hash"]),
        completed_at=_parse_completed_at(raw["completed_at"]),
        changed_files=_parse_changed_files(raw["changed_files"]),
    )
    if len(_canonical_json_bytes(_run_payload(run))) > _MAX_ENVELOPE_BYTES:
        raise AutoresearchValidationError("payload_invalid")
    return run


def _run_payload(run: AutoresearchRun) -> dict[str, object]:
    return {
        "schema_version": run.schema_version,
        "run_id": run.run_id,
        "goal_digest": run.goal_digest,
        "score_name": run.score_name,
        "score_direction": run.score_direction,
        "baseline_score": run.baseline_score,
        "final_score": run.final_score,
        "kept_candidate_ref": run.kept_candidate_ref,
        "planning_options_hash": run.planning_options_hash,
        "scope_hash": run.scope_hash,
        "metrics_path": run.metrics_path,
        "metrics_hash": run.metrics_hash,
        "completed_at": run.completed_at,
        "changed_files": list(run.changed_files),
    }


def _open_child_directory(parent_fd: int, name: str) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        metadata = os.lstat(name, dir_fd=parent_fd)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise AutoresearchValidationError("workflow_layout_invalid")
        return os.open(name, flags, dir_fd=parent_fd)
    except AutoresearchValidationError:
        raise
    except OSError:
        raise AutoresearchValidationError("workflow_layout_invalid") from None


@contextmanager
def _artifact_transaction(root: Path) -> Iterator[int]:
    root_fd: Optional[int] = None
    workflow_fd: Optional[int] = None
    artifacts_fd: Optional[int] = None
    lock_fd: Optional[int] = None
    locked = False
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        try:
            root_fd = os.open(root, directory_flags)
            workflow_fd = _open_child_directory(root_fd, ".workflow")
            artifacts_fd = _open_child_directory(workflow_fd, "artifacts")
            lock_fd = os.open(
                ".autoresearch-run.json.lock",
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=artifacts_fd,
            )
            lock_metadata = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_metadata.st_mode) or lock_metadata.st_nlink != 1:
                raise AutoresearchValidationError("workflow_layout_invalid")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            locked = True
        except AutoresearchValidationError:
            raise
        except OSError:
            raise AutoresearchValidationError("workflow_layout_invalid") from None
        yield artifacts_fd
    finally:
        if lock_fd is not None:
            try:
                if locked:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        if artifacts_fd is not None:
            os.close(artifacts_fd)
        if workflow_fd is not None:
            os.close(workflow_fd)
        if root_fd is not None:
            os.close(root_fd)


def _read_bounded_regular_file(
    root: Path,
    path: str,
    *,
    maximum_bytes: int,
    missing_code: str,
    invalid_code: str,
) -> bytes:
    parts = PurePosixPath(path).parts
    root_fd: Optional[int] = None
    directory_fd: Optional[int] = None
    file_fd: Optional[int] = None
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_fd = os.open(root, directory_flags)
        directory_fd = root_fd
        for part in parts[:-1]:
            try:
                metadata = os.lstat(part, dir_fd=directory_fd)
            except FileNotFoundError:
                raise AutoresearchValidationError(missing_code) from None
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise AutoresearchValidationError(invalid_code)
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            if directory_fd != root_fd:
                os.close(directory_fd)
            directory_fd = next_fd
        try:
            metadata = os.lstat(parts[-1], dir_fd=directory_fd)
        except FileNotFoundError:
            raise AutoresearchValidationError(missing_code) from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise AutoresearchValidationError(invalid_code)
        file_fd = os.open(
            parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd
        )
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise AutoresearchValidationError(invalid_code)
        raw = bytearray()
        while len(raw) <= maximum_bytes:
            try:
                chunk = os.read(file_fd, maximum_bytes + 1 - len(raw))
            except OSError as error:
                if error.errno == errno.EINTR:
                    continue
                raise AutoresearchValidationError(invalid_code) from None
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > maximum_bytes:
                raise AutoresearchValidationError(invalid_code)
        return bytes(raw)
    except AutoresearchValidationError:
        raise
    except OSError:
        raise AutoresearchValidationError(invalid_code) from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None and directory_fd != root_fd:
            os.close(directory_fd)
        if root_fd is not None:
            os.close(root_fd)


def _validate_existing_path(root: Path, path: str) -> None:
    """Reject symlinks and non-file entries while allowing new/deleted files."""

    parts = PurePosixPath(path).parts
    root_fd: Optional[int] = None
    directory_fd: Optional[int] = None
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_fd = os.open(root, directory_flags)
        directory_fd = root_fd
        for index, part in enumerate(parts):
            try:
                metadata = os.lstat(part, dir_fd=directory_fd)
            except FileNotFoundError:
                return
            if stat.S_ISLNK(metadata.st_mode):
                raise AutoresearchValidationError("unsafe_path")
            if index == len(parts) - 1:
                if not stat.S_ISREG(metadata.st_mode):
                    raise AutoresearchValidationError("unsafe_path")
                return
            if not stat.S_ISDIR(metadata.st_mode):
                raise AutoresearchValidationError("unsafe_path")
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            if directory_fd != root_fd:
                os.close(directory_fd)
            directory_fd = next_fd
    except AutoresearchValidationError:
        raise
    except OSError:
        raise AutoresearchValidationError("unsafe_path") from None
    finally:
        if directory_fd is not None and directory_fd != root_fd:
            os.close(directory_fd)
        if root_fd is not None:
            os.close(root_fd)


def _read_json_file(
    root: Path,
    path: str,
    *,
    maximum_bytes: int,
    missing_code: str,
    invalid_code: str,
) -> dict[str, object]:
    raw = _read_bounded_regular_file(
        root,
        path,
        maximum_bytes=maximum_bytes,
        missing_code=missing_code,
        invalid_code=invalid_code,
    )
    try:
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise AutoresearchValidationError(invalid_code) from None
    if not isinstance(payload, dict):
        raise AutoresearchValidationError(invalid_code)
    return payload


def _validate_workflow_preconditions(
    root: Path,
    run: AutoresearchRun,
    *,
    state: dict[str, object] | None = None,
) -> ApprovedPlanningSeal:
    if state is None:
        state = _read_json_file(
            root,
            ".workflow/state.json",
            maximum_bytes=_MAX_STATE_BYTES,
            missing_code="workflow_state_missing",
            invalid_code="workflow_state_invalid",
        )
    gates = state.get("gates")
    g3 = gates.get("G3") if isinstance(gates, dict) else None
    if not isinstance(g3, dict) or g3.get("passed") is not True:
        raise AutoresearchValidationError("g3_not_passed")
    if state.get("currentPhase") != "impl":
        raise AutoresearchValidationError("phase_not_impl")

    try:
        approved = validate_approved_planning_seal(root, state=state)
    except ApprovalError as error:
        if error.code == "approval_seal_changed":
            raise AutoresearchValidationError("planning_identity_changed") from None
        raise AutoresearchValidationError("planning_identity_unavailable") from None
    if run.scope_hash != approved.scope_hash:
        raise AutoresearchValidationError("scope_identity_changed")

    from awf.core.planning_options import PlanningOptionsError, load_planning_options

    try:
        planning_options = load_planning_options(root)
    except PlanningOptionsError:
        raise AutoresearchValidationError("planning_identity_unavailable") from None
    if planning_options.status not in {"selected", "no_decision_required"}:
        raise AutoresearchValidationError("planning_identity_unavailable")
    if run.planning_options_hash != planning_options.artifact_hash:
        raise AutoresearchValidationError("planning_identity_changed")
    if (
        approved.plan_provenance.get("status") == "sealed"
        and approved.plan_provenance.get("planning_options_hash")
        != planning_options.artifact_hash
    ):
        raise AutoresearchValidationError("planning_identity_changed")
    return approved


def _load_allowed_files(
    root: Path,
    approved: ApprovedPlanningSeal,
) -> frozenset[str]:
    raw = _read_bounded_regular_file(
        root,
        ".workflow/artifacts/allowed-files.json",
        maximum_bytes=_MAX_ALLOWED_FILES_BYTES,
        missing_code="allowed_files_missing",
        invalid_code="allowed_files_invalid",
    )
    expected = approved.planning_seal["artifacts"].get("allowed-files.json")
    if not isinstance(expected, str) or hashlib.sha256(raw).hexdigest() != expected:
        raise AutoresearchValidationError("planning_identity_changed")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise AutoresearchValidationError("allowed_files_invalid") from None
    if not isinstance(payload, dict):
        raise AutoresearchValidationError("allowed_files_invalid")
    planned = payload.get("planned_files")
    expanded = payload.get("expanded_files", [])
    if not isinstance(planned, list) or not isinstance(expanded, list):
        raise AutoresearchValidationError("allowed_files_invalid")
    paths = tuple(_parse_repo_relative_path(item) for item in [*planned, *expanded])
    if len(paths) != len(set(paths)):
        raise AutoresearchValidationError("allowed_files_invalid")
    for path in paths:
        _validate_existing_path(root, path)
    return frozenset(paths)


def _validate_evidence(
    root: Path,
    run: AutoresearchRun,
    approved: ApprovedPlanningSeal,
) -> None:
    for path in run.changed_files:
        _validate_existing_path(root, path)
    allowed_files = _load_allowed_files(root, approved)
    if not set(run.changed_files) <= allowed_files:
        raise AutoresearchValidationError("changed_files_out_of_scope")
    metrics = _read_bounded_regular_file(
        root,
        run.metrics_path,
        maximum_bytes=_MAX_METRICS_BYTES,
        missing_code="metrics_missing",
        invalid_code="metrics_invalid",
    )
    if hashlib.sha256(metrics).hexdigest() != run.metrics_hash:
        raise AutoresearchValidationError("metrics_hash_mismatch")


def _read_existing_artifact(artifacts_fd: int) -> Optional[AutoresearchRun]:
    descriptor: Optional[int] = None
    try:
        try:
            metadata = os.lstat("autoresearch-run.json", dir_fd=artifacts_fd)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise AutoresearchValidationError("artifact_invalid")
        descriptor = os.open(
            "autoresearch-run.json",
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=artifacts_fd,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise AutoresearchValidationError("artifact_invalid")
        raw = bytearray()
        while len(raw) <= _MAX_ARTIFACT_BYTES:
            try:
                chunk = os.read(descriptor, _MAX_ARTIFACT_BYTES + 1 - len(raw))
            except OSError as error:
                if error.errno == errno.EINTR:
                    continue
                raise AutoresearchValidationError("artifact_invalid") from None
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > _MAX_ARTIFACT_BYTES:
                raise AutoresearchValidationError("artifact_invalid")
    except AutoresearchValidationError:
        raise
    except OSError:
        raise AutoresearchValidationError("artifact_invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        return validate_autoresearch_run(bytes(raw))
    except AutoresearchValidationError:
        raise AutoresearchValidationError("artifact_invalid") from None


def _write_artifact(artifacts_fd: int, run: AutoresearchRun) -> None:
    temporary_name = f".autoresearch-run.json.{secrets.token_hex(16)}.tmp"
    descriptor: Optional[int] = None
    created = False
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=artifacts_fd,
        )
        created = True
        raw = _canonical_json_bytes(_run_payload(run)) + b"\n"
        remaining = memoryview(raw)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("artifact write failed")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            "autoresearch-run.json",
            src_dir_fd=artifacts_fd,
            dst_dir_fd=artifacts_fd,
        )
        os.fsync(artifacts_fd)
    except OSError:
        raise AutoresearchValidationError("artifact_write_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                os.unlink(temporary_name, dir_fd=artifacts_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _result(
    status: Literal["written", "reuse", "blocked", "replan_required"],
    reason: str,
) -> AutoresearchRegistrationResult:
    exit_code = (
        EXIT_SUCCESS
        if status in {"written", "reuse"}
        else EXIT_REPLAN_REQUIRED
        if status == "replan_required"
        else EXIT_BLOCKED
    )
    return AutoresearchRegistrationResult(
        status=status,
        exit_code=exit_code,
        reason=reason,
        artifact_path=(
            AUTORESEARCH_ARTIFACT_RELATIVE_PATH
            if status in {"written", "reuse"}
            else None
        ),
    )


def register_autoresearch_run(
    repo_root: Path,
    payload: object,
) -> AutoresearchRegistrationResult:
    """Validate and atomically register a completed Autoresearch result.

    ``payload`` may be a decoded JSON object or raw UTF-8 JSON.  The function
    has no state-transition side effects: it only writes the immutable evidence
    artifact after the current G3, implementation phase, selected planning
    identity, scope identity, allowed-file scope, and metrics hash all validate.
    """

    try:
        run = validate_autoresearch_run(payload)
        root = _canonical_repo_root(repo_root)
        state_path = root / ".workflow" / "state.json"
        with workflow_state._workflow_state_lock(
            state_path
        ), workflow_state._workflow_state_transaction(root) as state_fd:
            state = workflow_state._read_workflow_state_from_directory(state_fd)
            with _artifact_transaction(root) as artifacts_fd:
                approved = _validate_workflow_preconditions(root, run, state=state)
                _validate_evidence(root, run, approved)
                existing = _read_existing_artifact(artifacts_fd)
                if existing is not None:
                    if _canonical_json_bytes(_run_payload(existing)) == _canonical_json_bytes(
                        _run_payload(run)
                    ):
                        return _result("reuse", "canonical_payload_reused")
                    return _result("blocked", "artifact_already_registered")

                commit_state = workflow_state._read_workflow_state_from_directory(state_fd)
                commit_approved = _validate_workflow_preconditions(
                    root, run, state=commit_state
                )
                _validate_evidence(root, run, commit_approved)
                _write_artifact(artifacts_fd, run)
    except AutoresearchValidationError as error:
        if error.code in {"scope_identity_changed", "planning_identity_changed"}:
            return _result("replan_required", error.code)
        return _result("blocked", error.code)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return _result("blocked", "workflow_state_invalid")
    return _result("written", "registered")
