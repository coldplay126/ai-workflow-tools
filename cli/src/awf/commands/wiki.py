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
import subprocess
import sys
from pathlib import Path

import os

from awf.core.operational_metrics import iter_events
from awf.core.wiki import (
    KNOWN_PROFILES,
    PROFILE_CONSUMER,
    PROFILE_SELF_IMPROVEMENT,
    WikiPage,
    decision_path,
    decision_template,
    event_summary_lines,
    lint as wiki_lint,
    log_path,
    read_profile,
    regenerate_index,
    starter_directories,
    wiki_root,
    write_page,
    write_profile,
)


def _resolve_repo_root(args: argparse.Namespace) -> Path:
    """Resolve repo root for wiki commands without requiring awf markers.

    The wiki layout is self-contained under ``.awf-operations/`` and works
    in any directory, not just an awf repo. This is intentional: a
    consumer project may use awf wiki without checking in awf's own
    config files.
    """
    explicit = getattr(args, "repo_root", None)
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path(os.getcwd()).resolve()


def _detect_self_improvement_hint(repo_root: Path) -> bool:
    """Heuristic: is this the awf repo itself?

    Used purely as an advisory hint. We never auto-set profile —
    auto-detection has too many false positives — but we do print a
    one-line recommendation when running ``awf wiki init`` from a repo
    that looks like awf itself.
    """
    pyproject = repo_root / "cli" / "pyproject.toml"
    if pyproject.exists():
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            return False
        return 'name = "awf-cli"' in text
    return False


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


def run_wiki_init(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args)
    profile = args.profile or PROFILE_CONSUMER
    if profile not in KNOWN_PROFILES:
        print(
            f"error: unknown profile {profile!r}; expected one of {KNOWN_PROFILES}",
            file=sys.stderr,
        )
        return 2

    if not args.profile and _detect_self_improvement_hint(repo_root):
        print(
            "hint: this repo looks like awf-cli itself — consider "
            "`awf wiki init --profile self_improvement`",
            file=sys.stderr,
        )

    write_profile(repo_root, profile)
    root = wiki_root(repo_root)
    for sub in starter_directories(profile):
        target = root / sub
        target.mkdir(parents=True, exist_ok=True)
        keep = target / ".gitkeep"
        if not keep.exists():
            keep.write_text("", encoding="utf-8")

    regenerate_index(repo_root)
    print(f"profile: {profile}")
    print(f"created starter dirs under {root}: {', '.join(starter_directories(profile))}")
    return 0


def _gh_pr_body(pr_number: int) -> tuple[list[str], str]:
    """Fetch ``gh pr view`` JSON and return (context_prs, body) tuple.

    Failure paths print a warning and return empty context — the
    decision still gets created with the user-provided title and an
    empty body, so the command never blocks on network/GitHub issues.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "number,title,body,url"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"warning: gh pr view failed: {exc}", file=sys.stderr)
        return [], ""
    if result.returncode != 0:
        print(
            f"warning: gh pr view exited with {result.returncode}: "
            f"{(result.stderr or '').strip()}",
            file=sys.stderr,
        )
        return [], ""
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"warning: gh pr view returned non-JSON: {exc}", file=sys.stderr)
        return [], ""
    pr_ref = f"#{data.get('number', pr_number)}"
    title = str(data.get("title") or "").strip()
    url = str(data.get("url") or "").strip()
    body = str(data.get("body") or "").strip()
    header_lines = [pr_ref]
    if title:
        header_lines.append(f"Title: {title}")
    if url:
        header_lines.append(f"URL: {url}")
    return [pr_ref], "\n".join(header_lines) + "\n\n" + body


def run_wiki_decision(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args)
    profile = args.profile or read_profile(repo_root)
    if profile not in KNOWN_PROFILES:
        print(f"error: unknown profile {profile!r}", file=sys.stderr)
        return 2

    target = decision_path(repo_root, title=args.title)
    if target.exists() and not args.force:
        print(
            f"error: decision already exists at {target} (use --force to overwrite)",
            file=sys.stderr,
        )
        return 2

    context_prs: list[str] = []
    pr_body: str | None = None
    if args.from_pr is not None:
        context_prs, pr_body = _gh_pr_body(args.from_pr)
        if not context_prs:
            # Best-effort: still record the requested PR ref so the user
            # can edit the page even when gh is unavailable.
            context_prs = [f"#{args.from_pr}"]

    page = decision_template(
        profile=profile,
        title=args.title,
        context_prs=context_prs,
        pr_body=pr_body,
    )
    write_page(target, page)
    regenerate_index(repo_root)
    print(f"created {target}")
    print(f"profile: {profile}")
    if context_prs:
        print(f"context_prs: {', '.join(context_prs)}")
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
