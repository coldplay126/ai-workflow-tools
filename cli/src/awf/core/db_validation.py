"""Detect database-affecting workflow artifacts without scanning a repository."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Iterable, Optional


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


def _read_bounded_utf8(path: Path) -> tuple[Optional[str], Optional[str]]:
    if not path.exists():
        return None, None
    if not path.is_file():
        return None, "unreadable"
    try:
        with path.open("rb") as artifact:
            raw = artifact.read(_MAX_ARTIFACT_BYTES + 1)
    except OSError:
        return None, "unreadable"
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
    path = root / _ALLOWED_FILES_ARTIFACT
    raw, error = _read_bounded_utf8(path)
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
    root = Path(repo_root)
    reasons = set()
    for relative_path in _TEXT_ARTIFACTS:
        text, error = _read_bounded_utf8(root / relative_path)
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
