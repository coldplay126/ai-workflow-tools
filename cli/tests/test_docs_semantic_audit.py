from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path, PurePosixPath

from awf.cli import KNOWN_COMMANDS, build_parser
from awf.core.config import AwfConfig
from awf.core.state import PHASE_GATE, PHASE_ORDER


REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_README = REPO_ROOT / "cli" / "README.md"
AGENT_CARDS_DIR = (
    REPO_ROOT / "claude" / "skills" / "wf-orchestrator" / "templates" / "agent-cards"
)
SKILL_COMMAND_RE = re.compile(r"^\s+command:\s*[\"']?(.+?)[\"']?\s*$", re.MULTILINE)
TEMPLATE_ARG_RE = re.compile(r"\{[^}]+\}")
ANGLE_TEMPLATE_ARG_RE = re.compile(r"<[^>]+>")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)
TOP_LEVEL_SCALAR_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$")
STALE_WF_ALIAS_RE = re.compile(r"/wf(?:\.|\b(?!-))")
SHELL_FENCE_RE = re.compile(r"```(?:bash|sh|shell)\n(.*?)\n```", re.DOTALL)
KNOWN_GATE_IDS = {gate for gate in PHASE_GATE.values() if gate is not None}


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
    concrete = ANGLE_TEMPLATE_ARG_RE.sub("1", TEMPLATE_ARG_RE.sub("1", command))
    argv = shlex.split(concrete)
    if argv and argv[0] == "awf":
        argv = argv[1:]
    return argv


def _shell_fenced_awf_commands(text: str) -> tuple[str, ...]:
    commands: list[str] = []
    continued = ""
    for block in SHELL_FENCE_RE.findall(text):
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if continued:
                fragment = line.removesuffix("\\").strip()
                continued = f"{continued} {fragment}"
                if line.endswith("\\"):
                    continue
                commands.append(continued)
                continued = ""
                continue
            if not line.startswith("awf wt "):
                continue
            if line.endswith("\\"):
                continued = line.removesuffix("\\").strip()
                continue
            commands.append(line)
    if continued:
        commands.append(continued)
    return tuple(commands)


def _skill_files() -> tuple[Path, ...]:
    return tuple(sorted((REPO_ROOT / "claude" / "skills").glob("*/SKILL.md")))


def _agent_card_files() -> tuple[Path, ...]:
    return tuple(sorted(AGENT_CARDS_DIR.glob("*.json")))


def _load_agent_card(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


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


def _is_workflow_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and "\\" not in value
        and not path.is_absolute()
        and ".." not in path.parts
    )


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


def test_readmes_mention_builtin_providers() -> None:
    cli_readme = CLI_README.read_text(encoding="utf-8")
    root_readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    builtin_providers = sorted(
        key
        for key, value in AwfConfig.defaults().raw.get("provider", {}).items()
        if isinstance(value, dict) and key not in {"aliases"}
    )

    missing: list[str] = []
    for provider in builtin_providers:
        if f"`{provider}`" not in cli_readme and f"provider:{provider}" not in cli_readme:
            missing.append(f"cli/README.md provider={provider}")

    for display_name in ("Claude", "Codex", "Gemini", "OpenAI", "fixture"):
        if display_name not in root_readme:
            missing.append(f"README.md provider_display={display_name}")

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


def test_workflow_agent_cards_exist_for_every_phase() -> None:
    expected = sorted(f"{phase}.json" for phase in PHASE_ORDER)
    actual = [path.name for path in _agent_card_files()]
    assert actual == expected


def test_workflow_agent_card_identity_and_gate_ids_match_state_contracts() -> None:
    invalid: list[str] = []
    for path in _agent_card_files():
        phase = path.stem
        card = _load_agent_card(path)
        expected_gate = PHASE_GATE.get(phase)
        gate = card.get("gate") or {}

        if phase not in PHASE_ORDER:
            invalid.append(f"{path.relative_to(REPO_ROOT)} unknown phase={phase!r}")
        if card.get("name") != f"phase-{phase}":
            invalid.append(
                f"{path.relative_to(REPO_ROOT)} name={card.get('name')!r}"
            )
        if gate.get("id") != expected_gate:
            invalid.append(
                f"{path.relative_to(REPO_ROOT)} gate.id={gate.get('id')!r} "
                f"expected={expected_gate!r}"
            )
        pass_conditions = gate.get("pass_conditions")
        if not isinstance(pass_conditions, list) or not pass_conditions:
            invalid.append(
                f"{path.relative_to(REPO_ROOT)} missing non-empty pass_conditions"
            )

    assert invalid == []


def test_workflow_agent_card_pass_transitions_follow_phase_order() -> None:
    invalid: list[str] = []
    for path in _agent_card_files():
        phase = path.stem
        card = _load_agent_card(path)
        gate = card.get("gate") or {}
        expected_next = None
        if phase in PHASE_ORDER:
            phase_index = PHASE_ORDER.index(phase)
            if phase_index + 1 < len(PHASE_ORDER):
                expected_next = PHASE_ORDER[phase_index + 1]

        on_pass = gate.get("on_pass") or {}
        if on_pass.get("next_phase") != expected_next:
            invalid.append(
                f"{path.relative_to(REPO_ROOT)} on_pass.next_phase="
                f"{on_pass.get('next_phase')!r} expected={expected_next!r}"
            )

        on_fail = gate.get("on_fail") or {}
        for condition, action in on_fail.items():
            if not isinstance(action, dict):
                invalid.append(
                    f"{path.relative_to(REPO_ROOT)} on_fail.{condition} not object"
                )
                continue
            next_phase = action.get("next_phase")
            if next_phase is not None and next_phase not in PHASE_ORDER:
                invalid.append(
                    f"{path.relative_to(REPO_ROOT)} on_fail.{condition}.next_phase="
                    f"{next_phase!r}"
                )

    assert invalid == []


def test_workflow_agent_card_required_state_gates_are_known_predecessors() -> None:
    gate_to_phase = {
        gate: phase for phase, gate in PHASE_GATE.items() if gate is not None
    }
    invalid: list[str] = []
    for path in _agent_card_files():
        phase = path.stem
        card = _load_agent_card(path)
        required_state = (card.get("input") or {}).get("required_state") or {}
        gates = required_state.get("gates") or {}
        if not isinstance(gates, dict):
            invalid.append(
                f"{path.relative_to(REPO_ROOT)} required_state.gates not object"
            )
            continue
        if phase not in PHASE_ORDER:
            invalid.append(f"{path.relative_to(REPO_ROOT)} unknown phase={phase!r}")
            continue

        phase_index = PHASE_ORDER.index(phase)
        for gate_id in gates:
            if gate_id not in KNOWN_GATE_IDS:
                invalid.append(
                    f"{path.relative_to(REPO_ROOT)} unknown required gate={gate_id!r}"
                )
                continue
            gate_phase = gate_to_phase[gate_id]
            if PHASE_ORDER.index(gate_phase) >= phase_index:
                invalid.append(
                    f"{path.relative_to(REPO_ROOT)} required gate={gate_id!r} "
                    f"does not precede phase={phase!r}"
                )

    assert invalid == []


def test_workflow_agent_card_agent_definitions_exist() -> None:
    invalid: list[str] = []
    for path in _agent_card_files():
        card = _load_agent_card(path)
        agent = card.get("agent")
        if agent is None:
            continue
        if not isinstance(agent, dict):
            invalid.append(f"{path.relative_to(REPO_ROOT)} agent not object")
            continue
        definition = agent.get("definition")
        if not isinstance(definition, str) or not definition:
            invalid.append(f"{path.relative_to(REPO_ROOT)} missing agent.definition")
            continue
        if not (REPO_ROOT / definition).is_file():
            invalid.append(
                f"{path.relative_to(REPO_ROOT)} missing agent.definition={definition!r}"
            )

    assert invalid == []


def test_workflow_agent_card_required_and_output_paths_are_workflow_relative() -> None:
    invalid: list[str] = []
    for path in _agent_card_files():
        card = _load_agent_card(path)
        sections = (
            (
                "input.required_artifacts",
                (card.get("input") or {}).get("required_artifacts"),
            ),
            ("output.artifacts", (card.get("output") or {}).get("artifacts")),
        )
        for section, artifacts in sections:
            if not isinstance(artifacts, list) or not artifacts:
                invalid.append(f"{path.relative_to(REPO_ROOT)} {section} missing list")
                continue
            for artifact in artifacts:
                if not isinstance(artifact, dict):
                    invalid.append(
                        f"{path.relative_to(REPO_ROOT)} {section} entry not object"
                    )
                    continue
                artifact_path = artifact.get("path")
                if not isinstance(artifact_path, str) or not _is_workflow_relative_path(
                    artifact_path
                ):
                    invalid.append(
                        f"{path.relative_to(REPO_ROOT)} {section} "
                        f"{artifact.get('key', '<unknown>')}.path={artifact_path!r}"
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


def test_only_umbrella_skill_uses_wf_slash_aliases() -> None:
    stale: list[str] = []
    for path in _skill_files():
        if path.parent.name == "wf":
            continue
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            if STALE_WF_ALIAS_RE.search(line):
                stale.append(f"{path.relative_to(REPO_ROOT)}:{line_no} {line.strip()}")

    assert stale == []


def test_release_worktree_lifecycle_skill_encodes_operator_safety() -> None:
    path = REPO_ROOT / "claude" / "skills" / "release-worktree-lifecycle" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    contracts = [
        json.loads(match.group(1))
        for match in re.finditer(r"```json\n(.*?)\n```", text, re.DOTALL)
    ]
    contract = next(
        item
        for item in contracts
        if item.get("schema") == "awf.release-worktree-lifecycle/v1"
    )

    commands = contract["commands"]
    expected_commands = {
        "status": ("wt", "status"),
        "doctor": ("wt", "doctor"),
        "acquire_preview": ("wt", "acquire"),
        "acquire_apply": ("wt", "acquire"),
        "promote_preview": ("wt", "promote"),
        "promote_apply": ("wt", "promote"),
        "finish_preview": ("wt", "finish"),
        "finish_apply": ("wt", "finish"),
        "gc_preview": ("wt", "gc"),
        "gc_apply": ("wt", "gc"),
    }
    parser = build_parser()
    for name, expected in expected_commands.items():
        argv = _argv_from_skill_command(commands[name])
        parsed = parser.parse_args(argv)
        assert (parsed.command, parsed.wt_command) == expected

    assert "--refresh" in commands["status"]
    assert "--json" in commands["status"]
    assert "--apply" not in commands["acquire_preview"]
    assert "--apply" in commands["acquire_apply"]
    assert "--apply" not in commands["promote_preview"]
    assert "--apply" in commands["promote_apply"]
    assert "--apply" not in commands["finish_preview"]
    assert "--apply" in commands["finish_apply"]
    assert "--merged" in commands["gc_preview"]
    assert "--older-than 7d" in commands["gc_preview"]
    assert "--apply" not in commands["gc_preview"]
    assert "--apply" in commands["gc_apply"]

    safety = contract["safety"]
    assert safety["preflight"] == "required_non_destructive_status_refresh"
    assert safety["lease_reuse"] == "exact"
    assert safety["promotion_scope"] == "source_pr_delta_only"
    assert safety["deployment_health"] == "repository_rollout_evidence"
    assert safety["blocked_action"] == "preserve_worktree_report_code_message"
    assert safety["preview_before_apply"] == [
        "acquire",
        "promote",
        "finish",
        "gc",
    ]
    assert safety["stop_conditions"] == [
        "deployment_health_unknown",
        "closed_unmerged",
        "dirty_worktree",
    ]
    assert set(safety["forbidden_fallbacks"]) == {
        "direct_worktree_mutation",
        "staging_wholesale_merge",
        "branch_merged_heuristic",
        "stash",
        "reset",
        "force_delete",
        "unmanaged_deletion",
    }
    assert contract["decisions"] == {
        "reuse": "use_exact_lease",
        "preview": {
            "acquire": "review_then_apply_explicitly",
            "promote": "review_then_apply_explicitly",
            "finish": "review_blockers_then_apply",
            "gc": "review_blockers_then_apply",
        },
        "ready": {
            "status": "inspect_select_lifecycle_action",
            "acquire_apply": "use_or_report_returned_lease",
            "promote_apply": "use_or_report_returned_lease",
        },
        "removed": "report_completion",
        "blocked": "preserve_worktree_report_code_message",
    }


def test_release_worktree_lifecycle_shell_examples_match_contract() -> None:
    path = REPO_ROOT / "claude" / "skills" / "release-worktree-lifecycle" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    contract = next(
        json.loads(match.group(1))
        for match in re.finditer(r"```json\n(.*?)\n```", text, re.DOTALL)
        if json.loads(match.group(1)).get("schema")
        == "awf.release-worktree-lifecycle/v1"
    )
    commands = contract["commands"]
    displayed_commands = _shell_fenced_awf_commands(text)

    assert displayed_commands[0] == commands["status"]
    assert set(displayed_commands) == set(commands.values())

    parser = build_parser()
    for command in displayed_commands:
        parsed = parser.parse_args(_argv_from_skill_command(command))
        assert parsed.command == "wt"
