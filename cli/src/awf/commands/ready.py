from __future__ import annotations

import argparse
import json
import sys

from awf.core.ready import collect_ready_report


def _fmt_status(status: str) -> str:
    return {
        "ready": "ready",
        "caution": "caution",
        "blocked": "blocked",
        "not_started": "not started",
    }.get(status, status)


def run_ready(args: argparse.Namespace) -> int:
    try:
        payload = collect_ready_report(
            args.repo_root,
            probe=bool(getattr(args, "probe", False)),
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

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
    return 0
