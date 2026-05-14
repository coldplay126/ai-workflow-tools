"""Detect when the installed `awf` package is out of sync with a local source checkout.

Operationally, `uv tool install --reinstall` is the only path that refreshes the
globally installed `awf` after `cli/` changes land on main. Operators forget,
especially mid-cycle when a feature (e.g., multi-repo `sibling_repos`) is added
but the installed binary still ships pre-feature code, which silently no-ops
manifest fields. See `docs/gaps/2026-05-14-dogfood-d-findings.md` §3 (G-OPS-001).

This module compares the currently-imported `awf` package contents against a
local checkout under `<repo>/cli/src/awf/` (if one is detected) and reports
whether the installed copy is stale.

Detection is purely deterministic — no network, no probes — so it can be
included unconditionally in `awf doctor`.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


STATUS_IN_SYNC = "in_sync"
STATUS_STALE = "stale"
STATUS_NO_SOURCE_FOUND = "no_source_found"
STATUS_EDITABLE = "editable"


@dataclass(frozen=True)
class FreshnessResult:
    """Result of comparing installed `awf/` to a local source checkout."""

    status: str
    installed_path: str
    source_path: str | None
    installed_hash: str | None
    source_hash: str | None
    file_count_installed: int
    file_count_source: int
    detail: str
    reinstall_command: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "installed_path": self.installed_path,
            "source_path": self.source_path,
            "installed_hash": self.installed_hash,
            "source_hash": self.source_hash,
            "file_count_installed": self.file_count_installed,
            "file_count_source": self.file_count_source,
            "detail": self.detail,
            "reinstall_command": self.reinstall_command,
        }


def detect_source_root(start: Path) -> Path | None:
    """Walk up from `start` looking for an ai-workflow-tools source checkout.

    A directory qualifies if it contains both `cli/pyproject.toml` and
    `cli/src/awf/__init__.py`. Returns the path to `cli/src/awf/` on a match,
    or None if no ancestor qualifies.
    """
    current = start.resolve()
    while True:
        candidate = current / "cli" / "src" / "awf"
        pyproject = current / "cli" / "pyproject.toml"
        if candidate.is_dir() and pyproject.is_file():
            init_file = candidate / "__init__.py"
            if init_file.is_file():
                return candidate
        if current.parent == current:
            return None
        current = current.parent


def _iter_py_files(pkg_dir: Path) -> list[Path]:
    """Return sorted list of .py files under `pkg_dir`, excluding caches."""
    files: list[Path] = []
    for path in pkg_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        files.append(path)
    files.sort(key=lambda p: p.relative_to(pkg_dir).as_posix())
    return files


def compute_package_hash(pkg_dir: Path) -> tuple[str, int]:
    """SHA256 over the sorted relative paths and contents of `pkg_dir`'s .py files.

    Returns (hex_digest, file_count). Deterministic across machines as long as
    the same source tree is on disk. Skips bytecode caches so a freshly-built
    package matches one whose .pyc files have been pruned.
    """
    digest = hashlib.sha256()
    count = 0
    for path in _iter_py_files(pkg_dir):
        rel = path.relative_to(pkg_dir).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
        count += 1
    return digest.hexdigest(), count


def check_install_freshness(repo_root: str | Path | None = None) -> dict[str, Any]:
    """Compare installed `awf` package against a detected source checkout.

    `repo_root` is the starting point for source detection. When None we walk up
    from cwd. The function never raises — any unexpected condition resolves to
    `status=no_source_found` with a `detail` explaining why.
    """
    import awf

    installed_path = Path(awf.__file__).resolve().parent
    start = Path(repo_root).resolve() if repo_root else Path.cwd().resolve()
    source_path = detect_source_root(start)

    if source_path is None:
        return FreshnessResult(
            status=STATUS_NO_SOURCE_FOUND,
            installed_path=str(installed_path),
            source_path=None,
            installed_hash=None,
            source_hash=None,
            file_count_installed=0,
            file_count_source=0,
            detail=(
                "no ai-workflow-tools source checkout found above "
                f"{start} — install freshness cannot be compared"
            ),
            reinstall_command=None,
        ).to_dict()

    if installed_path == source_path:
        return FreshnessResult(
            status=STATUS_EDITABLE,
            installed_path=str(installed_path),
            source_path=str(source_path),
            installed_hash=None,
            source_hash=None,
            file_count_installed=0,
            file_count_source=0,
            detail=(
                "installed `awf` resolves to the same directory as source "
                "(editable / pip install -e .) — no drift possible"
            ),
            reinstall_command=None,
        ).to_dict()

    installed_hash, installed_count = compute_package_hash(installed_path)
    source_hash, source_count = compute_package_hash(source_path)
    repo_root_path = source_path.parent.parent.parent
    reinstall_command = f"uv tool install --reinstall {repo_root_path}/cli"

    if installed_hash == source_hash:
        status = STATUS_IN_SYNC
        detail = (
            f"installed package hash matches source at {source_path} "
            f"({installed_count} .py files)"
        )
        reinstall_cmd: str | None = None
    else:
        status = STATUS_STALE
        detail = (
            f"installed `awf` ({installed_count} files, hash {installed_hash[:12]}) "
            f"differs from source at {source_path} "
            f"({source_count} files, hash {source_hash[:12]}) — "
            f"new behavior in `cli/` may be silently disabled until you reinstall"
        )
        reinstall_cmd = reinstall_command

    return FreshnessResult(
        status=status,
        installed_path=str(installed_path),
        source_path=str(source_path),
        installed_hash=installed_hash,
        source_hash=source_hash,
        file_count_installed=installed_count,
        file_count_source=source_count,
        detail=detail,
        reinstall_command=reinstall_cmd,
    ).to_dict()
