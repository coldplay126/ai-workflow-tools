from __future__ import annotations

import argparse
import sys

from awf.commands.wf import _ensure_database_evidence_before_apply
from awf.core.state import resolve_repo_root
from awf.core.workflow_results import apply_workflow_result


def run_wf_apply_result(args: argparse.Namespace) -> int:
    if args.phase in {"verify", "test"}:
        try:
            _ensure_database_evidence_before_apply(
                resolve_repo_root(args.repo_root),
                args.phase,
            )
        except Exception:
            pass

    try:
        output_path, passed = apply_workflow_result(args.repo_root, args.phase, args.result_file)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"phase: {args.phase}")
    print(f"result_file: {args.result_file}")
    print(f"artifact: {output_path}")
    print(f"gate: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 3
