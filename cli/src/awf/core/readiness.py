from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import shutil
from pathlib import Path
from typing import Any

from awf.core.config import AwfConfig, resolve_runtime_paths
from awf.core.mcp import discover_mcp_servers


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


def collect_doctor_report(config: AwfConfig, repo_root: str | None, *, probe: bool = False) -> dict[str, Any]:
    runtime_paths = resolve_runtime_paths(repo_root)
    provider_names = ["claude-code", "codex", "claude-sdk", "openai", "fixture"]
    providers = [check_provider_readiness(name, config) for name in provider_names]
    if probe:
        for item in providers:
            item["probe"] = check_provider_probe(str(item["provider"]), config)
    mcp_servers = discover_mcp_servers(config)
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
