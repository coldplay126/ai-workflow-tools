from __future__ import annotations

import argparse
import re
import shlex
from pathlib import Path

from awf.cli import KNOWN_COMMANDS, build_parser


REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_README = REPO_ROOT / "cli" / "README.md"
SKILL_COMMAND_RE = re.compile(r"^\s+command:\s*[\"']?(.+?)[\"']?\s*$", re.MULTILINE)
TEMPLATE_ARG_RE = re.compile(r"\{[^}]+\}")


def _subparser_action(
    parser: argparse.ArgumentParser, dest: str
) -> argparse._SubParsersAction:  # type: ignore[attr-defined]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction) and action.dest == dest:  # type: ignore[attr-defined]
            return action
    raise AssertionError(f"missing subparser action for dest={dest!r}")


def _subcommands(parser: argparse.ArgumentParser, dest: str) -> list[str]:
    return sorted(_subparser_action(parser, dest).choices)


def _nested_command_surface() -> dict[str, list[str]]:
    parser = build_parser()
    top_level = _subparser_action(parser, "command")
    nested: dict[str, list[str]] = {}
    for command, subparser in top_level.choices.items():
        for action in subparser._actions:
            if isinstance(action, argparse._SubParsersAction):  # type: ignore[attr-defined]
                nested[command] = sorted(action.choices)
                break
    return nested


def _argv_from_skill_command(command: str) -> list[str]:
    concrete = TEMPLATE_ARG_RE.sub("example", command)
    argv = shlex.split(concrete)
    if argv and argv[0] == "awf":
        argv = argv[1:]
    return argv


def test_known_commands_match_argparse_surface() -> None:
    parser = build_parser()
    assert set(KNOWN_COMMANDS) == set(_subcommands(parser, "command"))


def test_cli_readme_mentions_current_command_surface() -> None:
    parser = build_parser()
    readme = CLI_README.read_text(encoding="utf-8")

    missing: list[str] = []
    for command in _subcommands(parser, "command"):
        needle = f"awf {command}"
        if needle not in readme:
            missing.append(needle)

    for command, subcommands in _nested_command_surface().items():
        for subcommand in subcommands:
            needle = f"awf {command} {subcommand}"
            if needle not in readme:
                missing.append(needle)

    assert missing == []


def test_skill_cli_command_templates_parse_with_current_cli() -> None:
    parser = build_parser()
    invalid: list[str] = []
    for path in sorted((REPO_ROOT / "claude" / "skills").glob("*/SKILL.md")):
        text = path.read_text(encoding="utf-8")
        for match in SKILL_COMMAND_RE.finditer(text):
            command = match.group(1)
            argv = _argv_from_skill_command(command)
            try:
                parser.parse_args(argv)
            except SystemExit as exc:
                invalid.append(
                    f"{path.relative_to(REPO_ROOT)}: awf {' '.join(argv)} "
                    f"(exit={exc.code})"
                )

    assert invalid == []
