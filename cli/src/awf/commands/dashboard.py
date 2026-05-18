"""`awf dashboard` command handler (FR-010 분리)."""

from __future__ import annotations

import argparse

from awf.core.dashboard import run_dashboard


def run_dashboard_command(args: argparse.Namespace) -> int:
    interval = int(getattr(args, "interval", 5))
    repo_root = getattr(args, "repo_root", None)
    return run_dashboard(repo_root, interval)
