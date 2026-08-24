"""Strict, safe loader for planning-option workflow artifacts.

The artifact is planner-produced but repository-owned input.  This module keeps
its validation and filesystem boundary self-contained so gate and CLI callers
can rely on immutable, normalized records instead of untrusted JSON.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import html
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
from typing import Any, Iterator, Optional
import unicodedata


_MAX_ARTIFACT_BYTES = 128 * 1024
_MAX_TEXT_BYTES = 4 * 1024
_ARTIFACT_PATH = (".workflow", "artifacts", "planning-options.json")
_MANIFEST_PATH = (".workflow", "manifest.json")

_ARTIFACT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "no_decision_reason",
        "decisions",
        "selection_history",
    }
)
_DECISION_FIELDS = frozenset(
    {
        "id",
        "question",
        "materiality_axes",
        "options",
        "recommended_option_id",
        "recommendation_rationale",
        "selected_option_id",
        "selected_by",
        "selected_at",
    }
)
_OPTION_FIELDS = frozenset(
    {
        "id",
        "summary",
        "affected_work",
        "acceptance_delta",
        "work_risks",
        "transition_risks",
        "rollback_or_exit",
    }
)
_HISTORY_FIELDS = frozenset(
    {
        "decision_id",
        "previous_option_id",
        "selected_option_id",
        "selected_by",
        "selected_at",
        "source",
    }
)
_PROFILE_FIELDS = frozenset({"required"})

PLANNING_OPTIONS_STATUSES = frozenset(
    {"no_decision_required", "selection_required", "selected"}
)
MATERIALITY_AXES = frozenset(
    {
        "external_behavior",
        "compatibility_migration",
        "security_slo",
        "scope_delivery_risk",
        "lifecycle_reversibility",
    }
)

_DECISION_ID = re.compile(r"D-[0-9]{3}\Z")
_OPTION_ID = re.compile(r"O-[0-9]{3}\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_URL_WITH_USERINFO = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/@]+@", re.IGNORECASE)
_SENSITIVE_TEXT = re.compile(
    r"(?:\b(?:api[_ -]?key|access[_ -]?token|password|passwd|secret|"
    r"credential|dsn|connection[_ -]?string)\b\s*[:=]|\bbearer\s+\S+"
    r"|-----BEGIN(?: [A-Z0-9_-]+)?(?: PRIVATE)? KEY-----"
    r"|\b(?:raw[_ -]?data|raw(?:\s+(?:production|database))?\s+"
    r"(?:data|rows?|records?|samples?))\b)",
    re.IGNORECASE,
)
_UNSAFE_MARKDOWN_URI = re.compile(r"\b(?:javascript|data|vbscript)\s*:", re.IGNORECASE)
_UNSAFE_MARKDOWN_HTML = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")
_COMMONMARK_ESCAPED_PUNCTUATION = re.compile(
    r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])"
)


class PlanningOptionsError(ValueError):
    """A stable, non-sensitive planning-options failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PlanningOption:
    """One validated, materially distinct implementation option."""

    id: str
    summary: str
    affected_work: tuple[str, ...]
    acceptance_delta: str
    work_risks: tuple[str, ...]
    transition_risks: tuple[str, ...]
    rollback_or_exit: str


@dataclass(frozen=True)
class PlanningDecision:
    """A material decision and its recommended and selected option state."""

    id: str
    question: str
    materiality_axes: tuple[str, ...]
    options: tuple[PlanningOption, ...]
    recommended_option_id: str
    recommendation_rationale: str
    selected_option_id: Optional[str]
    selected_by: Optional[str]
    selected_at: Optional[str]


@dataclass(frozen=True)
class SelectionHistoryEntry:
    """One append-only option selection event."""

    decision_id: str
    previous_option_id: Optional[str]
    selected_option_id: str
    selected_by: str
    selected_at: str
    source: str


@dataclass(frozen=True)
class PlanningOptionsArtifact:
    """The complete normalized artifact together with its canonical hash."""

    schema_version: int
    status: str
    no_decision_reason: Optional[str]
    decisions: tuple[PlanningDecision, ...]
    selection_history: tuple[SelectionHistoryEntry, ...]
    artifact_hash: str


@dataclass(frozen=True)
class PlanningOptionsPolicy:
    """Resolved manifest policy and a validated artifact if one was present."""

    required: bool
    status: str
    artifact: Optional[PlanningOptionsArtifact]


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_hash(payload: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


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
        supplied_metadata = os.lstat(supplied)
        if stat.S_ISLNK(supplied_metadata.st_mode):
            raise PlanningOptionsError("repo_root_invalid")
        root = supplied.resolve(strict=True)
        metadata = os.lstat(root)
    except PlanningOptionsError:
        raise
    except OSError:
        raise PlanningOptionsError("repo_root_invalid") from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise PlanningOptionsError("repo_root_invalid")
    return root


def _read_bounded_utf8(
    root: Path, parts: tuple[str, ...]
) -> tuple[Optional[str], Optional[str]]:
    """Read a fixed relative file through directory descriptors without links.

    A ``None`` error means the file is absent.  Every other error is deliberately
    collapsed by public callers so invalid paths and content never expose host
    paths or artifact text.
    """

    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    directory_fd: Optional[int] = None
    file_fd: Optional[int] = None
    try:
        directory_fd = os.open(root, directory_flags)
        for part in parts[:-1]:
            try:
                metadata = os.lstat(part, dir_fd=directory_fd)
            except FileNotFoundError:
                return None, None
            if stat.S_ISLNK(metadata.st_mode):
                return None, "unsafe_path"
            if not stat.S_ISDIR(metadata.st_mode):
                return None, "unreadable"
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd

        try:
            metadata = os.lstat(parts[-1], dir_fd=directory_fd)
        except FileNotFoundError:
            return None, None
        if stat.S_ISLNK(metadata.st_mode):
            return None, "unsafe_path"
        if not stat.S_ISREG(metadata.st_mode):
            return None, "unreadable"

        file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        raw = bytearray()
        while len(raw) <= _MAX_ARTIFACT_BYTES:
            try:
                chunk = os.read(file_fd, _MAX_ARTIFACT_BYTES + 1 - len(raw))
            except OSError as error:
                if error.errno == errno.EINTR:
                    continue
                raise
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > _MAX_ARTIFACT_BYTES:
                return None, "oversize"
    except OSError:
        return None, "unreadable"
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)

    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "invalid_utf8"


def _read_json_object(
    root: Path,
    parts: tuple[str, ...],
    *,
    missing_code: str,
    invalid_code: str,
) -> dict[str, Any]:
    raw, error = _read_bounded_utf8(root, parts)
    if raw is None:
        raise PlanningOptionsError(missing_code if error is None else invalid_code)
    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise PlanningOptionsError(invalid_code) from None
    if not isinstance(payload, dict):
        raise PlanningOptionsError(invalid_code)
    return payload



def _inspection_text(value: str) -> str:
    """Decode renderer-visible text without changing canonical stored text."""

    inspected = unicodedata.normalize("NFKC", value)
    for _ in range(2):
        decoded = html.unescape(inspected)
        if decoded == inspected:
            break
        inspected = decoded
    return _COMMONMARK_ESCAPED_PUNCTUATION.sub(r"\1", inspected)


def _normalize_text(value: object) -> str:
    if not isinstance(value, str):
        raise PlanningOptionsError("artifact_invalid")
    try:
        input_size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise PlanningOptionsError("artifact_invalid") from None
    if input_size > _MAX_TEXT_BYTES:
        raise PlanningOptionsError("artifact_invalid")
    if any(ord(character) == 0 or unicodedata.category(character) == "Cc" for character in value):
        raise PlanningOptionsError("artifact_invalid")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    try:
        normalized_size = len(normalized.encode("utf-8"))
    except UnicodeEncodeError:
        raise PlanningOptionsError("artifact_invalid") from None
    if not normalized or normalized_size > _MAX_TEXT_BYTES:
        raise PlanningOptionsError("artifact_invalid")
    inspection_text = _inspection_text(value)
    if (
        _URL_WITH_USERINFO.search(inspection_text) is not None
        or _SENSITIVE_TEXT.search(inspection_text) is not None
        or _UNSAFE_MARKDOWN_URI.search(inspection_text) is not None
        or _UNSAFE_MARKDOWN_HTML.search(inspection_text) is not None
    ):
        raise PlanningOptionsError("artifact_invalid")
    return normalized


def _normalize_identifier(value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise PlanningOptionsError("artifact_invalid")
    return value


def _normalize_text_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise PlanningOptionsError("artifact_invalid")
    normalized = tuple(_normalize_text(item) for item in value)
    if len(set(normalized)) != len(normalized):
        raise PlanningOptionsError("artifact_invalid")
    return normalized


def _parse_utc_timestamp(value: object) -> str:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise PlanningOptionsError("artifact_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise PlanningOptionsError("artifact_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise PlanningOptionsError("artifact_invalid")
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != value:
        raise PlanningOptionsError("artifact_invalid")
    return value


def _parse_option(value: object) -> PlanningOption:
    if not isinstance(value, dict) or set(value) != _OPTION_FIELDS:
        raise PlanningOptionsError("artifact_invalid")
    return PlanningOption(
        id=_normalize_identifier(value["id"], _OPTION_ID),
        summary=_normalize_text(value["summary"]),
        affected_work=_normalize_text_list(value["affected_work"]),
        acceptance_delta=_normalize_text(value["acceptance_delta"]),
        work_risks=_normalize_text_list(value["work_risks"]),
        transition_risks=_normalize_text_list(value["transition_risks"]),
        rollback_or_exit=_normalize_text(value["rollback_or_exit"]),
    )


def _normalize_material_value(value: object) -> object:
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFKC", value).casefold()
        return "".join(
            character
            for character in normalized
            if not unicodedata.category(character).startswith("P")
            and not character.isspace()
        )
    if isinstance(value, tuple):
        return sorted(_normalize_material_value(item) for item in value)
    raise TypeError("option fields are already validated text or tuples")


def _option_material_fingerprint(option: PlanningOption) -> str:
    return _canonical_hash(
        {
            "summary": _normalize_material_value(option.summary),
            "affected_work": _normalize_material_value(option.affected_work),
            "acceptance_delta": _normalize_material_value(option.acceptance_delta),
            "work_risks": _normalize_material_value(option.work_risks),
            "transition_risks": _normalize_material_value(option.transition_risks),
            "rollback_or_exit": _normalize_material_value(option.rollback_or_exit),
        }
    )


def _parse_selected_fields(
    payload: dict[str, Any], options: tuple[PlanningOption, ...]
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    selected_option_id = payload["selected_option_id"]
    selected_by = payload["selected_by"]
    selected_at = payload["selected_at"]
    if selected_option_id is None and selected_by is None and selected_at is None:
        return None, None, None
    if selected_option_id is None or selected_by is None or selected_at is None:
        raise PlanningOptionsError("artifact_invalid")
    option_id = _normalize_identifier(selected_option_id, _OPTION_ID)
    if option_id not in {option.id for option in options}:
        raise PlanningOptionsError("artifact_invalid")
    return option_id, _normalize_text(selected_by), _parse_utc_timestamp(selected_at)


def _parse_decision(value: object) -> PlanningDecision:
    if not isinstance(value, dict) or set(value) != _DECISION_FIELDS:
        raise PlanningOptionsError("artifact_invalid")
    options_value = value["options"]
    if not isinstance(options_value, list) or not 2 <= len(options_value) <= 3:
        raise PlanningOptionsError("artifact_invalid")
    options = tuple(_parse_option(option) for option in options_value)
    if len({option.id for option in options}) != len(options):
        raise PlanningOptionsError("artifact_invalid")
    fingerprints = {_option_material_fingerprint(option) for option in options}
    if len(fingerprints) != len(options):
        raise PlanningOptionsError("artifact_invalid")

    axes_value = value["materiality_axes"]
    if not isinstance(axes_value, list) or not axes_value:
        raise PlanningOptionsError("artifact_invalid")
    axes = tuple(_normalize_text(axis) for axis in axes_value)
    if len(set(axes)) != len(axes) or not set(axes) <= MATERIALITY_AXES:
        raise PlanningOptionsError("artifact_invalid")

    recommended_option_id = _normalize_identifier(value["recommended_option_id"], _OPTION_ID)
    if recommended_option_id != options[0].id:
        raise PlanningOptionsError("artifact_invalid")
    selected_option_id, selected_by, selected_at = _parse_selected_fields(value, options)
    return PlanningDecision(
        id=_normalize_identifier(value["id"], _DECISION_ID),
        question=_normalize_text(value["question"]),
        materiality_axes=axes,
        options=options,
        recommended_option_id=recommended_option_id,
        recommendation_rationale=_normalize_text(value["recommendation_rationale"]),
        selected_option_id=selected_option_id,
        selected_by=selected_by,
        selected_at=selected_at,
    )


def _parse_history_entry(value: object) -> SelectionHistoryEntry:
    if not isinstance(value, dict) or set(value) != _HISTORY_FIELDS:
        raise PlanningOptionsError("artifact_invalid")
    previous_option_id = value["previous_option_id"]
    if previous_option_id is not None:
        previous_option_id = _normalize_identifier(previous_option_id, _OPTION_ID)
    source = _normalize_text(value["source"])
    if source != "cli":
        raise PlanningOptionsError("artifact_invalid")
    return SelectionHistoryEntry(
        decision_id=_normalize_identifier(value["decision_id"], _DECISION_ID),
        previous_option_id=previous_option_id,
        selected_option_id=_normalize_identifier(value["selected_option_id"], _OPTION_ID),
        selected_by=_normalize_text(value["selected_by"]),
        selected_at=_parse_utc_timestamp(value["selected_at"]),
        source=source,
    )


def _validate_history(
    history: tuple[SelectionHistoryEntry, ...], decisions: tuple[PlanningDecision, ...]
) -> None:
    decisions_by_id = {decision.id: decision for decision in decisions}
    previous_timestamp: Optional[datetime] = None
    current_options: dict[str, Optional[str]] = {decision.id: None for decision in decisions}
    final_entries: dict[str, SelectionHistoryEntry] = {}

    for entry in history:
        decision = decisions_by_id.get(entry.decision_id)
        if decision is None:
            raise PlanningOptionsError("artifact_invalid")
        option_ids = {option.id for option in decision.options}
        if entry.selected_option_id not in option_ids:
            raise PlanningOptionsError("artifact_invalid")
        if entry.previous_option_id is not None and entry.previous_option_id not in option_ids:
            raise PlanningOptionsError("artifact_invalid")
        if entry.previous_option_id != current_options[entry.decision_id]:
            raise PlanningOptionsError("artifact_invalid")
        if entry.previous_option_id == entry.selected_option_id:
            raise PlanningOptionsError("artifact_invalid")
        parsed = datetime.fromisoformat(entry.selected_at[:-1] + "+00:00")
        if previous_timestamp is not None and parsed < previous_timestamp:
            raise PlanningOptionsError("artifact_invalid")
        previous_timestamp = parsed
        current_options[entry.decision_id] = entry.selected_option_id
        final_entries[entry.decision_id] = entry

    for decision in decisions:
        final_entry = final_entries.get(decision.id)
        if decision.selected_option_id is None:
            if final_entry is not None:
                raise PlanningOptionsError("artifact_invalid")
            continue
        if final_entry is None or (
            final_entry.selected_option_id != decision.selected_option_id
            or final_entry.selected_by != decision.selected_by
            or final_entry.selected_at != decision.selected_at
        ):
            raise PlanningOptionsError("artifact_invalid")


def _canonical_payload(
    *,
    schema_version: int,
    status: str,
    no_decision_reason: Optional[str],
    decisions: tuple[PlanningDecision, ...],
    selection_history: tuple[SelectionHistoryEntry, ...],
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "status": status,
        "no_decision_reason": no_decision_reason,
        "decisions": [
            {
                "id": decision.id,
                "question": decision.question,
                "materiality_axes": list(decision.materiality_axes),
                "options": [
                    {
                        "id": option.id,
                        "summary": option.summary,
                        "affected_work": list(option.affected_work),
                        "acceptance_delta": option.acceptance_delta,
                        "work_risks": list(option.work_risks),
                        "transition_risks": list(option.transition_risks),
                        "rollback_or_exit": option.rollback_or_exit,
                    }
                    for option in decision.options
                ],
                "recommended_option_id": decision.recommended_option_id,
                "recommendation_rationale": decision.recommendation_rationale,
                "selected_option_id": decision.selected_option_id,
                "selected_by": decision.selected_by,
                "selected_at": decision.selected_at,
            }
            for decision in decisions
        ],
        "selection_history": [
            {
                "decision_id": entry.decision_id,
                "previous_option_id": entry.previous_option_id,
                "selected_option_id": entry.selected_option_id,
                "selected_by": entry.selected_by,
                "selected_at": entry.selected_at,
                "source": entry.source,
            }
            for entry in selection_history
        ],
    }


def _parse_artifact(payload: object) -> PlanningOptionsArtifact:
    if not isinstance(payload, dict) or set(payload) != _ARTIFACT_FIELDS:
        raise PlanningOptionsError("artifact_invalid")
    schema_version = payload["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != 1:
        raise PlanningOptionsError("artifact_invalid")
    status = payload["status"]
    if not isinstance(status, str) or status not in PLANNING_OPTIONS_STATUSES:
        raise PlanningOptionsError("artifact_invalid")
    decisions_value = payload["decisions"]
    history_value = payload["selection_history"]
    if not isinstance(decisions_value, list) or not isinstance(history_value, list):
        raise PlanningOptionsError("artifact_invalid")
    decisions = tuple(_parse_decision(decision) for decision in decisions_value)
    if len({decision.id for decision in decisions}) != len(decisions):
        raise PlanningOptionsError("artifact_invalid")
    selection_history = tuple(_parse_history_entry(entry) for entry in history_value)

    no_decision_reason_value = payload["no_decision_reason"]
    if status == "no_decision_required":
        if decisions or selection_history:
            raise PlanningOptionsError("artifact_invalid")
        no_decision_reason = _normalize_text(no_decision_reason_value)
    else:
        if no_decision_reason_value is not None or not decisions:
            raise PlanningOptionsError("artifact_invalid")
        no_decision_reason = None
        _validate_history(selection_history, decisions)
        selected_decisions = [
            decision for decision in decisions if decision.selected_option_id is not None
        ]
        if status == "selection_required":
            if len(selected_decisions) == len(decisions):
                raise PlanningOptionsError("artifact_invalid")
        elif len(selected_decisions) != len(decisions):
            raise PlanningOptionsError("artifact_invalid")

    canonical_payload = _canonical_payload(
        schema_version=schema_version,
        status=status,
        no_decision_reason=no_decision_reason,
        decisions=decisions,
        selection_history=selection_history,
    )
    return PlanningOptionsArtifact(
        schema_version=schema_version,
        status=status,
        no_decision_reason=no_decision_reason,
        decisions=decisions,
        selection_history=selection_history,
        artifact_hash=_canonical_hash(canonical_payload),
    )


def load_planning_options(repo_root: Path) -> PlanningOptionsArtifact:
    """Load and validate ``planning-options.json`` as immutable normalized data."""

    root = _canonical_repo_root(repo_root)
    payload = _read_json_object(
        root,
        _ARTIFACT_PATH,
        missing_code="artifact_missing",
        invalid_code="artifact_invalid",
    )
    return _parse_artifact(payload)


def _load_manifest(root: Path) -> Optional[dict[str, Any]]:
    raw, error = _read_bounded_utf8(root, _MANIFEST_PATH)
    if raw is None:
        if error is None:
            return None
        raise PlanningOptionsError("profile_invalid")
    try:
        manifest = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise PlanningOptionsError("profile_invalid") from None
    if not isinstance(manifest, dict):
        raise PlanningOptionsError("profile_invalid")
    return manifest


def _artifact_is_present(root: Path) -> bool:
    raw, error = _read_bounded_utf8(root, _ARTIFACT_PATH)
    if raw is not None:
        return True
    if error is not None:
        # The path exists but is unsafe or unreadable.  Delegate to the loader
        # so the public result is the canonical artifact_invalid code.
        load_planning_options(root)
    return False


def resolve_planning_options_policy(repo_root: Path) -> PlanningOptionsPolicy:
    """Resolve manifest policy without granting malformed or legacy bypasses.

    Missing profile and missing artifact is the only legacy compatibility path.
    Any present artifact is fully validated even when a profile explicitly opts
    out, which keeps stale or malicious planner output from being ignored.
    """

    root = _canonical_repo_root(repo_root)
    manifest = _load_manifest(root)
    artifact_present = _artifact_is_present(root)
    artifact = load_planning_options(root) if artifact_present else None

    if manifest is None or "planning_options" not in manifest:
        if artifact is None:
            return PlanningOptionsPolicy(
                required=False, status="legacy_not_required", artifact=None
            )
        return PlanningOptionsPolicy(
            required=False, status=artifact.status, artifact=artifact
        )

    profile = manifest["planning_options"]
    if not isinstance(profile, dict) or set(profile) != _PROFILE_FIELDS:
        raise PlanningOptionsError("profile_invalid")
    required = profile["required"]
    if not isinstance(required, bool):
        raise PlanningOptionsError("profile_invalid")
    return PlanningOptionsPolicy(
        required=required,
        status=(
            artifact.status
            if artifact is not None
            else "required" if required else "not_required"
        ),
        artifact=artifact,
    )


@dataclass(frozen=True)
class PlanningOptionSelection:
    """Sanitized durable selection result for CLI and state reconciliation."""

    decision_id: str
    option_id: str
    status: str
    changed: bool
    previous_hash: str
    current_hash: str


_SELECTION_LOCKS: dict[str, threading.Lock] = {}
_SELECTION_LOCKS_GUARD = threading.Lock()


def _selection_thread_lock(root: Path) -> threading.Lock:
    key = str(root / ".workflow" / "artifacts" / "planning-options.json")
    with _SELECTION_LOCKS_GUARD:
        lock = _SELECTION_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SELECTION_LOCKS[key] = lock
        return lock


def _selection_artifact_error() -> PlanningOptionsError:
    return PlanningOptionsError("artifact_invalid")


def _validate_selection_file(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise _selection_artifact_error()


def _open_selection_directory(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError:
        raise _selection_artifact_error() from None
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise _selection_artifact_error()
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def _planning_options_transaction(root: Path) -> Iterator[int]:
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    root_fd: Optional[int] = None
    workflow_fd: Optional[int] = None
    artifacts_fd: Optional[int] = None
    lock_fd: Optional[int] = None
    locked = False
    try:
        try:
            root_fd = os.open(root, directory_flags)
        except OSError:
            raise _selection_artifact_error() from None
        workflow_fd = _open_selection_directory(root_fd, ".workflow")
        artifacts_fd = _open_selection_directory(workflow_fd, "artifacts")
        lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            lock_fd = os.open(
                "planning-options.json.lock", lock_flags, 0o600, dir_fd=artifacts_fd
            )
        except OSError:
            raise _selection_artifact_error() from None
        _validate_selection_file(lock_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        locked = True
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


def _read_selection_artifact(directory_fd: int) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor: Optional[int] = None
    try:
        try:
            descriptor = os.open("planning-options.json", flags, dir_fd=directory_fd)
        except FileNotFoundError:
            raise PlanningOptionsError("artifact_missing") from None
        except OSError:
            raise _selection_artifact_error() from None
        _validate_selection_file(descriptor)
        raw = bytearray()
        while len(raw) <= _MAX_ARTIFACT_BYTES:
            try:
                chunk = os.read(descriptor, _MAX_ARTIFACT_BYTES + 1 - len(raw))
            except OSError as error:
                if error.errno == errno.EINTR:
                    continue
                raise _selection_artifact_error() from None
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > _MAX_ARTIFACT_BYTES:
                raise _selection_artifact_error()
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise _selection_artifact_error() from None
    if not isinstance(payload, dict):
        raise _selection_artifact_error()
    return payload


def _write_selection_artifact(
    directory_fd: int,
    payload: dict[str, object],
) -> None:
    temporary_name = f".planning-options.json.{secrets.token_hex(16)}.tmp"
    temporary_fd: Optional[int] = None
    temporary_created = False
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            temporary_fd = os.open(
                temporary_name, flags, 0o600, dir_fd=directory_fd
            )
            temporary_created = True
        except (FileExistsError, OSError):
            raise _selection_artifact_error() from None
        try:
            _validate_selection_file(temporary_fd)
            remaining = memoryview(_canonical_json_bytes(payload) + b"\n")
            while remaining:
                written = os.write(temporary_fd, remaining)
                if written <= 0:
                    raise OSError("Could not write planning options artifact")
                remaining = remaining[written:]
            os.fsync(temporary_fd)
        finally:
            os.close(temporary_fd)
            temporary_fd = None
        os.replace(
            temporary_name,
            "planning-options.json",
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass




_RECONCILIATION_MARKER = ".planning-options-reconcile"


def _write_reconciliation_marker(directory_fd: int, journal: dict[str, object]) -> None:
    temporary_name = f"{_RECONCILIATION_MARKER}.{secrets.token_hex(16)}.tmp"
    descriptor: Optional[int] = None
    created = False
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        created = True
        _validate_selection_file(descriptor)
        remaining = memoryview(_canonical_json_bytes(journal))
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("Could not write reconciliation marker")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            _RECONCILIATION_MARKER,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def _reconciliation_pending(
    directory_fd: int, artifact_hash: str
) -> Optional[dict[str, object]]:
    descriptor: Optional[int] = None
    try:
        try:
            descriptor = os.open(
                _RECONCILIATION_MARKER,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return None
        _validate_selection_file(descriptor)
        try:
            journal = json.loads(
                os.read(descriptor, 1024).decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise _selection_artifact_error() from None
        required = {
            "schema_version", "current_hash", "previous_hash", "decision_id",
            "option_id", "artifact_status", "source",
        }
        if (
            not isinstance(journal, dict)
            or set(journal) != required
            or journal.get("schema_version") != 1
            or journal.get("current_hash") != artifact_hash
            or not all(isinstance(journal.get(key), str) for key in required - {"schema_version"})
        ):
            raise _selection_artifact_error()
        return journal
    except OSError:
        raise _selection_artifact_error() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _clear_reconciliation_marker(directory_fd: int) -> None:
    descriptor: Optional[int] = None
    try:
        try:
            descriptor = os.open(
                _RECONCILIATION_MARKER,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return
        _validate_selection_file(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    os.unlink(_RECONCILIATION_MARKER, dir_fd=directory_fd)
    os.fsync(directory_fd)
def _next_selection_timestamp(history: tuple[SelectionHistoryEntry, ...]) -> str:
    now = datetime.now(timezone.utc)
    if history:
        latest = datetime.fromisoformat(history[-1].selected_at[:-1] + "+00:00")
        now = max(now, latest)
    return now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _selection_request_values(
    decision_id: object, option_id: object, selected_by: object
) -> tuple[str, str, str]:
    try:
        return (
            _normalize_identifier(decision_id, _DECISION_ID),
            _normalize_identifier(option_id, _OPTION_ID),
            _normalize_text(selected_by),
        )
    except PlanningOptionsError:
        raise PlanningOptionsError("selection_invalid") from None


def _select_planning_option_in_transaction(
    directory_fd: int,
    decision_id: str,
    option_id: str,
    selected_by: str,
) -> PlanningOptionSelection:
    artifact = _parse_artifact(_read_selection_artifact(directory_fd))
    if artifact.status == "no_decision_required":
        raise PlanningOptionsError("selection_not_required")
    decision = next((item for item in artifact.decisions if item.id == decision_id), None)
    if decision is None:
        raise PlanningOptionsError("decision_not_found")
    if option_id not in {option.id for option in decision.options}:
        raise PlanningOptionsError("option_not_found")
    if decision.selected_option_id == option_id:
        os.fsync(directory_fd)
        return PlanningOptionSelection(
            decision_id=decision_id,
            option_id=option_id,
            status=artifact.status,
            changed=False,
            previous_hash=artifact.artifact_hash,
            current_hash=artifact.artifact_hash,
        )

    timestamp = _next_selection_timestamp(artifact.selection_history)
    payload = _canonical_payload(
        schema_version=artifact.schema_version,
        status=artifact.status,
        no_decision_reason=artifact.no_decision_reason,
        decisions=artifact.decisions,
        selection_history=artifact.selection_history,
    )
    decision_payload = next(item for item in payload["decisions"] if item["id"] == decision_id)
    decision_payload["selected_option_id"] = option_id
    decision_payload["selected_by"] = selected_by
    decision_payload["selected_at"] = timestamp
    payload["selection_history"].append(
        {
            "decision_id": decision_id,
            "previous_option_id": decision.selected_option_id,
            "selected_option_id": option_id,
            "selected_by": selected_by,
            "selected_at": timestamp,
            "source": "cli",
        }
    )
    all_selected = all(item["selected_option_id"] is not None for item in payload["decisions"])
    payload["status"] = "selected" if all_selected else "selection_required"
    updated = _parse_artifact(payload)
    _write_reconciliation_marker(
        directory_fd,
        {
            "schema_version": 1,
            "current_hash": updated.artifact_hash,
            "previous_hash": artifact.artifact_hash,
            "decision_id": decision_id,
            "option_id": option_id,
            "artifact_status": updated.status,
            "source": "cli",
        },
    )
    _write_selection_artifact(directory_fd, payload)
    return PlanningOptionSelection(
        decision_id=decision_id,
        option_id=option_id,
        status=updated.status,
        changed=True,
        previous_hash=artifact.artifact_hash,
        current_hash=updated.artifact_hash,
    )


@contextmanager
def planning_option_selection_transaction(
    repo_root: Path, *, canonical: bool = False
) -> Iterator[tuple[Path, int]]:
    root = Path(repo_root) if canonical else _canonical_repo_root(Path(repo_root))
    with _selection_thread_lock(root), _planning_options_transaction(root) as directory_fd:
        yield root, directory_fd


def select_planning_option(
    repo_root: Path,
    decision_id: object,
    option_id: object,
    selected_by: object,
) -> PlanningOptionSelection:
    """Atomically select one existing option and append its durable history."""

    decision_id, option_id, selected_by = _selection_request_values(
        decision_id, option_id, selected_by
    )
    with planning_option_selection_transaction(repo_root) as (_, directory_fd):
        return _select_planning_option_in_transaction(
            directory_fd, decision_id, option_id, selected_by
        )
