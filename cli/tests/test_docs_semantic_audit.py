from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path, PurePosixPath

import pytest

from awf.cli import KNOWN_COMMANDS, build_parser
from awf.core.config import AwfConfig
from awf.core.db_validation import load_database_decision
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
FENCED_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)\n```", re.DOTALL)
DISPLAYED_USAGE_SYNTAX_RE = re.compile(r"(?:^|\s)\[[^\]]+\](?=\s|$)")
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


def _argv_from_displayed_command(command: str) -> list[str]:
    concrete = ANGLE_TEMPLATE_ARG_RE.sub("1", TEMPLATE_ARG_RE.sub("1", command))
    argv = shlex.split(concrete, comments=True)
    assert argv and argv[0] == "awf"
    return argv[1:]


def _is_displayed_awf_command(line: str) -> bool:
    return line.startswith("awf ") and not DISPLAYED_USAGE_SYNTAX_RE.search(line)


def _has_shell_continuation(raw_line: str) -> bool:
    if not raw_line.endswith("\\"):
        return False
    trailing_backslashes = len(raw_line) - len(raw_line.rstrip("\\"))
    return trailing_backslashes % 2 == 1


def _shell_fenced_awf_commands(text: str) -> tuple[str, ...]:
    commands: list[str] = []
    continued = ""
    for block in FENCED_BLOCK_RE.findall(text):
        for raw_line in block.splitlines():
            line = raw_line.strip()
            continues = _has_shell_continuation(raw_line)
            if continued:
                fragment = line.removesuffix("\\").strip() if continues else line
                continued = f"{continued} {fragment}"
                if continues:
                    continue
                commands.append(continued)
                continued = ""
                continue
            if not _is_displayed_awf_command(line):
                continue
            if continues:
                continued = line.removesuffix("\\").strip()
                continue
            commands.append(line)
    if continued:
        commands.append(continued)
    return tuple(commands)


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _release_worktree_lifecycle_contract(text: str) -> dict[str, object]:
    contracts = tuple(
        json.loads(
            match.group(1),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        for match in re.finditer(r"```json\n(.*?)\n```", text, re.DOTALL)
    )
    matching = tuple(
        contract
        for contract in contracts
        if contract.get("schema") == "awf.release-worktree-lifecycle/v1"
    )
    assert len(matching) == 1
    return matching[0]


def _out_of_order_promotion_section(text: str) -> str:
    matches = tuple(
        re.finditer(
            r"^#{2,4} Out-of-order production promotion\s*$",
            text,
            re.MULTILINE,
        )
    )
    assert len(matches) == 1
    heading_level = len(matches[0].group()) - len(matches[0].group().lstrip("#"))
    start = matches[0].end()
    in_fence = False
    end = len(text)
    offset = start
    for line in text[start:].splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and re.match(rf"^#{{1,{heading_level}}} ", line):
            end = offset
            break
        offset += len(line)
    return text[start:end]


def test_shell_fenced_command_extractor_includes_non_worktree_awf_commands() -> None:
    text = """```bash
awf ready --repo-root . --json
awf wf status --repo-root .
```"""

    assert _shell_fenced_awf_commands(text) == (
        "awf ready --repo-root . --json",
        "awf wf status --repo-root .",
    )


def test_shell_fenced_command_extractor_includes_unlabelled_and_text_fence_commands() -> None:
    expected = (
        (
            REPO_ROOT / "claude" / "skills" / "analysis" / "SKILL.md",
            "awf analyze {service} --check        # drift detection",
        ),
        (
            REPO_ROOT / "claude" / "skills" / "wf" / "SKILL.md",
            'awf wf init "<concept>" --repo-root .',
        ),
    )

    for path, command in expected:
        assert command in _shell_fenced_awf_commands(path.read_text(encoding="utf-8"))


def test_shell_fenced_command_extractor_excludes_usage_syntax() -> None:
    path = REPO_ROOT / "claude" / "skills" / "analysis" / "SKILL.md"

    assert (
        "awf analyze {service} {unit} [--mode cross] [--all]"
        not in _shell_fenced_awf_commands(path.read_text(encoding="utf-8"))
    )


def test_shell_fenced_command_extractor_joins_only_valid_continuations() -> None:
    backslash = "\\"
    text = f"""```bash
awf ready --repo-root . {backslash} 
--json
awf wf status --repo-root . {backslash}{backslash}
--json
awf scan . {backslash}
--no-ai
```"""

    assert _shell_fenced_awf_commands(text) == (
        f"awf ready --repo-root . {backslash}",
        f"awf wf status --repo-root . {backslash}{backslash}",
        "awf scan . --no-ai",
    )


def _raw_contiguous_command_slice(
    displayed_commands: tuple[str, ...],
    expected_commands: tuple[str, ...],
) -> tuple[str, ...]:
    command_count = len(expected_commands)
    for start in range(len(displayed_commands) - command_count + 1):
        candidate = displayed_commands[start : start + command_count]
        if candidate == expected_commands:
            return candidate
    return ()


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


def test_root_parser_exposes_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["--version"])

    assert raised.value.code == 0
    assert capsys.readouterr().out == "awf 0.1.6\n"


def test_analysis_generation_integrity_docs_share_contract() -> None:
    paths = (
        REPO_ROOT / "cli" / "README.md",
        REPO_ROOT / "docs" / "reference" / "analysis-pipeline.md",
        REPO_ROOT / "docs" / "patterns" / "analysis-pipeline" / "02-stages.md",
        REPO_ROOT / "docs" / "patterns" / "analysis-pipeline" / "03-resume-optimization.md",
        REPO_ROOT / "docs" / "specs" / "ai-context-specification.md",
        REPO_ROOT / "claude" / "skills" / "analysis" / "reference.md",
    )
    obsolete = "status가 `\"failed\"`인 단계의 artifact → 삭제"
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert obsolete not in text
        assert "Stage 3" in text

    stages = paths[2].read_text(encoding="utf-8")
    for invariant in (
        "현재 attempt가 mode의 모든 required output을 공급",
        "같은 source/config generation에서만 재사용",
        "진단 artifact와 실패 상태를 보존",
        "새 source/config generation은 해당 retry budget을 reset",
    ):
        assert invariant in stages


def test_multi_agent_runtime_docs_share_contract() -> None:
    paths = (
        REPO_ROOT / "cli" / "README.md",
        REPO_ROOT / "docs" / "reference" / "multi-agent.md",
        REPO_ROOT / "docs" / "patterns" / "multi-agent" / "02-judge-rules.md",
        REPO_ROOT / "docs" / "patterns" / "multi-agent" / "03-provider-routing.md",
    )
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    for invariant in (
        "normalized `PASS`/`FAIL` prefix",
        "provider `returncode == 124`",
        "`dict`만 구조화 결과",
    ):
        assert invariant in combined


def test_analysis_runtime_recovery_docs_share_contract() -> None:
    paths = (
        REPO_ROOT / "cli" / "README.md",
        REPO_ROOT / "docs" / "reference" / "analysis-pipeline.md",
        REPO_ROOT / "docs" / "patterns" / "analysis-pipeline" / "02-stages.md",
        REPO_ROOT / "docs" / "reference" / "multi-agent.md",
    )
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    for invariant in (
        "fanout_unavailable:",
        ".analysis-run.lock",
        "exit code `130`",
        "read-only",
        "fanout-consistency",
        "`original_claims`",
        "문서 전체",
        "실제 재실행 Writer 결과",
    ):
        assert invariant in combined


def test_analysis_policy_consistency_docs_share_contract() -> None:
    paths = (
        REPO_ROOT / "README.md",
        REPO_ROOT / "cli" / "README.md",
        REPO_ROOT / "docs" / "reference" / "analysis-pipeline.md",
        REPO_ROOT / "docs" / "patterns" / "analysis-pipeline" / "03-resume-optimization.md",
        REPO_ROOT / "docs" / "reference" / "multi-agent.md",
        REPO_ROOT / "docs" / "patterns" / "multi-agent" / "03-provider-routing.md",
    )
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    for invariant in (
        "final output이 `completed`",
        "stdout에 JSON envelope 하나",
        "`cross → precise`",
        "`locations` 배열은 정렬",
        "fanout factory",
        "prompt는 stdin",
    ):
        assert invariant in combined


def test_release_metadata_versions_match() -> None:
    assert (REPO_ROOT / "cli" / "pyproject.toml").read_text(encoding="utf-8").count(
        'version = "0.1.6"'
    ) == 1
    assert (REPO_ROOT / "cli" / "src" / "awf" / "__init__.py").read_text(
        encoding="utf-8"
    ).count('__version__ = "0.1.6"') == 1
    assert (REPO_ROOT / "cli" / "uv.lock").read_text(encoding="utf-8").count(
        'version = "0.1.6"'
    ) >= 1
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.1.6] - 2026-08-13" in changelog


def test_multi_agent_snippet_requires_live_cmux_roster() -> None:
    text = (REPO_ROOT / "snippets" / "claude-md-multi-agent.md").read_text(
        encoding="utf-8"
    )

    assert "cmux-agent agents --json" in text
    assert "jq -e '.agents | length > 0'" in text
    assert "존재만으로는 활성으로 판정하지 않음" in text


def test_default_omp_role_models_preserve_cross_provider_intent() -> None:
    config = json.loads(
        (
            REPO_ROOT
            / "claude"
            / "skills"
            / "wf-orchestrator"
            / "templates"
            / "provider-config.default.json"
        ).read_text(encoding="utf-8")
    )

    role_models = config["dispatch"]["omp"]["role_models"]
    assert role_models["plan_conformance"] == "@default"
    assert role_models["quality_validation"] == "@slow"
    assert role_models["precision"] == "@default"
    assert role_models["primary"] == "@slow"


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


def test_all_displayed_skill_awf_commands_parse_with_current_cli() -> None:
    parser = build_parser()
    invalid: list[str] = []
    for path in _skill_files():
        text = path.read_text(encoding="utf-8")
        for command in _shell_fenced_awf_commands(text):
            try:
                parser.parse_args(_argv_from_displayed_command(command))
            except (AssertionError, SystemExit, ValueError) as exc:
                invalid.append(
                    f"{path.relative_to(REPO_ROOT)}: {command!r} "
                    f"({type(exc).__name__}: {exc})"
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
    contract = _release_worktree_lifecycle_contract(text)

    commands = contract["commands"]
    expected_commands = {
        "status": ("wt", "status"),
        "doctor": ("wt", "doctor"),
        "import_preview": ("wt", "import"),
        "import_apply": ("wt", "import"),
        "adopt_preview": ("wt", "adopt"),
        "adopt_apply": ("wt", "adopt"),
        "acquire_preview": ("wt", "acquire"),
        "acquire_apply": ("wt", "acquire"),
        "link_pr_preview": ("wt", "link-pr"),
        "link_pr_apply": ("wt", "link-pr"),
        "promote_preview": ("wt", "promote"),
        "promote_apply": ("wt", "promote"),
        "out_of_order_promote_preview": ("wt", "promote"),
        "out_of_order_promote_apply": ("wt", "promote"),
        "out_of_order_resolution_preview": ("wt", "promote"),
        "out_of_order_resolution_apply": ("wt", "promote"),
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
    assert "--dry-run" in commands["import_preview"]
    assert "--apply" not in commands["import_preview"]
    assert "--apply" in commands["import_apply"]
    assert "--pr" in commands["adopt_preview"]
    assert "--apply" not in commands["adopt_preview"]
    assert "--pr" in commands["adopt_apply"]
    assert "--apply" in commands["adopt_apply"]
    assert "--apply" not in commands["acquire_preview"]
    assert "--apply" in commands["acquire_apply"]
    assert "--lease" in commands["link_pr_preview"]
    assert "--pr" in commands["link_pr_preview"]
    assert "--apply" not in commands["link_pr_preview"]
    assert "--lease" in commands["link_pr_apply"]
    assert "--pr" in commands["link_pr_apply"]
    assert "--apply" in commands["link_pr_apply"]
    assert "--apply" not in commands["promote_preview"]
    assert "--apply" in commands["promote_apply"]

    assert "--out-of-order" in commands["out_of_order_promote_preview"]
    assert "--apply" not in commands["out_of_order_promote_preview"]
    assert "--out-of-order" in commands["out_of_order_promote_apply"]
    assert "--apply" in commands["out_of_order_promote_apply"]
    assert (
        commands["out_of_order_resolution_preview"]
        == commands["out_of_order_promote_preview"]
    )
    assert (
        commands["out_of_order_resolution_apply"]
        == commands["out_of_order_promote_apply"]
    )
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
    assert safety["out_of_order"] == {
        "mode": "explicit_opt_in",
        "exact_mode": "default",
        "single_source": True,
        "exclude_paths": "forbidden",
        "production_pr_review": "required",
        "production_pr_checks": "required",
        "direct_cherry_pick": "forbidden",
        "staging_squash_input": "forbidden",
        "conflict_resolution": "managed_conflicted_files_only_replay_same_command",
        "dependency_conflict": "blocked",
        "rename": "unsupported",
        "initial_preview_fields": [
            "source_base_sha",
            "source_head_sha",
            "target_base_sha",
            "reviewed_paths",
        ],
        "resolution_preview_actions": [
            "resolve_out_of_order_conflict",
            "stage_paths",
            "commit",
            "verify_production",
            "push_branch",
            "open_pull_request",
        ],
        "operator_edit_scope": "unstaged_unmerged_subset_of_conflicted_paths",
        "final_indexed_delta": "non_empty_reviewed_paths_subset",
        "conflict_marker_policy": "markers_only_trailing_whitespace_allowed",
        "live_target_recheck": "after_verification_before_publish",
        "protected_index_entries": {
            "paths": "clean_applied_reviewed_paths_outside_conflicted_paths",
            "entry": "stage_zero_mode_blob_oid_or_null",
            "pin": "exact_preview_apply_retry",
            "tamper": "promotion_resolution_scope_mismatch",
        },
        "blocker_codes": [
            "invalid_out_of_order_promotion",
            "unsupported_out_of_order_rename",
            "out_of_order_conflict",
            "promotion_provenance_changed",
            "promotion_resolution_scope_mismatch",
            "promotion_resolution_unmerged",
        ],
    }
    assert safety["managed_feature_pr_link"] == {
        "lease_state": "active_unlinked_or_cleanable_exact_reuse",
        "pr_provenance": (
            "already_merged_exact_repository_branch_and_current_worktree_head"
        ),
        "apply_transition": "replace_recorded_head_then_cleanable_not_required",
        "same_pr": "reuse",
        "different_pr": "blocked",
        "github_external_failure": "exit_4",
    }
    assert safety["imported_pr_lifecycle"] == {
        "pr_provenance": "already_merged_exact_branch_and_head",
        "same_pr": "reuse",
        "different_pr": "blocked",
        "github_external_failure": "exit_4",
        "runtime_source_before_removal": (
            "install_cli_and_skill_from_stable_merged_main_and_verify_links"
        ),
    }
    assert safety["preview_before_apply"] == [
        "acquire",
        "link-pr",
        "promote",
        "out_of_order_promote",
        "out_of_order_resolution",
        "import",
        "adopt",
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
        "direct_cherry_pick",
        "stash",
        "reset",
        "force_delete",
        "unmanaged_deletion",
    }
    assert contract["decisions"] == {
        "reuse": "use_exact_lease",
        "preview": {
            "acquire": "review_then_apply_explicitly",
            "link_pr": "review_then_apply_explicitly",
            "promote": "review_then_apply_explicitly",
            "out_of_order_promote": "review_then_apply_explicitly",
            "out_of_order_resolution": "review_same_blocked_lease_then_apply_explicitly",
            "finish": "review_blockers_then_apply",
            "gc": "review_blockers_then_apply",
        },
        "ready": {
            "status": "inspect_select_lifecycle_action",
            "acquire_apply": "use_or_report_returned_lease",
            "link_pr_apply": "restart_status_preflight_then_finish",
            "promote_apply": "use_or_report_returned_lease",
            "out_of_order_promote_apply": "use_or_report_returned_lease",
            "out_of_order_resolution_apply": "use_or_report_returned_lease",
        },
        "removed": "report_completion",
        "blocked": "preserve_worktree_report_code_message",
    }


def test_release_worktree_lifecycle_contract_rejects_duplicate_json_keys() -> None:
    text = """```json
{"schema": "awf.release-worktree-lifecycle/v1", "schema": "duplicate"}
```"""

    with pytest.raises(ValueError, match="duplicate key: schema"):
        _release_worktree_lifecycle_contract(text)


def test_managed_feature_pr_link_docs_share_ordered_safety_contract() -> None:
    paths = (
        REPO_ROOT / "claude" / "skills" / "release-worktree-lifecycle" / "SKILL.md",
        CLI_README,
    )
    expected_commands = (
        "awf wt link-pr --lease <id> --pr <merged-pr> --json",
        "awf wt link-pr --lease <id> --pr <merged-pr> --apply --json",
        "awf wt status --repo-root <repo-root> --refresh --json",
        "awf wt finish --repo-root <repo-root> --pr <merged-pr> --json",
        "awf wt finish --repo-root <repo-root> --pr <merged-pr> --apply --json",
    )
    expected_argv = tuple(
        _argv_from_skill_command(command) for command in expected_commands
    )

    for path in paths:
        text = path.read_text(encoding="utf-8")
        displayed_commands = _shell_fenced_awf_commands(text)
        raw_slice = _raw_contiguous_command_slice(
            displayed_commands, expected_commands
        )
        assert raw_slice == expected_commands
        normalized_argv = tuple(
            _argv_from_skill_command(command) for command in raw_slice
        )
        assert normalized_argv == expected_argv
        parser = build_parser()
        for argv in normalized_argv:
            parsed = parser.parse_args(argv)
            assert parsed.command == "wt"

        prose = " ".join(text.lower().split())
        assert "current registered/check-out worktree head" in prose
        assert "recorded acquisition sha may be older" in prose
        assert "github failure is exit code `4`" in prose or (
            "github external failure is exit code `4`" in prose
        )


def test_canonical_imported_pr_cleanup_docs_share_ordered_safety_contract() -> None:
    paths = (
        REPO_ROOT / "claude" / "skills" / "release-worktree-lifecycle" / "SKILL.md",
        CLI_README,
    )
    expected_commands = (
        "awf wt import --root <root> --dry-run --json",
        "awf wt import --root <root> --apply --json",
        "awf wt adopt --lease <id> --pr <merged-pr> --json",
        "awf wt adopt --lease <id> --pr <merged-pr> --apply --json",
        "awf wt status --repo-root <repo-root> --refresh --json",
        "awf wt finish --repo-root <repo-root> --pr <merged-pr> --json",
        "awf wt finish --repo-root <repo-root> --pr <merged-pr> --apply --json",
    )
    expected_argv = tuple(_argv_from_skill_command(command) for command in expected_commands)


    required_prose = (
        "parent directory whose direct-child repositories and worktrees are inventoried",
        "must not infer a pr automatically",
        "must not use direct git or filesystem cleanup",
        "before removing a source worktree backing installed cli or skill links, "
        "must install the cli and skill from a stable merged-main checkout",
        "verify that installed `awf` and every skill link no longer resolve to "
        "the source worktree",
        "accepts only an already-merged pr whose number, branch, and head sha "
        "exactly match the imported lease",
        "same linked pr returns `reuse`",
        "different pr, a dirty worktree, or any git/pr branch or head mismatch "
        "is `blocked`",
        "a github external failure is exit code `4`",
        "import preserves the local and remote branch",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        displayed_commands = _shell_fenced_awf_commands(text)
        raw_slice = _raw_contiguous_command_slice(
            displayed_commands, expected_commands
        )
        assert raw_slice == expected_commands

        normalized_argv = tuple(
            _argv_from_skill_command(command) for command in raw_slice
        )
        assert normalized_argv == expected_argv
        parser = build_parser()
        for argv in normalized_argv:
            parsed = parser.parse_args(argv)
            assert parsed.command == "wt"

        prose = " ".join(text.lower().split())
        for requirement in required_prose:
            assert requirement in prose


def test_imported_pr_cleanup_raw_sequence_rejects_placeholder_role_swaps() -> None:
    expected_commands = (
        "awf wt import --root <root> --dry-run --json",
        "awf wt import --root <root> --apply --json",
        "awf wt adopt --lease <id> --pr <merged-pr> --json",
        "awf wt adopt --lease <id> --pr <merged-pr> --apply --json",
        "awf wt status --repo-root <repo-root> --refresh --json",
        "awf wt finish --repo-root <repo-root> --pr <merged-pr> --json",
        "awf wt finish --repo-root <repo-root> --pr <merged-pr> --apply --json",
    )
    role_swapped_commands = tuple(
        command.replace("<root>", "<temporary>")
        .replace("<id>", "<root>")
        .replace("<temporary>", "<id>")
        for command in expected_commands
    )

    assert tuple(
        _argv_from_skill_command(command) for command in role_swapped_commands
    ) == tuple(_argv_from_skill_command(command) for command in expected_commands)
    with pytest.raises(AssertionError):
        assert (
            _raw_contiguous_command_slice(
                role_swapped_commands, expected_commands
            )
            == expected_commands
        )


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


def test_out_of_order_promotion_docs_share_operator_contract() -> None:
    paths = (
        REPO_ROOT / "claude" / "skills" / "release-worktree-lifecycle" / "SKILL.md",
        CLI_README,
    )
    expected_commands = (
        "awf wt promote --source-pr <number> --to <branch> --out-of-order --repo-root <repo-root> --json",
        "awf wt promote --source-pr <number> --to <branch> --out-of-order --repo-root <repo-root> --apply --json",
        "awf wt promote --source-pr <number> --to <branch> --out-of-order --repo-root <repo-root> --json",
        "awf wt promote --source-pr <number> --to <branch> --out-of-order --repo-root <repo-root> --apply --json",
    )
    expected_apply = (False, True, False, True)
    required_prose = (
        "a code may ship but must remain inactive",
        "a code must stay out of production; b applies cleanly",
        "a code must stay out; b has a mechanical patch conflict",
        "b requires a's api, schema, or behavior",
        "exactly one `--source-pr`",
        "must not use `--exclude-path`",
        "only the conflicted files returned by awf",
        "same preview command",
        "same command with `--apply`",
        "approval and successful checks on that exact production pr before merge",
        "staging squash commits are not production promotion inputs",
        "direct staging squash cherry-pick",
        "any direct cherry-pick is forbidden",
        "reviewed pr deltas only through `awf wt promote`",
        "all conflict markers must be removed before apply",
        "does not publish and preserves the worktree",
        "`invalid_out_of_order_promotion`",
        "`unsupported_out_of_order_rename`",
        "`out_of_order_conflict`",
        "`promotion_provenance_changed`",
        "`promotion_resolution_scope_mismatch`",
        "`promotion_resolution_unmerged`",
        "`source_base_sha`, `source_head_sha`, `target_base_sha`, and `reviewed_paths`",
        "action order is `resolve_out_of_order_conflict`, `stage_paths`, `commit`, `verify_production`, `push_branch`, then `open_pull_request`",
        "operator's unstaged edits and unmerged paths must be a subset of `conflicted_paths`",
        "awf clean-applied staged `protected_index_entries` may remain outside `conflicted_paths`",
        "their mode+oid pin is exact across preview, apply, and retry",
        "final indexed and committed paths must be a subset of `reviewed_paths`",
        "checks conflict markers only",
        "trailing whitespace is not prohibited",
        "rechecks the live target after verification before publish",
        "direct `git add` tampering or chmod/file-type mode tampering returns `promotion_resolution_scope_mismatch`",
    )
    forbidden_prose = (
        "every changed or unmerged path remains within the reported conflicted paths",
        "every changed or unmerged path is one of the reported conflicted paths",
    )

    parser = build_parser()
    for path in paths:
        section = _out_of_order_promotion_section(
            path.read_text(encoding="utf-8")
        )
        displayed_commands = _shell_fenced_awf_commands(section)
        assert displayed_commands == expected_commands
        for command, apply in zip(displayed_commands, expected_apply):
            parsed = parser.parse_args(_argv_from_displayed_command(command))
            assert (parsed.command, parsed.wt_command) == ("wt", "promote")
            assert parsed.source_pr == [1]
            assert parsed.out_of_order is True
            assert parsed.apply is apply
            assert parsed.exclude_path == []
            assert parsed.json is True

        prose = " ".join(section.lower().split())
        for requirement in required_prose:
            assert requirement in prose
        for stale_requirement in forbidden_prose:
            assert stale_requirement not in prose


def test_release_worktree_lifecycle_skill_copies_are_byte_identical() -> None:
    canonical = (
        REPO_ROOT / "claude" / "skills" / "release-worktree-lifecycle" / "SKILL.md"
    )
    packaged = (
        REPO_ROOT
        / "cli"
        / "src"
        / "awf"
        / "resources"
        / "release-worktree-lifecycle"
        / "SKILL.md"
    )

    assert canonical.read_bytes() == packaged.read_bytes()


def test_out_of_order_promotion_changelog_names_the_opt_in_safety_contract() -> None:
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8").lower()

    assert "기본 exact promotion은 변경하지 않았고" in changelog
    assert "`--out-of-order`" in changelog
    assert "`invalid_out_of_order_promotion`" in changelog
    assert "`unsupported_out_of_order_rename`" in changelog
    assert "`out_of_order_conflict`" in changelog
    assert "`promotion_provenance_changed`" in changelog
    assert "staging squash commit" in changelog


def test_protected_index_entry_docs_define_supported_stage_zero_modes() -> None:
    paths = (
        REPO_ROOT
        / "docs"
        / "superpowers"
        / "specs"
        / "2026-08-20-out-of-order-promotion-design.md",
        REPO_ROOT
        / "docs"
        / "superpowers"
        / "plans"
        / "2026-08-20-out-of-order-promotion.md",
    )
    required_prose = (
        "`100644` regular",
        "`100755` executable",
        "`120000` symlink",
        "`160000` gitlink",
        "invalid index modes fail closed",
        "symlink and gitlink entries are pinned",
    )

    for path in paths:
        prose = " ".join(path.read_text(encoding="utf-8").lower().split())
        for requirement in required_prose:
            assert requirement in prose


DATABASE_GATE_COMMANDS = {
    "plan": (
        "awf wf db-check --stage plan --repo-root . --json",
        "awf wf gate plan --repo-root . --json",
    ),
    "verify": (
        ': "${VERIFY_RESULT:?set from the result path emitted by awf wf next}"',
        "awf wf db-check --stage verify --repo-root . --json",
        'awf wf gate verify --repo-root . --result-file "$VERIFY_RESULT" --json',
    ),
    "test": (
        ': "${TEST_RESULT:?set from the result path emitted by awf wf next}"',
        "awf wf db-check --stage test --repo-root . --json",
        'awf wf gate test --repo-root . --result-file "$TEST_RESULT" --json',
    ),
}


def test_database_workflow_docs_define_evidence_and_safety_policy() -> None:
    parser = build_parser()
    phase_skills = {
        "plan": REPO_ROOT / "claude" / "skills" / "phase-plan" / "SKILL.md",
        "verify": REPO_ROOT / "claude" / "skills" / "phase-verify" / "SKILL.md",
        "test": REPO_ROOT / "claude" / "skills" / "phase-test" / "SKILL.md",
    }
    for stage, path in phase_skills.items():
        commands = DATABASE_GATE_COMMANDS[stage]
        assert f"```bash\n{chr(10).join(commands)}\n```" in path.read_text(
            encoding="utf-8"
        )
        result_variable = {
            "verify": "VERIFY_RESULT",
            "test": "TEST_RESULT",
        }.get(stage)
        result_path = (
            f".workflow/tmp/{stage}-result-from-next.json"
            if result_variable is not None
            else None
        )
        for command in commands:
            assert not any(marker in command for marker in ("<", ">", "|", ";"))
            if command.startswith(": "):
                assert command == (
                    f': "${{{result_variable}:?set from the result path emitted by awf wf next}}"'
                )
                continue
            expanded = (
                command.replace(f"${result_variable}", result_path)
                if result_variable is not None and result_path is not None
                else command
            )
            parsed = parser.parse_args(_argv_from_displayed_command(expanded))
            if result_path is not None and "gate" in command:
                assert parsed.result_file == result_path

    reference_path = REPO_ROOT / "docs" / "reference" / "workflow-pipeline.md"
    reference_block = "```bash\n" + "\n\n".join(
        "\n".join(DATABASE_GATE_COMMANDS[stage])
        for stage in ("plan", "verify", "test")
    ) + "\n```"
    assert reference_block in reference_path.read_text(encoding="utf-8")

    readme = CLI_README.read_text(encoding="utf-8")
    assert "result: /actual/result/path" in readme
    assert "`VERIFY_RESULT`" in readme
    assert "`TEST_RESULT`" in readme

    policy_paths = (
        reference_path,
        CLI_README,
        REPO_ROOT / "CHANGELOG.md",
    )
    required_policy = (
        "production schema",
        "same-engine local",
        "DuckDB",
        "project-specific replica",
        "raw primary rows",
        "waiver",
    )
    for path in policy_paths:
        prose = path.read_text(encoding="utf-8")
        for requirement in required_policy:
            assert requirement in prose, f"{path}: missing {requirement}"

    reference = policy_paths[0].read_text(encoding="utf-8")
    required_schema_fields = (
        '"schema_version": 1',
        '"kind": "production_schema"',
        '"target_class": "production_metadata"',
        '"read_only": true',
        '"schema_only": true',
        '"engine"',
        '"engine_version"',
        '"captured_at"',
        '"schema_hash"',
        '"object_counts"',
        '"tables"',
        '"columns"',
        '"indexes"',
        '"constraints"',
    )
    for field in required_schema_fields:
        assert field in reference, f"missing production-schema field: {field}"

    primary_policy_paths = (*phase_skills.values(), reference_path, CLI_README)
    primary_policy = (
        "Production primary is never a verify/test benchmark or executable-query target.",
        "read-only schema metadata",
        "explicitly approved replica",
        "warehouse",
        "sanitized local",
    )
    for path in primary_policy_paths:
        prose = path.read_text(encoding="utf-8")
        for requirement in primary_policy:
            assert requirement in prose, f"{path}: missing {requirement}"

    operator_waiver_contract = (
        "`local_data_test_waiver`",
        "null or omitted",
        "`reason`",
        "`approver`",
        "`timestamp`",
        "UTC ISO 8601",
    )
    for path in (reference_path, CLI_README):
        prose = path.read_text(encoding="utf-8")
        for requirement in operator_waiver_contract:
            assert requirement in prose, f"{path}: missing {requirement}"


def test_database_planning_contract_compares_options_without_index_default() -> None:
    plan_skill = (
        REPO_ROOT / "claude" / "skills" / "phase-plan" / "SKILL.md"
    ).read_text(encoding="utf-8")
    required_contract = (
        "정확히 2개 또는 3개",
        "`maintain` baseline",
        "equivalence_plan",
        "integrity_plan",
        "`query`",
        "`index`",
        "`column`",
        "`constraint`",
        "`erd`",
        "`normalize`",
        "`denormalize`",
        "hard gate",
        "Every candidate covers every decision surface.",
    )
    for requirement in required_contract:
        assert requirement in plan_skill, f"missing planning requirement: {requirement}"

    candidate_fields = (
        "`id`",
        "`kind`",
        "`applicable`",
        "`unavailable_reason`",
        "`summary`",
        "`equivalence_plan`",
        "`integrity_plan`",
        "`normalization_assessment`",
        "`denormalization_assessment`",
        "`physical_design_assessment`",
        "`covered_surfaces`",
        "`surface_assessments`",
        "`read_write_cost`",
        "`operational_risks`",
        "`transition_risks`",
        "`rollback_or_exit`",
        "`source_of_truth`",
        "`consistency_window`",
        "`reconciliation`",
        "`read_benefit`",
        "`write_amplification`",
        "`storage`",
        "`build_or_lock`",
        "`rollback`",
    )
    contract_paths = (
        REPO_ROOT / "claude" / "skills" / "phase-plan" / "SKILL.md",
        REPO_ROOT / "claude" / "agents" / "spec-writer.md",
        REPO_ROOT / "claude" / "skills" / "multi-agent" / "protocols" / "spec_writer.md",
    )
    for path in contract_paths:
        prose = path.read_text(encoding="utf-8")
        for field in candidate_fields:
            assert field in prose, f"{path}: missing candidate field {field}"
        assert "Every candidate covers every decision surface." in prose

    writer = (REPO_ROOT / "claude" / "agents" / "spec-writer.md").read_text(
        encoding="utf-8"
    )
    assert "tools: Read, Grep, Glob, Edit, Write, Bash, AskUserQuestion" in writer
    assert "material" in writer

    waiver_contract = (
        "`local_data_test_waiver`",
        "null or omitted",
        "`reason`",
        "`approver`",
        "`timestamp`",
        "UTC ISO 8601",
    )
    for path in contract_paths:
        prose = path.read_text(encoding="utf-8")
        for field in waiver_contract:
            assert field in prose, f"{path}: missing waiver field {field}"

    test_skill = (
        REPO_ROOT / "claude" / "skills" / "phase-test" / "SKILL.md"
    ).read_text(encoding="utf-8")
    for field in waiver_contract:
        assert field in test_skill, f"phase-test: missing waiver field {field}"



def test_documented_local_data_waiver_loads_through_canonical_decision_parser(
    tmp_path: Path,
) -> None:
    candidates = [
        {
            "id": "maintain-current",
            "kind": "maintain",
            "applicable": True,
            "unavailable_reason": None,
            "summary": "Keep the current query and schema.",
            "equivalence_plan": "Use the current result set as a baseline.",
            "integrity_plan": "Verify current constraints.",
            "normalization_assessment": "No model change.",
            "denormalization_assessment": None,
            "physical_design_assessment": None,
            "covered_surfaces": ["query"],
            "surface_assessments": {"query": "Maintain the current query."},
            "read_write_cost": "Measure the production-shaped workload.",
            "operational_risks": [],
            "transition_risks": [],
            "rollback_or_exit": "No change.",
        },
        {
            "id": "rewrite-query",
            "kind": "query_change",
            "applicable": True,
            "unavailable_reason": None,
            "summary": "Rewrite the aggregation query.",
            "equivalence_plan": "Compare result sets with the baseline.",
            "integrity_plan": "Verify constraints before and after the query.",
            "normalization_assessment": "No model change.",
            "denormalization_assessment": None,
            "physical_design_assessment": None,
            "covered_surfaces": ["query"],
            "surface_assessments": {"query": "Rewrite the aggregation query."},
            "read_write_cost": "Measure latency on production-shaped data.",
            "operational_risks": [],
            "transition_risks": [],
            "rollback_or_exit": "Restore the current query.",
        },
    ]
    payload = {
        "schema_version": 1,
        "status": "selected",
        "change_surfaces": ["query"],
        "baseline_option_id": "maintain-current",
        "recommended_option_id": "rewrite-query",
        "selected_option_id": "rewrite-query",
        "candidates": candidates,
        "recommendation_rationale": "The rewrite preserves correctness at lower cost.",
        "local_data_test_waiver": {
            "reason": "No approved masked fixture is available.",
            "approver": "database-owner",
            "timestamp": "2026-08-24T00:00:00Z",
        },
    }
    decision_path = tmp_path / ".workflow" / "artifacts" / "database-decision.json"
    decision_path.parent.mkdir(parents=True)
    decision_path.write_text(json.dumps(payload), encoding="utf-8")

    decision = load_database_decision(tmp_path)

    assert decision.local_data_test_waiver == payload["local_data_test_waiver"]


def test_index_surface_compares_holistic_options_without_forcing_physical_kind(
    tmp_path: Path,
) -> None:
    baseline = {
        "id": "maintain-current",
        "kind": "maintain",
        "applicable": True,
        "unavailable_reason": None,
        "summary": "Keep the current query and schema.",
        "equivalence_plan": "Use the current result set as a baseline.",
        "integrity_plan": "Verify current constraints.",
        "normalization_assessment": "No model change.",
        "denormalization_assessment": None,
        "physical_design_assessment": None,
        "covered_surfaces": ["index"],
        "surface_assessments": {"index": "Maintain the current index design."},
        "read_write_cost": "Measure the production-shaped workload.",
        "operational_risks": [],
        "transition_risks": [],
        "rollback_or_exit": "No change.",
    }
    query = {
        "id": "rewrite-query",
        "kind": "query_change",
        "applicable": True,
        "unavailable_reason": None,
        "summary": "Rewrite the aggregation query.",
        "equivalence_plan": "Compare result sets with the baseline.",
        "integrity_plan": "Verify constraints before and after the query.",
        "normalization_assessment": "No model change.",
        "denormalization_assessment": None,
        "physical_design_assessment": None,
        "covered_surfaces": ["index"],
        "surface_assessments": {"index": "Reject a new index after comparison."},
        "read_write_cost": "Measure latency on production-shaped data.",
        "operational_risks": [],
        "transition_risks": [],
        "rollback_or_exit": "Restore the current query.",
    }
    physical_design = {
        "id": "add-covering-index",
        "kind": "physical_design",
        "applicable": True,
        "unavailable_reason": None,
        "summary": "Add a covering index for the current query.",
        "equivalence_plan": "Compare result sets with the baseline.",
        "integrity_plan": "Verify constraints before and after the index build.",
        "normalization_assessment": "No model change.",
        "denormalization_assessment": None,
        "physical_design_assessment": {
            "read_benefit": "Avoid a full scan.",
            "write_amplification": "Measure insert overhead.",
            "storage": "Measure index size.",
            "build_or_lock": "Use the engine's online build path.",
            "rollback": "Drop the index.",
        },
        "covered_surfaces": ["index"],
        "surface_assessments": {"index": "Add a covering index."},
        "read_write_cost": "Measure read and write cost.",
        "operational_risks": [],
        "transition_risks": [],
        "rollback_or_exit": "Drop the index.",
    }
    payload = {
        "schema_version": 1,
        "status": "selected",
        "change_surfaces": ["index"],
        "baseline_option_id": "maintain-current",
        "recommended_option_id": "rewrite-query",
        "selected_option_id": "rewrite-query",
        "candidates": [baseline, query, physical_design],
        "recommendation_rationale": "The query rewrite meets the workload budget.",
    }
    decision_path = tmp_path / ".workflow" / "artifacts" / "database-decision.json"
    decision_path.parent.mkdir(parents=True)
    decision_path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_database_decision(tmp_path).selected_option_id == "rewrite-query"

    payload["candidates"] = [baseline, query]
    decision_path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_database_decision(tmp_path).selected_option_id == "rewrite-query"


def test_database_risk_routing_requires_a_selected_decision() -> None:
    routing = (
        REPO_ROOT
        / "docs"
        / "patterns"
        / "workflow-pipeline"
        / "03-risk-routing.md"
    ).read_text(encoding="utf-8")

    assert "take the `high_risk` route" in routing
    assert "A missing index is not a recommendation." in routing


def test_database_agents_require_machine_validated_evidence() -> None:
    agent_paths = (
        REPO_ROOT / "claude" / "agents" / "spec-writer.md",
        REPO_ROOT / "claude" / "agents" / "spec-verifier.md",
        REPO_ROOT / "claude" / "agents" / "happy-path-tester.md",
        REPO_ROOT / "claude" / "skills" / "multi-agent" / "protocols" / "spec_writer.md",
    )
    for path in agent_paths:
        prose = path.read_text(encoding="utf-8")
        assert "database-validation-evidence.json" in prose
        assert "prose is not a substitute" in prose.lower()


def test_database_verifier_and_tester_prohibit_primary_execution() -> None:
    primary_policy = (
        "Production primary is never a verify/test benchmark or executable-query target.",
        "read-only schema metadata",
        "explicitly approved replica",
        "warehouse",
        "sanitized local",
    )
    for name in ("spec-verifier.md", "happy-path-tester.md"):
        source = (REPO_ROOT / "claude" / "agents" / name).read_text(encoding="utf-8")
        for requirement in primary_policy:
            assert requirement in source, f"{name}: missing {requirement}"


def test_database_verify_and_signal_contracts_match_the_core_policy() -> None:
    verify_paths = (
        REPO_ROOT / "claude" / "skills" / "phase-verify" / "SKILL.md",
        REPO_ROOT / "claude" / "agents" / "spec-verifier.md",
        REPO_ROOT / "docs" / "reference" / "workflow-pipeline.md",
        CLI_README,
        REPO_ROOT
        / "docs"
        / "superpowers"
        / "specs"
        / "2026-08-24-p0-database-safety-gate-design.md",
    )
    verify_contract = (
        "`engine`",
        "`execution_target`",
        "`production_primary_queries`: false",
        "`raw_production_rows`: false",
        "`local_same_engine`",
        "`approved_read_replica`",
        "DuckDB",
        "cross-engine",
        "production primary",
    )
    for path in verify_paths:
        prose = path.read_text(encoding="utf-8")
        for requirement in verify_contract:
            assert requirement in prose, f"{path}: missing {requirement}"

    test_evidence_paths = (
        REPO_ROOT / "claude" / "skills" / "phase-test" / "SKILL.md",
        REPO_ROOT / "docs" / "reference" / "workflow-pipeline.md",
    )
    for path in test_evidence_paths:
        prose = path.read_text(encoding="utf-8")
        assert "`raw_production_rows`: false" in prose
        assert "profile.test_command" in prose

    planner_paths = (
        REPO_ROOT / "claude" / "skills" / "phase-plan" / "SKILL.md",
        REPO_ROOT / "claude" / "agents" / "spec-writer.md",
        REPO_ROOT / "claude" / "skills" / "multi-agent" / "protocols" / "spec_writer.md",
    )
    signal_rules = (
        "`text:ddl`",
        "`path:migration:`",
        "`path:schema:`",
        "`path:prisma:`",
        "`text:index`",
        "`text:column`",
        "`text:query`",
        "`artifact_error:`",
    )
    for path in planner_paths:
        prose = path.read_text(encoding="utf-8")
        for rule in signal_rules:
            assert rule in prose, f"{path}: missing signal rule {rule}"
