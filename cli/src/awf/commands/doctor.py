from __future__ import annotations

import argparse
import json
import sys

from awf.core.config import load_awf_config
from awf.core.readiness import collect_doctor_report, evaluate_doctor_ci


def run_doctor(args: argparse.Namespace) -> int:
    try:
        config = load_awf_config(args.repo_root)
        payload = collect_doctor_report(config, args.repo_root, probe=bool(getattr(args, "probe", False)))
        ci_result = evaluate_doctor_ci(payload) if getattr(args, "ci", False) else None
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if getattr(args, "json", False):
        if ci_result is not None:
            payload = {**payload, "ci": ci_result}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if ci_result is None or ci_result["ok"] else 1

    print(f"default_provider: {payload['default_provider']}")
    print(f"provider_fallback: {json.dumps(payload['provider_fallback'], ensure_ascii=False)}")
    print(f"repo_root: {payload['paths']['repo_root']}")
    print(f"session_db: {payload['paths']['session_db']}")
    print(f"mcp_servers: {payload['mcp']['server_count']} ({', '.join(payload['mcp']['servers']) or 'none'})")

    dispatch = payload.get("dispatch", {})
    if dispatch:
        surfaces = ", ".join(dispatch.get("available_surfaces") or ["inline"])
        cmux_note = (
            "ready"
            if dispatch.get("cmux_backend_ready")
            else (
                "binary on PATH but backend not yet wired up"
                if dispatch.get("cmux_binary_on_path")
                else "binary not on PATH"
            )
        )
        pi_note = "ready" if dispatch.get("pi_backend_ready") else "binary not on PATH"
        print(f"dispatch_surfaces: {surfaces} (cmux: {cmux_note}; pi: {pi_note})")
    runners = payload.get("runners", []) or []
    if runners:
        print("runners:")
        for runner in runners:
            installed = runner.get("installed", {}) or {}
            backend = runner.get("backend", {}) or {}
            print(
                f"  - {runner.get('runner')}: "
                f"{installed.get('status')} ({installed.get('detail')}); "
                f"backend: {backend.get('status')} ({backend.get('detail')})"
            )
    pi_readiness = payload.get("pi_readiness", {}) or {}
    if pi_readiness:
        version = pi_readiness.get("version", {}) or {}
        auth_env = pi_readiness.get("auth_env_present", {}) or {}
        auth_env_names = [name for name, present in auth_env.items() if present]
        print("pi_readiness:")
        print(
            f"  status: {pi_readiness.get('status')} "
            f"(surface={pi_readiness.get('dispatch_surface')})"
        )
        print(
            f"  command: {pi_readiness.get('command')} "
            f"({pi_readiness.get('command_source')}, "
            f"path={pi_readiness.get('path') or 'missing'})"
        )
        print(f"  version: {version.get('status')} ({version.get('detail')})")
        print(f"  auth_env: {', '.join(auth_env_names) if auth_env_names else 'none'}")
        billing = pi_readiness.get("billing_warning", {}) or {}
        print(f"  billing: {billing.get('status')} ({billing.get('detail')})")
        print(f"  field_smoke: {pi_readiness.get('field_smoke_command')}")
    print("")
    print("providers:")
    for provider in payload["providers"]:
        print(f"  - {provider['provider']}")
        print(f"    installed: {provider['installed']['status']} ({provider['installed']['detail']})")
        print(f"    configured: {provider['configured']['status']} ({provider['configured']['detail']})")
        if "probe" in provider:
            print(f"    probe: {provider['probe']['status']} ({provider['probe']['detail']})")
    if ci_result is not None:
        print("")
        print(f"ci_provider: {ci_result['provider']}")
        print(f"ci_checks: {', '.join(ci_result.get('checks', [])) or 'none'}")
        print(f"ci_status: {'ok' if ci_result['ok'] else 'fail'}")
        print(f"ci_reason: {ci_result['reason']}")
    return 0 if ci_result is None or ci_result["ok"] else 1
