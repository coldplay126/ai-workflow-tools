#!/usr/bin/env python3
"""Manual Pi field smoke.

This script is intentionally not part of default CI. It runs a real Pi
print-mode call through awf's PiDispatch and therefore requires a usable Pi
installation plus provider authentication.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CLI_SRC = ROOT / "cli" / "src"
sys.path.insert(0, str(CLI_SRC))

from awf.core.dispatch import PiDispatch, PiDispatchOptions, WorkerSpec  # noqa: E402
from awf.core.pi_field_smoke import (  # noqa: E402
    pi_field_smoke_latest_path,
    write_pi_field_smoke_result,
)
from awf.runners.pi import PiRunnerConfig  # noqa: E402


AUTH_ENV_NAMES = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)


def _diagnosis(
    kind: str,
    summary: str,
    next_action: str,
    *,
    billing_context: str | None = None,
) -> dict[str, str]:
    payload = {
        "kind": kind,
        "summary": summary,
        "next_action": next_action,
    }
    if billing_context:
        payload["billing_context"] = billing_context
    return payload


def _combined_output(check: dict[str, Any]) -> str:
    parts = [
        str(check.get("stdout") or ""),
        str(check.get("stderr") or ""),
        str(check.get("stdout_preview") or ""),
        str(check.get("stderr_preview") or ""),
    ]
    return "\n".join(parts).lower()


def _diagnose_version(check: dict[str, Any]) -> dict[str, str]:
    if check.get("returncode") == 127:
        return _diagnosis(
            "pi_not_found",
            "Pi command could not be executed.",
            "Install Pi, set AWF_PI_COMMAND, or rerun with --npm-exec.",
        )
    if check.get("returncode") == 124:
        return _diagnosis(
            "pi_version_timeout",
            "Pi did not return a version before the timeout.",
            "Retry with a larger --timeout-sec or inspect the Pi installation.",
        )
    return _diagnosis(
        "pi_version_failed",
        "Pi started, but `pi --version` failed.",
        "Run `pi --version` directly and fix the local Pi installation first.",
    )


def _diagnose_dispatch(check: dict[str, Any]) -> dict[str, str]:
    output = _combined_output(check)
    if check.get("ok"):
        if "subscription auth" in output and "extra usage" in output:
            return _diagnosis(
                "dispatch_ok_with_anthropic_extra_usage",
                (
                    "Pi dispatch passed, but Anthropic subscription auth may "
                    "bill through Extra Usage."
                ),
                "Review Claude Extra Usage limits before repeated Pi runs.",
                billing_context="anthropic_extra_usage",
            )
        return _diagnosis(
            "dispatch_ok",
            "Pi dispatch field smoke passed.",
            "No action required.",
        )
    if check.get("timed_out"):
        return _diagnosis(
            "pi_dispatch_timeout",
            "Pi dispatch did not finish before the timeout.",
            "Retry with a larger --timeout-sec or inspect the provider session.",
        )
    if (
        "out of extra usage" in output
        or "add more at claude.ai/settings/usage" in output
    ):
        return _diagnosis(
            "provider_quota_exhausted",
            (
                "Anthropic rejected the Pi request because Claude Extra "
                "Usage is exhausted."
            ),
            "Enable or increase Claude Extra Usage, or use a different provider/API key.",
            billing_context="anthropic_extra_usage",
        )
    if "subscription auth" in output and "extra usage" in output:
        return _diagnosis(
            "anthropic_extra_usage_warning",
            (
                "Pi is using Anthropic subscription auth, which bills "
                "third-party harness calls through Extra Usage."
            ),
            "Confirm Extra Usage settings before continuing repeated Pi runs.",
            billing_context="anthropic_extra_usage",
        )
    if "no api key found" in output or (
        "api key" in output and ("not found" in output or "missing" in output)
    ):
        return _diagnosis(
            "missing_provider_auth",
            "Pi runs, but no usable provider authentication was found.",
            (
                "Log in through Pi or set a provider API key such as "
                "ANTHROPIC_API_KEY or OPENAI_API_KEY."
            ),
        )
    if (
        "authentication_error" in output
        or "invalid_api_key" in output
        or "invalid x-api-key" in output
        or "unauthorized" in output
    ):
        return _diagnosis(
            "provider_auth_failed",
            "Pi reached the provider, but provider authentication was rejected.",
            "Refresh Pi login or replace the provider API key.",
        )
    if "rate_limit" in output or "rate limit" in output:
        return _diagnosis(
            "provider_rate_limited",
            "Pi reached the provider, but the provider rate-limited the request.",
            (
                "Wait for the provider rate limit to reset or use a different "
                "provider/model."
            ),
        )
    if check.get("parse_error"):
        return _diagnosis(
            "provider_contract_parse_error",
            "Pi/provider responded, but not with the JSON contract awf requires.",
            (
                "Inspect stdout_preview and stderr_preview, then retry with a "
                "stable provider/model."
            ),
        )
    if int(check.get("returncode") or 0) != 0:
        return _diagnosis(
            "provider_error",
            "Pi dispatch failed after reaching the provider path.",
            "Inspect stderr_preview for provider-specific details.",
        )
    return _diagnosis(
        "provider_contract_failed",
        "Pi dispatch completed, but the response did not satisfy the PASS contract.",
        "Inspect stdout_preview and rerun after adjusting the provider/model.",
    )


def _set_diagnosis(payload: dict[str, Any], diagnosis: dict[str, str]) -> None:
    payload["reason"] = diagnosis["kind"]
    payload["diagnosis"] = diagnosis
    for key in ("billing_context", "next_action"):
        if key in diagnosis:
            payload[key] = diagnosis[key]


def _auth_env_status() -> dict[str, bool]:
    return {name: bool(os.environ.get(name, "").strip()) for name in AUTH_ENV_NAMES}


def _make_npm_exec_wrapper(tmpdir: Path, package: str) -> Path:
    wrapper = tmpdir / "pi"
    wrapper.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                "export PI_SKIP_VERSION_CHECK=${PI_SKIP_VERSION_CHECK:-1}",
                "export PI_TELEMETRY=${PI_TELEMETRY:-0}",
                f'exec npm exec --yes --package "{package}" -- pi "$@"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def _resolve_pi_command(args: argparse.Namespace, tmpdir: Path) -> tuple[str | None, str]:
    if args.pi_command:
        return args.pi_command, "explicit"
    env_command = os.environ.get("AWF_PI_COMMAND", "").strip()
    if env_command:
        return env_command, "AWF_PI_COMMAND"
    path_command = shutil.which("pi")
    if path_command:
        return path_command, "PATH"
    if args.npm_exec:
        return str(_make_npm_exec_wrapper(tmpdir, args.npm_package)), "npm_exec"
    return None, "missing"


def _check_version(command: str, cwd: Path, timeout_sec: int) -> dict[str, Any]:
    env = os.environ.copy()
    env.setdefault("PI_SKIP_VERSION_CHECK", "1")
    env.setdefault("PI_TELEMETRY", "0")
    try:
        completed = subprocess.run(
            [command, "--version"],
            cwd=str(cwd),
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "returncode": 127,
            "stdout": "",
            "stderr": f"not found: {command}",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": 124,
            "stdout": "",
            "stderr": f"timed out after {timeout_sec}s",
        }
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _run_dispatch(command: str, cwd: Path, timeout_sec: int) -> dict[str, Any]:
    prompt = (
        'Return exactly this JSON object and nothing else: '
        '{"conclusion":"PASS","findings":[]}'
    )
    dispatch = PiDispatch(
        PiDispatchOptions(
            config=PiRunnerConfig(command=command, timeout_sec=timeout_sec)
        )
    )
    result = dispatch.run(
        [
            WorkerSpec(
                role="field_smoke",
                provider=object(),
                prompt=prompt,
                timeout_sec=timeout_sec,
                require_json=True,
            )
        ],
        cwd=str(cwd),
        strategy="sequential",
    )[0]
    return {
        "ok": result.ok and not result.parse_error and result.conclusion == "PASS",
        "provider_name": result.provider_name,
        "role": result.role,
        "returncode": result.returncode,
        "elapsed_sec": round(result.elapsed_sec, 3),
        "timed_out": result.timed_out,
        "parse_error": result.parse_error,
        "conclusion": result.conclusion,
        "stdout_preview": result.stdout[:500],
        "stderr_preview": result.stderr[:500],
    }


def _build_payload(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="awf-pi-field-") as tmp:
        tmpdir = Path(tmp)
        command, source = _resolve_pi_command(args, tmpdir)
        payload: dict[str, Any] = {
            "schema": "awf_pi_field_smoke_v1",
            "ok": False,
            "pi_command_source": source,
            "pi_command": command,
            "auth_env_present": _auth_env_status(),
            "checks": [],
        }
        if command is None:
            _set_diagnosis(
                payload,
                _diagnosis(
                    "pi_not_found",
                    "Pi command was not found.",
                    "Install Pi, set AWF_PI_COMMAND, or rerun with --npm-exec.",
                ),
            )
            return 2, payload

        version = _check_version(command, tmpdir, args.timeout_sec)
        payload["checks"].append({"name": "pi_version", **version})
        if not version["ok"]:
            _set_diagnosis(payload, _diagnose_version(version))
            return 2, payload

        dispatch = _run_dispatch(command, tmpdir, args.timeout_sec)
        payload["checks"].append({"name": "pi_dispatch", **dispatch})
        payload["ok"] = bool(dispatch["ok"])
        diagnosis = _diagnose_dispatch(dispatch)
        _set_diagnosis(payload, diagnosis)
        if payload["ok"]:
            return 0, payload
        return 1, payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run manual Pi field smoke.")
    parser.add_argument("--pi-command", help="Path/name of a pi executable")
    parser.add_argument(
        "--npm-exec",
        action="store_true",
        help="Use npm exec with a temporary wrapper when pi is not on PATH",
    )
    parser.add_argument(
        "--npm-package",
        default="@earendil-works/pi-coding-agent",
        help="npm package used with --npm-exec",
    )
    parser.add_argument("--timeout-sec", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--write-result",
        action="store_true",
        help="Persist the latest result under .awf-operations/pi-field-smoke/.",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repo root used with --write-result. Defaults to the current directory.",
    )
    args = parser.parse_args(argv)

    rc, payload = _build_payload(args)
    if args.write_result:
        result_path = pi_field_smoke_latest_path(args.repo_root)
        payload["result_path"] = str(result_path)
        write_pi_field_smoke_result(args.repo_root, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        status = "PASS" if payload["ok"] else "FAIL"
        print(f"{status}: {payload['reason']}")
        print(f"pi_command: {payload.get('pi_command') or '(missing)'}")
        print(f"pi_command_source: {payload['pi_command_source']}")
        print(f"auth_env_present: {payload['auth_env_present']}")
        diagnosis = payload.get("diagnosis") or {}
        if diagnosis.get("summary"):
            print(f"diagnosis: {diagnosis['summary']}")
        if diagnosis.get("billing_context"):
            print(f"billing_context: {diagnosis['billing_context']}")
        if diagnosis.get("next_action"):
            print(f"next_action: {diagnosis['next_action']}")
        for check in payload["checks"]:
            print(
                f"- {check['name']}: "
                f"ok={check['ok']} returncode={check.get('returncode')}"
            )
            stderr = str(check.get("stderr") or check.get("stderr_preview") or "")
            if stderr:
                print(f"  stderr: {stderr}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
