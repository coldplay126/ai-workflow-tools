from __future__ import annotations

import argparse
import json
import sys

from awf.core.ready import collect_ready_report, evaluate_ready_gate


def _fmt_status(status: str) -> str:
    return {
        "ready": "ready",
        "caution": "caution",
        "blocked": "blocked",
        "not_started": "not started",
    }.get(status, status)


def run_ready(args: argparse.Namespace) -> int:
    gate_name = getattr(args, "gate", None)
    try:
        payload = collect_ready_report(
            args.repo_root,
            probe=bool(getattr(args, "probe", False)),
        )
        gate = evaluate_ready_gate(payload, gate_name) if gate_name else None
        if gate:
            payload["gate"] = gate
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return int(gate["exit_code"]) if gate else 0

    level = payload["automation_level"]
    print(f"repo_root: {payload['repo_root']}")
    print(
        "automation_level: "
        f"{level['safe_level']} ({level['safe_label']})"
    )
    if level["max_possible_level"] != level["safe_level"]:
        print(
            "automation_level_with_caution: "
            f"{level['max_possible_level']} ({level['max_possible_label']})"
        )
    print("")
    print("readiness:")
    print(
        f"  config: {_fmt_status(payload['config']['status'])} "
        f"(project_config={payload['config']['project_config_exists']})"
    )
    provider = payload["provider"]
    print(
        f"  provider: {_fmt_status(provider['status'])} "
        f"({provider['provider']}: {provider['reason']})"
    )
    scan = payload["scan"]
    print(
        f"  scan: {_fmt_status(scan['status'])} "
        f"({scan['language']}/{scan['framework']}, units={scan['unit_count']})"
    )
    if scan.get("subprojects"):
        listed = ", ".join(item["path"] for item in scan["subprojects"][:5])
        print(f"    subprojects: {listed}")
    print(
        f"  skills: {_fmt_status(payload['skills']['status'])} "
        f"(count={payload['skills']['count']})"
    )
    print(
        f"  workflow: {_fmt_status(payload['workflow']['status'])} "
        f"(state={payload['workflow']['state_exists']})"
    )
    print(
        f"  operations: {_fmt_status(payload['operations']['status'])} "
        f"(profile={payload['operations']['profile_exists']})"
    )
    runners = payload.get("doctor", {}).get("runners", []) or []
    if runners:
        summary = ", ".join(
            f"{item.get('runner')}={item.get('installed', {}).get('status')}"
            for item in runners
        )
        print(f"  runners: {summary}")
    pi_readiness = payload.get("doctor", {}).get("pi_readiness", {}) or {}
    if pi_readiness:
        version = pi_readiness.get("version", {}) or {}
        print(
            f"  pi: {pi_readiness.get('status')} "
            f"(version={version.get('status')}, "
            f"surface={pi_readiness.get('dispatch_surface')})"
        )
        last_smoke = pi_readiness.get("last_field_smoke", {}) or {}
        if last_smoke.get("status") == "found":
            print(
                "    last_field_smoke: "
                f"ok={last_smoke.get('ok')} "
                f"reason={last_smoke.get('reason')} "
                f"at={last_smoke.get('recorded_at')}"
            )
    dispatch = payload.get("doctor", {}).get("dispatch", {}) or {}
    preference = dispatch.get("surface_preference", {}) or {}
    preference_ready = dispatch.get("surface_preference_ready", {}) or {}
    if preference:
        print(
            f"  dispatch: preference={preference.get('surface_preference')} "
            f"({preference_ready.get('status')})"
        )
    print("")
    print("capabilities:")
    for cap in payload["capabilities"]:
        print(
            f"  - L{cap['level']} {cap['name']}: "
            f"{_fmt_status(cap['status'])} - {cap['reason']}"
        )
    print("")
    print("recommended_next:")
    for item in payload["recommended_next"]:
        print(f"  - {item['command']}")
        print(f"    {item['why']}")
    if gate:
        print("")
        print("gate:")
        print(f"  name: {gate['name']}")
        print(f"  decision: {gate['decision']}")
        print(f"  exit_code: {gate['exit_code']}")
        print(f"  reason: {gate['reason']}")
        if gate["recommended_next"]:
            print("  gate_next:")
            for item in gate["recommended_next"]:
                print(f"    - {item['command']}")
                print(f"      {item['why']}")
    return int(gate["exit_code"]) if gate else 0
