"""``awf wiki`` subcommand handlers — operations log + lint + index.

The wiki subsystem persists project-scoped operational state under
``<repo_root>/.awf-operations/``. These handlers expose:

- ``awf wiki log``           — print log.md (or tail it)
- ``awf wiki lint``          — surface orphan/stale/missing-provenance issues
- ``awf wiki regenerate-index`` — rebuild index.md from wiki/ contents
- ``awf wiki events``        — print recorded JSONL event stream

All commands are read-mostly except ``regenerate-index``; failures print
to stderr and return a non-zero exit code.
"""
from __future__ import annotations

import argparse
import json
import sys

from awf.core.paths import find_repo_root
from awf.core.operational_metrics import iter_events
from awf.core.wiki import (
    event_summary_lines,
    lint as wiki_lint,
    log_path,
    regenerate_index,
)


def _resolve_repo_root(args: argparse.Namespace):
    explicit = getattr(args, "repo_root", None)
    return find_repo_root(explicit)


def run_wiki_log(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args)
    target = log_path(repo_root)
    if not target.exists():
        print("no operations log yet — log.md will be created on first event", file=sys.stderr)
        return 0
    text = target.read_text(encoding="utf-8")
    if getattr(args, "tail", None):
        # ``## `` lines mark events; show the last N entries.
        lines = text.splitlines()
        entry_indices = [i for i, line in enumerate(lines) if line.startswith("## ")]
        if entry_indices:
            cutoff = entry_indices[-args.tail] if args.tail <= len(entry_indices) else 0
            text = "\n".join(lines[cutoff:])
    print(text)
    return 0


def run_wiki_lint(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args)
    issues = wiki_lint(repo_root, stale_days=args.stale_days)
    if args.json:
        print(json.dumps(
            [{"page": i.page, "code": i.code, "detail": i.detail} for i in issues],
            ensure_ascii=False,
            indent=2,
        ))
        return 1 if issues else 0

    if not issues:
        print("✓ wiki lint clean")
        return 0
    print(f"found {len(issues)} issue(s):")
    for issue in issues:
        print(f"  [{issue.code}] {issue.page} — {issue.detail}")
    return 1


def run_wiki_regenerate_index(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args)
    target = regenerate_index(repo_root)
    print(f"regenerated {target}")
    return 0


def run_wiki_events(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args)
    events = list(iter_events(repo_root))
    if getattr(args, "type", None):
        events = [e for e in events if e.get("type") == args.type]
    if getattr(args, "limit", None):
        events = events[-args.limit:]
    if args.json:
        for event in events:
            print(json.dumps(event, ensure_ascii=False))
        return 0
    if not events:
        print("no events recorded yet")
        return 0
    for line in event_summary_lines(events):
        print(line)
    return 0
