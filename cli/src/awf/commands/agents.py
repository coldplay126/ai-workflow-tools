from __future__ import annotations

import json
import sys

from awf.core.omp_agents import sync_omp_agents


def run_agents_sync_omp(args) -> int:
    try:
        result = sync_omp_agents(
            args.repo_root or ".",
            dry_run=bool(args.dry_run),
            force=bool(args.force),
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"agents_sync_error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"target_dir: {result['target_dir']}")
        for key in ("created", "updated", "unchanged", "removed", "conflicts"):
            values = result[key]
            print(f"{key}: {', '.join(values) if values else '-'}")
        print(f"generated_count: {result['generated_count']}")
    return 1 if result["conflicts"] else 0
