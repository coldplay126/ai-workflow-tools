"""Detect database-affecting workflow artifacts without scanning a repository."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Iterable


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

# Reasons use a canonical English term even when the source artifact uses Korean.
_TEXT_SIGNAL_PATTERNS = (
    (
        "database",
        re.compile(
            r"\bdatabase\b|"
            r"\bdb\b(?=\s+(?:schema|table|column|query|sql|index|migration|"
            r"normalization|denormalization|erd|foreign\s+key|primary\s+key|"
            r"unique\s+constraint|partition|warehouse|duckdb)\b)|데이터베이스"
        ),
    ),
    ("schema", re.compile(r"\bschema\b|스키마")),
    ("table", re.compile(r"\btable\b|테이블")),
    ("column", re.compile(r"\bcolumn\b|컬럼")),
    ("query", re.compile(r"\bquer(?:y|ies)\b|쿼리")),
    ("sql", re.compile(r"\bsql\b")),
    ("order by", re.compile(r"\border\s+by\b")),
    ("index", re.compile(r"\bindex\b|인덱스")),
    ("migration", re.compile(r"\bmigration(?:s)?\b|마이그레이션")),
    ("normalization", re.compile(r"\bnormalization\b|(?<!비)정규화")),
    ("denormalization", re.compile(r"\bdenormalization\b|비정규화")),
    ("erd", re.compile(r"\berd\b")),
    ("foreign key", re.compile(r"\bforeign\s+key\b|외래\s*키")),
    ("primary key", re.compile(r"\bprimary\s+key\b|기본\s*키")),
    ("unique constraint", re.compile(r"\bunique\s+constraint\b|고유\s*제약")),
    ("partition", re.compile(r"\bpartition(?:ing)?\b|파티션")),
    ("warehouse", re.compile(r"\bwarehouse\b|웨어하우스")),
    ("duckdb", re.compile(r"\bduckdb\b")),
)


@dataclass(frozen=True)
class DatabaseSignal:
    """Normalized database-change classification for workflow artifacts."""

    detected: bool
    reasons: tuple[str, ...]


def _read_bounded_utf8(path: Path) -> str:
    try:
        with path.open("rb") as artifact:
            return artifact.read(_MAX_ARTIFACT_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _text_reasons(text: str) -> Iterable[str]:
    normalized_text = text.casefold()
    for reason, pattern in _TEXT_SIGNAL_PATTERNS:
        if pattern.search(normalized_text):
            yield f"text:{reason}"


def _normalized_allowed_paths(root: Path) -> Iterable[str]:
    raw = _read_bounded_utf8(root / _ALLOWED_FILES_ARTIFACT)
    if not raw:
        return ()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(payload, dict):
        return ()
    normalized_paths = set()
    for key in ("planned_files", "files", "expanded_files"):
        paths = payload.get(key)
        if not isinstance(paths, list):
            continue
        normalized_paths.update(
            str(value).replace("\\", "/").lstrip("./").casefold()
            for value in paths
            if isinstance(value, str) and value.strip()
        )
    return tuple(sorted(normalized_paths))


def _is_database_path(path: str) -> bool:
    candidate = Path(path)
    if candidate.suffix in _PATH_SUFFIXES:
        return True
    return any(part in _PATH_DIRECTORIES for part in candidate.parts[:-1])


def detect_database_signal(repo_root: Path) -> DatabaseSignal:
    """Return DB signals from bounded, known workflow artifacts only."""
    root = Path(repo_root)
    reasons = set()
    for relative_path in _TEXT_ARTIFACTS:
        reasons.update(_text_reasons(_read_bounded_utf8(root / relative_path)))
    for path in _normalized_allowed_paths(root):
        if _is_database_path(path):
            reasons.add(f"path:{path}")
    normalized_reasons = tuple(sorted(reasons))
    return DatabaseSignal(detected=bool(normalized_reasons), reasons=normalized_reasons)
