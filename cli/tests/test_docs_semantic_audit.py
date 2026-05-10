from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path

from awf.cli import KNOWN_COMMANDS, build_parser
from awf.core.state import PHASE_GATE, PHASE_ORDER


REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_README = REPO_ROOT / "cli" / "README.md"
SKILL_COMMAND_RE = re.compile(r"^\s+command:\s*[\"']?(.+?)[\"']?\s*$", re.MULTILINE)
TEMPLATE_ARG_RE = re.compile(r"\{[^}]+\}")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)
TOP_LEVEL_SCALAR_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$")
STALE_WF_ALIAS_RE = re.compile(r"/wf(?:\.|\b(?!-))")


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


def _skill_files() -> tuple[Path, ...]:
    return tuple(sorted((REPO_ROOT / "claude" / "skills").glob("*/SKILL.md")))


def _skill_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}

    result: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if line.startswith((" ", "\t")):
            continue
        scalar = TOP_LEVEL_SCALAR_RE.match(line)
        if not scalar:
            continue
        key, value = scalar.groups()
        result[key] = value.strip().strip("\"'")
    return result


def _skill_command_template(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8")
    match = SKILL_COMMAND_RE.search(text)
    return match.group(1) if match else None


def _resolve_repo_template_path(value: str) -> Path:
    if value.startswith("repo/"):
        return REPO_ROOT / value.removeprefix("repo/")
    return REPO_ROOT / value


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
    for path in _skill_files():
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


def test_skill_frontmatter_has_required_identity_metadata() -> None:
    invalid: list[str] = []
    required = {"name", "version", "description", "type"}
    for path in _skill_files():
        meta = _skill_frontmatter(path)
        missing = sorted(required - set(meta))
        if missing:
            invalid.append(f"{path.relative_to(REPO_ROOT)} missing={missing}")
            continue
        if meta["name"] != path.parent.name:
            invalid.append(
                f"{path.relative_to(REPO_ROOT)} name={meta['name']!r} "
                f"dir={path.parent.name!r}"
            )

    assert invalid == []


def test_workflow_phase_skill_metadata_matches_phase_contracts() -> None:
    invalid: list[str] = []
    for path in _skill_files():
        meta = _skill_frontmatter(path)
        name = meta.get("name", "")
        if not name.startswith("phase-"):
            continue

        phase = name.removeprefix("phase-")
        expected_gate = PHASE_GATE.get(phase)
        expected_command = f"awf wf next --phase {phase}"
        command = _skill_command_template(path)

        if phase not in PHASE_ORDER:
            invalid.append(f"{path.relative_to(REPO_ROOT)} unknown phase={phase!r}")
        if meta.get("type") != "workflow-phase":
            invalid.append(f"{path.relative_to(REPO_ROOT)} type={meta.get('type')!r}")
        if meta.get("phase") != phase:
            invalid.append(f"{path.relative_to(REPO_ROOT)} phase={meta.get('phase')!r}")
        if meta.get("gate") != expected_gate:
            invalid.append(
                f"{path.relative_to(REPO_ROOT)} gate={meta.get('gate')!r} "
                f"expected={expected_gate!r}"
            )
        if command != expected_command:
            invalid.append(
                f"{path.relative_to(REPO_ROOT)} command={command!r} "
                f"expected={expected_command!r}"
            )
        if meta.get("runtime_contract") != f".workflow/agent-cards/{phase}.json":
            invalid.append(
                f"{path.relative_to(REPO_ROOT)} "
                f"runtime_contract={meta.get('runtime_contract')!r}"
            )

    assert invalid == []


def test_workflow_phase_skill_contract_templates_exist_and_match_gates() -> None:
    invalid: list[str] = []
    for path in _skill_files():
        meta = _skill_frontmatter(path)
        name = meta.get("name", "")
        if not name.startswith("phase-"):
            continue

        phase = name.removeprefix("phase-")
        expected_gate = PHASE_GATE.get(phase)
        template = meta.get("contract_template", "")
        template_path = _resolve_repo_template_path(template)
        if not template_path.is_file():
            invalid.append(f"{path.relative_to(REPO_ROOT)} missing {template}")
            continue

        card = json.loads(template_path.read_text(encoding="utf-8"))
        if card.get("name") != name:
            invalid.append(
                f"{path.relative_to(REPO_ROOT)} card.name={card.get('name')!r}"
            )
        if ((card.get("gate") or {}).get("id")) != expected_gate:
            invalid.append(
                f"{path.relative_to(REPO_ROOT)} card.gate.id="
                f"{((card.get('gate') or {}).get('id'))!r} expected={expected_gate!r}"
            )

    assert invalid == []


def test_core_skill_command_templates_are_current() -> None:
    expected = {
        "analysis": "awf analyze {service} {unit}",
        "phase-approve": "awf wf next --phase approve",
        "phase-done": "awf wf next --phase done",
        "phase-impl": "awf wf next --phase impl",
        "phase-plan": "awf wf next --phase plan",
        "phase-review": "awf wf next --phase review",
        "phase-test": "awf wf next --phase test",
        "phase-verify": "awf wf next --phase verify",
        "wf-orchestrator": "awf wf next",
        "wf-reset": "awf wf reset",
        "wf-status": "awf wf status",
    }

    invalid: list[str] = []
    for name, command in expected.items():
        path = REPO_ROOT / "claude" / "skills" / name / "SKILL.md"
        actual = _skill_command_template(path)
        if actual != command:
            invalid.append(f"{path.relative_to(REPO_ROOT)} {actual!r} != {command!r}")

    assert invalid == []


def test_skill_docs_do_not_use_removed_wf_slash_aliases() -> None:
    stale: list[str] = []
    for path in _skill_files():
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            if STALE_WF_ALIAS_RE.search(line):
                stale.append(f"{path.relative_to(REPO_ROOT)}:{line_no} {line.strip()}")

    assert stale == []
