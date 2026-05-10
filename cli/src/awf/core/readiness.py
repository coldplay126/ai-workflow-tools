from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import shutil
from pathlib import Path
from typing import Any

from awf.core.config import AwfConfig, resolve_runtime_paths
from awf.core.mcp import discover_mcp_servers


PI_AUTH_ENV_NAMES = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)


def readiness_item(status: str, detail: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "detail": detail,
    }
    payload.update(extra)
    return payload


def check_command_installed(command: str) -> dict[str, Any]:
    resolved = shutil.which(command)
    if resolved:
        return readiness_item("ok", f"found at {resolved}", command=command, path=resolved)
    return readiness_item("fail", f"command not found: {command}", command=command, path=None)


def check_api_key_set(env_var: str) -> dict[str, Any]:
    if os.environ.get(env_var, "").strip():
        return readiness_item("ok", f"{env_var} is set", env_var=env_var)
    return readiness_item("fail", f"{env_var} is not set", env_var=env_var)


def check_package_importable(module_name: str) -> dict[str, Any]:
    spec = importlib.util.find_spec(module_name)
    if spec is None:
        return readiness_item("fail", f"package not installed: {module_name}", module=module_name, version=None)
    version = None
    try:
        module = importlib.import_module(module_name)
        version = getattr(module, "__version__", None)
    except Exception:
        version = None
    detail = f"package importable: {module_name}"
    if version:
        detail += f" ({version})"
    return readiness_item("ok", detail, module=module_name, version=version)


def _session_db_path(config: AwfConfig, repo_root: str | None) -> Path:
    env_override = os.environ.get("AWF_SESSION_DB", "").strip()
    if env_override:
        return Path(env_override).expanduser().resolve()
    override = config.path_override("session_db")
    if override:
        return Path(override).expanduser().resolve()
    resolve_runtime_paths(repo_root)
    return (Path.home() / ".local" / "share" / "awf" / "awf.db").resolve()


def check_provider_readiness(provider_name: str, config: AwfConfig) -> dict[str, Any]:
    settings = config.provider_settings(provider_name)
    if provider_name == "claude-code":
        command = str(settings.get("command") or os.environ.get("AWF_CLAUDE_COMMAND", "claude"))
        return {
            "provider": provider_name,
            "installed": check_command_installed(command),
            "configured": readiness_item("skip", "CLI auth is not checked in doctor MVP"),
        }
    if provider_name == "codex":
        command = str(settings.get("command") or os.environ.get("AWF_CODEX_COMMAND", "codex"))
        return {
            "provider": provider_name,
            "installed": check_command_installed(command),
            "configured": readiness_item("skip", "CLI auth is not checked in doctor MVP"),
        }
    if provider_name == "claude-sdk":
        env_var = str(settings.get("api_key_env") or "ANTHROPIC_API_KEY")
        return {
            "provider": provider_name,
            "installed": check_package_importable("anthropic"),
            "configured": check_api_key_set(env_var),
        }
    if provider_name == "openai":
        env_var = str(settings.get("api_key_env") or "OPENAI_API_KEY")
        return {
            "provider": provider_name,
            "installed": check_package_importable("openai"),
            "configured": check_api_key_set(env_var),
        }
    if provider_name == "fixture":
        result_file = str(settings.get("result_file", "") or "")
        detail = f"internal test provider (result_file={result_file or 'unset'})"
        return {
            "provider": provider_name,
            "installed": readiness_item("ok", detail),
            "configured": readiness_item("ok", "fixture provider is always locally available"),
        }
    return {
        "provider": provider_name,
        "installed": readiness_item("skip", f"no readiness checker for provider: {provider_name}"),
        "configured": readiness_item("skip", f"no readiness checker for provider: {provider_name}"),
    }


def check_subprocess_probe(command: str, version_flag: str = "--version", timeout_sec: int = 5) -> dict[str, Any]:
    resolved = shutil.which(command)
    if not resolved:
        return readiness_item("fail", f"command not found: {command}", command=command)
    try:
        completed = subprocess.run(
            [resolved, version_flag],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except Exception as exc:
        return readiness_item("fail", f"probe failed: {exc}", command=command)
    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode == 0:
        return readiness_item("ok", output or f"{command} {version_flag} succeeded", command=command)
    return readiness_item(
        "fail",
        output or f"{command} {version_flag} exited with {completed.returncode}",
        command=command,
        returncode=completed.returncode,
    )


def check_provider_probe(provider_name: str, config: AwfConfig) -> dict[str, Any]:
    settings = config.provider_settings(provider_name)
    if provider_name == "claude-code":
        command = str(settings.get("command") or os.environ.get("AWF_CLAUDE_COMMAND", "claude"))
        return check_subprocess_probe(command)
    if provider_name == "codex":
        command = str(settings.get("command") or os.environ.get("AWF_CODEX_COMMAND", "codex"))
        return check_subprocess_probe(command)
    if provider_name in {"claude-sdk", "openai"}:
        return readiness_item("skip", "SDK network probe is not enabled in doctor MVP")
    if provider_name == "fixture":
        return readiness_item("ok", "fixture provider does not require an external probe")
    return readiness_item("skip", f"no probe checker for provider: {provider_name}")


def _pi_command() -> tuple[str, str]:
    env_command = os.environ.get("AWF_PI_COMMAND", "").strip()
    if env_command:
        return env_command, "AWF_PI_COMMAND"
    return "pi", "default"


def _auth_env_status() -> dict[str, bool]:
    return {name: bool(os.environ.get(name, "").strip()) for name in PI_AUTH_ENV_NAMES}


def _check_pi_version(command: str, timeout_sec: int = 5) -> dict[str, Any]:
    resolved = shutil.which(command)
    if not resolved:
        return readiness_item(
            "skip",
            "Pi version not checked because the command is unavailable",
            command=command,
            version=None,
        )
    env = os.environ.copy()
    env.setdefault("PI_SKIP_VERSION_CHECK", "1")
    env.setdefault("PI_TELEMETRY", "0")
    try:
        completed = subprocess.run(
            [resolved, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return readiness_item(
            "fail",
            f"pi --version timed out after {timeout_sec}s",
            command=command,
            path=resolved,
            version=None,
        )
    except Exception as exc:
        return readiness_item(
            "fail",
            f"pi --version failed: {exc}",
            command=command,
            path=resolved,
            version=None,
        )
    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode == 0:
        return readiness_item(
            "ok",
            output or "pi --version succeeded",
            command=command,
            path=resolved,
            version=output or None,
        )
    return readiness_item(
        "fail",
        output or f"pi --version exited with {completed.returncode}",
        command=command,
        path=resolved,
        version=None,
        returncode=completed.returncode,
    )


def _pi_field_smoke_command(*, command_source: str, installed_status: str) -> str:
    base = "python3 cli/tests/run_pi_field_smoke.py"
    if command_source == "default" and installed_status != "ok":
        return f"{base} --npm-exec --json"
    return f"{base} --json"


def collect_pi_readiness() -> dict[str, Any]:
    command, command_source = _pi_command()
    installed = check_command_installed(command)
    version = _check_pi_version(command)
    auth_env_present = _auth_env_status()
    auth_env_any = any(auth_env_present.values())
    if installed["status"] != "ok":
        status = "missing"
    elif version["status"] == "ok":
        status = "ready"
    else:
        status = "caution"
    auth_detail = (
        "provider API key env is present; Pi may also use its own stored login"
        if auth_env_any
        else "no provider API key env detected; Pi may still use its own stored login"
    )
    return {
        "status": status,
        "command": command,
        "command_source": command_source,
        "path": installed.get("path"),
        "installed": installed,
        "version": version,
        "auth_env_present": auth_env_present,
        "auth_env_any": auth_env_any,
        "auth": readiness_item("skip", auth_detail),
        "dispatch_surface": "opt_in_only",
        "billing_warning": readiness_item(
            "caution",
            (
                "Anthropic subscription auth in Pi may bill third-party "
                "harness calls through Claude Extra Usage"
            ),
            billing_context="anthropic_extra_usage",
        ),
        "field_smoke_command": _pi_field_smoke_command(
            command_source=command_source,
            installed_status=str(installed["status"]),
        ),
    }


def check_runner_readiness(
    runner_name: str,
    *,
    pi_readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if runner_name == "pi":
        pi = pi_readiness or collect_pi_readiness()
        installed = pi["installed"]
        backend_status = "ok" if installed["status"] == "ok" else "skip"
        backend_detail = (
            "Pi dispatch adapter available; enable with dispatch.surface_preference=pi"
            if installed["status"] == "ok"
            else "Pi dispatch adapter available but command is not on PATH; inline remains default"
        )
        return {
            "runner": runner_name,
            "kind": "terminal_harness",
            "installed": installed,
            "configured": readiness_item(
                "skip",
                (
                    "Pi authentication and session state are managed by Pi; "
                    "awf only detects the harness"
                ),
            ),
            "backend": readiness_item(backend_status, backend_detail),
        }
    return {
        "runner": runner_name,
        "kind": "unknown",
        "installed": readiness_item("skip", f"no readiness checker for runner: {runner_name}"),
        "configured": readiness_item("skip", f"no readiness checker for runner: {runner_name}"),
        "backend": readiness_item("skip", f"no awf backend registered for runner: {runner_name}"),
    }


def collect_doctor_report(config: AwfConfig, repo_root: str | None, *, probe: bool = False) -> dict[str, Any]:
    runtime_paths = resolve_runtime_paths(repo_root)
    resolved_repo_root = str(runtime_paths["repo_root"])
    provider_names = ["claude-code", "codex", "claude-sdk", "openai", "fixture"]
    providers = [check_provider_readiness(name, config) for name in provider_names]
    if probe:
        for item in providers:
            item["probe"] = check_provider_probe(str(item["provider"]), config)
    mcp_servers = discover_mcp_servers(config)
    pi_readiness = collect_pi_readiness()
    return {
        "default_provider": config.provider_name(),
        "provider_fallback": config.raw.get("provider", {}).get("fallback", []),
        "probe_enabled": probe,
        "paths": {
            **runtime_paths,
            "session_db": str(_session_db_path(config, repo_root)),
        },
        "mcp": {
            "server_count": len(mcp_servers),
            "servers": [server.name for server in mcp_servers],
        },
        "providers": providers,
        "runners": [check_runner_readiness("pi", pi_readiness=pi_readiness)],
        "pi_readiness": pi_readiness,
        "dispatch": _collect_dispatch_status(resolved_repo_root),
    }


def _dispatch_provider_config_path(repo_root: str | None) -> Path:
    cwd = Path(repo_root or os.getcwd())
    return cwd / ".workflow" / "provider-config.json"


def _collect_dispatch_preference(repo_root: str | None) -> dict[str, Any]:
    from awf.core.dispatch import resolve_preference_from_config

    path = _dispatch_provider_config_path(repo_root)
    base: dict[str, Any] = {
        "provider_config_path": str(path),
        "provider_config_exists": path.is_file(),
        "surface_preference": "auto",
        "raw_surface_preference": None,
        "source": "default",
        "status": "default",
        "detail": (
            "no workflow provider-config found; dispatch surface preference "
            "defaults to auto"
        ),
    }
    if not path.is_file():
        return base
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            **base,
            "provider_config_exists": True,
            "source": "invalid_provider_config",
            "status": "invalid",
            "detail": f"provider-config could not be parsed; using auto: {exc}",
        }
    if not isinstance(data, dict):
        return {
            **base,
            "provider_config_exists": True,
            "source": "invalid_provider_config",
            "status": "invalid",
            "detail": "provider-config root is not an object; using auto",
        }

    section = data.get("dispatch", {})
    if "dispatch" in data and not isinstance(section, dict):
        return {
            **base,
            "provider_config_exists": True,
            "source": "invalid_provider_config",
            "status": "invalid",
            "detail": "provider-config dispatch section is not an object; using auto",
        }
    raw_value = None
    if isinstance(section, dict) and "surface_preference" in section:
        raw_value = section.get("surface_preference")
    resolved = resolve_preference_from_config(data)
    raw_text = None if raw_value is None else str(raw_value)
    if raw_value is None:
        return {
            **base,
            "provider_config_exists": True,
            "source": "provider_config_default",
            "status": "default",
            "detail": (
                "provider-config exists but dispatch.surface_preference is "
                "unset; using auto"
            ),
        }
    normalized = raw_text.strip().lower() if raw_text is not None else ""
    if normalized != resolved:
        return {
            **base,
            "provider_config_exists": True,
            "surface_preference": resolved,
            "raw_surface_preference": raw_text,
            "source": "invalid_provider_config",
            "status": "invalid",
            "detail": (
                "unsupported dispatch.surface_preference "
                f"{raw_text!r}; using auto"
            ),
        }
    return {
        **base,
        "provider_config_exists": True,
        "surface_preference": resolved,
        "raw_surface_preference": raw_text,
        "source": "provider_config",
        "status": "configured",
        "detail": f"provider-config requests dispatch.surface_preference={resolved}",
    }


def _dispatch_preference_readiness(
    preference: str,
    *,
    cmux_backend_ready: bool,
    cmux_on_path: bool,
    pi_backend_ready: bool,
) -> dict[str, Any]:
    if preference == "inline":
        return readiness_item("ok", "inline dispatch is always available")
    if preference == "auto":
        return readiness_item(
            "ok",
            "auto dispatch can always fall back to inline",
        )
    if preference == "cmux":
        if cmux_backend_ready:
            return readiness_item("ok", "cmux dispatch preference is ready")
        detail = (
            "cmux-agent is on PATH but no active cmux backend is ready; "
            "dispatch falls back to inline"
            if cmux_on_path
            else "cmux dispatch requested but cmux-agent is not on PATH; "
            "dispatch falls back to inline"
        )
        return readiness_item("caution", detail)
    if preference == "pi":
        if pi_backend_ready:
            return readiness_item(
                "ok",
                (
                    "Pi command is available; run the field smoke before "
                    "relying on provider auth/quota"
                ),
            )
        return readiness_item(
            "caution",
            (
                "Pi dispatch requested but pi is not on PATH; dispatch falls "
                "back to inline"
            ),
        )
    return readiness_item(
        "caution",
        f"unsupported dispatch preference {preference}; dispatch falls back to inline",
    )


def _collect_dispatch_status(repo_root: str | None = None) -> dict[str, Any]:
    """Snapshot which dispatch backends are usable for ``repo_root``.

    cmux readiness is per-project: it depends on the ``.agent/`` state in
    the working directory, not just the binary on PATH.
    """
    import shutil

    from awf.core.dispatch import (
        SURFACE_CMUX,
        SURFACE_INLINE,
        SURFACE_PI,
        cmux_dispatch_available,
        pi_dispatch_available,
    )

    cwd = repo_root or os.getcwd()
    cmux_on_path = shutil.which("cmux-agent") is not None
    cmux_backend_ready = cmux_dispatch_available(cwd)
    pi_backend_ready = pi_dispatch_available()
    preference = _collect_dispatch_preference(repo_root)
    preference_name = str(preference.get("surface_preference") or "auto")
    available_surfaces = [SURFACE_INLINE]
    if cmux_backend_ready:
        available_surfaces.append(SURFACE_CMUX)
    if pi_backend_ready:
        available_surfaces.append(SURFACE_PI)
    return {
        "default_surface": SURFACE_INLINE,
        "cmux_binary_on_path": cmux_on_path,
        "cmux_backend_ready": cmux_backend_ready,
        "pi_binary_on_path": pi_backend_ready,
        "pi_backend_ready": pi_backend_ready,
        "cwd_checked": str(cwd),
        "available_surfaces": available_surfaces,
        "surface_preference": preference,
        "surface_preference_ready": _dispatch_preference_readiness(
            preference_name,
            cmux_backend_ready=cmux_backend_ready,
            cmux_on_path=cmux_on_path,
            pi_backend_ready=pi_backend_ready,
        ),
    }


def _is_ready_status(item: dict[str, Any] | None) -> bool:
    if not item:
        return False
    return str(item.get("status")) in {"ok", "skip"}


def evaluate_doctor_ci(report: dict[str, Any]) -> dict[str, Any]:
    default_provider = str(report.get("default_provider") or "")
    providers = report.get("providers", [])
    provider_entry = next((item for item in providers if str(item.get("provider")) == default_provider), None)
    if provider_entry is None:
        return {
            "ok": False,
            "provider": default_provider,
            "reason": "default_provider_missing",
        }

    checks: list[str] = []
    ok = _is_ready_status(provider_entry.get("installed"))
    checks.append("installed")
    if ok:
        ok = _is_ready_status(provider_entry.get("configured"))
        checks.append("configured")
    if ok and report.get("probe_enabled"):
        ok = _is_ready_status(provider_entry.get("probe"))
        checks.append("probe")

    reason = "ok" if ok else f"{default_provider} failed {'/'.join(checks)} readiness"
    return {
        "ok": ok,
        "provider": default_provider,
        "checks": checks,
        "reason": reason,
    }


def maybe_doctor_hint(provider_name: str, detail: str) -> str | None:
    normalized = detail.lower()
    if any(token in normalized for token in ["command not found", "missing api key env", "package is not installed", "provider failed"]):
        return f"hint: run `awf doctor --repo-root .` to inspect readiness for provider `{provider_name}`"
    return None
