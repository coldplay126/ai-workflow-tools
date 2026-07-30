from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.9 and 3.10
    import tomli as tomllib


class ConfigError(ValueError):
    """Raised when a worktree configuration is malformed or unsafe."""


@dataclass(frozen=True)
class WorktreeConfig:
    default_base: str | None = None
    production_branch: str | None = None
    prepare_inputs: tuple[str, ...] = ()
    prepare_command: tuple[str, ...] = ()
    verify_production: tuple[tuple[str, ...], ...] = ()
    deployment_status_command: tuple[str, ...] = ()


def _argv(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ConfigError(f"{field} must be a non-empty argv array")
    if any("\0" in item for item in value):
        raise ConfigError(f"{field} must not contain an embedded NUL")
    return tuple(value)


def load_worktree_config(repository_root: Path) -> WorktreeConfig:
    path = repository_root / ".awf" / "worktree.toml"
    if not path.is_file():
        return WorktreeConfig()

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"could not load {path}: {exc}") from exc
    if b"\0" in raw:
        raise ConfigError(f"{path} must not contain an embedded NUL")
    try:
        loaded = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"could not load {path}: {exc}") from exc

    if not isinstance(loaded, dict):  # pragma: no cover - TOML documents are tables
        raise ConfigError("worktree configuration must be a TOML table")
    _reject_unknown_keys(loaded, {"worktree", "prepare", "verify", "deployment"}, "top-level table")

    worktree = _table(loaded.get("worktree"), "worktree")
    prepare = _table(loaded.get("prepare"), "prepare")
    verify = _table(loaded.get("verify"), "verify")
    deployment = _table(loaded.get("deployment"), "deployment")
    _reject_unknown_keys(worktree, {"default_base", "production_branch"}, "worktree field")
    _reject_unknown_keys(prepare, {"inputs", "command"}, "prepare field")
    _reject_unknown_keys(verify, {"production"}, "verify table")
    _reject_unknown_keys(deployment, {"status_command"}, "deployment field")

    production = _table(verify.get("production"), "verify.production")
    _reject_unknown_keys(production, {"commands"}, "verify.production field")

    return WorktreeConfig(
        default_base=_optional_string(worktree.get("default_base"), "worktree.default_base"),
        production_branch=_optional_string(
            worktree.get("production_branch"), "worktree.production_branch"
        ),
        prepare_inputs=_string_list(prepare.get("inputs"), "prepare.inputs"),
        prepare_command=_argv(prepare.get("command"), "prepare.command"),
        verify_production=_argv_list(
            production.get("commands"), "verify.production.commands"
        ),
        deployment_status_command=_argv(
            deployment.get("status_command"), "deployment.status_command"
        ),
    )


def _table(value: object, field: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{field} must be a TOML table")
    return value


def _reject_unknown_keys(
    value: Mapping[str, Any], allowed: set[str], kind: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"unknown {kind}: {unknown[0]}")


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{field} must be a non-empty string")
    if "\0" in value:
        raise ConfigError(f"{field} must not contain an embedded NUL")
    return value


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{field} must be an array of non-empty strings")
    if any("\0" in item for item in value):
        raise ConfigError(f"{field} must not contain an embedded NUL")
    return tuple(value)


def _argv_list(value: object, field: str) -> tuple[tuple[str, ...], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{field} must be an array of argv arrays")
    return tuple(_argv(item, field) for item in value)
