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
from awf.runners.pi import PiRunnerConfig  # noqa: E402


AUTH_ENV_NAMES = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)


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
        return {"ok": False, "returncode": 127, "stdout": "", "stderr": f"not found: {command}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": 124, "stdout": "", "stderr": f"timed out after {timeout_sec}s"}
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
            payload["reason"] = (
                "pi command not found; install Pi or rerun with --npm-exec"
            )
            return 2, payload

        version = _check_version(command, tmpdir, args.timeout_sec)
        payload["checks"].append({"name": "pi_version", **version})
        if not version["ok"]:
            payload["reason"] = "pi --version failed"
            return 2, payload

        dispatch = _run_dispatch(command, tmpdir, args.timeout_sec)
        payload["checks"].append({"name": "pi_dispatch", **dispatch})
        payload["ok"] = bool(dispatch["ok"])
        if payload["ok"]:
            payload["reason"] = "pi dispatch field smoke passed"
            return 0, payload
        payload["reason"] = "pi dispatch field smoke failed"
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
    args = parser.parse_args(argv)

    rc, payload = _build_payload(args)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        status = "PASS" if payload["ok"] else "FAIL"
        print(f"{status}: {payload['reason']}")
        print(f"pi_command: {payload.get('pi_command') or '(missing)'}")
        print(f"pi_command_source: {payload['pi_command_source']}")
        print(f"auth_env_present: {payload['auth_env_present']}")
        for check in payload["checks"]:
            print(f"- {check['name']}: ok={check['ok']} returncode={check.get('returncode')}")
            stderr = str(check.get("stderr") or check.get("stderr_preview") or "")
            if stderr:
                print(f"  stderr: {stderr}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
