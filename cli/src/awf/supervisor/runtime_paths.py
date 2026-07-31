"""Canonical filesystem locations shared by the Supervisor runtime layers."""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Optional


class RuntimePathError(ValueError):
    """A configured Supervisor runtime path is not safe to use."""


@dataclass(frozen=True)
class RuntimePaths:
    """The only state, ledger, marker, and repository roots for one runtime."""

    state_root: Path
    store_path: Path
    active_lease_path: Path
    repo_root: Path


_LOCAL_STATE_SEGMENTS = ("Library", "Application Support", "AWF", "supervisor")
_LOCAL_REPOSITORY_SEGMENTS = ("Documents", "GitHub")
_AWS_STATE_ROOT = Path("/workspace/.awf-supervisor")
_AWS_ACTIVE_LEASE_PATH = Path("/var/lib/aws-agent/supervisor-active-lease.json")
_AWS_REPOSITORY_ROOT = Path("/workspace/repos")


def _absolute_path(value: Path, field: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise RuntimePathError("{} must be an absolute path".format(field))
    return path


def _private_directory(path: Path, field: str) -> Path:
    """Create and lock one runtime-owned directory to its effective user."""

    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise RuntimePathError("{} cannot be created".format(field)) from error
    if not path.is_dir():
        raise RuntimePathError("{} must be a directory".format(field))
    try:
        os.chmod(str(path), 0o700)
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        raise RuntimePathError("{} cannot be owner-only".format(field)) from error
    if mode & 0o077:
        raise RuntimePathError("{} must be owner-only".format(field))
    return path.resolve()


def _repository_root(path: Path) -> Path:
    if not path.exists() or not path.is_dir():
        raise RuntimePathError("repository root must be an existing directory")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise RuntimePathError("repository root cannot be resolved") from error
    if not resolved.is_dir():
        raise RuntimePathError("repository root must be an existing directory")
    return resolved


def _pick(
    explicit: Optional[Path], environ: Mapping[str, str], variable: str, default: Path
) -> Path:
    if explicit is not None:
        return Path(explicit)
    value = environ.get(variable)
    return Path(value) if value is not None else default


def resolve_runtime_paths(
    *,
    environment: Literal["local", "aws"],
    state_dir: Optional[Path] = None,
    active_lease_path: Optional[Path] = None,
    repo_root: Optional[Path] = None,
    environ: Mapping[str, str] = os.environ,
    home: Optional[Path] = None,
) -> RuntimePaths:
    """Resolve, create, and validate the runtime's four shared paths.

    Each independently configurable path follows CLI option, environment variable,
    and selected-environment default precedence.  The returned paths are canonical
    roots, not caller-controlled child paths.
    """

    if environment not in ("local", "aws"):
        raise RuntimePathError("environment must be 'local' or 'aws'")
    effective_home = Path.home() if home is None else _absolute_path(home, "home")
    if environment == "local":
        default_state = effective_home.joinpath(*_LOCAL_STATE_SEGMENTS)
        default_marker = default_state / "active-lease.json"
        default_repo = effective_home.joinpath(*_LOCAL_REPOSITORY_SEGMENTS)
    else:
        default_state = _AWS_STATE_ROOT
        default_marker = _AWS_ACTIVE_LEASE_PATH
        default_repo = _AWS_REPOSITORY_ROOT

    requested_state = _absolute_path(
        _pick(state_dir, environ, "AWF_SUPERVISOR_STATE_DIR", default_state), "state root"
    )
    requested_marker = _absolute_path(
        _pick(
            active_lease_path,
            environ,
            "AWF_SUPERVISOR_ACTIVE_LEASE_PATH",
            default_marker,
        ),
        "active lease path",
    )
    requested_repo = _absolute_path(
        _pick(repo_root, environ, "AWF_SUPERVISOR_REPO_ROOT", default_repo),
        "repository root",
    )

    state_root = _private_directory(requested_state, "state root")
    marker_parent = _private_directory(requested_marker.parent, "active lease parent")
    marker = (marker_parent / requested_marker.name).resolve(strict=False)
    if marker.exists() and marker.is_dir():
        raise RuntimePathError("active lease path must be a file")

    return RuntimePaths(
        state_root=state_root,
        store_path=state_root / "supervisor.db",
        active_lease_path=marker,
        repo_root=_repository_root(requested_repo),
    )


__all__ = ["RuntimePathError", "RuntimePaths", "resolve_runtime_paths"]
