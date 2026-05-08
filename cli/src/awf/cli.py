from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

from awf.commands.analyze import run_analyze
from awf.commands.chat import run_chat
from awf.commands.cmux import run_cmux_failures, run_cmux_runs, run_cmux_tail
from awf.commands.config import run_config_show
from awf.commands.doctor import run_doctor
from awf.commands.mcp import run_mcp_check, run_mcp_invoke, run_mcp_list, run_mcp_read
from awf.commands.skills import run_skills_list
from awf.commands.init import run_init
from awf.commands.scan import run_scan
from awf.commands.wf_apply import run_wf_apply_result
from awf.commands.wf import (
    run_wf_decide,
    run_wf_detect_class,
    run_wf_expand_scope,
    run_wf_gate,
    run_wf_init,
    run_wf_next,
    run_wf_reset,
    run_wf_status,
)
from awf.core.router import route_natural_language


KNOWN_COMMANDS = {"chat", "analyze", "wf", "config", "skills", "mcp", "doctor", "scan", "init", "cmux"}


def _should_skip_execution_confirmation(args: argparse.Namespace) -> bool:
    if getattr(args, "non_interactive", False):
        return True
    if os.environ.get("AWF_AUTO_CONFIRM", "").strip().lower() in {"1", "true", "yes", "y"}:
        return True
    if not sys.stdin.isatty():
        return True
    return False


def _confirm_execution(args: argparse.Namespace, routed_argv: list[str]) -> bool:
    if _should_skip_execution_confirmation(args):
        return True
    command_preview = "awf " + " ".join(routed_argv)
    risk_level = str(getattr(args, "route_risk_level", "low"))
    print(f"routed_command: {command_preview}", file=sys.stderr)
    print(f"risk_level: {risk_level}", file=sys.stderr)
    reply = input("Execute this command? [y/N] ").strip().lower()
    return reply in {"y", "yes"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="awf", description="AWF workflow and analysis CLI.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    chat_parser = subparsers.add_parser(
        "chat",
        help="Minimal Phase 5 chat session mode backed by SQLite session storage.",
    )
    chat_parser.add_argument("--repo-root", help="Repository root. Defaults to current or parent directories.")
    chat_parser.add_argument("--provider", help="Provider name. Defaults to config provider.default.")
    chat_parser.add_argument("--session-id", help="Reuse an existing chat session id.")
    chat_parser.add_argument("--latest", action="store_true", help="Reuse the most recently updated chat session.")
    chat_parser.add_argument("--message", help="Run a single non-interactive chat turn and exit.")
    chat_parser.add_argument("--list-sessions", action="store_true", help="List known chat sessions from the SQLite session store.")
    chat_parser.add_argument("--show-session", help="Print stored messages for an existing chat session id.")
    chat_parser.add_argument("--show-latest", action="store_true", help="Print stored messages for the most recently updated chat session.")
    chat_parser.add_argument("--compact-session", help="Compact an existing chat session id.")
    chat_parser.add_argument("--compact-latest", action="store_true", help="Compact the most recently updated chat session.")
    chat_parser.add_argument("--json", action="store_true", help="Print single-turn output as JSON.")
    chat_parser.add_argument("--yolo", action="store_true", help="Bypass configured permission checks for this run.")
    chat_parser.set_defaults(handler=run_chat)

    analyze_parser = subparsers.add_parser(
        "analyze",
        help="Run delegated analysis through the configured provider.",
    )
    analyze_parser.add_argument("service", help="Service name, e.g. sample-api")
    analyze_parser.add_argument("domain", nargs="?", default=None, help="Domain or domain cluster name, e.g. quest-challenge. Omit with --all.")
    analyze_parser.add_argument("--all", action="store_true", help="Analyze all domains in the service sequentially.")
    analyze_parser.add_argument("--delay", type=int, default=10, help="Seconds to wait between domains in --all mode. Default: 10")
    analyze_parser.add_argument(
        "--mode",
        choices=["solo", "quick", "precise", "cross", "critical"],
        help="Multi-agent mode. solo=default, precise=codex→primary, cross=codex+sonnet parallel+judge, critical=codex→sonnet→primary sequential.",
    )
    analyze_parser.add_argument("--repo-root", help="ai-workflow-tools repo root. Defaults to current or parent directories.")
    analyze_parser.add_argument("--docs-root", help="Override AWF_DOCS_ROOT.")
    analyze_parser.add_argument("--github-root", help="Override AWF_GITHUB_ROOT.")
    analyze_parser.add_argument("--provider", help="Provider name. Defaults to config provider.default.")
    analyze_parser.add_argument(
        "--provider-add-dirs",
        choices=["off", "minimal", "full"],
        default=os.environ.get("AWF_PROVIDER_ADD_DIRS", "off"),
        help="Pass extra readable directories to native CLI providers. Default: off. minimal adds external project roots; full adds all discovered analysis roots.",
    )
    analyze_parser.add_argument("--yolo", action="store_true", help="Bypass configured permission checks for this run.")
    analyze_parser.add_argument("--status", action="store_true", help="Show .analysis-state.json summary for this service/domain and exit.")
    analyze_parser.add_argument("--json", action="store_true", help="Print status as raw JSON when used with --status.")
    analyze_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="CI/CD-oriented mode. Avoid interactive guidance and prefer exit-code-driven behavior.",
    )
    analyze_parser.add_argument("--dry-run", action="store_true", help="Print resolved paths and prompt without invoking the provider.")
    analyze_parser.add_argument("--print-prompt", action="store_true", help="Print the delegated prompt before execution.")
    analyze_parser.add_argument("--output-format", choices=["text", "json"], default="text", help="Output format. json outputs structured data to stdout.")
    analyze_parser.add_argument("--check", action="store_true", help="Detect stale .ai-context by comparing hashes. No analysis run.")
    analyze_parser.add_argument("--catalog", action="store_true", help="Show analysis status for all units in a service.")
    analyze_parser.add_argument("--cycles", action="store_true", help="Report import cycles in each analyzed unit's saved import graph. No analysis run.")
    analyze_parser.set_defaults(handler=run_analyze)

    wf_parser = subparsers.add_parser("wf", help="Workflow helpers.")
    wf_subparsers = wf_parser.add_subparsers(dest="wf_command", required=True)
    wf_init_parser = wf_subparsers.add_parser("init", help="Initialize .workflow with a concept and baseline state.")
    wf_init_parser.add_argument("concept", help="Initial workflow concept text.")
    wf_init_parser.add_argument("--repo-root", help="Repository root containing .workflow/. Defaults to current or parent directories.")
    wf_init_parser.add_argument("--force", action="store_true", help="Overwrite existing workflow state and concept if present.")
    wf_init_parser.set_defaults(handler=run_wf_init)

    wf_status_parser = wf_subparsers.add_parser("status", help="Show .workflow/state.json summary.")
    wf_status_parser.add_argument("--repo-root", help="Repository root containing .workflow/. Defaults to current or parent directories.")
    wf_status_parser.add_argument("--json", action="store_true", help="Print raw JSON instead of a text summary.")
    wf_status_parser.set_defaults(handler=run_wf_status)

    wf_next_parser = wf_subparsers.add_parser(
        "next",
        help="Resolve the next workflow phase and build or execute a delegated prompt.",
    )
    wf_next_parser.add_argument("--repo-root", help="Repository root containing .workflow/. Defaults to current or parent directories.")
    wf_next_parser.add_argument("--phase", help="Override the target phase instead of resolving from state.")
    wf_next_parser.add_argument("--provider", help="Provider name. Defaults to config provider.default.")
    wf_next_parser.add_argument(
        "--mode",
        choices=["solo", "quick", "precise", "cross", "critical"],
        help="Multi-agent mode. solo=primary only, quick=codex fast, precise=codex→primary, cross=codex+sonnet parallel, critical=codex→sonnet→primary sequential.",
    )
    wf_next_parser.add_argument("--yolo", action="store_true", help="Bypass configured permission checks for this run.")
    wf_next_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="CI/CD-oriented mode. Review/verify phases auto-apply by default and output stays exit-code-driven.",
    )
    wf_next_parser.add_argument("--dry-run", action="store_true", help="Print phase metadata and prompt without invoking the provider.")
    wf_next_parser.add_argument("--print-prompt", action="store_true", help="Print the delegated prompt before execution.")
    wf_next_parser.add_argument("--auto-apply", action="store_true", help="Automatically apply review/verify results into workflow artifacts when possible.")
    wf_next_parser.add_argument("--output-format", choices=["text", "json"], default="text", help="Output format. json outputs structured data to stdout.")
    wf_next_parser.set_defaults(handler=run_wf_next)

    wf_decide_parser = wf_subparsers.add_parser(
        "decide",
        help="Apply a manual closed-loop workflow decision for a phase in deciding state.",
    )
    wf_decide_parser.add_argument("decision", choices=["continue", "replan", "abort"], help="Decision to apply.")
    wf_decide_parser.add_argument("--repo-root", help="Repository root containing .workflow/. Defaults to current or parent directories.")
    wf_decide_parser.add_argument("--phase", help="Target phase. Defaults to currentPhase from workflow state.")
    wf_decide_parser.add_argument("--target", help="Replan target phase. Defaults to plan when decision=replan.")
    wf_decide_parser.set_defaults(handler=run_wf_decide)

    wf_apply_parser = wf_subparsers.add_parser(
        "apply-result",
        help="Render provider JSON results into workflow artifacts for review/verify.",
    )
    wf_apply_parser.add_argument("phase", choices=["review", "verify"], help="Workflow phase to apply.")
    wf_apply_parser.add_argument("result_file", help="Path to a provider JSON result file.")
    wf_apply_parser.add_argument("--repo-root", help="Repository root containing .workflow/. Defaults to current or parent directories.")
    wf_apply_parser.set_defaults(handler=run_wf_apply_result)

    wf_gate_parser = wf_subparsers.add_parser("gate", help="Run deterministic gate evaluation (plan, review, verify).")
    wf_gate_parser.add_argument("phase", help="Phase to evaluate gate for (plan, review, verify).")
    wf_gate_parser.add_argument("--repo-root", help="Repository root containing .workflow/.")
    wf_gate_parser.add_argument("--result-file", help="Path to provider result file (required for review/verify). Accepts raw or enveloped JSON.")
    wf_gate_parser.add_argument("--json", action="store_true", help="Also print structured JSON output.")
    wf_gate_parser.set_defaults(handler=run_wf_gate)

    wf_detect_class_parser = wf_subparsers.add_parser("detect-class", help="Detect change class from concept text.")
    wf_detect_class_parser.add_argument("concept", nargs="?", default="", help="Concept text to classify.")
    wf_detect_class_parser.add_argument("--concept-file", help="Read concept from file (default: .workflow/concept.md if exists).")
    wf_detect_class_parser.add_argument("--json", action="store_true", help="Print structured JSON with skip/hil/path and risk investment.")
    wf_detect_class_parser.set_defaults(handler=run_wf_detect_class)

    wf_reset_parser = wf_subparsers.add_parser("reset", help="Reset workflow state back to the plan phase.")
    wf_reset_parser.add_argument("--repo-root", help="Repository root containing .workflow/. Defaults to current or parent directories.")
    wf_reset_parser.add_argument("--concept", help="Optional replacement concept text. Defaults to existing .workflow/concept.md.")
    wf_reset_parser.set_defaults(handler=run_wf_reset)

    wf_expand_parser = wf_subparsers.add_parser(
        "expand-scope",
        help="Expand allowed-files.json with reverse dependents / imports from saved import graphs.",
    )
    wf_expand_parser.add_argument("--repo-root", help="Repository root containing .workflow/. Defaults to current or parent directories.")
    wf_expand_parser.add_argument(
        "--direction",
        choices=("dependents", "imports", "both"),
        default="dependents",
        help="Which neighbors to add. dependents = files that import a planned file (default). imports = files a planned file depends on. both = combine.",
    )
    wf_expand_parser.add_argument(
        "--depth",
        type=int,
        default=1,
        help="Graph traversal depth. 1 = direct neighbors (default). 0 or negative = full transitive closure.",
    )
    wf_expand_parser.add_argument(
        "--service",
        action="append",
        help="Restrict graph search to one or more services (repeatable). Default: scan every service under docs_root.",
    )
    wf_expand_parser.add_argument(
        "--runtime-only",
        action="store_true",
        help="Skip type-only edges. Default keeps them — analysis pipelines treat type changes as scope-relevant.",
    )
    wf_expand_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the expansion without writing back to allowed-files.json.",
    )
    wf_expand_parser.add_argument("--json", action="store_true", help="Print expansion result as JSON.")
    wf_expand_parser.set_defaults(handler=run_wf_expand_scope)

    config_parser = subparsers.add_parser("config", help="Configuration helpers.")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)
    config_show_parser = config_subparsers.add_parser("show", help="Show merged config and resolved runtime paths.")
    config_show_parser.add_argument("--repo-root", help="ai-workflow-tools repo root. Defaults to current or parent directories.")
    config_show_parser.add_argument("--json", action="store_true", help="Print raw JSON.")
    config_show_parser.set_defaults(handler=run_config_show)

    skills_parser = subparsers.add_parser("skills", help="Skill discovery helpers.")
    skills_subparsers = skills_parser.add_subparsers(dest="skills_command", required=True)
    skills_list_parser = skills_subparsers.add_parser("list", help="List discovered skills from supported search paths.")
    skills_list_parser.add_argument("--repo-root", help="ai-workflow-tools repo root. Defaults to current or parent directories.")
    skills_list_parser.add_argument("--json", action="store_true", help="Print raw JSON.")
    skills_list_parser.set_defaults(handler=run_skills_list)

    mcp_parser = subparsers.add_parser("mcp", help="MCP registry helpers.")
    mcp_subparsers = mcp_parser.add_subparsers(dest="mcp_command", required=True)
    mcp_list_parser = mcp_subparsers.add_parser("list", help="List configured MCP servers from merged config.")
    mcp_list_parser.add_argument("--repo-root", help="ai-workflow-tools repo root. Defaults to current or parent directories.")
    mcp_list_parser.add_argument("--json", action="store_true", help="Print raw JSON.")
    mcp_list_parser.set_defaults(handler=run_mcp_list)
    mcp_check_parser = mcp_subparsers.add_parser(
        "check",
        help="Validate transport-level MCP connectivity and minimal protocol support for a configured server.",
    )
    mcp_check_parser.add_argument("name", help="Configured MCP server name.")
    mcp_check_parser.add_argument("--repo-root", help="ai-workflow-tools repo root. Defaults to current or parent directories.")
    mcp_check_parser.set_defaults(handler=run_mcp_check)
    mcp_invoke_parser = mcp_subparsers.add_parser(
        "invoke",
        help="Invoke a tool on a configured MCP server. Currently supports stdio and http transports.",
    )
    mcp_invoke_parser.add_argument("name", help="Configured MCP server name.")
    mcp_invoke_parser.add_argument("tool", help="Tool name to call.")
    mcp_invoke_parser.add_argument(
        "--input",
        default="{}",
        help="JSON object string passed as tools/call arguments. Defaults to {}.",
    )
    mcp_invoke_parser.add_argument("--repo-root", help="ai-workflow-tools repo root. Defaults to current or parent directories.")
    mcp_invoke_parser.add_argument("--json", action="store_true", help="Print raw JSON.")
    mcp_invoke_parser.set_defaults(handler=run_mcp_invoke)
    mcp_read_parser = mcp_subparsers.add_parser(
        "read",
        help="Read a resource from a configured MCP server. Currently supports stdio and http transports.",
    )
    mcp_read_parser.add_argument("name", help="Configured MCP server name.")
    mcp_read_parser.add_argument("uri", help="Resource URI to read.")
    mcp_read_parser.add_argument("--repo-root", help="ai-workflow-tools repo root. Defaults to current or parent directories.")
    mcp_read_parser.add_argument("--json", action="store_true", help="Print raw JSON.")
    mcp_read_parser.set_defaults(handler=run_mcp_read)

    doctor_parser = subparsers.add_parser("doctor", help="Show provider readiness and local runtime configuration.")
    doctor_parser.add_argument("--repo-root", help="ai-workflow-tools repo root. Defaults to current or parent directories.")
    doctor_parser.add_argument("--json", action="store_true", help="Print raw JSON.")
    doctor_parser.add_argument("--probe", action="store_true", help="Run lightweight provider probes where supported.")
    doctor_parser.add_argument(
        "--ci",
        action="store_true",
        help="Exit non-zero when the default provider is not ready for installed/configured checks; includes probe when --probe is set.",
    )
    doctor_parser.set_defaults(handler=run_doctor)

    init_parser = subparsers.add_parser("init", help="Initialize .awf.toml in the current project.")
    init_parser.add_argument("--repo-root", help="Target directory. Defaults to current directory.")
    init_parser.add_argument("--force", action="store_true", help="Overwrite existing .awf.toml.")
    init_parser.set_defaults(handler=run_init)

    cmux_parser = subparsers.add_parser("cmux", help="cmux-agent observability helpers.")
    cmux_subparsers = cmux_parser.add_subparsers(dest="cmux_command", required=True)

    cmux_tail_parser = cmux_subparsers.add_parser(
        "tail",
        help="Tail the cmux-agent JSONL event log (read-only).",
    )
    cmux_tail_parser.add_argument("path", nargs="?", help="Path to events.jsonl or a directory containing .agent/events.jsonl.")
    cmux_tail_parser.add_argument("--repo-root", help="Repository root for auto-discovery. Defaults to current directory.")
    cmux_tail_parser.add_argument("--run-id", help="Filter events by run_id.")
    cmux_tail_parser.add_argument("--event", help="Filter events by exact event name (e.g. run.status_changed).")
    cmux_tail_parser.add_argument("--limit", type=int, help="Only print the last N matching events.")
    cmux_tail_parser.add_argument("--json", action="store_true", help="Emit raw JSON lines instead of a formatted row.")
    cmux_tail_parser.add_argument("-f", "--follow", action="store_true", help="Continue reading as events are appended. Ctrl-C exits cleanly.")
    cmux_tail_parser.set_defaults(handler=run_cmux_tail)

    cmux_runs_parser = cmux_subparsers.add_parser(
        "runs",
        help="Summarize cmux-agent runs from the JSONL event log.",
    )
    cmux_runs_parser.add_argument("path", nargs="?", help="Path to events.jsonl or a directory containing .agent/events.jsonl.")
    cmux_runs_parser.add_argument("--repo-root", help="Repository root for auto-discovery. Defaults to current directory.")
    cmux_runs_parser.add_argument("--json", action="store_true", help="Print the summary as JSON.")
    cmux_runs_parser.add_argument("--event", help="Ignored for aggregation; kept for symmetry with tail.")
    cmux_runs_parser.add_argument("--limit", type=int, help="Only show the most recent N runs.")
    cmux_runs_parser.set_defaults(handler=run_cmux_runs)

    cmux_failures_parser = cmux_subparsers.add_parser(
        "failures",
        help="Show cmux-agent failure events from the JSONL event log.",
    )
    cmux_failures_parser.add_argument("path", nargs="?", help="Path to events.jsonl or a directory containing .agent/events.jsonl.")
    cmux_failures_parser.add_argument("--repo-root", help="Repository root for auto-discovery. Defaults to current directory.")
    cmux_failures_parser.add_argument("--run-id", help="Filter failures by run_id.")
    cmux_failures_parser.add_argument("--limit", type=int, help="Only show the most recent N failures.")
    cmux_failures_parser.add_argument("--json", action="store_true", help="Print failures as JSON.")
    cmux_failures_parser.set_defaults(handler=run_cmux_failures)

    scan_parser = subparsers.add_parser("scan", help="Auto-discover project structure for analysis config.")
    scan_parser.add_argument("repo_path", nargs="?", help="Path to a single repo to scan.")
    scan_parser.add_argument("--all", action="store_true", help="Scan all repos under --github-root.")
    scan_parser.add_argument("--github-root", help="Parent directory containing repos. Default: ~/Documents/GitHub")
    scan_parser.add_argument("--docs-root", help="analysis-docs root for --merge. Default: {github-root}/analysis-docs")
    scan_parser.add_argument("--merge", action="store_true", help="Merge results into analysis-config.json.")
    scan_parser.add_argument("--dry-run", action="store_true", help="With --merge, print merged config instead of writing.")
    scan_parser.add_argument("--no-ai", action="store_true", help="Heuristic only, skip AI domain discovery fallback.")
    scan_parser.set_defaults(handler=run_scan)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in KNOWN_COMMANDS and not argv[0].startswith("-"):
        natural_text = " ".join(argv).strip()
        decision = route_natural_language(natural_text, os.getcwd())
        routed_argv = list(decision.argv or ["chat", "--message", natural_text])
        parser = build_parser()
        args = parser.parse_args(routed_argv)
        setattr(args, "route_reason", decision.reason)
        setattr(args, "route_target", decision.target)
        setattr(args, "route_requires_confirmation", decision.requires_confirmation)
        setattr(args, "route_risk_level", decision.risk_level)
        handler = getattr(args, "handler", None)
        if handler is None:
            parser.print_help()
            return 1
        if decision.requires_confirmation and not _confirm_execution(args, routed_argv):
            print("execution_cancelled: confirmation declined", file=sys.stderr)
            return 1
        return handler(args)

    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


def cli_entry() -> None:
    """Console script entry point — calls main() and converts return code to sys.exit()."""
    sys.exit(main())
