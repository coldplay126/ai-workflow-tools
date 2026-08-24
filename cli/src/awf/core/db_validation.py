"""Detect database-affecting workflow artifacts without scanning a repository."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import stat
import time
import unicodedata
from typing import Any, Iterable, Optional, Sequence


_MAX_ARTIFACT_BYTES = 128 * 1024
_TEXT_ARTIFACTS = (
    ".workflow/concept.md",
    ".workflow/artifacts/spec.md",
    ".workflow/artifacts/plan.md",
    ".workflow/artifacts/tasks.md",
    ".workflow/artifacts/test-criteria.md",
)
_ALLOWED_FILES_ARTIFACT = ".workflow/artifacts/allowed-files.json"
_PATH_SUFFIXES = {".sql", ".prisma"}
_PATH_DIRECTORIES = {
    "migration",
    "migrations",
    "model",
    "models",
    "entity",
    "entities",
    "repository",
    "repositories",
    "query",
    "queries",
    "database",
    "databases",
    "schema",
    "schemas",
}
_WINDOWS_DRIVE_PATH = re.compile(r"^[a-zA-Z]:")

# Strong terms are database-specific on their own. Weak terms need a DB anchor
# on the same artifact line so HTML, OpenAPI, and ML vocabulary stays neutral.
_STRONG_TEXT_SIGNAL_PATTERNS = (
    ("database", re.compile(r"\bdatabase\b|데이터베이스")),
    ("sql", re.compile(r"\bsql\b")),
    (
        "sql syntax",
        re.compile(
            r"\b(?:select\s+.+\s+from|insert\s+into|update\s+\w+\s+set|"
            r"delete\s+from|group\s+by|join)\b"
        ),
    ),
    ("order by", re.compile(r"\border\s+by\b")),
    ("migration", re.compile(r"\bmigration(?:s)?\b|마이그레이션")),
    ("normalization", re.compile(r"\bnormalization\b|(?<!비)정규화")),
    ("denormalization", re.compile(r"\bdenormalization\b|비정규화")),
    ("erd", re.compile(r"\berd\b")),
    ("foreign key", re.compile(r"\bforeign\s+key\b|외래\s*키")),
    ("primary key", re.compile(r"\bprimary\s+key\b|기본\s*키")),
    ("unique constraint", re.compile(r"\bunique\s+constraint\b|고유\s*제약")),
    ("partition", re.compile(r"\bpartition(?:ing)?\b|파티션")),
    (
        "database engine",
        re.compile(r"\b(?:mysql|mariadb|postgres(?:ql)?|sqlite|mongodb|duckdb)\b"),
    ),
    ("warehouse", re.compile(r"\bwarehouse\b|웨어하우스")),
)
_WEAK_TEXT_SIGNAL_PATTERNS = (
    ("index", re.compile(r"\bindex\b|인덱스")),
    ("schema", re.compile(r"\bschema\b|스키마")),
    ("query", re.compile(r"\bquer(?:y|ies)\b|쿼리")),
    ("table", re.compile(r"\btable\b|테이블")),
    ("column", re.compile(r"\bcolumn\b|컬럼")),
    ("model", re.compile(r"\bmodel\b|모델")),
)
_DATABASE_CONTEXT_PATTERN = re.compile(
    r"\b(?:database|db|sql|orm|prisma|mysql|mariadb|postgres(?:ql)?|"
    r"sqlite|mongodb|duckdb)\b|데이터베이스"
)
_DATABASE_ANCHOR_PATTERN = re.compile(r"\b(?:database|db)\b|데이터베이스")


@dataclass(frozen=True)
class DatabaseSignal:
    """Normalized database-change classification for workflow artifacts."""

    detected: bool
    reasons: tuple[str, ...]


def _artifact_error(relative_path: str, code: str) -> str:
    return f"artifact_error:{relative_path}:{code}"


def _canonical_repo_root(repo_root: Path) -> Path:
    try:
        root = Path(repo_root).resolve(strict=True)
        metadata = os.lstat(root)
    except OSError:
        raise DatabaseValidationError("repo_root_invalid") from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise DatabaseValidationError("repo_root_invalid")
    return root


def _read_bounded_utf8(
    root: Path,
    relative_path: str,
) -> tuple[Optional[str], Optional[str]]:
    parts = tuple(Path(relative_path).parts)
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None, "unsafe_path"
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
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
    if len(raw) > _MAX_ARTIFACT_BYTES:
        return None, "oversize"
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "invalid_utf8"


def _text_reasons(text: str) -> Iterable[str]:
    for line in text.splitlines():
        normalized_line = line.casefold()
        for reason, pattern in _STRONG_TEXT_SIGNAL_PATTERNS:
            if pattern.search(normalized_line):
                yield f"text:{reason}"
        weak_reasons = tuple(
            f"text:{reason}"
            for reason, pattern in _WEAK_TEXT_SIGNAL_PATTERNS
            if pattern.search(normalized_line)
        )
        if not weak_reasons or not _DATABASE_CONTEXT_PATTERN.search(normalized_line):
            continue
        if _DATABASE_ANCHOR_PATTERN.search(normalized_line):
            yield "text:database"
        yield from weak_reasons


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict:
    payload = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _canonicalize_allowed_path(value: str) -> tuple[Optional[str], Optional[str]]:
    normalized = value.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or _WINDOWS_DRIVE_PATH.match(normalized)
        or "\x00" in normalized
    ):
        return None, "unsafe_path"
    parts: list[str] = []
    for part in normalized.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if not parts:
                return None, "unsafe_path"
            parts.pop()
            continue
        parts.append(part.casefold())
    if not parts:
        return None, "unsafe_path"
    return "/".join(parts), None


def _normalized_allowed_paths(
    root: Path,
) -> tuple[tuple[str, ...], Optional[str]]:
    raw, error = _read_bounded_utf8(root, _ALLOWED_FILES_ARTIFACT)
    if error:
        return (), error
    if raw is None:
        return (), None
    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, ValueError):
        return (), "invalid_json"
    if not isinstance(payload, dict):
        return (), "invalid_shape"

    if "planned_files" in payload:
        canonical_paths = payload["planned_files"]
    else:
        canonical_paths = payload.get("files", [])
    expanded_paths = payload.get("expanded_files", [])
    if not isinstance(canonical_paths, list) or not isinstance(expanded_paths, list):
        return (), "invalid_shape"

    normalized_paths = set()
    for paths in (canonical_paths, expanded_paths):
        for value in paths:
            if not isinstance(value, str):
                return (), "invalid_shape"
            canonical_path, path_error = _canonicalize_allowed_path(value)
            if path_error:
                return (), path_error
            if canonical_path:
                normalized_paths.add(canonical_path)
    return tuple(sorted(normalized_paths)), None


def _is_database_path(path: str) -> bool:
    candidate = Path(path)
    if candidate.suffix in _PATH_SUFFIXES:
        return True
    return any(part in _PATH_DIRECTORIES for part in candidate.parts[:-1])


def detect_database_signal(repo_root: Path) -> DatabaseSignal:
    """Return DB signals or conservative artifact errors from known artifacts."""
    try:
        root = _canonical_repo_root(repo_root)
    except DatabaseValidationError:
        return DatabaseSignal(
            detected=True,
            reasons=(_artifact_error(".workflow", "unsafe_path"),),
        )
    reasons = set()
    for relative_path in _TEXT_ARTIFACTS:
        text, error = _read_bounded_utf8(root, relative_path)
        if error:
            reasons.add(_artifact_error(relative_path, error))
        elif text is not None:
            reasons.update(_text_reasons(text))

    paths, error = _normalized_allowed_paths(root)
    if error:
        reasons.add(_artifact_error(_ALLOWED_FILES_ARTIFACT, error))
    else:
        for path in paths:
            if _is_database_path(path):
                reasons.add(f"path:{path}")

    normalized_reasons = tuple(sorted(reasons))
    return DatabaseSignal(detected=bool(normalized_reasons), reasons=normalized_reasons)


_PROFILE_ARTIFACT = ".workflow/manifest.json"
_DECISION_ARTIFACT = ".workflow/artifacts/database-decision.json"
_EVIDENCE_ARTIFACT = ".workflow/artifacts/database-validation-evidence.json"
_MAX_COMMAND_OUTPUT_BYTES = 128 * 1024
_MAX_COMMAND_TIMEOUT_SECONDS = 300
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SECRET_COMMAND_OPTION = re.compile(
    r"(?:^|[\s;])--(?:password|token|dsn|secret|credential)(?:=|\s|$)",
    re.IGNORECASE,
)
_SHORT_SECRET_COMMAND_OPTION = re.compile(
    r"(?:^|[\s;])-[pt](?:\S*)?(?=$|[\s;])",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*(?:password|token|dsn|secret|credential|url)"
    r"[A-Za-z0-9_]*\s*=",
    re.IGNORECASE,
)
_URI_VALUE = re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_SHELL_EXECUTABLES = {"sh", "bash", "zsh", "dash", "fish", "ksh"}
_DECISION_KINDS = {
    "maintain",
    "query_change",
    "physical_design",
    "normalize",
    "denormalize",
}
_CHANGE_SURFACES = {
    "query",
    "index",
    "column",
    "constraint",
    "erd",
    "normalize",
    "denormalize",
}
_STRUCTURAL_SURFACES = {"column", "constraint", "erd", "normalize", "denormalize"}
_NORMALIZATION_SURFACES = {"column", "constraint", "erd"}
_LOCAL_TARGETS = {
    "same_engine",
    "duckdb",
    "both",
    "sanitized_snapshot",
    "warehouse",
    "read_replica",
}
_PROFILE_FIELDS = {
    "enabled",
    "schema_command",
    "verify_command",
    "test_command",
    "command_timeout_seconds",
    "max_schema_age_hours",
    "allow_production_replica_sample",
}
_DECISION_FIELDS = {
    "schema_version",
    "status",
    "change_surfaces",
    "baseline_option_id",
    "recommended_option_id",
    "selected_option_id",
    "candidates",
    "recommendation_rationale",
}
_DECISION_OPTIONAL_FIELDS = {"local_data_test_waiver"}
_CANDIDATE_FIELDS = {
    "id",
    "kind",
    "applicable",
    "summary",
    "equivalence_plan",
    "integrity_plan",
    "normalization_assessment",
    "read_write_cost",
    "operational_risks",
    "transition_risks",
    "rollback_or_exit",
    "unavailable_reason",
    "denormalization_assessment",
    "physical_design_assessment",
}
_DENORMALIZATION_ASSESSMENT_FIELDS = {
    "source_of_truth",
    "consistency_window",
    "reconciliation",
    "rollback",
}
_PHYSICAL_DESIGN_ASSESSMENT_FIELDS = {
    "read_benefit",
    "write_amplification",
    "storage",
    "build_or_lock",
    "rollback",
}
_SCHEMA_FIELDS = {
    "schema_version",
    "kind",
    "target_class",
    "read_only",
    "schema_only",
    "engine",
    "engine_version",
    "captured_at",
    "schema_hash",
    "object_counts",
}
_VERIFY_FIELDS = {
    "schema_version",
    "kind",
    "production_schema_hash",
    "selected_option_id",
    "equivalence",
    "integrity",
    "query_plan",
    "migration",
    "rollback",
}
_TEST_FIELDS = {
    "schema_version",
    "kind",
    "production_schema_hash",
    "selected_option_id",
    "local_target",
    "masked",
    "raw_production_rows",
    "equivalence",
    "integrity",
    "performance",
}
_EVIDENCE_FIELDS = {
    "schema_version",
    "database_signal",
    "signal_reasons",
    "change_class",
    "profile_hash",
    "decision_hash",
    "stages",
}
_SENSITIVE_KEY_FRAGMENTS = {
    "dsn",
    "url",
    "password",
    "token",
    "credential",
    "secret",
    "ddl",
    "rows",
    "records",
    "samples",
    "sample",
    "rawdata",
}
_SENSITIVE_KEY_ALLOWLIST = {
    "allowproductionreplicasample",
    "rawproductionrows",
}


class DatabaseValidationError(ValueError):
    """A stable, sanitized database-validation failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DatabaseProfile:
    """Validated project-owned database command profile."""

    enabled: bool
    schema_command: tuple[str, ...]
    verify_command: tuple[str, ...]
    test_command: tuple[str, ...]
    command_timeout_seconds: int
    max_schema_age_hours: int
    allow_production_replica_sample: bool
    profile_hash: str


@dataclass(frozen=True)
class DatabaseDecision:
    """Validated comparative database design decision."""

    selected_option_id: str
    recommended_option_id: str
    baseline_option_id: str
    change_surfaces: tuple[str, ...]
    decision_hash: str
    local_data_test_waiver: Optional[dict[str, str]]


@dataclass(frozen=True)
class DatabaseCheckResult:
    """Sanitized result returned to the CLI and database gate callers."""

    stage: str
    status: str
    evidence_path: Optional[str]
    evidence_hash: Optional[str]
    signal_reasons: tuple[str, ...]
    blockers: tuple[str, ...]


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_hash(payload: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_string(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.casefold())


def _reject_sensitive_fields(payload: object, code: str) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if not isinstance(key, str):
                raise DatabaseValidationError(code)
            normalized_key = _normalized_key(key)
            if (
                normalized_key not in _SENSITIVE_KEY_ALLOWLIST
                and any(fragment in normalized_key for fragment in _SENSITIVE_KEY_FRAGMENTS)
            ):
                raise DatabaseValidationError(code)
            _reject_sensitive_fields(value, code)
    elif isinstance(payload, list):
        for value in payload:
            _reject_sensitive_fields(value, code)


def _load_json_object(
    root: Path,
    relative_path: str,
    *,
    missing_code: str,
    invalid_code: str,
) -> dict[str, Any]:
    raw, error = _read_bounded_utf8(root, relative_path)
    if raw is None:
        if error is None:
            raise DatabaseValidationError(missing_code)
        raise DatabaseValidationError(invalid_code)
    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, ValueError):
        raise DatabaseValidationError(invalid_code) from None
    if not isinstance(payload, dict):
        raise DatabaseValidationError(invalid_code)
    return payload


def _contains_literal_profile_secret(argument: str) -> bool:
    return any(
        pattern.search(argument) is not None
        for pattern in (
            _SECRET_COMMAND_OPTION,
            _SHORT_SECRET_COMMAND_OPTION,
            _SENSITIVE_ASSIGNMENT,
            _URI_VALUE,
        )
    )


def _validate_command(value: object, *, required: bool) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise DatabaseValidationError("profile_invalid")
    if not value:
        if required:
            raise DatabaseValidationError("profile_incomplete")
        return ()
    if len(value) > 64:
        raise DatabaseValidationError("profile_invalid")
    if not isinstance(value[0], str) or Path(value[0]).name.casefold() in _SHELL_EXECUTABLES:
        raise DatabaseValidationError("profile_invalid")
    command: list[str] = []
    for argument in value:
        if (
            not isinstance(argument, str)
            or not argument
            or "\x00" in argument
            or _contains_literal_profile_secret(argument)
        ):
            raise DatabaseValidationError("profile_invalid")
        command.append(argument)
    return tuple(command)


def _parse_timestamp(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def load_database_profile(repo_root: Path) -> DatabaseProfile:
    """Load the strictly bounded database-validation profile from manifest.json."""

    root = _canonical_repo_root(repo_root)
    manifest = _load_json_object(
        root,
        _PROFILE_ARTIFACT,
        missing_code="profile_missing",
        invalid_code="profile_invalid",
    )
    profile = manifest.get("database_validation")
    if not isinstance(profile, dict):
        raise DatabaseValidationError("profile_missing")
    _reject_sensitive_fields(profile, "profile_invalid")
    if set(profile) != _PROFILE_FIELDS:
        raise DatabaseValidationError("profile_invalid")

    enabled = profile["enabled"]
    if not isinstance(enabled, bool):
        raise DatabaseValidationError("profile_invalid")
    if not enabled:
        raise DatabaseValidationError("profile_disabled")

    timeout_seconds = profile["command_timeout_seconds"]
    max_schema_age_hours = profile["max_schema_age_hours"]
    allow_replica = profile["allow_production_replica_sample"]
    if (
        not _is_strict_int(timeout_seconds)
        or not 1 <= timeout_seconds <= _MAX_COMMAND_TIMEOUT_SECONDS
        or not _is_strict_int(max_schema_age_hours)
        or not 1 <= max_schema_age_hours <= 24 * 31
        or not isinstance(allow_replica, bool)
    ):
        raise DatabaseValidationError("profile_invalid")

    canonical_profile = {
        field: profile[field]
        for field in sorted(_PROFILE_FIELDS)
    }
    return DatabaseProfile(
        enabled=True,
        schema_command=_validate_command(profile["schema_command"], required=True),
        verify_command=_validate_command(profile["verify_command"], required=False),
        test_command=_validate_command(profile["test_command"], required=False),
        command_timeout_seconds=timeout_seconds,
        max_schema_age_hours=max_schema_age_hours,
        allow_production_replica_sample=allow_replica,
        profile_hash=_canonical_hash(canonical_profile),
    )


def _validate_string_list(value: object) -> bool:
    return isinstance(value, list) and all(_nonempty_string(item) is not None for item in value)


def _validate_structured_assessment(
    value: object,
    *,
    fields: set[str],
) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise DatabaseValidationError("decision_invalid")
    _reject_sensitive_fields(value, "decision_invalid")
    if any(_nonempty_string(entry) is None for entry in value.values()):
        raise DatabaseValidationError("decision_invalid")


def _validate_candidate(candidate: object) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise DatabaseValidationError("decision_invalid")
    _reject_sensitive_fields(candidate, "decision_invalid")
    if set(candidate) != _CANDIDATE_FIELDS:
        raise DatabaseValidationError("decision_invalid")
    if (
        _nonempty_string(candidate["id"]) is None
        or candidate["kind"] not in _DECISION_KINDS
        or not isinstance(candidate["applicable"], bool)
        or any(
            _nonempty_string(candidate[field]) is None
            for field in (
                "summary",
                "equivalence_plan",
                "integrity_plan",
                "read_write_cost",
                "rollback_or_exit",
            )
        )
        or (
            candidate["normalization_assessment"] is not None
            and _nonempty_string(candidate["normalization_assessment"]) is None
        )
        or not _validate_string_list(candidate["operational_risks"])
        or not _validate_string_list(candidate["transition_risks"])
    ):


        raise DatabaseValidationError("decision_invalid")

    unavailable_reason = candidate["unavailable_reason"]
    if candidate["applicable"]:
        if unavailable_reason not in (None, ""):
            raise DatabaseValidationError("decision_invalid")
    elif _nonempty_string(unavailable_reason) is None:
        raise DatabaseValidationError("decision_invalid")

    kind = candidate["kind"]
    denormalization = candidate["denormalization_assessment"]
    physical_design = candidate["physical_design_assessment"]
    if kind == "denormalize":
        _validate_structured_assessment(
            denormalization,
            fields=_DENORMALIZATION_ASSESSMENT_FIELDS,
        )
        if physical_design is not None:
            raise DatabaseValidationError("decision_invalid")
    elif kind == "physical_design":
        _validate_structured_assessment(
            physical_design,
            fields=_PHYSICAL_DESIGN_ASSESSMENT_FIELDS,
        )
        if denormalization is not None:
            raise DatabaseValidationError("decision_invalid")
    elif denormalization is not None or physical_design is not None:
        raise DatabaseValidationError("decision_invalid")
    return candidate
def _normalize_material_value(value: object) -> object:
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFKC", value).casefold()
        return "".join(
            character
            for character in normalized
            if not unicodedata.category(character).startswith("P")
            and not character.isspace()
        )
    if isinstance(value, list):
        return [_normalize_material_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _normalize_material_value(entry)
            for key, entry in sorted(value.items())
        }
    return value


def _candidate_material_fingerprint(candidate: dict[str, Any]) -> str:
    fingerprint = {
        field: _normalize_material_value(candidate[field])
        for field in sorted(_CANDIDATE_FIELDS - {"id", "summary"})
    }
    for field in ("operational_risks", "transition_risks"):
        fingerprint[field] = sorted(fingerprint[field])
    return _canonical_hash(fingerprint)


def _validate_waiver(value: object) -> Optional[dict[str, str]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DatabaseValidationError("decision_invalid")
    _reject_sensitive_fields(value, "decision_invalid")
    if set(value) != {"reason", "approver", "timestamp"}:
        raise DatabaseValidationError("decision_invalid")
    reason = _nonempty_string(value["reason"])
    approver = _nonempty_string(value["approver"])
    timestamp = _nonempty_string(value["timestamp"])
    parsed_timestamp = _parse_timestamp(timestamp) if timestamp else None
    if not reason or not approver or parsed_timestamp is None:
        raise DatabaseValidationError("decision_invalid")
    return {
        "reason": reason,
        "approver": approver,
        "timestamp": _utc_timestamp(parsed_timestamp),
    }


def load_database_decision(repo_root: Path) -> DatabaseDecision:
    """Load the selected, comparable database design decision artifact."""

    root = _canonical_repo_root(repo_root)
    decision = _load_json_object(
        root,
        _DECISION_ARTIFACT,
        missing_code="decision_missing",
        invalid_code="decision_invalid",
    )
    _reject_sensitive_fields(decision, "decision_invalid")
    if not _DECISION_FIELDS <= set(decision) or set(decision) - (
        _DECISION_FIELDS | _DECISION_OPTIONAL_FIELDS
    ):
        raise DatabaseValidationError("decision_invalid")
    if not _is_strict_int(decision["schema_version"]) or decision["schema_version"] != 1 or decision["status"] != "selected":
        raise DatabaseValidationError("decision_invalid")

    surfaces = decision["change_surfaces"]
    if not isinstance(surfaces, list) or not 1 <= len(surfaces) <= len(_CHANGE_SURFACES):
        raise DatabaseValidationError("decision_invalid")
    normalized_surfaces = tuple(sorted({surface.strip().casefold() for surface in surfaces if isinstance(surface, str)}))
    if (
        len(normalized_surfaces) != len(surfaces)
        or not normalized_surfaces
        or any(surface not in _CHANGE_SURFACES for surface in normalized_surfaces)
    ):
        raise DatabaseValidationError("decision_invalid")

    candidates = decision["candidates"]
    if not isinstance(candidates, list) or not 2 <= len(candidates) <= 3:
        raise DatabaseValidationError("decision_invalid")
    parsed_candidates = [_validate_candidate(candidate) for candidate in candidates]
    candidate_ids = [candidate["id"] for candidate in parsed_candidates]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise DatabaseValidationError("decision_invalid")
    candidate_fingerprints = {
        _candidate_material_fingerprint(candidate)
        for candidate in parsed_candidates
    }
    if len(candidate_fingerprints) != len(parsed_candidates):
        raise DatabaseValidationError("decision_invalid")
    candidates_by_id = {candidate["id"]: candidate for candidate in parsed_candidates}

    baseline_id = _nonempty_string(decision["baseline_option_id"])
    recommended_id = _nonempty_string(decision["recommended_option_id"])
    selected_id = _nonempty_string(decision["selected_option_id"])
    if (
        baseline_id not in candidates_by_id
        or recommended_id not in candidates_by_id
        or selected_id not in candidates_by_id
        or candidates_by_id[baseline_id]["kind"] != "maintain"
        or not candidates_by_id[recommended_id]["applicable"]
        or not candidates_by_id[selected_id]["applicable"]
        or _nonempty_string(decision["recommendation_rationale"]) is None
    ):
        raise DatabaseValidationError("decision_invalid")
    if (
        set(normalized_surfaces) & _NORMALIZATION_SURFACES
        and _nonempty_string(candidates_by_id[selected_id]["normalization_assessment"]) is None
    ):
        raise DatabaseValidationError("decision_invalid")

    waiver = _validate_waiver(decision.get("local_data_test_waiver"))
    canonical_decision = dict(decision)
    if waiver is not None:
        canonical_decision["local_data_test_waiver"] = waiver
    return DatabaseDecision(
        selected_option_id=selected_id,
        recommended_option_id=recommended_id,
        baseline_option_id=baseline_id,
        change_surfaces=normalized_surfaces,
        decision_hash=_canonical_hash(canonical_decision),
        local_data_test_waiver=waiver,
    )


_PROCESS_GROUP_GRACE_SECONDS = 0.2


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_group(process: subprocess.Popen[bytes], pgid: int) -> None:
    if _process_group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + _PROCESS_GROUP_GRACE_SECONDS
        while _process_group_exists(pgid) and time.monotonic() < deadline:
            time.sleep(0.01)
        if _process_group_exists(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
    try:
        process.wait(timeout=_PROCESS_GROUP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=_PROCESS_GROUP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass


def _run_json_command(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    allowed_fields: set[str],
) -> dict[str, Any]:
    try:
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            start_new_session=True,
        )
    except (OSError, ValueError):
        raise DatabaseValidationError("command_start_failed") from None
    pgid = process.pid

    output = bytearray()
    output_oversize = threading.Event()
    reader_error = threading.Event()

    def read_stdout() -> None:
        try:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    return
                remaining = _MAX_COMMAND_OUTPUT_BYTES + 1 - len(output)
                if remaining > 0:
                    output.extend(chunk[:remaining])
                if len(output) > _MAX_COMMAND_OUTPUT_BYTES:
                    output_oversize.set()
        except (OSError, ValueError):
            reader_error.set()

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    while process.poll() is None and not output_oversize.is_set():
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.01)

    reader.join(timeout=_PROCESS_GROUP_GRACE_SECONDS)
    leader_returncode = process.poll()
    cleanup_required = (
        timed_out
        or output_oversize.is_set()
        or leader_returncode not in (None, 0)
        or reader_error.is_set()
        or reader.is_alive()
        or _process_group_exists(pgid)
    )
    if cleanup_required:
        _terminate_process_group(process, pgid)
    if process.stdout is not None:
        try:
            process.stdout.close()
        except OSError:
            pass
    reader.join()

    if timed_out:
        raise DatabaseValidationError("command_timeout")
    if output_oversize.is_set():
        raise DatabaseValidationError("command_output_oversize")
    if process.returncode not in (None, 0):
        raise DatabaseValidationError("command_nonzero")
    if reader_error.is_set() or reader.is_alive():
        raise DatabaseValidationError("command_output_invalid")
    try:
        parsed = json.loads(
            bytes(output).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise DatabaseValidationError("command_output_invalid") from None
    if not isinstance(parsed, dict):
        raise DatabaseValidationError("command_output_invalid")
    _reject_sensitive_fields(parsed, "command_output_unsafe")
    if set(parsed) != allowed_fields:
        raise DatabaseValidationError("command_output_unsafe")
    return parsed


def _validate_exact_object(
    payload: object,
    *,
    fields: set[str],
    code: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != fields:
        raise DatabaseValidationError(code)
    return payload


def _validate_schema_evidence(
    payload: dict[str, Any],
    profile: DatabaseProfile,
) -> dict[str, Any]:
    if (
        not _is_strict_int(payload["schema_version"])
        or payload["schema_version"] != 1
        or payload["kind"] != "production_schema"
        or payload["target_class"] != "production_metadata"
        or payload["read_only"] is not True
        or payload["schema_only"] is not True
        or _nonempty_string(payload["engine"]) is None
        or _nonempty_string(payload["engine_version"]) is None
        or not isinstance(payload["schema_hash"], str)
        or _SHA256_PATTERN.fullmatch(payload["schema_hash"]) is None
    ):
        raise DatabaseValidationError("schema_evidence_invalid")
    captured_at = _parse_timestamp(payload["captured_at"])
    if captured_at is None:
        raise DatabaseValidationError("schema_evidence_invalid")
    now = _utc_now()
    if captured_at > now or now - captured_at > timedelta(hours=profile.max_schema_age_hours):
        raise DatabaseValidationError("schema_evidence_invalid")
    counts = _validate_exact_object(
        payload["object_counts"],
        fields={"tables", "columns", "indexes", "constraints"},
        code="schema_evidence_invalid",
    )
    if any(not _is_strict_int(count) or count < 0 for count in counts.values()):
        raise DatabaseValidationError("schema_evidence_invalid")
    return {
        "schema_hash": payload["schema_hash"],
        "engine": payload["engine"].strip(),
        "engine_version": payload["engine_version"].strip(),
        "captured_at": _utc_timestamp(captured_at),
        "object_counts": {
            "tables": counts["tables"],
            "columns": counts["columns"],
            "indexes": counts["indexes"],
            "constraints": counts["constraints"],
        },
    }


def _validate_verify_evidence(
    payload: dict[str, Any],
    decision: DatabaseDecision,
    schema_hash: str,
) -> dict[str, str]:
    statuses = ("equivalence", "integrity", "query_plan", "migration", "rollback")
    if (
        not _is_strict_int(payload["schema_version"])
        or payload["schema_version"] != 1
        or payload["kind"] != "database_verify"
        or payload["production_schema_hash"] != schema_hash
        or payload["selected_option_id"] != decision.selected_option_id
        or any(payload[field] not in {"pass", "fail", "not_applicable"} for field in statuses)
        or payload["equivalence"] != "pass"
        or payload["integrity"] != "pass"
        or any(payload[field] == "fail" for field in ("query_plan", "migration", "rollback"))
    ):
        raise DatabaseValidationError("verify_evidence_invalid")
    if "query" in decision.change_surfaces or "index" in decision.change_surfaces:
        if payload["query_plan"] != "pass":
            raise DatabaseValidationError("verify_evidence_invalid")
    elif payload["query_plan"] == "not_applicable":
        pass
    if set(decision.change_surfaces) & _STRUCTURAL_SURFACES:
        if payload["migration"] != "pass" or payload["rollback"] != "pass":
            raise DatabaseValidationError("verify_evidence_invalid")
    elif payload["migration"] == "not_applicable":
        pass
    return {
        "production_schema_hash": payload["production_schema_hash"],
        "selected_option_id": payload["selected_option_id"],
        "equivalence": payload["equivalence"],
        "integrity": payload["integrity"],
        "query_plan": payload["query_plan"],
        "migration": payload["migration"],
        "rollback": payload["rollback"],
    }


def _validate_test_evidence(
    payload: dict[str, Any],
    decision: DatabaseDecision,
    profile: DatabaseProfile,
    schema_hash: str,
) -> dict[str, object]:
    if (
        not _is_strict_int(payload["schema_version"])
        or payload["schema_version"] != 1
        or payload["kind"] != "database_test"
        or payload["production_schema_hash"] != schema_hash
        or payload["selected_option_id"] != decision.selected_option_id
        or payload["local_target"] not in _LOCAL_TARGETS
        or payload["masked"] is not True
        or payload["raw_production_rows"] is not False
        or any(payload[field] != "pass" for field in ("equivalence", "integrity", "performance"))
        or (
            payload["local_target"] == "read_replica"
            and not profile.allow_production_replica_sample
        )
    ):
        raise DatabaseValidationError("test_evidence_invalid")
    return {
        "status": "pass",
        "production_schema_hash": payload["production_schema_hash"],
        "selected_option_id": payload["selected_option_id"],
        "local_target": payload["local_target"],
        "masked": True,
        "equivalence": payload["equivalence"],
        "integrity": payload["integrity"],
        "performance": payload["performance"],
    }


def _load_existing_evidence(root: Path) -> Optional[dict[str, Any]]:
    raw, error = _read_bounded_utf8(root, _EVIDENCE_ARTIFACT)
    if raw is None:
        if error is None:
            return None
        raise DatabaseValidationError("evidence_invalid")
    try:
        evidence = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, ValueError):
        raise DatabaseValidationError("evidence_invalid") from None
    _reject_sensitive_fields(evidence, "evidence_invalid")
    if (
        not isinstance(evidence, dict)
        or set(evidence) != _EVIDENCE_FIELDS
        or not _is_strict_int(evidence["schema_version"])
        or evidence["schema_version"] != 1
        or evidence["database_signal"] is not True
        or evidence["change_class"] != "high_risk"
        or not isinstance(evidence["signal_reasons"], list)
        or not all(isinstance(reason, str) for reason in evidence["signal_reasons"])
        or not isinstance(evidence["profile_hash"], str)
        or _SHA256_PATTERN.fullmatch(evidence["profile_hash"]) is None
        or not isinstance(evidence["decision_hash"], str)
        or _SHA256_PATTERN.fullmatch(evidence["decision_hash"]) is None
        or not isinstance(evidence["stages"], dict)
        or not evidence["stages"]
        or not set(evidence["stages"]) <= {"plan", "verify", "test"}
        or not all(isinstance(stage, dict) for stage in evidence["stages"].values())
    ):
        raise DatabaseValidationError("evidence_invalid")
    return evidence


def _parse_utc_timestamp(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    return _parse_timestamp(value)


def _validated_stored_schema(
    schema: object,
    profile: DatabaseProfile,
) -> dict[str, Any]:
    if not isinstance(schema, dict) or set(schema) != {
        "schema_hash",
        "engine",
        "engine_version",
        "captured_at",
        "object_counts",
    }:
        raise DatabaseValidationError("evidence_invalid")
    captured_at = _parse_utc_timestamp(schema["captured_at"])
    counts = schema["object_counts"]
    now = _utc_now()
    if (
        captured_at is None
        or captured_at > now
        or now - captured_at > timedelta(hours=profile.max_schema_age_hours)
        or not isinstance(schema["schema_hash"], str)
        or _SHA256_PATTERN.fullmatch(schema["schema_hash"]) is None
        or _nonempty_string(schema["engine"]) is None
        or _nonempty_string(schema["engine_version"]) is None
        or not isinstance(counts, dict)
        or set(counts) != {"tables", "columns", "indexes", "constraints"}
        or any(not _is_strict_int(count) or count < 0 for count in counts.values())
    ):
        raise DatabaseValidationError("evidence_invalid")
    return schema


def _validate_existing_stage_record(
    stage: str,
    record: object,
    profile: DatabaseProfile,
    decision: DatabaseDecision,
) -> dict[str, Any]:
    expected_fields = {
        "plan": {"status", "checked_at", "schema"},
        "verify": {"status", "checked_at", "schema", "verify"},
        "test": {"status", "checked_at", "schema", "test"},
    }[stage]
    if not isinstance(record, dict) or set(record) != expected_fields:
        raise DatabaseValidationError("evidence_invalid")
    checked_at = _parse_utc_timestamp(record["checked_at"])
    if (
        record["status"] != "pass"
        or checked_at is None
        or checked_at > _utc_now()
    ):
        raise DatabaseValidationError("evidence_invalid")
    schema = _validated_stored_schema(record["schema"], profile)
    if stage == "verify":
        verification = record["verify"]
        if not isinstance(verification, dict) or set(verification) != {
            "production_schema_hash",
            "selected_option_id",
            "equivalence",
            "integrity",
            "query_plan",
            "migration",
            "rollback",
        }:
            raise DatabaseValidationError("evidence_invalid")
        try:
            _validate_verify_evidence(
                {"schema_version": 1, "kind": "database_verify", **verification},
                decision,
                schema["schema_hash"],
            )
        except DatabaseValidationError:
            raise DatabaseValidationError("evidence_invalid") from None
    elif stage == "test":
        test = record["test"]
        if not isinstance(test, dict):
            raise DatabaseValidationError("evidence_invalid")
        if test.get("status") == "waived":
            if (
                set(test) != {"status", "waiver"}
                or decision.local_data_test_waiver is None
                or test["waiver"] != decision.local_data_test_waiver
            ):
                raise DatabaseValidationError("evidence_invalid")
        else:
            if set(test) != {
                "status",
                "production_schema_hash",
                "selected_option_id",
                "local_target",
                "masked",
                "equivalence",
                "integrity",
                "performance",
            }:
                raise DatabaseValidationError("evidence_invalid")
            try:
                _validate_test_evidence(
                    {
                        "schema_version": 1,
                        "kind": "database_test",
                        "raw_production_rows": False,
                        **test,
                    },
                    decision,
                    profile,
                    schema["schema_hash"],
                )
            except DatabaseValidationError:
                raise DatabaseValidationError("evidence_invalid") from None
    return schema


def _validated_existing_stages(
    evidence: dict[str, Any],
    profile: DatabaseProfile,
    decision: DatabaseDecision,
) -> dict[str, Any]:
    stages = evidence["stages"]
    if "plan" not in stages:
        raise DatabaseValidationError("evidence_invalid")
    schemas = {
        name: _validate_existing_stage_record(name, record, profile, decision)
        for name, record in stages.items()
    }
    plan_hash = schemas["plan"]["schema_hash"]
    if any(schema["schema_hash"] != plan_hash for schema in schemas.values()):
        raise DatabaseValidationError("evidence_invalid")
    return schemas["plan"]


_EVIDENCE_LOCK_FILENAME = ".database-validation-evidence.lock"


def _open_artifacts_directory(root: Path) -> int:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd: Optional[int] = None
    try:
        directory_fd = os.open(root, directory_flags)
        for component in (".workflow", "artifacts"):
            try:
                metadata = os.lstat(component, dir_fd=directory_fd)
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=directory_fd)
                metadata = os.lstat(component, dir_fd=directory_fd)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise DatabaseValidationError("evidence_write_failed")
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        result = directory_fd
        directory_fd = None
        return result
    except (OSError, DatabaseValidationError):
        raise DatabaseValidationError("evidence_write_failed") from None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _acquire_evidence_lock(root: Path) -> tuple[int, int]:
    directory_fd = _open_artifacts_directory(root)
    lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            metadata = os.lstat(_EVIDENCE_LOCK_FILENAME, dir_fd=directory_fd)
        except FileNotFoundError:
            metadata = None
        if metadata is not None and (
            stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode)
        ):
            raise DatabaseValidationError("evidence_invalid")
        lock_fd = os.open(
            _EVIDENCE_LOCK_FILENAME,
            lock_flags,
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except (OSError, DatabaseValidationError):
        os.close(directory_fd)
        raise DatabaseValidationError("evidence_invalid") from None
    return directory_fd, lock_fd


def _release_evidence_lock(directory_fd: int, lock_fd: int) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)
        os.close(directory_fd)


def _atomic_write_evidence(directory_fd: int, payload: dict[str, Any]) -> str:
    encoded = _canonical_json_bytes(payload)
    temporary_name: Optional[str] = None
    temporary_fd: Optional[int] = None
    try:
        try:
            metadata = os.lstat(_EVIDENCE_ARTIFACT.rsplit("/", 1)[-1], dir_fd=directory_fd)
        except FileNotFoundError:
            metadata = None
        if metadata is not None and (
            stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode)
        ):
            raise DatabaseValidationError("evidence_invalid")
        for attempt in range(16):
            candidate = (
                f".database-validation-evidence.{os.getpid()}."
                f"{threading.get_ident()}.{attempt}.tmp"
            )
            try:
                temporary_fd = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
                temporary_name = candidate
                break
            except FileExistsError:
                continue
        if temporary_fd is None or temporary_name is None:
            raise DatabaseValidationError("evidence_write_failed")
        os.fchmod(temporary_fd, 0o600)
        written = 0
        while written < len(encoded):
            written += os.write(temporary_fd, encoded[written:])
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        os.replace(
            temporary_name,
            _EVIDENCE_ARTIFACT.rsplit("/", 1)[-1],
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = None
        os.fsync(directory_fd)
    except (OSError, DatabaseValidationError):
        raise DatabaseValidationError("evidence_write_failed") from None
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass
    return hashlib.sha256(encoded).hexdigest()


def _build_evidence(
    signal: DatabaseSignal,
    profile: DatabaseProfile,
    decision: DatabaseDecision,
    stages: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "database_signal": True,
        "signal_reasons": list(signal.reasons),
        "change_class": "high_risk",
        "profile_hash": profile.profile_hash,
        "decision_hash": decision.decision_hash,
        "stages": stages,
    }


def _failed_check(
    stage: str,
    signal: DatabaseSignal,
    code: str,
) -> DatabaseCheckResult:
    return DatabaseCheckResult(
        stage=stage,
        status="fail",
        evidence_path=None,
        evidence_hash=None,
        signal_reasons=signal.reasons,
        blockers=(code,),
    )


def run_database_check(repo_root: Path, stage: str) -> DatabaseCheckResult:
    """Run one DB-evidence stage and atomically merge only validated metadata."""

    try:
        root = _canonical_repo_root(repo_root)
    except DatabaseValidationError as error:
        return _failed_check(
            stage,
            DatabaseSignal(True, (_artifact_error(".workflow", "unsafe_path"),)),
            error.code,
        )
    if stage not in {"plan", "verify", "test"}:
        return _failed_check(stage, DatabaseSignal(False, ()), "stage_invalid")

    signal = detect_database_signal(root)
    if not signal.detected:
        return DatabaseCheckResult(
            stage=stage,
            status="not_applicable",
            evidence_path=None,
            evidence_hash=None,
            signal_reasons=(),
            blockers=(),
        )

    try:
        profile = load_database_profile(root)
        decision = load_database_decision(root)
        pre_run_profile_hash = profile.profile_hash
        pre_run_decision_hash = decision.decision_hash
        existing = _load_existing_evidence(root)
        if existing is not None:
            if existing["profile_hash"] != profile.profile_hash:
                raise DatabaseValidationError("profile_changed")
            if existing["decision_hash"] != decision.decision_hash:
                raise DatabaseValidationError("decision_changed")
            _validated_existing_stages(existing, profile, decision)
        elif stage in {"verify", "test"}:
            raise DatabaseValidationError("plan_evidence_missing")

        schema_payload = _run_json_command(
            profile.schema_command,
            cwd=root,
            timeout_seconds=profile.command_timeout_seconds,
            allowed_fields=_SCHEMA_FIELDS,
        )
        schema = _validate_schema_evidence(schema_payload, profile)
        verify: Optional[dict[str, str]] = None
        test: Optional[dict[str, object]] = None
        if stage == "verify":
            if not profile.verify_command:
                raise DatabaseValidationError("profile_incomplete")
            verify_payload = _run_json_command(
                profile.verify_command,
                cwd=root,
                timeout_seconds=profile.command_timeout_seconds,
                allowed_fields=_VERIFY_FIELDS,
            )
            verify = _validate_verify_evidence(
                verify_payload,
                decision,
                schema["schema_hash"],
            )
        elif stage == "test" and profile.test_command:
            test_payload = _run_json_command(
                profile.test_command,
                cwd=root,
                timeout_seconds=profile.command_timeout_seconds,
                allowed_fields=_TEST_FIELDS,
            )
            test = _validate_test_evidence(
                test_payload,
                decision,
                profile,
                schema["schema_hash"],
            )
        elif stage == "test" and decision.local_data_test_waiver is None:
            raise DatabaseValidationError("test_waiver_missing")

        directory_fd, lock_fd = _acquire_evidence_lock(root)
        try:
            locked_profile = load_database_profile(root)
            locked_decision = load_database_decision(root)
            if locked_profile.profile_hash != pre_run_profile_hash:
                raise DatabaseValidationError("profile_changed")
            if locked_decision.decision_hash != pre_run_decision_hash:
                raise DatabaseValidationError("decision_changed")
            current = _load_existing_evidence(root)
            if current is None:
                if stage in {"verify", "test"}:
                    raise DatabaseValidationError("plan_evidence_missing")
                stages: dict[str, Any] = {}
                locked_plan_schema = None
            else:
                if current["profile_hash"] != locked_profile.profile_hash:
                    raise DatabaseValidationError("profile_changed")
                if current["decision_hash"] != locked_decision.decision_hash:
                    raise DatabaseValidationError("decision_changed")
                locked_plan_schema = _validated_existing_stages(
                    current,
                    locked_profile,
                    locked_decision,
                )
                stages = dict(current["stages"])

            if stage in {"verify", "test"}:
                assert locked_plan_schema is not None
                if schema["schema_hash"] != locked_plan_schema["schema_hash"]:
                    raise DatabaseValidationError("production_schema_changed")

            checked_at = _utc_timestamp(_utc_now())
            if stage == "plan":
                if (
                    stages.get("plan", {}).get("schema", {}).get("schema_hash")
                    != schema["schema_hash"]
                ):
                    stages.pop("verify", None)
                    stages.pop("test", None)
                stages["plan"] = {
                    "status": "pass",
                    "checked_at": checked_at,
                    "schema": schema,
                }
            elif stage == "verify":
                assert verify is not None
                stages["verify"] = {
                    "status": "pass",
                    "checked_at": checked_at,
                    "schema": schema,
                    "verify": verify,
                }
            elif test is not None:
                stages["test"] = {
                    "status": "pass",
                    "checked_at": checked_at,
                    "schema": schema,
                    "test": test,
                }
            else:
                stages["test"] = {
                    "status": "pass",
                    "checked_at": checked_at,
                    "schema": schema,
                    "test": {
                        "status": "waived",
                        "waiver": locked_decision.local_data_test_waiver,
                    },
                }
            evidence = _build_evidence(signal, locked_profile, locked_decision, stages)
            evidence_hash = _atomic_write_evidence(directory_fd, evidence)
        finally:
            _release_evidence_lock(directory_fd, lock_fd)
    except DatabaseValidationError as error:
        return _failed_check(stage, signal, error.code)

    return DatabaseCheckResult(
        stage=stage,
        status="pass",
        evidence_path=_EVIDENCE_ARTIFACT,
        evidence_hash=evidence_hash,
        signal_reasons=signal.reasons,
        blockers=(),
    )
