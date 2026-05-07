from __future__ import annotations

import argparse
import json
import sys

from awf.core.config import load_awf_config, resolve_runtime_paths


def run_config_show(args: argparse.Namespace) -> int:
    try:
        config = load_awf_config(args.repo_root)
        runtime_paths = resolve_runtime_paths(args.repo_root)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = {
        "provider_default": config.provider_name(),
        "provider_fallback": config.raw.get("provider", {}).get("fallback", []),
        "providers": {
            key: value
            for key, value in config.raw.get("provider", {}).items()
            if key not in {"default", "fallback"}
        },
        "analysis": config.raw.get("analysis", {}),
        "permissions": config.raw.get("permissions", {}),
        "mcp": config.raw.get("mcp", {}),
        "paths": runtime_paths,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    lines = [
        f"repo_root: {runtime_paths['repo_root']}",
        f"project_config: {runtime_paths['project_config']}",
        f"user_config: {runtime_paths['user_config']}",
        f"provider_default: {payload['provider_default']}",
        f"provider_fallback: {json.dumps(payload['provider_fallback'], ensure_ascii=False)}",
        f"permissions: {json.dumps(payload['permissions'], ensure_ascii=False)}",
        f"mcp_servers: {json.dumps(sorted(payload['mcp'].keys()), ensure_ascii=False)}",
        f"analysis_docs: {runtime_paths['analysis_docs']}",
        f"awf_github: {runtime_paths['awf_github']}",
        "providers:",
    ]
    for name, settings in payload["providers"].items():
        lines.append(f"  - {name}: {json.dumps(settings, ensure_ascii=False)}")

    print("\n".join(lines))
    return 0
