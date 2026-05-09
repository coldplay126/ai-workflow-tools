from __future__ import annotations

import argparse
import json
import sys
from typing import TextIO

from awf.core.ready import collect_ready_report, evaluate_ready_gate


def ready_gate_disabled(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "no_ready_gate", False))


def enforce_ready_gate(
    args: argparse.Namespace,
    gate: str,
    *,
    json_output: bool = False,
    stream: TextIO | None = None,
) -> int:
    """Enforce an ``awf ready --gate`` decision inside mutating commands."""
    if ready_gate_disabled(args):
        return 0

    repo_root = getattr(args, "repo_root", None)
    try:
        report = collect_ready_report(repo_root)
        decision = evaluate_ready_gate(report, gate)
    except Exception as exc:
        if json_output:
            print(json.dumps(
                {
                    "error": "ready_gate_failed",
                    "gate": gate,
                    "reason": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ))
        else:
            print(f"error: ready gate `{gate}` failed: {exc}", file=stream or sys.stderr)
        return 2

    if decision["decision"] == "allow":
        return 0

    if json_output:
        print(json.dumps(
            {
                "error": "ready_gate_blocked",
                "gate": decision,
            },
            ensure_ascii=False,
            indent=2,
        ))
        return int(decision["exit_code"])

    out = stream or sys.stderr
    print(
        f"error: ready gate `{gate}` returned {decision['decision']}: "
        f"{decision['reason']}",
        file=out,
    )
    for item in decision.get("recommended_next", []) or []:
        print(f"next_step: {item['command']}", file=out)
        if item.get("why"):
            print(f"  {item['why']}", file=out)
    print("hint: pass --no-ready-gate to bypass this deterministic preflight.", file=out)
    return int(decision["exit_code"])
