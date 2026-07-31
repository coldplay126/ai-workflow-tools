"""Install the local Supervisor runtime as a safe user launchd service."""

from __future__ import annotations

import os
from importlib import resources
import plistlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional, Sequence
from xml.sax.saxutils import escape


LABEL = "com.awf.supervisor-agent"
_PLIST_NAME = LABEL + ".plist"
_NOT_LOADED_EXIT_CODE = 3
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class LaunchdInstallError(RuntimeError):
    """A launchd lifecycle operation could not be completed safely."""


def resolve_awf_executable(
    executable: Optional[Path] = None,
    *,
    development_root: Optional[Path] = None,
) -> Path:
    """Resolve a durable external `awf` console script or raise a safe error."""

    value = shutil.which("awf") if executable is None else os.fspath(executable)
    if not value:
        raise LaunchdInstallError("awf executable was not found on PATH")
    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise LaunchdInstallError("awf executable cannot be resolved") from error
    if not resolved.is_file() or not os.access(str(resolved), os.X_OK):
        raise LaunchdInstallError("awf executable must be an executable file")

    source_root = development_root
    if source_root is None:
        # src/awf/supervisor/install.py -> checked-out worktree/project root.
        source_root = Path(__file__).resolve().parents[4]
    root = Path(source_root).expanduser().resolve(strict=False)
    if _is_under(resolved, root):
        raise LaunchdInstallError("awf executable must not be inside the development repository")
    return resolved


def install_launchd(
    *,
    agent_id: str,
    repo_root: Path,
    executable: Optional[Path] = None,
    home: Optional[Path] = None,
    uid: Optional[int] = None,
    run_command: Optional[Callable[[Sequence[str]], int]] = None,
    development_root: Optional[Path] = None,
    running_process: Optional[Callable[[], bool]] = None,
) -> Path:
    """Atomically install and verify the local agent's canonical launchd plist."""

    _validate_agent_id(agent_id)
    resolved_repo = _resolve_repository(repo_root)
    resolved_executable = resolve_awf_executable(
        executable, development_root=development_root
    )
    if running_process is not None and running_process():
        raise LaunchdInstallError("refusing to replace a running process")

    effective_home = Path.home() if home is None else Path(home)
    if not effective_home.is_absolute():
        raise LaunchdInstallError("home must be absolute")
    launch_agents = effective_home / "Library" / "LaunchAgents"
    logs = effective_home / "Library" / "Logs" / "awf"
    try:
        launch_agents.mkdir(mode=0o755, parents=True, exist_ok=True)
        logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise LaunchdInstallError("launchd directories cannot be created") from error

    plist_path = launch_agents / _PLIST_NAME
    rendered = _render_plist(
        executable=resolved_executable,
        agent_id=agent_id,
        repo_root=resolved_repo,
        log_path=logs / "supervisor-agent.log",
    )
    _validate_rendered_plist(
        rendered,
        executable=resolved_executable,
        agent_id=agent_id,
        repo_root=resolved_repo,
    )

    effective_uid = os.getuid() if uid is None else uid
    if type(effective_uid) is not int or effective_uid < 0:
        raise LaunchdInstallError("uid must be a non-negative integer")
    runner = _run_command if run_command is None else run_command
    target = "gui/{}/{}".format(effective_uid, LABEL)
    _bootout(runner, target)

    previous = _read_existing(plist_path)
    _atomic_write(plist_path, rendered)
    try:
        _require_success(
            runner(("launchctl", "bootstrap", "gui/{}".format(effective_uid), str(plist_path))),
            "bootstrap",
        )
        _require_success(
            runner(("launchctl", "print", target)),
            "print",
        )
    except Exception:
        # Do not leave an enabled service backed by a failed replacement.  The
        # current error remains primary even if launchctl rejects the cleanup.
        try:
            runner(("launchctl", "bootout", target))
        except Exception:
            pass
        _restore(plist_path, previous)
        raise
    return plist_path


def uninstall_launchd(
    *,
    home: Optional[Path] = None,
    uid: Optional[int] = None,
    run_command: Optional[Callable[[Sequence[str]], int]] = None,
) -> None:
    """Boot out the service before deleting its plist; absent service is harmless."""

    effective_home = Path.home() if home is None else Path(home)
    if not effective_home.is_absolute():
        raise LaunchdInstallError("home must be absolute")
    effective_uid = os.getuid() if uid is None else uid
    if type(effective_uid) is not int or effective_uid < 0:
        raise LaunchdInstallError("uid must be a non-negative integer")
    runner = _run_command if run_command is None else run_command
    target = "gui/{}/{}".format(effective_uid, LABEL)
    _bootout(runner, target)
    plist_path = effective_home / "Library" / "LaunchAgents" / _PLIST_NAME
    try:
        plist_path.unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        raise LaunchdInstallError("launchd plist cannot be removed") from error


def _validate_agent_id(value: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise LaunchdInstallError("agent ID is invalid")


def _resolve_repository(value: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise LaunchdInstallError("repository root must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise LaunchdInstallError("repository root cannot be resolved") from error
    if not resolved.is_dir():
        raise LaunchdInstallError("repository root must be a directory")
    return resolved


def _render_plist(
    *, executable: Path, agent_id: str, repo_root: Path, log_path: Path
) -> bytes:
    try:
        template = (
            resources.files("awf.resources.launchd")
            .joinpath(_PLIST_NAME)
            .read_text(encoding="utf-8")
        )
    except (ModuleNotFoundError, OSError) as error:
        raise LaunchdInstallError("launchd plist template is unavailable") from error
    replacements = {
        "AWF_EXECUTABLE": escape(str(executable)),
        "AGENT_ID": escape(agent_id),
        "REPO_ROOT": escape(str(repo_root)),
        "LOG_PATH": escape(str(log_path)),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    if any(marker in template for marker in replacements):
        raise LaunchdInstallError("launchd plist template is incomplete")
    return template.encode("utf-8")


def _validate_rendered_plist(
    rendered: bytes, *, executable: Path, agent_id: str, repo_root: Path
) -> None:
    try:
        plist = plistlib.loads(rendered)
    except (ValueError, TypeError) as error:
        raise LaunchdInstallError("rendered launchd plist is invalid") from error
    expected = [
        str(executable),
        "supervisor",
        "agent",
        "run",
        "--agent-id",
        agent_id,
        "--environment",
        "local",
        "--transport",
        "http",
        "--repo-root",
        str(repo_root),
    ]
    if (
        not isinstance(plist, dict)
        or plist.get("Label") != LABEL
        or plist.get("ProgramArguments") != expected
        or plist.get("RunAtLoad") is not True
        or plist.get("KeepAlive") is not True
        or plist.get("ThrottleInterval") != 10
    ):
        raise LaunchdInstallError("rendered launchd plist does not match the agent contract")


def _bootout(runner: Callable[[Sequence[str]], int], target: str) -> None:
    result = runner(("launchctl", "bootout", target))
    if result not in (0, _NOT_LOADED_EXIT_CODE):
        raise LaunchdInstallError("launchctl bootout failed (exit {})".format(result))


def _require_success(result: int, operation: str) -> None:
    if result != 0:
        raise LaunchdInstallError("launchctl {} failed (exit {})".format(operation, result))


def _run_command(arguments: Sequence[str]) -> int:
    result = subprocess.run(list(arguments), check=False)
    return result.returncode


def _read_existing(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise LaunchdInstallError("launchd plist cannot be read") from error


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".{}-".format(path.name), dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    except OSError as error:
        raise LaunchdInstallError("launchd plist cannot be written") from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _restore(path: Path, previous: Optional[bytes]) -> None:
    if previous is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    _atomic_write(path, previous)


def _is_under(child: Path, root: Path) -> bool:
    try:
        child.relative_to(root)
    except ValueError:
        return False
    return True


__all__ = ["LABEL", "LaunchdInstallError", "install_launchd", "resolve_awf_executable", "uninstall_launchd"]
