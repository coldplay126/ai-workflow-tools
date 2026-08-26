from __future__ import annotations

import argparse
import json

from awf.core.lsp_setup import materialize_lsp, setup_lsp, status_lsp


def run_lsp_setup(args: argparse.Namespace) -> int:
    result = setup_lsp(getattr(args, "repo_root", None), apply=bool(getattr(args, "apply", False)))
    return _render(result, as_json=bool(getattr(args, "json", False)))


def run_lsp_status(args: argparse.Namespace) -> int:
    result = status_lsp(getattr(args, "repo_root", None))
    return _render(result, as_json=bool(getattr(args, "json", False)))


def run_lsp_materialize(args: argparse.Namespace) -> int:
    result = materialize_lsp(getattr(args, "repo_root", None))
    return _render(result, as_json=bool(getattr(args, "json", False)))


def _render(result: dict, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"decision: {result['decision']}")
        languages = ", ".join(result["languages"]) or "none"
        print(f"languages: {languages}")
        for server in result["servers"]:
            availability = "available" if server["available"] else "missing"
            print(f"server: {server['name']} ({server['binary']}, {availability})")
        for action in result["actions"]:
            print(f"action: {action['kind']}={action['status']}")
        for blocker in result["blockers"]:
            print(f"blocker: {blocker['code']}: {blocker['message']}")
        for warning in result["warnings"]:
            print(f"warning: {warning['code']}: {warning['message']}")
    return 2 if result["decision"] in {"blocked", "partial"} else 0
