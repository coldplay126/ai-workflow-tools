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
from typing import Any, Callable, Iterable, Optional, Sequence


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
    "index",
    "indexes",
    "column",
    "columns",
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
        "table_ddl",
        re.compile(r"\b(?:create|alter|drop|truncate)\s+table\b"),
    ),
    (
        "index_ddl",
        re.compile(
            r"\b(?:create|drop)\s+(?:unique\s+)?index\b|"
            r"\balter\s+table\b.*\b(?:add|drop)\s+(?:unique\s+)?(?:index|key)\b"
        ),
    ),
    ("column_ddl", re.compile(r"\b(?:add|drop)\s+column\b")),
    (
        "database engine",
        re.compile(
            r"\b(?:mysql|mariadb|postgres(?:ql)?|sqlite|mongodb|duckdb|"
            r"snowflake|redshift|bigquery)\b"
        ),
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

DATABASE_CHECK_STATUSES = frozenset({"pass", "fail", "not_applicable"})
DATABASE_CHECK_MAX_VALUES = 64
DATABASE_CHECK_EVIDENCE_PATH = ".workflow/artifacts/database-validation-evidence.json"
_DATABASE_CHECK_EVIDENCE_HASH = re.compile(r"^[a-f0-9]{64}$")
DATABASE_CHECK_BLOCKERS = frozenset({
        "repo_root_invalid",
        "artifact_invalid",
        "profile_missing",
        "profile_invalid",
        "profile_incomplete",
        "profile_disabled",
        "decision_missing",
        "decision_invalid",
        "command_start_failed",
        "command_timeout",
        "command_output_oversize",
        "command_nonzero",
        "command_output_invalid",
        "command_output_unsafe",
        "schema_evidence_invalid",
        "verify_evidence_invalid",
        "test_evidence_invalid",
        "evidence_invalid",
        "evidence_write_failed",
        "profile_changed",
        "decision_changed",
        "database_signal_changed",
        "plan_evidence_missing",
        "test_waiver_missing",
        "production_schema_changed",
        "stage_invalid",
        "signal_callback_failed",
})
DATABASE_CHECK_TEXT_REASONS = frozenset(
    {
        *(f"text:{reason}" for reason, _ in _STRONG_TEXT_SIGNAL_PATTERNS),
        *(f"text:{reason}" for reason, _ in _WEAK_TEXT_SIGNAL_PATTERNS),
    }
)
_ARTIFACT_CATEGORIES = {
    ".workflow": "workflow",
    ".workflow/concept.md": "concept",
    ".workflow/artifacts/spec.md": "spec",
    ".workflow/artifacts/plan.md": "plan",
    ".workflow/artifacts/tasks.md": "tasks",
    ".workflow/artifacts/test-criteria.md": "test_criteria",
    ".workflow/artifacts/allowed-files.json": "allowed_files",
}
_ARTIFACT_ERROR_CODES = frozenset(
    {"unsafe_path", "unreadable", "oversize", "invalid_utf8", "invalid_json", "invalid_shape"}
)
DATABASE_CHECK_ARTIFACT_REASONS = frozenset(
    f"artifact_error:{category}:{code}"
    for category in _ARTIFACT_CATEGORIES.values()
    for code in _ARTIFACT_ERROR_CODES
)
_DATABASE_PATH_CATEGORY = {
    "migration": "migration",
    "migrations": "migration",
    "model": "model",
    "models": "model",
    "entity": "entity",
    "entities": "entity",
    "repository": "repository",
    "repositories": "repository",
    "query": "query",
    "queries": "query",
    "index": "index",
    "indexes": "index",
    "column": "column",
    "columns": "column",
    "database": "database",
    "databases": "database",
    "schema": "schema",
    "schemas": "schema",
}
_DATABASE_PATH_REASON = re.compile(
    r"^path:(?:(?:sql|prisma|migration|model|entity|repository|query|index|column|database|schema):[a-f0-9]{16}|truncated:[1-9][0-9]*)$"
)


@dataclass(frozen=True)
class DatabaseSignal:
    """Normalized database-change classification for workflow artifacts."""

    detected: bool
    reasons: tuple[str, ...]
    snapshot_hash: str


def _artifact_error(relative_path: str, code: str) -> str:
    return f"artifact_error:{_ARTIFACT_CATEGORIES[relative_path]}:{code}"


def _database_path_reason(path: str) -> str:
    categories = {
        _DATABASE_PATH_CATEGORY[part]
        for part in Path(path).parts[:-1]
        if part in _DATABASE_PATH_CATEGORY
    }
    category = (
        "prisma"
        if Path(path).suffix == ".prisma"
        else next(
            (
                candidate
                for candidate in (
                    "migration",
                    "query",
                    "index",
                    "column",
                    "schema",
                    "entity",
                    "model",
                    "database",
                    "repository",
                )
                if candidate in categories
            ),
            "sql" if Path(path).suffix == ".sql" else "database",
        )
    )
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    return f"path:{category}:{digest}"

def _bounded_database_signal_reasons(reasons: set[str]) -> tuple[str, ...]:
    normalized = tuple(sorted(reasons))
    if len(normalized) <= DATABASE_CHECK_MAX_VALUES:
        return normalized

    static_reasons = [reason for reason in normalized if not reason.startswith("path:")]
    path_reasons = [reason for reason in normalized if reason.startswith("path:")]
    path_slots = DATABASE_CHECK_MAX_VALUES - len(static_reasons)
    if path_slots <= 0:
        return tuple(static_reasons[:DATABASE_CHECK_MAX_VALUES])

    retained_paths = path_reasons[: path_slots - 1]
    truncated_count = len(path_reasons) - len(retained_paths)
    return tuple(
        sorted(
            [
                *static_reasons,
                *retained_paths,
                f"path:truncated:{truncated_count}",
            ]
        )
    )


def _database_signal_snapshot_hash(inputs: list[dict[str, object]]) -> str:
    return hashlib.sha256(
        json.dumps(
            inputs,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

def is_database_check_evidence_hash(value: object) -> bool:
    """Return whether an evidence hash has the canonical SHA-256 form."""
    return isinstance(value, str) and _DATABASE_CHECK_EVIDENCE_HASH.fullmatch(value) is not None


def is_database_check_reason(value: object) -> bool:
    """Return whether a reason is safe for CLI/state exposure."""
    return (
        isinstance(value, str)
        and (
            value in DATABASE_CHECK_TEXT_REASONS
            or value in DATABASE_CHECK_ARTIFACT_REASONS
            or _DATABASE_PATH_REASON.fullmatch(value) is not None
        )
    )


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
    """Return DB signals and a canonical snapshot of every known input."""
    try:
        root = _canonical_repo_root(repo_root)
    except DatabaseValidationError:
        inputs = [{"path": ".workflow", "error": "unsafe_path"}]
        return DatabaseSignal(
            detected=True,
            reasons=(_artifact_error(".workflow", "unsafe_path"),),
            snapshot_hash=_database_signal_snapshot_hash(inputs),
        )

    reasons = set()
    inputs: list[dict[str, object]] = []
    for relative_path in _TEXT_ARTIFACTS:
        text, error = _read_bounded_utf8(root, relative_path)
        if error:
            reasons.add(_artifact_error(relative_path, error))
            inputs.append({"path": relative_path, "error": error})
        elif text is None:
            inputs.append({"path": relative_path, "error": "missing"})
        else:
            inputs.append(
                {
                    "path": relative_path,
                    "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
            )
            reasons.update(_text_reasons(text))

    paths, error = _normalized_allowed_paths(root)
    allowed_input: dict[str, object] = {"path": _ALLOWED_FILES_ARTIFACT}
    if error:
        reasons.add(_artifact_error(_ALLOWED_FILES_ARTIFACT, error))
        allowed_input["error"] = error
    else:
        allowed_input["paths"] = list(paths)
        for path in paths:
            if _is_database_path(path):
                reasons.add(_database_path_reason(path))
    inputs.append(allowed_input)

    normalized_reasons = _bounded_database_signal_reasons(reasons)
    return DatabaseSignal(
        detected=bool(normalized_reasons),
        reasons=normalized_reasons,
        snapshot_hash=_database_signal_snapshot_hash(inputs),
    )


_PROFILE_ARTIFACT = ".workflow/manifest.json"
_DECISION_ARTIFACT = ".workflow/artifacts/database-decision.json"
_EVIDENCE_ARTIFACT = DATABASE_CHECK_EVIDENCE_PATH
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
_MAX_PERSISTED_TEXT_BYTES = 4 * 1024
_MAX_PERSISTED_IDENTIFIER_BYTES = 128
_MAX_ENGINE_IDENTIFIER_BYTES = 64
_MAX_ENGINE_VERSION_BYTES = 64
_ENGINE_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_ENGINE_VERSION = re.compile(r"^[vV]?[0-9]+(?:[._+-][0-9A-Za-z]+)*$")
_PERSISTED_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_PERSISTED_SECRET = re.compile(
    r"\b(?:api[_-]?key|access[_-]?token|password|passwd|secret|"
    r"credential|dsn|connection[_-]?string)\b\s*(?:=|:)|\bbearer\s+\S+",
    re.IGNORECASE,
)
_PERSISTED_DDL = re.compile(
    r"\b(?:create|alter|drop|truncate)\s+(?:table|index|schema|database)|"
    r"\b(?:add|drop)\s+column\b",
    re.IGNORECASE,
)
_PERSISTED_RAW_DATA = re.compile(
    r"\b(?:select\s+.+\s+from|insert\s+into|update\s+\w+\s+set|delete\s+from|"
    r"raw(?:\s+(?:production|database))?\s+(?:data|rows?|records?|samples?))\b",
    re.IGNORECASE,
)
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
    "partition",
}
_SIGNAL_STRUCTURAL_SURFACES = {
    "index",
    "column",
    "constraint",
    "erd",
    "normalize",
    "denormalize",
    "partition",
}
_STRUCTURAL_SURFACES = _SIGNAL_STRUCTURAL_SURFACES
_VERIFY_EXECUTION_TARGETS = {"local_same_engine", "approved_read_replica"}
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
    "engine",
    "execution_target",
    "production_primary_queries",
    "raw_production_rows",
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
    "signal_hash",
    "change_class",
    "profile_hash",
    "decision_hash",
    "stages",
}
_STAGE_IDENTITY_FIELDS = {"signal_hash", "profile_hash", "decision_hash"}
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


def _within_byte_limit(value: str, maximum: int) -> bool:
    return len(value.encode("utf-8")) <= maximum


def _is_safe_persisted_text(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if (
        not _within_byte_limit(value, _MAX_PERSISTED_TEXT_BYTES)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or _URI_VALUE.search(value) is not None
        or _PERSISTED_SECRET.search(value) is not None
        or _PERSISTED_DDL.search(value) is not None
        or _PERSISTED_RAW_DATA.search(value) is not None
    ):
        return False
    return True


def _is_safe_persisted_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and _within_byte_limit(value, _MAX_PERSISTED_IDENTIFIER_BYTES)
        and _PERSISTED_IDENTIFIER.fullmatch(value) is not None
    )


def _is_safe_engine_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and _within_byte_limit(value, _MAX_ENGINE_IDENTIFIER_BYTES)
        and _ENGINE_IDENTIFIER.fullmatch(value) is not None
    )


def _is_safe_engine_version(value: object) -> bool:
    return (
        isinstance(value, str)
        and _within_byte_limit(value, _MAX_ENGINE_VERSION_BYTES)
        and _ENGINE_VERSION.fullmatch(value) is not None
    )


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
    return isinstance(value, list) and all(_is_safe_persisted_text(item) for item in value)


def _validate_structured_assessment(
    value: object,
    *,
    fields: set[str],
) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise DatabaseValidationError("decision_invalid")
    _reject_sensitive_fields(value, "decision_invalid")
    if any(not _is_safe_persisted_text(entry) for entry in value.values()):
        raise DatabaseValidationError("decision_invalid")


def _validate_candidate(candidate: object) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise DatabaseValidationError("decision_invalid")
    _reject_sensitive_fields(candidate, "decision_invalid")
    if set(candidate) != _CANDIDATE_FIELDS:
        raise DatabaseValidationError("decision_invalid")
    if (
        not _is_safe_persisted_identifier(candidate["id"])
        or candidate["kind"] not in _DECISION_KINDS
        or not isinstance(candidate["applicable"], bool)
        or any(
            not _is_safe_persisted_text(candidate[field])
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
            and not _is_safe_persisted_text(candidate["normalization_assessment"])
        )
        or not _validate_string_list(candidate["operational_risks"])
        or not _validate_string_list(candidate["transition_risks"])
    ):


        raise DatabaseValidationError("decision_invalid")

    unavailable_reason = candidate["unavailable_reason"]
    if candidate["applicable"]:
        if unavailable_reason not in (None, ""):
            raise DatabaseValidationError("decision_invalid")
    elif not _is_safe_persisted_text(unavailable_reason):
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
    reason = value["reason"] if _is_safe_persisted_text(value["reason"]) else None
    approver = value["approver"] if _is_safe_persisted_text(value["approver"]) else None
    timestamp = _nonempty_string(value["timestamp"])
    parsed_timestamp = _parse_timestamp(timestamp) if timestamp else None
    if not reason or not approver or parsed_timestamp is None:
        raise DatabaseValidationError("decision_invalid")
    return {
        "reason": reason,
        "approver": approver,
        "timestamp": _utc_timestamp(parsed_timestamp),
    }


def _validate_decision_signal_surfaces(
    root: Path,
    change_surfaces: tuple[str, ...],
) -> None:
    signal = detect_database_signal(root)
    if any(reason.startswith("artifact_error:") for reason in signal.reasons):
        raise DatabaseValidationError("artifact_invalid")

    signal_reasons = set(signal.reasons)
    requires_path_structural_surface = any(
        reason.startswith(
            (
                "path:model:",
                "path:entity:",
                "path:migration:",
                "path:schema:",
                "path:prisma:",
            )
        )
        for reason in signal_reasons
    )
    required_surfaces = {
        surface
        for reason, surface in (
            ("text:normalization", "normalize"),
            ("text:denormalization", "denormalize"),
            ("text:erd", "erd"),
            ("text:foreign key", "constraint"),
            ("text:primary key", "constraint"),
            ("text:unique constraint", "constraint"),
            ("text:partition", "partition"),
            ("text:index", "index"),
            ("text:index_ddl", "index"),
            ("text:column", "column"),
            ("text:column_ddl", "column"),
            ("text:query", "query"),
        )
        if reason in signal_reasons
    }
    required_surfaces.update(
        surface
        for reason, surface in (
            ("path:index:", "index"),
            ("path:column:", "column"),
            ("path:query:", "query"),
        )
        if any(signal_reason.startswith(reason) for signal_reason in signal_reasons)
    )
    surfaces = set(change_surfaces)
    if (
        (
            "text:table_ddl" in signal_reasons
            and not surfaces & (_SIGNAL_STRUCTURAL_SURFACES - {"index"})
        )
        or (
            requires_path_structural_surface
            and not surfaces & _SIGNAL_STRUCTURAL_SURFACES
        )
        or not required_surfaces <= surfaces
    ):
        raise DatabaseValidationError("decision_invalid")


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

    _validate_decision_signal_surfaces(root, normalized_surfaces)

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
    required_candidate_kinds = {
        "query": "query_change",
        "index": "physical_design",
        "normalize": "normalize",
        "denormalize": "denormalize",
    }
    if any(
        not any(
            candidate["applicable"] and candidate["kind"] == required_kind
            for candidate in parsed_candidates
        )
        for surface, required_kind in required_candidate_kinds.items()
        if surface in normalized_surfaces
    ):
        raise DatabaseValidationError("decision_invalid")

    baseline_id = decision["baseline_option_id"] if _is_safe_persisted_identifier(decision["baseline_option_id"]) else None
    recommended_id = decision["recommended_option_id"] if _is_safe_persisted_identifier(decision["recommended_option_id"]) else None
    selected_id = decision["selected_option_id"] if _is_safe_persisted_identifier(decision["selected_option_id"]) else None
    if (
        baseline_id not in candidates_by_id
        or recommended_id not in candidates_by_id
        or selected_id not in candidates_by_id
        or candidates_by_id[baseline_id]["kind"] != "maintain"
        or not candidates_by_id[recommended_id]["applicable"]
        or not candidates_by_id[selected_id]["applicable"]
        or not _is_safe_persisted_text(decision["recommendation_rationale"])
    ):
        raise DatabaseValidationError("decision_invalid")
    if (
        set(normalized_surfaces) & _NORMALIZATION_SURFACES
        and not _is_safe_persisted_text(candidates_by_id[selected_id]["normalization_assessment"])
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
        or not _is_safe_engine_identifier(payload["engine"])
        or not _is_safe_engine_version(payload["engine_version"])
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
        "engine": payload["engine"],
        "engine_version": payload["engine_version"],
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
    schema: dict[str, Any],
) -> dict[str, object]:
    statuses = ("equivalence", "integrity", "query_plan", "migration", "rollback")
    if (
        not _is_strict_int(payload["schema_version"])
        or payload["schema_version"] != 1
        or payload["kind"] != "database_verify"
        or payload["production_schema_hash"] != schema["schema_hash"]
        or not _is_safe_persisted_identifier(payload["selected_option_id"])
        or payload["selected_option_id"] != decision.selected_option_id
        or payload["engine"] != schema["engine"]
        or payload["execution_target"] not in _VERIFY_EXECUTION_TARGETS
        or payload["production_primary_queries"] is not False
        or payload["raw_production_rows"] is not False
        or any(payload[field] not in {"pass", "fail", "not_applicable"} for field in statuses)
        or payload["equivalence"] != "pass"
        or payload["integrity"] != "pass"
        or any(payload[field] == "fail" for field in ("query_plan", "migration", "rollback"))
    ):
        raise DatabaseValidationError("verify_evidence_invalid")
    if (
        set(decision.change_surfaces) & _SIGNAL_STRUCTURAL_SURFACES
        and payload["execution_target"] != "local_same_engine"
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
        "engine": payload["engine"],
        "execution_target": payload["execution_target"],
        "production_primary_queries": False,
        "raw_production_rows": False,
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
        or not _is_safe_persisted_identifier(payload["selected_option_id"])
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
        "raw_production_rows": False,
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
        or not all(is_database_check_reason(reason) for reason in evidence["signal_reasons"])
        or not isinstance(evidence["signal_hash"], str)
        or _SHA256_PATTERN.fullmatch(evidence["signal_hash"]) is None
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
    profile: Optional[DatabaseProfile],
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
        or (
            profile is not None
            and now - captured_at > timedelta(hours=profile.max_schema_age_hours)
        )
        or not isinstance(schema["schema_hash"], str)
        or _SHA256_PATTERN.fullmatch(schema["schema_hash"]) is None
        or not _is_safe_engine_identifier(schema["engine"])
        or not _is_safe_engine_version(schema["engine_version"])
        or not isinstance(counts, dict)
        or set(counts) != {"tables", "columns", "indexes", "constraints"}
        or any(not _is_strict_int(count) or count < 0 for count in counts.values())
    ):
        raise DatabaseValidationError("evidence_invalid")
    return schema


def _database_evidence_identity(
    signal: DatabaseSignal,
    profile: DatabaseProfile,
    decision: DatabaseDecision,
) -> dict[str, str]:
    return {
        "signal_hash": signal.snapshot_hash,
        "profile_hash": profile.profile_hash,
        "decision_hash": decision.decision_hash,
    }


def _stored_evidence_identity(evidence: dict[str, Any]) -> dict[str, str]:
    return {field: evidence[field] for field in _STAGE_IDENTITY_FIELDS}


def _evidence_matches_current(
    evidence: dict[str, Any],
    signal: DatabaseSignal,
    profile: DatabaseProfile,
    decision: DatabaseDecision,
) -> bool:
    return (
        _stored_evidence_identity(evidence)
        == _database_evidence_identity(signal, profile, decision)
        and tuple(evidence["signal_reasons"]) == signal.reasons
    )


def _validate_stored_verify_shape(
    verification: object,
    schema: dict[str, Any],
) -> None:
    statuses = ("equivalence", "integrity", "query_plan", "migration", "rollback")
    if (
        not isinstance(verification, dict)
        or set(verification)
        != {
            "production_schema_hash",
            "selected_option_id",
            "engine",
            "execution_target",
            "production_primary_queries",
            "raw_production_rows",
            "equivalence",
            "integrity",
            "query_plan",
            "migration",
            "rollback",
        }
        or verification["production_schema_hash"] != schema["schema_hash"]
        or not _is_safe_persisted_identifier(verification["selected_option_id"])
        or verification["engine"] != schema["engine"]
        or verification["execution_target"] not in _VERIFY_EXECUTION_TARGETS
        or verification["production_primary_queries"] is not False
        or verification["raw_production_rows"] is not False
        or any(verification[field] not in {"pass", "fail", "not_applicable"} for field in statuses)
        or verification["equivalence"] != "pass"
        or verification["integrity"] != "pass"
        or any(verification[field] == "fail" for field in ("query_plan", "migration", "rollback"))
    ):
        raise DatabaseValidationError("evidence_invalid")


def _validate_stored_test_shape(
    test: object,
    schema_hash: str,
    profile: Optional[DatabaseProfile],
) -> None:
    if not isinstance(test, dict):
        raise DatabaseValidationError("evidence_invalid")
    if test.get("status") == "waived":
        if profile is not None and profile.test_command:
            raise DatabaseValidationError("evidence_invalid")
        if set(test) != {"status", "waiver"}:
            raise DatabaseValidationError("evidence_invalid")
        try:
            waiver = _validate_waiver(test["waiver"])
        except DatabaseValidationError:
            raise DatabaseValidationError("evidence_invalid") from None
        if waiver != test["waiver"]:
            raise DatabaseValidationError("evidence_invalid")
        return
    if (
        set(test)
        != {
            "status",
            "production_schema_hash",
            "selected_option_id",
            "local_target",
            "masked",
            "raw_production_rows",
            "equivalence",
            "integrity",
            "performance",
        }
        or test["status"] != "pass"
        or test["production_schema_hash"] != schema_hash
        or not _is_safe_persisted_identifier(test["selected_option_id"])
        or test["local_target"] not in _LOCAL_TARGETS
        or test["masked"] is not True
        or test["raw_production_rows"] is not False
        or any(test[field] != "pass" for field in ("equivalence", "integrity", "performance"))
    ):
        raise DatabaseValidationError("evidence_invalid")


def _validate_existing_stage_record(
    stage: str,
    record: object,
    profile: Optional[DatabaseProfile],
    decision: Optional[DatabaseDecision],
    identity: dict[str, str],
) -> dict[str, Any]:
    expected_fields = {
        "plan": {"status", "checked_at", "schema"},
        "verify": {"status", "checked_at", "schema", "verify"},
        "test": {"status", "checked_at", "schema", "test"},
    }[stage] | _STAGE_IDENTITY_FIELDS
    if (
        not isinstance(record, dict)
        or set(record) != expected_fields
        or any(record[field] != identity[field] for field in _STAGE_IDENTITY_FIELDS)
    ):
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
        _validate_stored_verify_shape(record["verify"], schema)
        if decision is not None:
            try:
                _validate_verify_evidence(
                    {"schema_version": 1, "kind": "database_verify", **record["verify"]},
                    decision,
                    schema,
                )
            except DatabaseValidationError:
                raise DatabaseValidationError("evidence_invalid") from None
    elif stage == "test":
        _validate_stored_test_shape(
            record["test"],
            schema["schema_hash"],
            profile,
        )
        if decision is not None and record["test"]["status"] == "waived":
            if (
                decision.local_data_test_waiver is None
                or record["test"]["waiver"] != decision.local_data_test_waiver
            ):
                raise DatabaseValidationError("evidence_invalid")
        elif decision is not None:
            try:
                _validate_test_evidence(
                    {
                        "schema_version": 1,
                        "kind": "database_test",
                        **record["test"],
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
    profile: Optional[DatabaseProfile],
    decision: Optional[DatabaseDecision],
    identity: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    stages = evidence["stages"]
    if "plan" not in stages:
        raise DatabaseValidationError("evidence_invalid")
    expected_identity = identity or _stored_evidence_identity(evidence)
    schemas = {
        name: _validate_existing_stage_record(
            name,
            record,
            profile,
            decision,
            expected_identity,
        )
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
        "signal_hash": signal.snapshot_hash,
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


def run_database_check(
    repo_root: Path,
    stage: str,
    *,
    on_database_signal: Optional[Callable[[tuple[str, ...]], None]] = None,
) -> DatabaseCheckResult:
    """Run one DB-evidence stage and atomically merge only validated metadata."""

    try:
        root = _canonical_repo_root(repo_root)
    except DatabaseValidationError as error:
        return _failed_check(
            stage,
            DatabaseSignal(
                True,
                (_artifact_error(".workflow", "unsafe_path"),),
                _database_signal_snapshot_hash(
                    [{"path": ".workflow", "error": "unsafe_path"}]
                ),
            ),
            error.code,
        )
    if stage not in {"plan", "verify", "test"}:
        return _failed_check(
            stage,
            DatabaseSignal(False, (), _database_signal_snapshot_hash([])),
            "stage_invalid",
        )

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
    if any(reason.startswith("artifact_error:") for reason in signal.reasons):
        return _failed_check(stage, signal, "artifact_invalid")

    if stage == "plan" and on_database_signal is not None:
        try:
            on_database_signal(signal.reasons)
        except Exception:
            return _failed_check(stage, signal, "signal_callback_failed")


    try:
        profile = load_database_profile(root)
        decision = load_database_decision(root)
        pre_run_profile_hash = profile.profile_hash
        pre_run_decision_hash = decision.decision_hash
        current_identity = _database_evidence_identity(signal, profile, decision)
        existing = _load_existing_evidence(root)
        prior_stale = False
        if existing is not None:
            _validated_existing_stages(existing, None, None)
            prior_stale = not _evidence_matches_current(
                existing,
                signal,
                profile,
                decision,
            )
            if stage in {"verify", "test"}:
                if existing["profile_hash"] != profile.profile_hash:
                    raise DatabaseValidationError("profile_changed")
                if existing["decision_hash"] != decision.decision_hash:
                    raise DatabaseValidationError("decision_changed")
                if (
                    existing["signal_hash"] != signal.snapshot_hash
                    or tuple(existing["signal_reasons"]) != signal.reasons
                ):
                    raise DatabaseValidationError("database_signal_changed")
                _validated_existing_stages(existing, profile, decision, current_identity)
            elif not prior_stale:
                _validated_existing_stages(existing, profile, decision, current_identity)
        elif stage in {"verify", "test"}:
            raise DatabaseValidationError("plan_evidence_missing")

        schema_payload = _run_json_command(
            profile.schema_command,
            cwd=root,
            timeout_seconds=profile.command_timeout_seconds,
            allowed_fields=_SCHEMA_FIELDS,
        )
        schema = _validate_schema_evidence(schema_payload, profile)
        verify: Optional[dict[str, object]] = None
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
                schema,
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
            locked_signal = detect_database_signal(root)
            if (
                not locked_signal.detected
                or locked_signal.snapshot_hash != signal.snapshot_hash
                or locked_signal.reasons != signal.reasons
            ):
                raise DatabaseValidationError("database_signal_changed")
            locked_profile = load_database_profile(root)
            locked_decision = load_database_decision(root)
            if locked_profile.profile_hash != pre_run_profile_hash:
                raise DatabaseValidationError("profile_changed")
            if locked_decision.decision_hash != pre_run_decision_hash:
                raise DatabaseValidationError("decision_changed")
            locked_identity = _database_evidence_identity(
                locked_signal,
                locked_profile,
                locked_decision,
            )
            current = _load_existing_evidence(root)
            if current is None:
                if stage in {"verify", "test"}:
                    raise DatabaseValidationError("plan_evidence_missing")
                stages: dict[str, Any] = {}
                locked_plan_schema = None
                refresh_invalidates_downstream = False
            else:
                _validated_existing_stages(current, None, None)
                current_stale = not _evidence_matches_current(
                    current,
                    locked_signal,
                    locked_profile,
                    locked_decision,
                )
                refresh_invalidates_downstream = prior_stale or current_stale
                if stage == "plan" and not refresh_invalidates_downstream:
                    _validated_existing_stages(
                        current,
                        locked_profile,
                        locked_decision,
                        locked_identity,
                    )
                if stage in {"verify", "test"}:
                    if current["profile_hash"] != locked_profile.profile_hash:
                        raise DatabaseValidationError("profile_changed")
                    if current["decision_hash"] != locked_decision.decision_hash:
                        raise DatabaseValidationError("decision_changed")
                    if (
                        current["signal_hash"] != locked_signal.snapshot_hash
                        or tuple(current["signal_reasons"]) != locked_signal.reasons
                    ):
                        raise DatabaseValidationError("database_signal_changed")
                    locked_plan_schema = _validated_existing_stages(
                        current,
                        locked_profile,
                        locked_decision,
                        locked_identity,
                    )
                else:
                    locked_plan_schema = current["stages"]["plan"]["schema"]
                stages = dict(current["stages"])

            if stage in {"verify", "test"}:
                assert locked_plan_schema is not None
                if schema["schema_hash"] != locked_plan_schema["schema_hash"]:
                    raise DatabaseValidationError("production_schema_changed")

            checked_at = _utc_timestamp(_utc_now())
            if stage == "plan":
                if (
                    refresh_invalidates_downstream
                    or stages.get("plan", {}).get("schema", {}).get("schema_hash")
                    != schema["schema_hash"]
                ):
                    stages.pop("verify", None)
                    stages.pop("test", None)
                stages["plan"] = {
                    "status": "pass",
                    "checked_at": checked_at,
                    **locked_identity,
                    "schema": schema,
                }
            elif stage == "verify":
                assert verify is not None
                stages["verify"] = {
                    "status": "pass",
                    "checked_at": checked_at,
                    **locked_identity,
                    "schema": schema,
                    "verify": verify,
                }
            elif test is not None:
                stages["test"] = {
                    "status": "pass",
                    "checked_at": checked_at,
                    **locked_identity,
                    "schema": schema,
                    "test": test,
                }
            else:
                stages["test"] = {
                    "status": "pass",
                    "checked_at": checked_at,
                    **locked_identity,
                    "schema": schema,
                    "test": {
                        "status": "waived",
                        "waiver": locked_decision.local_data_test_waiver,
                    },
                }
            evidence = _build_evidence(
                locked_signal,
                locked_profile,
                locked_decision,
                stages,
            )
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


_DATABASE_GATE_CONDITIONS = {
    "plan": (
        "database.signal",
        "database.risk_class",
        "database.decision",
        "database.production_schema",
    ),
    "verify": (
        "database.production_schema",
        "database.risk_class",
        "database.equivalence",
        "database.integrity",
        "database.query_plan",
        "database.migration",
        "database.rollback",
    ),
    "test": (
        "database.production_schema",
        "database.risk_class",
        "database.local_test",
    ),
}


def _database_gate_evaluation(
    condition: str,
    passed: bool,
    *,
    stage: Optional[str] = None,
    status: str,
    schema: Optional[dict[str, Any]] = None,
    decision: Optional[DatabaseDecision] = None,
    local_target: Optional[str] = None,
    waiver_present: bool = False,
) -> dict[str, Any]:
    evaluation: dict[str, Any] = {
        "condition": condition,
        "passed": passed,
        "detail": f"stage={stage},status={status}" if stage else f"status={status}",
    }
    if stage is None:
        return evaluation

    summary: dict[str, str] = {"stage": stage, "status": status}
    if schema is not None:
        summary.update(
            {
                "schema_hash_prefix": schema["schema_hash"][:12],
                "engine": schema["engine"],
                "engine_version": schema["engine_version"],
            }
        )
    if decision is not None:
        summary["selected_option"] = decision.selected_option_id
    if local_target is not None:
        summary["local_target"] = local_target
    if waiver_present:
        summary["waiver_present"] = "true"
    evaluation["database_summary"] = summary
    return evaluation


def _workflow_database_risk_is_current(
    root: Path,
    signal: DatabaseSignal,
) -> bool:
    """Require the state promotion and its exact audit event for this signal."""

    try:
        from awf.core.state import load_workflow_state

        state = load_workflow_state(str(root))
    except (FileNotFoundError, OSError, ValueError):
        return False
    if not isinstance(state, dict) or state.get("changeClass") != "high_risk":
        return False
    history = state.get("history")
    if not isinstance(history, list):
        return False
    expected_reasons = list(signal.reasons)
    return any(
        isinstance(event, dict)
        and event.get("action") == "database_risk_escalated"
        and event.get("reasons") == expected_reasons
        for event in history
    )


def evaluate_database_gate(repo_root: Path, stage: str) -> list[dict[str, Any]]:
    """Return mandatory, sanitized database-evidence conditions for one gate."""

    if stage not in _DATABASE_GATE_CONDITIONS:
        raise ValueError(f"database_gate_stage_invalid:{stage}")

    try:
        root = _canonical_repo_root(repo_root)
        signal = detect_database_signal(root)
    except DatabaseValidationError:
        signal = DatabaseSignal(
            True,
            (),
            _database_signal_snapshot_hash(
                [{"path": ".workflow", "error": "unsafe_path"}]
            ),
        )
        root = repo_root

    if not signal.detected:
        evaluation = _database_gate_evaluation(
            "database.signal",
            True,
            status="not_applicable",
        )
        if stage in {"verify", "test"}:
            evaluation["database_summary"] = {
                "stage": stage,
                "status": "not_applicable",
            }
        return [evaluation]
    workflow_risk_is_current = _workflow_database_risk_is_current(root, signal)


    decision: Optional[DatabaseDecision] = None
    profile: Optional[DatabaseProfile] = None
    evidence: Optional[dict[str, Any]] = None
    schema: Optional[dict[str, Any]] = None
    evidence_is_high_risk = False
    signal_is_current = False
    decision_is_current = False
    stage_is_current = False

    try:
        decision = load_database_decision(root)
    except DatabaseValidationError:
        pass
    try:
        profile = load_database_profile(root)
    except DatabaseValidationError:
        pass
    try:
        evidence = _load_existing_evidence(root)
        if evidence is not None:
            signal_is_current = (
                evidence["signal_hash"] == signal.snapshot_hash
                and tuple(evidence["signal_reasons"]) == signal.reasons
            )
            evidence_is_high_risk = signal_is_current
    except DatabaseValidationError:
        pass

    if evidence is not None and decision is not None:
        decision_is_current = evidence["decision_hash"] == decision.decision_hash

    if (
        evidence is not None
        and profile is not None
        and decision is not None
        and signal_is_current
        and evidence["profile_hash"] == profile.profile_hash
        and decision_is_current
    ):
        try:
            identity = _database_evidence_identity(signal, profile, decision)
            _validated_existing_stages(evidence, profile, decision, identity)
            record = evidence["stages"].get(stage)
            if record is not None:
                schema = _validate_existing_stage_record(
                    stage,
                    record,
                    profile,
                    decision,
                    identity,
                )
                stage_is_current = True
        except DatabaseValidationError:
            schema = None

    if stage == "plan":
        return [
            _database_gate_evaluation(
                "database.signal",
                signal_is_current,
                status="pass" if signal_is_current else "fail",
            ),
            _database_gate_evaluation(
                "database.risk_class",
                workflow_risk_is_current,
                stage="plan",
                status="pass" if workflow_risk_is_current else "fail",
            ),
            _database_gate_evaluation(
                "database.decision",
                decision_is_current,
                stage="plan",
                status="pass" if decision_is_current else "fail",
                decision=decision if decision_is_current else None,
            ),
            _database_gate_evaluation(
                "database.production_schema",
                stage_is_current,
                stage="plan",
                status="pass" if stage_is_current else "fail",
                schema=schema,
                decision=decision if stage_is_current else None,
            ),
        ]

    if stage == "verify":
        verification = (
            evidence["stages"]["verify"]["verify"]
            if stage_is_current and evidence is not None
            else None
        )
        return [
            _database_gate_evaluation(
                "database.production_schema",
                stage_is_current,
                stage="verify",
                status="pass" if stage_is_current else "fail",
                schema=schema,
                decision=decision if stage_is_current else None,
            ),
            _database_gate_evaluation(
                "database.risk_class",
                workflow_risk_is_current,
                stage="verify",
                status="pass" if workflow_risk_is_current else "fail",
            ),
            *[
                _database_gate_evaluation(
                    f"database.{name}",
                    verification is not None
                    and verification[name] in {"pass", "not_applicable"},
                    stage="verify",
                    status=(
                        verification[name]
                        if verification is not None
                        else "fail"
                    ),
                    schema=schema,
                    decision=decision if verification is not None else None,
                )
                for name in ("equivalence", "integrity", "query_plan", "migration", "rollback")
            ],
        ]

    test = (
        evidence["stages"]["test"]["test"]
        if stage_is_current and evidence is not None
        else None
    )
    if test is not None and test["status"] == "waived":
        return [
            _database_gate_evaluation(
                "database.production_schema",
                True,
                stage="test",
                status="pass",
                schema=schema,
                decision=decision,
            ),
            _database_gate_evaluation(
                "database.risk_class",
                workflow_risk_is_current,
                stage="test",
                status="pass" if workflow_risk_is_current else "fail",
            ),
            _database_gate_evaluation(
                "database.local_test",
                True,
                stage="test",
                status="waived",
                decision=decision,
                waiver_present=True,
            ),
        ]

    local_target = test["local_target"] if test is not None else None
    return [
        _database_gate_evaluation(
            "database.production_schema",
            stage_is_current,
            stage="test",
            status="pass" if stage_is_current else "fail",
            schema=schema,
            decision=decision if stage_is_current else None,
        ),
        _database_gate_evaluation(
            "database.risk_class",
            workflow_risk_is_current,
            stage="test",
            status="pass" if workflow_risk_is_current else "fail",
        ),
        _database_gate_evaluation(
            "database.local_test",
            test is not None and test["status"] == "pass",
            stage="test",
            status="pass" if test is not None and test["status"] == "pass" else "fail",
            schema=schema,
            decision=decision if test is not None else None,
            local_target=local_target,
        ),
    ]
