"""awf init — project initialization."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from awf.core.scanner import detect_language


DEFAULT_TOML = """\
[provider]
default = "claude-code"

[analysis]
default_mode = "standard"

[permissions]
yolo = false
"""


def run_init(args: argparse.Namespace) -> int:
    target = Path(args.repo_root).resolve() if args.repo_root else Path.cwd().resolve()

    if not target.is_dir():
        print(f"error: directory not found: {target}", file=sys.stderr)
        return 2

    toml_path = target / ".awf.toml"
    if toml_path.exists() and not getattr(args, "force", False):
        print(f"error: .awf.toml already exists at {toml_path}. Use --force to overwrite.", file=sys.stderr)
        return 1

    # Detect project
    language, framework = detect_language(target)
    context_providers = []
    for name in ["CLAUDE.md", "AGENTS.md", ".mcp.json", ".specify"]:
        if (target / name).exists():
            context_providers.append(name)

    is_git = (target / ".git").exists()

    # Generate .awf.toml
    toml_path.write_text(DEFAULT_TOML, encoding="utf-8")

    # Output
    print(f"project: {target.name}")
    print(f"language: {language}/{framework}")
    if context_providers:
        print(f"context_providers: {', '.join(context_providers)}")
    if is_git:
        print(f"git: yes")
    print(f"created: {toml_path}")

    return 0
