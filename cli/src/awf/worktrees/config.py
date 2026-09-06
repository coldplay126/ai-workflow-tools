from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.9 and 3.10
    import tomli as tomllib


_OPERATOR_CONFIG_MAX_BYTES = 64 * 1024
_DEFAULT_EVIDENCE_MAX_AGE_SECONDS = 300
_MAX_EVIDENCE_MAX_AGE_SECONDS = 86_400

_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")

class ConfigError(ValueError):
    """Raised when a worktree configuration is malformed or unsafe."""


@dataclass(frozen=True)
class WorktreeConfig:
    default_base: str | None = None
    production_branch: str | None = None
    prepare_inputs: tuple[str, ...] = ()
    prepare_command: tuple[str, ...] = ()
    verify_production: tuple[tuple[str, ...], ...] = ()
    source_review_policy: str = "approved_or_self_merged"
    feature_base: str | None = None


@dataclass(frozen=True)
class DeploymentAdapter:
    """An operator-owned executable that speaks the deployment evidence protocol."""

    command: tuple[str, ...]
    environment: tuple[str, ...]
    max_age_seconds: int
    config_digest: str
    adapter_directory: Path
    executable_device: int
    executable_inode: int

    def validate_executable(self) -> None:
        executable = Path(self.command[0])
        details = _validate_adapter_executable(executable, self.adapter_directory)
        if not os.access(executable, os.X_OK):
            raise ConfigError(
                f"deployment adapter executable is not executable: {executable}"
            )
        if (
            details.st_dev != self.executable_device
            or details.st_ino != self.executable_inode
        ):
            raise ConfigError(
                "deployment adapter executable changed after operator configuration load"
            )


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
    if "deployment" in loaded:
        raise ConfigError(
            "repository [deployment] is no longer supported; configure the "
            "operator-owned adapter in ~/.config/awf/config.toml"
        )
    _reject_unknown_keys(
        loaded,
        {"worktree", "prepare", "verify", "promotion"},
        "top-level table",
    )

    worktree = _table(loaded.get("worktree"), "worktree")
    prepare = _table(loaded.get("prepare"), "prepare")
    verify = _table(loaded.get("verify"), "verify")
    promotion = _table(loaded.get("promotion"), "promotion")
    _reject_unknown_keys(
        worktree, {"default_base", "production_branch", "feature_base"}, "worktree field"
    )
    _reject_unknown_keys(prepare, {"inputs", "command"}, "prepare field")
    _reject_unknown_keys(verify, {"production"}, "verify table")
    _reject_unknown_keys(
        promotion, {"source_review_policy"}, "promotion field"
    )

    production = _table(verify.get("production"), "verify.production")
    _reject_unknown_keys(production, {"commands"}, "verify.production field")

    return WorktreeConfig(
        default_base=_optional_string(worktree.get("default_base"), "worktree.default_base"),
        production_branch=_optional_string(
            worktree.get("production_branch"), "worktree.production_branch"
        ),
        feature_base=_optional_string(worktree.get("feature_base"), "worktree.feature_base"),
        prepare_inputs=_string_list(prepare.get("inputs"), "prepare.inputs"),
        prepare_command=_argv(prepare.get("command"), "prepare.command"),
        verify_production=_argv_list(
            production.get("commands"), "verify.production.commands"
        ),
        source_review_policy=_choice(
            promotion.get("source_review_policy"),
            "promotion.source_review_policy",
            {"approved", "approved_or_self_merged"},
            default="approved_or_self_merged",
        ),
    )


def load_deployment_adapter(
    repository_id: str, *, home_dir: Path | None = None
) -> DeploymentAdapter | None:
    """Load only the exact operator mapping for one immutable repository identity."""

    if not isinstance(repository_id, str) or not repository_id or "\0" in repository_id:
        raise ConfigError("repository_id must be a non-empty string")
    home = (home_dir or Path.home()).expanduser().resolve()
    path = home / ".config" / "awf" / "config.toml"
    raw = _read_operator_config(path)
    if raw is None:
        return None
    try:
        loaded = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"could not load {path}: {exc}") from exc
    if not isinstance(loaded, dict):  # pragma: no cover - TOML documents are tables
        raise ConfigError("operator configuration must be a TOML table")

    worktree = _table(loaded.get("worktree"), "operator worktree")
    deployment = _table(worktree.get("deployment"), "operator worktree.deployment")
    adapters = _table(
        deployment.get("adapters"), "operator worktree.deployment.adapters"
    )
    candidate = adapters.get(repository_id)
    if candidate is None:
        return None
    adapter = _table(candidate, f"deployment adapter for {repository_id!r}")
    _reject_unknown_keys(
        adapter, {"command", "environment", "max_age_seconds"}, "deployment adapter field"
    )
    command = _argv(adapter.get("command"), "deployment adapter command")
    if not command:
        raise ConfigError("deployment adapter command must be a non-empty argv array")
    executable = Path(command[0])
    if not executable.is_absolute():
        raise ConfigError("deployment adapter command must start with an absolute executable")
    adapter_directory = home / ".config" / "awf" / "adapters"
    environment = _environment_names(
        adapter.get("environment"), "deployment adapter environment"
    )
    max_age_seconds = _bounded_positive_int(
        adapter.get("max_age_seconds"),
        "deployment adapter max_age_seconds",
        default=_DEFAULT_EVIDENCE_MAX_AGE_SECONDS,
        maximum=_MAX_EVIDENCE_MAX_AGE_SECONDS,
    )
    details = _validate_adapter_executable(executable, adapter_directory)
    return DeploymentAdapter(
        command=command,
        environment=environment,
        max_age_seconds=max_age_seconds,
        config_digest=hashlib.sha256(raw).hexdigest(),
        adapter_directory=adapter_directory,
        executable_device=details.st_dev,
        executable_inode=details.st_ino,
    )


def _read_operator_config(path: Path) -> bytes | None:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(f"could not load operator configuration {path}: {exc}") from exc
    try:
        details = os.fstat(descriptor)
        _validate_owned_regular_file(details, "operator configuration")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 8192)
            if not chunk:
                break
            size += len(chunk)
            if size > _OPERATOR_CONFIG_MAX_BYTES:
                raise ConfigError("operator configuration exceeds the maximum size")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _validate_adapter_executable(
    executable: Path, adapter_directory: Path
) -> os.stat_result:
    try:
        relative = executable.relative_to(adapter_directory)
    except ValueError as exc:
        raise ConfigError(
            "deployment adapter executable must be under ~/.config/awf/adapters"
        ) from exc
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise ConfigError(
            "deployment adapter executable must be under ~/.config/awf/adapters"
        )
    _validate_adapter_directory_chain(adapter_directory, relative.parts[:-1])
    try:
        details = executable.lstat()
    except OSError as exc:
        raise ConfigError(
            f"deployment adapter executable is unavailable: {executable}"
        ) from exc
    if stat.S_ISLNK(details.st_mode):
        raise ConfigError("deployment adapter executable must not be a symlink")
    _validate_owned_regular_file(details, "deployment adapter executable")
    if not os.access(executable, os.X_OK):
        raise ConfigError(f"deployment adapter executable is not executable: {executable}")
    return details


def _validate_owned_regular_file(details: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(details.st_mode):
        raise ConfigError(f"{label} must be a regular file")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise ConfigError(f"{label} must be owned by the current operator")
    if details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError(f"{label} must not be group- or world-writable")


def _validate_adapter_directory_chain(
    adapter_directory: Path, relative_parent: tuple[str, ...]
) -> None:
    configuration_directory = adapter_directory.parent.parent
    for directory in (
        configuration_directory,
        configuration_directory / "awf",
        adapter_directory,
    ):
        _validate_owned_directory(directory, "deployment adapter directory")
    current = adapter_directory
    for component in relative_parent:
        current = current / component
        _validate_owned_directory(current, "deployment adapter directory")


def _validate_owned_directory(path: Path, label: str) -> None:
    try:
        details = path.lstat()
    except OSError as exc:
        raise ConfigError(f"{label} is unavailable: {path}") from exc
    if stat.S_ISLNK(details.st_mode):
        raise ConfigError(f"{label} must not be a symlink")
    if not stat.S_ISDIR(details.st_mode):
        raise ConfigError(f"{label} must be a directory")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise ConfigError(f"{label} must be owned by the current operator")
    if details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError(f"{label} must not be group- or world-writable")


def _environment_names(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if (
        not isinstance(value, list)
        or len(value) > 32
        or any(
            not isinstance(name, str)
            or _ENVIRONMENT_NAME.fullmatch(name) is None
            for name in value
        )
        or len(set(value)) != len(value)
    ):
        raise ConfigError(f"{field} must be a unique environment-name array")
    return tuple(value)


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


def _choice(
    value: object,
    field: str,
    allowed: set[str],
    *,
    default: str,
) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ConfigError(f"{field} must be one of: {choices}")
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


def _bounded_positive_int(
    value: object, field: str, *, default: int, maximum: int
) -> int:
    if value is None:
        return default
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
        or value > maximum
    ):
        raise ConfigError(f"{field} must be an integer from 1 to {maximum}")
    return value
