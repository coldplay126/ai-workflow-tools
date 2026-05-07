from __future__ import annotations

import argparse
import json
import sys

from awf.core.config import load_awf_config
from awf.core.mcp import (
    check_mcp_server,
    discover_mcp_servers,
    invoke_mcp_tool,
    read_mcp_resource,
    resolve_mcp_server,
    summarize_mcp_server,
)


def run_mcp_list(args: argparse.Namespace) -> int:
    try:
        config = load_awf_config(args.repo_root)
        servers = discover_mcp_servers(config)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = {
        "servers": [
            {
                "name": server.name,
                "transport": server.transport,
                "config": server.config,
            }
            for server in servers
        ]
    }

    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    lines = ["mcp_servers:"]
    if not servers:
        lines.append("  - (none configured)")
    else:
        for server in servers:
            lines.append(f"  - {summarize_mcp_server(server)}")
    print("\n".join(lines))
    return 0


def run_mcp_check(args: argparse.Namespace) -> int:
    try:
        config = load_awf_config(args.repo_root)
        server = resolve_mcp_server(config, args.name)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    ok, detail = check_mcp_server(server)
    print(f"name: {server.name}")
    print(f"transport: {server.transport}")
    print(f"status: {'ok' if ok else 'failed'}")
    print(f"detail: {detail}")
    return 0 if ok else 1


def run_mcp_invoke(args: argparse.Namespace) -> int:
    try:
        config = load_awf_config(args.repo_root)
        server = resolve_mcp_server(config, args.name)
        arguments = json.loads(args.input) if args.input else {}
        if not isinstance(arguments, dict):
            raise ValueError("--input must decode to a JSON object")
        result = invoke_mcp_tool(server, args.tool, arguments)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = {
        "server": result.server,
        "transport": result.transport,
        "tool": result.tool,
        "protocol_version": result.protocol_version,
        "server_info": result.server_info,
        "result": result.result,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"server: {result.server}")
    print(f"transport: {result.transport}")
    print(f"tool: {result.tool}")
    print(f"protocol_version: {result.protocol_version}")
    print(f"server_info: {json.dumps(result.server_info, ensure_ascii=False)}")
    print("result:")
    print(json.dumps(result.result, ensure_ascii=False, indent=2))
    return 0


def run_mcp_read(args: argparse.Namespace) -> int:
    try:
        config = load_awf_config(args.repo_root)
        server = resolve_mcp_server(config, args.name)
        result = read_mcp_resource(server, args.uri)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = {
        "server": result.server,
        "transport": result.transport,
        "uri": result.uri,
        "protocol_version": result.protocol_version,
        "server_info": result.server_info,
        "result": result.result,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"server: {result.server}")
    print(f"transport: {result.transport}")
    print(f"uri: {result.uri}")
    print(f"protocol_version: {result.protocol_version}")
    print(f"server_info: {json.dumps(result.server_info, ensure_ascii=False)}")
    print("result:")
    print(json.dumps(result.result, ensure_ascii=False, indent=2))
    return 0
