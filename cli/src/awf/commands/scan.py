"""awf scan — project structure auto-discovery."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from awf.core.scanner import scan_repo, scan_result_to_dict
from awf.core.config_merger import merge_scan_results, merge_with_existing


def run_scan(args: argparse.Namespace) -> int:
    github_root = args.github_root or str(Path.home() / "Documents" / "GitHub")
    github_root_path = Path(github_root).resolve()

    if not github_root_path.is_dir():
        print(f"error: github root not found: {github_root_path}", file=sys.stderr)
        return 2

    # Collect repos to scan
    if getattr(args, "all", False):
        repos = sorted([
            d for d in github_root_path.iterdir()
            if d.is_dir() and not d.name.startswith(".") and (d / "package.json").exists() or (d / "pyproject.toml").exists() or (d / "go.mod").exists() or (d / "Cargo.toml").exists()
        ])
        if not repos:
            print(f"error: no projects found in {github_root_path}", file=sys.stderr)
            return 2
        print(f"scanning {len(repos)} repos in {github_root_path}", file=sys.stderr)
    elif args.repo_path:
        repo_path = Path(args.repo_path).resolve()
        if not repo_path.is_dir():
            print(f"error: repo not found: {repo_path}", file=sys.stderr)
            return 2
        repos = [repo_path]
    else:
        print("error: specify a repo path or --all", file=sys.stderr)
        return 2

    # Scan
    results = []
    use_ai = not getattr(args, "no_ai", False)
    for repo in repos:
        result = scan_repo(repo, use_ai=use_ai)
        if result.units:
            results.append(result)
            print(f"  {result.service}: {result.language}/{result.framework}, {len(result.units)} units, pattern={result.unit_pattern}", file=sys.stderr)
        else:
            print(f"  {result.service}: no units found (skipped)", file=sys.stderr)

    if not results:
        print("no units found in any repo.", file=sys.stderr)
        return 1

    # Single repo: output fragment
    if len(repos) == 1 and not getattr(args, "merge", False):
        output = scan_result_to_dict(results[0])
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    # Multiple repos: merge
    merged = merge_scan_results(results, str(github_root_path))

    # Merge with existing config if --merge
    if getattr(args, "merge", False):
        docs_root = args.docs_root or str(github_root_path / "analysis-docs")
        config_path = Path(docs_root) / "_templates" / "analysis-config.json"
        if config_path.exists():
            existing = json.loads(config_path.read_text(encoding="utf-8"))
            merged = merge_with_existing(merged, existing)
            print(f"merged with existing config: {config_path}", file=sys.stderr)
        else:
            print(f"no existing config at {config_path}, creating new", file=sys.stderr)

        if getattr(args, "dry_run", False):
            print(json.dumps(merged, ensure_ascii=False, indent=2))
        else:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"written: {config_path}")
    else:
        print(json.dumps(merged, ensure_ascii=False, indent=2))

    # Summary
    services = merged.get("service_map", {})
    domains = merged.get("domain_definitions", {})
    cross_service = sum(1 for d in domains.values() if len(d.get("directories", {})) > 1)
    print(f"\nsummary: {len(services)} services, {len(domains)} domains ({cross_service} cross-service)", file=sys.stderr)

    return 0
