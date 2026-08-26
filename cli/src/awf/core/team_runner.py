"""TeamRunner — 3-layer agent team orchestrator.

Layer 1 (Python): Deterministic flow — turn ordering, termination, timeout, state.
Layer 2 (Leader): Mission building — task analysis, worker instructions, iteration context.
Layer 3 (Workers): Autonomous execution — read board, write discussion, produce findings.

All worker communication goes through the Blackboard (file-based).
Python evaluates termination deterministically from discussion JSON.
"""
from __future__ import annotations

from copy import deepcopy
from fnmatch import fnmatchcase
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from awf.core.agent_runner import AgentResult, MultiAgentResult, run_agent
from awf.core.blackboard import Blackboard, TeamFinding
from awf.core.dispatch import (
    WorkerSpec,
    resolve_cmux_options_from_config,
    resolve_omp_options_from_config,
    resolve_preference_from_config,
    select_dispatch,
)


def _record_omp_team_provenance(
    cwd: str,
    *,
    backend: str,
    strategy: str,
    phase: str,
    turn: int,
    agents: list[AgentResult],
    elapsed_sec: float,
) -> None:
    if backend != "omp":
        return
    from awf.core.dispatch_provenance import write_omp_dispatch_provenance

    provenance = write_omp_dispatch_provenance(
        cwd,
        strategy=strategy,
        mode=f"team:{phase}:turn-{turn}",
        agents=agents,
        elapsed_sec=elapsed_sec,
    )
    if provenance is None:
        raise RuntimeError("OMP provenance requires an initialized .workflow directory")


@dataclass
class RoleConfig:
    """Single role definition within a team."""

    id: str
    provider: str  # e.g., "claude-code", "codex"
    protocol: str = ""  # protocol file name (defaults to id)
    write_scope: list[str] = field(default_factory=list)
    baseline_research: bool = False
    review_lens: str = ""
    isolated_omp: bool = False
    task_selector: str = ""
    selected_task_ids: tuple[str, ...] = ()
    planning_seal_identity: str = ""
    read_only_agent: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RoleConfig:
        write_scope = data.get("write_scope", [])
        return cls(
            id=data.get("id", ""),
            provider=data.get("provider", "claude-code"),
            protocol=data.get("protocol", "") or data.get("id", ""),
            write_scope=write_scope if isinstance(write_scope, list) else [],
            baseline_research=data.get("baseline_research") is True,
            review_lens=data.get("review_lens", "")
            if isinstance(data.get("review_lens", ""), str)
            else "",
            isolated_omp=data.get("isolated_omp") is True,
            task_selector=data.get("task_selector", "")
            if isinstance(data.get("task_selector", ""), str)
            else "",
        )


@dataclass
class TeamConfig:
    """Parsed team configuration from provider-config.json."""

    name: str
    roles: list[RoleConfig]
    execution: str = "sequential"  # sequential | parallel
    max_turns: int = 3
    timeout_sec: int = 600
    workspace: str = ""
    on_write_scope_overlap: str = "fail"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TeamConfig:
        roles = [
            RoleConfig.from_dict(role)
            for role in data.get("roles", [])
            if isinstance(role, dict)
        ]
        return cls(
            name=data.get("name", "team"),
            roles=roles,
            execution=data.get("execution", "sequential"),
            max_turns=data.get("max_turns", 3),
            timeout_sec=data.get("timeout_sec", 600),
            workspace=data.get("workspace", ""),
            on_write_scope_overlap=data.get("on_write_scope_overlap", "fail"),
        )


def _is_read_only_role(role: RoleConfig) -> bool:
    return role.baseline_research or bool(role.review_lens)


def _team_entrypoint_validation_errors(
    phase: str,
    team_config: dict[str, Any],
    provider_config: dict[str, Any] | None,
) -> list[str]:
    """Validate the exact team that will be dispatched before workspace creation."""
    from awf.core.team_config import validate_provider_config

    routing = (
        provider_config.get("phase_routing")
        if isinstance(provider_config, dict)
        else None
    )
    if isinstance(routing, dict):
        errors = validate_provider_config(provider_config)
        phase_config = routing.get(phase)
        if not isinstance(phase_config, dict):
            errors.append(f"phase_routing.{phase}: missing for dispatched team")
        elif phase_config.get("pattern") != "team":
            errors.append(f"phase_routing.{phase}.pattern: must be 'team' for dispatched team")
        elif phase_config.get("team") != team_config:
            errors.append(f"phase_routing.{phase}.team: differs from dispatched team config")
        return errors

    return validate_provider_config(
        {
            "version": "3.0.0",
            "phase_routing": {
                phase: {
                    "pattern": "team",
                    "team": deepcopy(team_config),
                }
            },
        }
    )


def _read_only_role_preflight_error(config: TeamConfig) -> str | None:
    """Resolve evidence-only roles to an agent without mutation-capable tools."""
    from awf.core.spec_loader import load_agent_definition, resolve_agent_for_role

    prohibited_tools = {
        "ast_edit",
        "bash",
        "edit",
        "edit_file",
        "write",
        "write_file",
    }
    for role in config.roles:
        if not _is_read_only_role(role):
            continue
        try:
            agent_name = resolve_agent_for_role(role.id)
            if not agent_name:
                return f"read-only worker '{role.id}' does not resolve to an agent definition"
            definition = load_agent_definition(agent_name)
        except (FileNotFoundError, OSError, ValueError) as exc:
            return f"read-only worker '{role.id}' cannot load its agent definition: {exc}"

        metadata = definition.get("meta")
        raw_tools = metadata.get("tools", ()) if isinstance(metadata, dict) else ()
        declared_tools = (
            [str(item) for item in raw_tools]
            if isinstance(raw_tools, list)
            else str(raw_tools).split(",")
        )
        unsafe = sorted(
            tool.strip().replace("-", "_").replace(" ", "_").casefold()
            for tool in declared_tools
            if tool.strip().replace("-", "_").replace(" ", "_").casefold()
            in prohibited_tools
        )
        if unsafe:
            return (
                f"read-only worker '{role.id}' resolves to mutation-capable agent "
                f"'{agent_name}' with tools: {', '.join(unsafe)}"
            )
        role.read_only_agent = agent_name
    return None


def _effective_team_config(
    team_config: dict[str, Any],
    config: TeamConfig,
) -> dict[str, Any]:
    """Return a config copy whose isolated lanes use their sealed exact scopes."""
    effective = deepcopy(team_config)
    roles = effective.get("roles")
    if not isinstance(roles, list):
        return effective
    for raw_role, role in zip(roles, config.roles):
        if isinstance(raw_role, dict) and role.isolated_omp:
            raw_role["write_scope"] = list(role.write_scope)
    return effective


@dataclass(frozen=True)
class _SealedTask:
    identifier: str
    parallel: bool
    paths: tuple[str, ...]


_INCOMPLETE_TASK = re.compile(
    r"^\s*[-*+]\s+\[\s\]\s+(?P<identifier>T[0-9]+)\b(?P<body>.*)$"
)


def _normalize_repo_path(value: str) -> str:
    candidate = value.strip()
    if (
        not candidate
        or "\\" in candidate
        or candidate.startswith("/")
        or any(marker in candidate for marker in "*?[")
    ):
        raise ValueError(f"invalid repo-relative path: {value!r}")
    path = PurePosixPath(candidate)
    if path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise ValueError(f"invalid repo-relative path: {value!r}")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        raise ValueError(f"invalid repo-relative path: {value!r}")
    return normalized


def _task_paths(scope: str) -> tuple[str, ...]:
    if "`" in scope:
        paths = re.findall(r"`([^`]+)`", scope)
        remainder = re.sub(r"`[^`]+`", "", scope).strip()
        if not paths or remainder.strip(" ,;"):
            raise ValueError("task scope must contain only comma-separated repo-relative paths")
    else:
        paths = scope.split(",")
    normalized = tuple(dict.fromkeys(_normalize_repo_path(path) for path in paths))
    if not normalized:
        raise ValueError("task has no file scope")
    return normalized


def _load_incomplete_tasks(tasks_text: str) -> dict[str, _SealedTask]:
    tasks: dict[str, _SealedTask] = {}
    for line in tasks_text.splitlines():
        match = _INCOMPLETE_TASK.match(line)
        if not match:
            continue
        identifier = match.group("identifier")
        if identifier in tasks:
            raise ValueError(f"sealed tasks.md defines incomplete task '{identifier}' more than once")
        body = match.group("body")
        _, separator, scope = body.partition(" — ")
        if not separator:
            raise ValueError(f"sealed task '{identifier}' has no explicit file scope")
        tasks[identifier] = _SealedTask(
            identifier=identifier,
            parallel=bool(re.search(r"(?:^|\s)\[P\](?:\s|$)", body)),
            paths=_task_paths(scope),
        )
    return tasks


def _load_allowed_scope(allowed_bytes: bytes) -> set[str]:
    try:
        payload = json.loads(allowed_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"sealed allowed-files.json is invalid: {exc}") from None
    if not isinstance(payload, dict):
        raise ValueError("sealed allowed-files.json must be an object")

    planned = payload.get("planned_files", payload.get("files"))
    expanded = payload.get("expanded_files", [])
    if not isinstance(planned, list) or not isinstance(expanded, list):
        raise ValueError("sealed allowed-files.json must declare planned_files and expanded_files lists")
    values = [*planned, *expanded]
    if not values or not all(isinstance(value, str) for value in values):
        raise ValueError("sealed allowed-files.json must contain non-empty string paths")
    return {_normalize_repo_path(value) for value in values}


def _load_sealed_impl_scope(
    cwd: str,
) -> tuple[dict[str, _SealedTask], set[str], str]:
    """Load exact task and file scopes only after their G3 seal validates."""
    from awf.core.approval import ApprovalError, validate_approved_planning_seal

    root = Path(cwd)
    try:
        seal = validate_approved_planning_seal(root)
        artifacts = seal.planning_seal.get("artifacts")
        if not isinstance(artifacts, dict):
            raise ValueError("G3 planning seal has no artifact digests")
        tasks_bytes = (root / ".workflow" / "artifacts" / "tasks.md").read_bytes()
        allowed_bytes = (
            root / ".workflow" / "artifacts" / "allowed-files.json"
        ).read_bytes()
        for name, content in (
            ("tasks.md", tasks_bytes),
            ("allowed-files.json", allowed_bytes),
        ):
            expected = artifacts.get(name)
            actual = hashlib.sha256(content).hexdigest()
            if not isinstance(expected, str) or expected != actual:
                raise ValueError(f"G3 sealed {name} changed while loading impl scope")
        seal_identity = seal.planning_seal.get("identity")
        if not isinstance(seal_identity, str) or not seal_identity:
            raise ValueError("G3 planning seal has no identity")
        return (
            _load_incomplete_tasks(tasks_bytes.decode("utf-8")),
            _load_allowed_scope(allowed_bytes),
            seal_identity,
        )
    except (ApprovalError, OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"isolated_omp requires a valid immutable G3 planning seal: {exc}") from None


def _restrict_isolated_omp_scopes(cwd: str, config: TeamConfig) -> str | None:
    """Bind each isolated lane to selected sealed tasks and approved files."""
    workers = [role for role in config.roles if role.isolated_omp]
    if not workers:
        return None
    try:
        tasks, allowed_paths, seal_identity = _load_sealed_impl_scope(cwd)
    except ValueError as exc:
        return str(exc)

    for role in workers:
        if role.task_selector == "parallel":
            selected = [task for task in tasks.values() if task.parallel]
            if not selected:
                return (
                    f"isolated_omp worker '{role.id}' task_selector 'parallel' "
                    "does not resolve to an incomplete [P] task in sealed tasks.md"
                )
        else:
            selected_task = tasks.get(role.task_selector)
            if selected_task is None:
                return (
                    f"isolated_omp worker '{role.id}' task_selector "
                    f"'{role.task_selector}' does not resolve to an incomplete sealed task"
                )
            selected = [selected_task]

        selected_paths = tuple(
            dict.fromkeys(path for task in selected for path in task.paths)
        )
        for path in selected_paths:
            if path not in allowed_paths:
                return (
                    f"isolated_omp worker '{role.id}' selected task path '{path}' "
                    "is outside G3-sealed allowed-files scope"
                )
            if not any(fnmatchcase(path, scope) for scope in role.write_scope):
                return (
                    f"isolated_omp worker '{role.id}' selected task path '{path}' "
                    "is outside its configured write_scope"
                )
        role.write_scope = list(selected_paths)
        role.selected_task_ids = tuple(task.identifier for task in selected)
        role.planning_seal_identity = seal_identity
    return None


def _scope_patterns_overlap(left: list[str], right: list[str]) -> bool:
    """Return true unless two configured scopes are provably disjoint.

    An isolated patch worker starts from the same parent checkout as its peers.
    Globs cannot be proven disjoint without materializing the checkout, so they
    are conservatively treated as overlapping. Exact paths are compared by
    ancestor relationship.
    """
    for left_path in left:
        for right_path in right:
            if any(marker in left_path or marker in right_path for marker in "*?["):
                return True
            normalized_left = left_path.strip("/")
            normalized_right = right_path.strip("/")
            if (
                normalized_left == normalized_right
                or normalized_left.startswith(f"{normalized_right}/")
                or normalized_right.startswith(f"{normalized_left}/")
            ):
                return True
    return False


def _isolated_omp_roles_overlap(config: TeamConfig) -> bool:
    """Check isolated implementation roles before concurrent patch generation."""
    workers = [role for role in config.roles if role.isolated_omp]
    return any(
        _scope_patterns_overlap(left.write_scope, right.write_scope)
        for index, left in enumerate(workers)
        for right in workers[index + 1:]
    )


def _isolated_omp_preflight_error(
    phase: str,
    config: TeamConfig,
    *,
    check_overlap: bool = True,
) -> str | None:
    """Fail closed before dispatching an invalid isolated implementation lane."""
    workers = [role for role in config.roles if role.isolated_omp]
    if not workers:
        return None
    if phase != "impl":
        return "isolated_omp workers are only permitted during impl"
    for role in workers:
        if not role.write_scope:
            return f"isolated_omp worker '{role.id}' requires a non-empty write_scope"
        if not (
            role.task_selector == "parallel"
            or (
                role.task_selector.startswith("T")
                and role.task_selector[1:].isdigit()
            )
        ):
            return (
                f"isolated_omp worker '{role.id}' requires task_selector "
                "'parallel' or an explicit T-number"
            )
    if (
        check_overlap
        and config.execution == "parallel"
        and _isolated_omp_roles_overlap(config)
        and config.on_write_scope_overlap != "sequential"
    ):
        return (
            "isolated_omp workers have overlapping or unprovable write_scope; "
            "set on_write_scope_overlap to 'sequential' or make scopes disjoint"
        )
    return None


def run_team(
    phase: str,
    prompt: str,
    team_config: dict[str, Any],
    registry,
    cwd: str,
    *,
    processor=None,
    add_dirs: list[str] | None = None,
    provider_config: dict[str, Any] | None = None,
) -> MultiAgentResult:
    """Run a team of workers through the 3-layer turn loop.

    Args:
        phase: Workflow phase (plan, impl, test, ...).
        prompt: Task prompt for the team.
        team_config: Team config dict from provider-config.json.
        registry: Provider registry for resolving provider names.
        cwd: Project root directory.
        processor: Optional event processor for observability.
        add_dirs: Additional directories for providers.

    Returns:
        MultiAgentResult compatible with existing multi-agent pipeline.
    """
    config = TeamConfig.from_dict(team_config)

    if not config.roles:
        _log(f"team: {config.name} — no roles configured, returning FAIL")
        return MultiAgentResult(
            mode=f"team:{config.name}",
            judge_verdict="FAIL",
            judge_reason="no roles configured in team",
            selected_agent=config.name,
        )

    validation_errors = _team_entrypoint_validation_errors(
        phase, team_config, provider_config
    )
    if validation_errors:
        detail = "; ".join(validation_errors)
        _log(f"team: {config.name} — config rejected: {detail}")
        return MultiAgentResult(
            mode=f"team:{config.name}",
            judge_verdict="FAIL",
            judge_reason=f"invalid provider/team config: {detail}",
            selected_agent=config.name,
        )

    read_only_error = _read_only_role_preflight_error(config)
    if read_only_error:
        _log(f"team: {config.name} — read-only lane rejected: {read_only_error}")
        return MultiAgentResult(
            mode=f"team:{config.name}",
            judge_verdict="FAIL",
            judge_reason=read_only_error,
            selected_agent=config.name,
        )

    lane_error = _isolated_omp_preflight_error(
        phase, config, check_overlap=False
    )
    if lane_error is None:
        lane_error = _restrict_isolated_omp_scopes(cwd, config)
    if lane_error is None:
        lane_error = _isolated_omp_preflight_error(phase, config)
    if lane_error:
        _log(f"team: {config.name} — isolated OMP lane rejected: {lane_error}")
        return MultiAgentResult(
            mode=f"team:{config.name}",
            judge_verdict="FAIL",
            judge_reason=lane_error,
            selected_agent=config.name,
        )

    # Resolve phase-specific effort for providers
    _pm = (provider_config or {}).get("phase_models", {}).get(phase, {})
    _effort = _pm.get("effort")
    _codex_re = _pm.get("codex_reasoning")

    bb = Blackboard.create(
        cwd,
        phase,
        team_config=_effective_team_config(team_config, config),
    )
    bb.begin_run()

    all_agents: list[AgentResult] = []
    started = time.monotonic()
    final_turn = 0
    stop_reason = "max_turns"

    _log(f"team: {config.name} | phase={phase} | roles={[r.id for r in config.roles]} | max_turns={config.max_turns}")

    for turn in range(1, config.max_turns + 1):
        final_turn = turn
        elapsed = time.monotonic() - started

        # Hard timeout check
        if elapsed > config.timeout_sec:
            stop_reason = "timeout"
            _log(f"turn {turn}: timeout ({elapsed:.0f}s > {config.timeout_sec}s)")
            break

        turn_started = time.monotonic()
        _emit_turn_event(processor, phase, turn, "started")
        _log(f"turn {turn}/{config.max_turns}")

        # Layer 2: Build mission
        mission = _build_mission(prompt, bb, turn, config)
        bb.write_mission(mission, turn)

        # Layer 3: Execute workers (strict timeout budget)
        remaining_sec = config.timeout_sec - int(time.monotonic() - started)
        if remaining_sec < 10:
            stop_reason = "timeout"
            _log(f"turn {turn}: insufficient remaining budget ({remaining_sec}s)")
            break
        turn_results = _execute_workers(
            bb, turn, config, registry, cwd,
            timeout_sec=remaining_sec,
            add_dirs=add_dirs,
            processor=processor,
            phase=phase,
            effort=_effort,
            codex_reasoning=_codex_re,
            provider_config=provider_config,
        )
        all_agents.extend(turn_results)

        _emit_turn_event(processor, phase, turn, "completed", data={
            "worker_count": len(turn_results),
            "duration_sec": round(time.monotonic() - turn_started, 1),
        })

        # Layer 1: Deterministic termination
        should_stop, reason = bb.evaluate_termination(turn)
        if should_stop:
            stop_reason = reason
            _log(f"turn {turn}: stop ({reason})")
            break

        _log(f"turn {turn}: continue ({reason})")

    # Build final result — verdict based on latest turn (not all history)
    total_elapsed = time.monotonic() - started
    latest_findings = bb.collect_findings(final_turn) if final_turn > 0 else []
    all_findings = bb.collect_all_findings()
    verdict, verdict_reason = _compute_verdict(latest_findings, stop_reason, final_turn, config.max_turns)

    _log(f"done: {verdict} | turns={final_turn} | findings={len(all_findings)} | {total_elapsed:.1f}s | {stop_reason}")

    result = MultiAgentResult(
        mode=f"team:{config.name}",
        agents=all_agents,
        judge_verdict=verdict,
        judge_reason=verdict_reason,
        selected_agent=config.name,
        combined_output=_build_combined_output(
            bb,
            latest_findings,
            phase=phase,
            turn=final_turn,
            verdict=verdict,
        ),
    )

    # Emit events matching existing consumers (state_updater.py, progress.py)
    if processor:
        from awf.core.events import EventType
        task_id = f"team-{phase}"

        # Emit AGENT_COMPLETED for each worker (matches multi_agent.py pattern)
        for agent in all_agents:
            processor.emit(
                event_type=EventType.AGENT_COMPLETED,
                task_id=task_id,
                source="team_runner",
                data={
                    "provider": agent.provider_name,
                    "role": agent.role,
                    "elapsed_sec": round(agent.elapsed_sec, 1),
                    "ok": agent.ok,
                    "input_tokens": agent.input_tokens,
                    "output_tokens": agent.output_tokens,
                },
            )

        # Emit JUDGE_VERDICT with selected_agent (required by state_updater.py)
        processor.emit(
            event_type=EventType.JUDGE_VERDICT,
            task_id=task_id,
            source="team_runner",
            data={
                "mode": f"team:{config.name}",
                "verdict": verdict,
                "reason": verdict_reason,
                "selected_agent": config.name,
                "agent_count": len(all_agents),
                "turns": final_turn,
                "findings_total": len(all_findings),
                "findings_critical": sum(1 for f in all_findings if f.is_critical),
            },
        )

    return result


# --- Layer 2: Mission Builder ---


def _build_mission(prompt: str, bb: Blackboard, turn: int, config: TeamConfig) -> str:
    """Build mission content for the current turn.

    Turn 1: Original prompt + role assignments.
    Turn N>1: Original prompt + previous findings + iteration guidance.
    """
    sections: list[str] = []

    sections.append(f"# Team Mission — Turn {turn}\n")
    sections.append(f"## Task\n\n{prompt}\n")

    # Role assignments
    role_lines = [f"- **{r.id}** (provider: {r.provider})" for r in config.roles]
    sections.append(f"## Roles\n\n" + "\n".join(role_lines) + "\n")

    # Previous findings for iteration context
    if turn > 1:
        prev_findings = bb.collect_findings(turn - 1)
        if prev_findings:
            finding_lines = []
            for f in prev_findings:
                finding_lines.append(
                    f"- [{f.severity}] {f.category} @ {f.location}: {f.description}"
                )
            sections.append(
                f"## Previous Findings (Turn {turn - 1})\n\n"
                f"아래 이슈를 해결하세요:\n\n"
                + "\n".join(finding_lines) + "\n"
            )

    # Board artifacts context — inline content for prompt-only providers
    artifacts = bb.list_board_artifacts()
    if artifacts:
        artifact_sections: list[str] = []
        for a in artifacts:
            content = bb.read_artifact(a.name)
            if content and len(content) < 4000:
                artifact_sections.append(f"### {a.name}\n\n{content}")
            else:
                size = f" ({len(content)} chars)" if content else ""
                artifact_sections.append(f"### {a.name}{size}\n\n*(file available in board/)*")
        sections.append("## Board Artifacts\n\n" + "\n\n".join(artifact_sections) + "\n")

    return "\n".join(sections)


# --- Layer 3: Worker Execution ---


def _execute_workers(
    bb: Blackboard,
    turn: int,
    config: TeamConfig,
    registry,
    cwd: str,
    *,
    timeout_sec: int,
    add_dirs: list[str] | None,
    processor=None,
    phase: str = "",
    effort: str | None = None,
    codex_reasoning: str | None = None,
    provider_config: dict[str, Any] | None = None,
) -> list[AgentResult]:
    """Execute all workers for a turn.

    Sequential: one by one (each sees previous worker's output).
    Parallel: all at once via the dispatch backend.
    """
    if (
        config.execution == "parallel"
        and _isolated_omp_roles_overlap(config)
        and config.on_write_scope_overlap == "sequential"
    ):
        _log("isolated OMP write scopes overlap; falling back to sequential execution")
        return _execute_sequential(bb, turn, config, registry, cwd,
                                   timeout_sec=timeout_sec, add_dirs=add_dirs,
                                   processor=processor, phase=phase,
                                   effort=effort, codex_reasoning=codex_reasoning,
                                   provider_config=provider_config)
    if config.execution == "parallel":
        return _execute_parallel(bb, turn, config, registry, cwd,
                                 timeout_sec=timeout_sec, add_dirs=add_dirs,
                                 processor=processor, phase=phase,
                                 effort=effort, codex_reasoning=codex_reasoning,
                                 provider_config=provider_config)
    return _execute_sequential(bb, turn, config, registry, cwd,
                               timeout_sec=timeout_sec, add_dirs=add_dirs,
                               processor=processor, phase=phase,
                               effort=effort, codex_reasoning=codex_reasoning,
                               provider_config=provider_config)


def _resolve_team_dispatch(
    *,
    cwd: str,
    worker_count: int,
    estimated_seconds: float,
    provider_config: dict[str, Any] | None,
    workers: list[WorkerSpec] | None = None,
):
    """Pick a dispatch backend honoring ``provider-config.json::dispatch``.

    Single-call helper so both parallel and sequential paths apply the same
    surface preference, capability/cost policy, and legacy workload heuristic.
    """
    return select_dispatch(
        worker_count=max(worker_count, 1),
        estimated_seconds=estimated_seconds,
        preference=resolve_preference_from_config(provider_config),
        cwd=cwd,
        options=resolve_cmux_options_from_config(provider_config),
        workers=workers,
        provider_config=provider_config,
        omp_options=resolve_omp_options_from_config(provider_config),
    )


def _parent_checkout_snapshot(cwd: str) -> bytes:
    """Hash all parent files except the runner-owned team workspace."""
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=cwd,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if status.returncode != 0:
        detail = status.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(
            "cannot establish read-only parent mutation guard"
            + (f": {detail}" if detail else "")
        )

    root = Path(cwd).resolve()
    digest = hashlib.sha256()
    for directory, subdirectories, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        relative_directory = current.relative_to(root)
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if (relative_directory / name).as_posix()
            not in {".git", ".workflow/team"}
        )
        for filename in sorted(filenames):
            path = current / filename
            relative = path.relative_to(root).as_posix()
            if relative == ".git" or relative.startswith(".workflow/team/"):
                continue
            metadata = path.lstat()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(stat.S_IMODE(metadata.st_mode)).encode("ascii"))
            digest.update(b"\0")
            if stat.S_ISLNK(metadata.st_mode):
                digest.update(os.readlink(path).encode("utf-8"))
            elif stat.S_ISREG(metadata.st_mode):
                with path.open("rb") as source:
                    for chunk in iter(lambda: source.read(64 * 1024), b""):
                        digest.update(chunk)
            else:
                digest.update(str(metadata.st_mode).encode("ascii"))
            digest.update(b"\0")
    return status.stdout + digest.digest()

def _mark_parent_mutation_guard(
    result: AgentResult,
    *,
    before: bytes,
    cwd: str,
) -> bool:
    """Fail closed if a read-only worker changed the parent checkout."""
    try:
        after = _parent_checkout_snapshot(cwd)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        result.returncode = 2
        result.stderr = f"{result.stderr}\nread_only_guard_failed: {exc}".strip()
        result.metadata["read_only_guard"] = {"valid": False, "error": str(exc)}
        return False
    if after == before:
        result.metadata["read_only_guard"] = {"valid": True}
        return True
    result.returncode = 2
    result.stderr = (
        f"{result.stderr}\nread_only_parent_mutation_detected".strip()
    )
    result.metadata["read_only_guard"] = {
        "valid": False,
        "error": "parent_checkout_changed",
    }
    return False

def _read_only_dispatch_failure(role_cfg: RoleConfig, reason: str) -> AgentResult:
    return AgentResult(
        provider_name=role_cfg.provider,
        role=role_cfg.id,
        stdout="",
        stderr=reason,
        returncode=2,
        elapsed_sec=0.0,
    )


def _execute_sequential(
    bb: Blackboard,
    turn: int,
    config: TeamConfig,
    registry,
    cwd: str,
    *,
    timeout_sec: int,
    add_dirs: list[str] | None,
    processor=None,
    phase: str = "",
    effort: str | None = None,
    codex_reasoning: str | None = None,
    provider_config: dict[str, Any] | None = None,
) -> list[AgentResult]:
    """Run workers one at a time. Each can read previous workers' output.

    Per-worker prompt building stays inline because the next worker's
    prompt depends on the prior worker's blackboard write — that's the
    blackboard-mediated equivalent of ``ChainedStep.factory(prior_results)``.
    Each worker is dispatched individually so the project's configured
    surface (inline or cmux) and the dispatch_complete telemetry both
    apply, even though we don't use ``run_chained`` (workers have
    distinct roles, so cmux's role-pinning doesn't apply here).
    """
    results: list[AgentResult] = []
    role_count = max(len(config.roles), 1)
    per_worker_timeout = min(timeout_sec, max(30, timeout_sec // role_count))
    seq_start = time.monotonic()

    for role_cfg in config.roles:
        provider = _resolve_provider(registry, role_cfg.provider, effort=effort, codex_reasoning=codex_reasoning)
        if provider is None:
            _log(f"  skip {role_cfg.id}: provider '{role_cfg.provider}' unavailable")
            # Write synthetic failure so termination sees this as a problem
            _save_skipped_worker(bb, turn, role_cfg)
            continue

        worker_prompt = _build_worker_prompt(bb, turn, role_cfg)

        # Recompute remaining budget before each sequential worker
        remaining_budget = timeout_sec - int(time.monotonic() - seq_start)
        actual_timeout = min(per_worker_timeout, max(10, remaining_budget))

        _log(f"  worker: {role_cfg.id} ({role_cfg.provider})")
        _emit_worker_event(processor, phase, turn, role_cfg.id, "spawned")

        requires_read_only_isolation = _is_read_only_role(role_cfg)
        spec = WorkerSpec(
            role=role_cfg.id,
            provider=provider,
            prompt=worker_prompt,
            timeout_sec=actual_timeout,
            require_json=True,
            add_dirs=tuple(add_dirs or ()),
            agent_type=role_cfg.read_only_agent or None,
            isolated=True
            if role_cfg.write_scope or requires_read_only_isolation
            else None,
        )
        parent_snapshot: bytes | None = None
        dispatch = None
        if requires_read_only_isolation:
            try:
                parent_snapshot = _parent_checkout_snapshot(cwd)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                result = _read_only_dispatch_failure(
                    role_cfg,
                    f"read-only worker cannot establish parent mutation guard: {exc}",
                )
            else:
                try:
                    dispatch = _resolve_team_dispatch(
                        cwd=cwd,
                        worker_count=1,
                        estimated_seconds=float(actual_timeout),
                        provider_config=provider_config,
                        workers=[spec],
                    )
                except Exception as exc:
                    result = _read_only_dispatch_failure(
                        role_cfg,
                        f"read-only worker cannot acquire disposable isolation: {exc}",
                    )
                else:
                    if dispatch.name != "omp":
                        result = _read_only_dispatch_failure(
                            role_cfg,
                            "read-only worker requires the OMP isolated dispatch backend",
                        )
                    else:
                        result = dispatch.run([spec], cwd=cwd, strategy="sequential")[0]
                        _mark_parent_mutation_guard(
                            result,
                            before=parent_snapshot,
                            cwd=cwd,
                        )
        else:
            dispatch = _resolve_team_dispatch(
                cwd=cwd,
                worker_count=1,
                estimated_seconds=float(actual_timeout),
                provider_config=provider_config,
                workers=[spec],
            )
            if role_cfg.isolated_omp and dispatch.name != "omp":
                result = AgentResult(
                    provider_name=role_cfg.provider,
                    role=role_cfg.id,
                    stdout="",
                    stderr="isolated_omp worker requires the OMP dispatch backend",
                    returncode=2,
                    elapsed_sec=0.0,
                )
            else:
                result = dispatch.run([spec], cwd=cwd, strategy="sequential")[0]
        _enforce_worker_write_scope(bb, role_cfg, result, cwd)
        results.append(result)
        if dispatch is not None:
            _record_omp_team_provenance(
                cwd,
                backend=dispatch.name,
                strategy="sequential",
                phase=phase,
                turn=turn,
                agents=[result],
                elapsed_sec=result.elapsed_sec,
            )

        _emit_worker_event(processor, phase, turn, role_cfg.id, "completed", data={
            "passed": _worker_passed(result),
            "duration_sec": round(result.elapsed_sec, 1),
        })

        # Save worker output to discussion
        _save_worker_output(bb, turn, role_cfg.id, result)

        _log(f"  {role_cfg.id}: {'ok' if result.ok else 'FAIL'} ({result.elapsed_sec:.0f}s)")

    return results


def _execute_parallel(
    bb: Blackboard,
    turn: int,
    config: TeamConfig,
    registry,
    cwd: str,
    *,
    timeout_sec: int,
    add_dirs: list[str] | None,
    processor=None,
    phase: str = "",
    effort: str | None = None,
    codex_reasoning: str | None = None,
    provider_config: dict[str, Any] | None = None,
) -> list[AgentResult]:
    """Run workers concurrently through the dispatch backend.

    All worker prompts are built upfront — parallel execution can't
    incorporate within-turn prior outputs. Cross-turn context still
    flows via the leader's mission update (turn N reads turn N-1
    findings via ``_build_mission``).
    """
    available: list[tuple] = []
    for role_cfg in config.roles:
        provider = _resolve_provider(
            registry, role_cfg.provider, effort=effort, codex_reasoning=codex_reasoning
        )
        if provider is None:
            _log(f"  skip {role_cfg.id}: provider '{role_cfg.provider}' unavailable")
            _save_skipped_worker(bb, turn, role_cfg)
            continue
        worker_prompt = _build_worker_prompt(bb, turn, role_cfg)
        available.append((role_cfg, provider, worker_prompt))
        _emit_worker_event(processor, phase, turn, role_cfg.id, "spawned")

    if not available:
        return []

    requires_read_only_isolation = any(
        _is_read_only_role(role_cfg) for role_cfg, _, _ in available
    )
    specs = [
        WorkerSpec(
            role=role_cfg.id,
            provider=provider,
            prompt=prompt,
            timeout_sec=timeout_sec,
            require_json=True,
            add_dirs=tuple(add_dirs or ()),
            agent_type=role_cfg.read_only_agent or None,
            isolated=True
            if role_cfg.write_scope or _is_read_only_role(role_cfg)
            else None,
        )
        for role_cfg, provider, prompt in available
    ]
    parent_snapshot: bytes | None = None
    dispatch = None
    dispatch_started_at = time.monotonic()
    executed = False

    if requires_read_only_isolation:
        try:
            parent_snapshot = _parent_checkout_snapshot(cwd)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            results = [
                _read_only_dispatch_failure(
                    role_cfg,
                    f"parallel batch cannot establish read-only parent mutation guard: {exc}",
                )
                for role_cfg, _, _ in available
            ]
        else:
            try:
                dispatch = _resolve_team_dispatch(
                    cwd=cwd,
                    worker_count=len(specs),
                    estimated_seconds=float(timeout_sec),
                    provider_config=provider_config,
                    workers=specs,
                )
            except Exception as exc:
                results = [
                    _read_only_dispatch_failure(
                        role_cfg,
                        f"parallel batch cannot acquire disposable isolation: {exc}",
                    )
                    for role_cfg, _, _ in available
                ]
            else:
                results = []
    else:
        dispatch = _resolve_team_dispatch(
            cwd=cwd,
            worker_count=len(specs),
            estimated_seconds=float(timeout_sec),
            provider_config=provider_config,
            workers=specs,
        )
        results = []

    if dispatch is not None:
        requires_omp = requires_read_only_isolation or any(
            role_cfg.isolated_omp for role_cfg, _, _ in available
        )
        if requires_omp and dispatch.name != "omp":
            reason = (
                "read-only worker requires the OMP isolated dispatch backend"
                if requires_read_only_isolation
                else "isolated_omp worker requires the OMP dispatch backend"
            )
            results = [
                _read_only_dispatch_failure(role_cfg, reason)
                for role_cfg, _, _ in available
            ]
        else:
            try:
                results = list(dispatch.run(specs, cwd=cwd, strategy="parallel"))
                executed = True
            except Exception as exc:
                # Dispatch backend failure (e.g., cmux unavailable mid-batch). Synthesize
                # failure rows so blackboard termination sees the problem rather than
                # losing the whole turn silently.
                _log(f"  dispatch failure: {exc}")
                results = [
                    AgentResult(
                        provider_name=role_cfg.provider,
                        role=role_cfg.id,
                        stdout="",
                        stderr=f"dispatch failure: {exc}",
                        returncode=2,
                        elapsed_sec=0.0,
                    )
                    for role_cfg, _, _ in available
                ]

    parent_guard_valid = True
    if executed and parent_snapshot is not None:
        for (role_cfg, _, _), result in zip(available, results):
            if _is_read_only_role(role_cfg):
                parent_guard_valid = (
                    _mark_parent_mutation_guard(
                        result,
                        before=parent_snapshot,
                        cwd=cwd,
                    )
                    and parent_guard_valid
                )
    if parent_guard_valid:
        for (role_cfg, _, _), result in zip(available, results):
            _enforce_worker_write_scope(bb, role_cfg, result, cwd)
    else:
        for (role_cfg, _, _), result in zip(available, results):
            if role_cfg.isolated_omp:
                result.returncode = 2
                result.stderr = (
                    f"{result.stderr}\nwrite_scope_validation_failed: "
                    "parent checkout changed during read-only dispatch"
                ).strip()

    if dispatch is not None:
        _record_omp_team_provenance(
            cwd,
            backend=dispatch.name,
            strategy="parallel",
            phase=phase,
            turn=turn,
            agents=results,
            elapsed_sec=time.monotonic() - dispatch_started_at,
        )

    for (role_cfg, _, _), result in zip(available, results):
        _save_worker_output(bb, turn, role_cfg.id, result)
        _emit_worker_event(processor, phase, turn, role_cfg.id, "completed", data={
            "passed": _worker_passed(result),
            "duration_sec": round(result.elapsed_sec, 1),
        })
        _log(f"  {role_cfg.id}: {'ok' if result.ok else 'FAIL'} ({result.elapsed_sec:.0f}s)")

    return results


def _build_worker_prompt(bb: Blackboard, turn: int, role_cfg: RoleConfig) -> str:
    """Build the full prompt for a worker: protocol + mission + board context."""
    parts: list[str] = []

    # Load protocol
    protocol = _load_team_protocol(role_cfg.protocol or role_cfg.id)
    if protocol:
        parts.append(protocol)

    # Mission
    mission = bb.read_mission()
    if mission:
        parts.append(mission)
    if role_cfg.baseline_research:
        parts.append(
            "## Opt-in Baseline Research\n\n"
            "Collect baseline facts and cite their source paths as evidence only. "
            "Do not edit canonical workflow artifacts, workflow state, gates, or "
            "HIL records; the parent planner/judge owns those decisions."
        )
    if role_cfg.review_lens:
        parts.append(
            "## Assigned Review Lens\n\n"
            f"Review from the `{role_cfg.review_lens}` perspective. Report evidence "
            "and findings only; the parent judge owns canonical artifacts, gates, "
            "and HIL decisions."
        )
    if role_cfg.isolated_omp:
        parts.append(
            "## Isolated OMP Implementation Lane\n\n"
            f"Work only on sealed parent-selected task(s) "
            f"`{', '.join(role_cfg.selected_task_ids) or role_cfg.task_selector}`. "
            "Return an isolated patch proposal inside the enforced write scope. "
            "Do not apply patches to the parent checkout, update task status, create "
            "commits, or modify G4/gates/HIL; the parent alone owns patch application "
            "and workflow completion."
        )
    if bb.phase == "test":
        namespace = f"awf-test-turn-{turn}-{role_cfg.id}"
        parts.append(
            "## Test Runtime Isolation\n\n"
            f"Use `{namespace}` as the unique namespace for every temporary port, "
            "browser profile/session, debugger target, and scratch artifact. Do not "
            "share these resources with another worker. Browser/debug evidence is "
            "optional: if unavailable, emit explicit `not_run` or `skipped` "
            "capability evidence and do not treat it as a passing result. The parent "
            "alone merges canonical results and owns G6/HIL."
        )
    if role_cfg.write_scope:
        allowed = "\n".join(f"- `{scope}`" for scope in role_cfg.write_scope)
        parts.append(
            "## Enforced Write Scope\n\n"
            "This worker runs in an isolated workspace. Only these repo-relative "
            "paths or glob patterns may change:\n"
            f"{allowed}\n\n"
            "Changes outside this scope are rejected before merge. Do not edit "
            "other files."
        )

    # Previous discussion context (for sequential: include earlier workers' output this turn)
    prev_discussion = _gather_discussion_context(bb, turn, role_cfg.id)
    if prev_discussion:
        parts.append(f"## Prior Worker Output (Turn {turn})\n\n{prev_discussion}")

    # Output contract reminder
    from awf.core.spec_loader import load_prompt_optional
    output_fmt = load_prompt_optional("multi-agent", "team-output-format")
    if output_fmt:
        parts.append(f"\n{output_fmt}")
    else:
        parts.append("\n## Output Format\n\n반드시 JSON으로 결과를 반환하세요.\n")

    return "\n\n".join(parts)


def _save_skipped_worker(bb: Blackboard, turn: int, role_cfg: RoleConfig) -> None:
    """Write a synthetic CRITICAL finding for a worker whose provider was unavailable."""
    bb.write_findings(turn, role_cfg.id, {
        "conclusion": "FAIL",
        "findings": [{"severity": "CRITICAL", "category": "worker_skipped",
                      "location": role_cfg.id,
                      "description": f"Provider '{role_cfg.provider}' unavailable for worker '{role_cfg.id}'",
                      "suggestion": "Check provider configuration or install the provider"}],
    })


def _worker_passed(result: AgentResult) -> bool:
    """Strict success check: process ok AND valid JSON output produced."""
    return result.ok and not result.parse_error and bool(result.stdout.strip())

def _patch_changed_paths(cwd: str, patch_path: Path) -> list[Path]:
    """Return changed paths and reject renames before a parent patch apply."""
    with patch_path.open("rb") as source:
        for line in source:
            if line.startswith(
                (b"rename from ", b"rename to ", b"similarity index ")
            ):
                raise ValueError("isolated patch renames are not permitted")
    preview = subprocess.run(
        ["git", "apply", "--numstat", "-z", str(patch_path)],
        cwd=cwd,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if preview.returncode != 0:
        detail = preview.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"cannot inspect isolated patch: {detail or 'git apply failed'}")

    chunks = preview.stdout.split(b"\0")
    paths: list[Path] = []
    index = 0
    while index < len(chunks):
        chunk = chunks[index]
        index += 1
        if not chunk:
            continue
        fields = chunk.split(b"\t", 2)
        if len(fields) != 3:
            raise ValueError("cannot inspect isolated patch: invalid numstat output")
        if fields[2]:
            paths.append(Path(fields[2].decode("utf-8", errors="strict")))
            continue
        raise ValueError("isolated patch renames are not permitted")
    if not paths:
        raise ValueError("cannot inspect isolated patch: no changed paths")
    return paths



def _validate_current_impl_seal(cwd: str, role_cfg: RoleConfig) -> None:
    from awf.core.approval import ApprovalError, validate_approved_planning_seal
    from awf.core.state import load_workflow_state

    try:
        state = load_workflow_state(cwd)
        if state.get("currentPhase") != "impl":
            raise ValueError("workflow phase changed after isolated dispatch")
        seal = validate_approved_planning_seal(Path(cwd), state=state)
        current_identity = seal.planning_seal.get("identity")
        if (
            not role_cfg.planning_seal_identity
            or current_identity != role_cfg.planning_seal_identity
        ):
            raise ValueError("G3 planning seal changed after isolated dispatch")
    except (ApprovalError, OSError, ValueError, TypeError) as exc:
        raise ValueError(f"isolated patch approval identity is stale: {exc}") from None

def _enforce_worker_write_scope(
    bb: Blackboard,
    role_cfg: RoleConfig,
    result: AgentResult,
    cwd: str,
) -> None:
    """Validate and apply an isolated OMP patch only within the role's scope."""
    if _is_read_only_role(role_cfg):
        if result.ok and result.metadata.get("patch_path"):
            result.returncode = 2
            result.stderr = (
                f"{result.stderr}\nread_only_patch_rejected".strip()
            )
            result.metadata["write_scope_validation"] = {
                "valid": False,
                "applied": False,
                "error": "read-only worker cannot submit a patch",
            }
        return
    if not role_cfg.write_scope or not result.ok:
        return
    patch_value = result.metadata.get("patch_path")
    if not patch_value:
        result.metadata["write_scope_validation"] = {
            "valid": True,
            "applied": False,
            "changed_paths": [],
            "reason": "worker produced no patch",
        }
        return

    try:
        if role_cfg.isolated_omp:
            _validate_current_impl_seal(cwd, role_cfg)
        patch_path = Path(str(patch_value)).expanduser()
        if not patch_path.is_absolute():
            patch_path = Path(cwd) / patch_path
        patch_path = patch_path.resolve(strict=True)
        changed_paths = _patch_changed_paths(cwd, patch_path)
        violations = [
            str(path)
            for path in changed_paths
            if not bb.validate_write_scope(role_cfg.id, Path(cwd) / path)
        ]
        if violations:
            raise ValueError(
                "isolated patch exceeds write_scope: " + ", ".join(violations)
            )
        check = subprocess.run(
            ["git", "apply", "--check", str(patch_path)],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if check.returncode != 0:
            raise ValueError(
                "isolated patch does not apply cleanly: "
                + (check.stderr.strip() or "git apply --check failed")
            )
        if role_cfg.isolated_omp:
            _validate_current_impl_seal(cwd, role_cfg)
        applied = subprocess.run(
            ["git", "apply", str(patch_path)],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if applied.returncode != 0:
            raise ValueError(
                "isolated patch apply failed: "
                + (applied.stderr.strip() or "git apply failed")
            )
        result.metadata["write_scope_validation"] = {
            "valid": True,
            "applied": True,
            "changed_paths": [str(path) for path in changed_paths],
            "selected_tasks": list(role_cfg.selected_task_ids),
        }
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
        result.returncode = 2
        result.stderr = (
            f"{result.stderr}\nwrite_scope_validation_failed: {exc}".strip()
        )
        result.metadata["write_scope_validation"] = {
            "valid": False,
            "applied": False,
            "error": str(exc),
        }


def _save_worker_output(bb: Blackboard, turn: int, role: str, result: AgentResult) -> None:
    """Save worker output to discussion files.

    Always writes a findings JSON — including for failures, timeouts, and empty output —
    so that termination logic never sees an empty turn as 'no_findings'.
    """
    # Save raw markdown output
    if result.stdout:
        bb.write_discussion(turn, role, result.stdout, fmt="md")

    # Save parsed findings JSON — always write something so termination logic works
    if result.timed_out:
        bb.write_findings(turn, role, {
            "conclusion": "FAIL",
            "findings": [{"severity": "CRITICAL", "category": "worker_timeout",
                          "location": role, "description": f"Worker '{role}' timed out",
                          "suggestion": "Increase timeout or simplify task"}],
        })
    elif not result.stdout.strip():
        # Empty output (regardless of exit code) → no review produced
        bb.write_findings(turn, role, {
            "conclusion": "FAIL",
            "findings": [{"severity": "CRITICAL", "category": "worker_empty_output",
                          "location": role, "description": f"Worker '{role}' produced no output",
                          "suggestion": "Check provider availability and prompt"}],
        })
    elif result.parse_error:
        bb.write_findings(turn, role, {
            "conclusion": "FAIL",
            "findings": [{"severity": "CRITICAL", "category": "worker_parse_error",
                          "location": role, "description": f"Worker '{role}' output is not valid JSON",
                          "suggestion": "Check protocol output format instructions"}],
        })
    elif isinstance(result.parsed, dict):
        bb.write_findings(turn, role, result.parsed)
    elif result.parsed is not None:
        bb.write_findings(turn, role, {
            "conclusion": "FAIL",
            "findings": [{"severity": "CRITICAL", "category": "worker_invalid_json_object",
                          "location": role, "description": f"Worker '{role}' output is not a JSON object",
                          "suggestion": "Check protocol output format instructions"}],
            "raw_output": result.stdout[:2000],
        })
    else:
        # Has stdout but no parsed JSON and no parse_error flag — wrap as non-blocking
        bb.write_findings(turn, role, {
            "conclusion": "PASS" if result.ok else "FAIL",
            "findings": [],
            "raw_output": result.stdout[:2000],
        })


def _gather_discussion_context(bb: Blackboard, turn: int, current_role: str) -> str:
    """Gather discussion files from earlier workers in this turn (sequential mode)."""
    lines: list[str] = []
    for json_path in sorted(bb.discussion_dir.glob(f"turn-{turn}-*.json")):
        # Only include files from OTHER roles (exact match, not substring)
        file_role = json_path.stem.replace(f"turn-{turn}-", "")
        if file_role == current_role:
            continue
        try:
            import json
            data = json.loads(json_path.read_text(encoding="utf-8"))
            conclusion = data.get("conclusion", "")
            findings = data.get("findings", [])
            role_name = json_path.stem.replace(f"turn-{turn}-", "")
            lines.append(f"### {role_name}: {conclusion}")
            for f in findings[:5]:  # limit to top 5
                if isinstance(f, dict):
                    sev = f.get("severity", "")
                    desc = f.get("description", "")
                    lines.append(f"  - [{sev}] {desc}")
        except (ValueError, OSError):
            continue
    return "\n".join(lines)


# --- Provider Resolution ---


def _resolve_provider(registry, provider_name: str, *, effort: str | None = None, codex_reasoning: str | None = None):
    """Resolve a provider from the registry by name. Returns None if unavailable."""
    # Map team config provider names to registry names
    _PROVIDER_MAP = {
        "claude-code": "claude-code",
        "claude": "claude-code",
        "codex": "codex",
        "claude:sonnet": "claude:sonnet",
        "sonnet": "claude:sonnet",
        "opus": "claude-code",
    }
    registry_name = _PROVIDER_MAP.get(provider_name, provider_name)

    try:
        if registry.supports(registry_name):
            provider = registry.get(registry_name)
            if effort and hasattr(provider, "effort"):
                provider.effort = effort
            if codex_reasoning and hasattr(provider, "reasoning_effort"):
                provider.reasoning_effort = codex_reasoning
            return provider
    except Exception:
        pass
    return None


def _load_team_protocol(role: str) -> str:
    """Load protocol for a team worker role.

    Search order:
    1. agents/{agent_name}.md — new unified agent definitions (body only)
    2. multi-agent/protocols/{role}.md — legacy protocol files
    3. Minimal built-in fallback
    """
    # 1. Try agent definition
    try:
        from awf.core.spec_loader import resolve_agent_for_role, load_agent_instructions
        agent_name = resolve_agent_for_role(role)
        if agent_name:
            content = load_agent_instructions(agent_name)
            if content.strip():
                return content.strip()
    except (FileNotFoundError, ValueError, ImportError):
        pass

    # 2. Try legacy protocol file
    try:
        from awf.core.spec_loader import load_skill_resource
        content = load_skill_resource("multi-agent", "protocols", role)
        if isinstance(content, str) and content.strip():
            return content.strip()
    except (FileNotFoundError, ValueError):
        pass

    # 3. Try external fallback prompt
    from awf.core.spec_loader import load_prompt_optional
    fallback = load_prompt_optional("multi-agent", "team-worker-fallback", role=role)
    if fallback:
        return fallback
    return f"당신은 '{role}' 역할의 워커입니다. 결과를 JSON으로 반환하세요.\n"


# --- Verdict Computation ---


def _compute_verdict(
    findings: list[TeamFinding],
    stop_reason: str,
    final_turn: int,
    max_turns: int,
) -> tuple[str, str]:
    """Compute final team verdict from all findings.

    Uses the same deterministic rules as multi_agent.judge():
    1. Any CRITICAL/HIGH → FAIL
    2. MAJOR/MEDIUM >= 2 (deduped) → FAIL
    3. Timeout → FAIL
    4. Otherwise → PASS
    """
    if stop_reason == "timeout":
        return "FAIL", f"team timed out at turn {final_turn}"

    if any(f.is_critical for f in findings):
        return "FAIL", "critical findings remain after all turns"

    # Dedup by (category, location)
    _severity_rank = {"CRITICAL": 4, "HIGH": 3, "MAJOR": 2, "MEDIUM": 1, "LOW": 0, "INFO": 0}
    seen: dict[str, str] = {}
    for f in findings:
        key = f"{f.category}:{f.location}"
        existing = seen.get(key)
        if existing is None or _severity_rank.get(f.severity, 0) > _severity_rank.get(existing, 0):
            seen[key] = f.severity
    major = sum(1 for sev in seen.values() if sev in {"MAJOR", "MEDIUM"})
    if major >= 2:
        return "FAIL", f"major findings ({major}) remain after {final_turn} turn(s)"

    if stop_reason == "max_turns":
        # Reached max turns but no critical/major threshold → cautious PASS
        return "PASS", f"completed {max_turns} turn(s), no blocking findings"

    return "PASS", f"clean after {final_turn} turn(s) ({stop_reason})"


def _latest_worker_payloads(bb: Blackboard, turn: int) -> list[dict[str, Any]]:
    if turn <= 0:
        return []
    payloads: list[dict[str, Any]] = []
    for path in sorted(bb.discussion_dir.glob(f"turn-{turn}-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _metric(payload: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = payload.get(key)
    if isinstance(value, dict):
        return value
    phase_metrics = payload.get("phase_metrics")
    if isinstance(phase_metrics, dict):
        value = phase_metrics.get(key)
        if isinstance(value, dict):
            return value
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _collect_strings(
    payloads: list[dict[str, Any]],
    key: str,
) -> list[str]:
    values: list[str] = []
    for payload in payloads:
        raw = payload.get(key)
        if not isinstance(raw, list):
            continue
        for item in raw:
            text = str(item).strip()
            if text and text not in values:
                values.append(text)
    return values


def _review_coverage(
    payloads: list[dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    metrics = [metric for payload in payloads if (metric := _metric(payload, "coverage"))]
    percentages = [
        value
        for metric in metrics
        if (value := _number(metric.get("percentage"))) is not None
    ]
    totals = [
        int(value)
        for metric in metrics
        if (value := _number(metric.get("total_requirements"))) is not None
    ]
    mapped = [
        int(value)
        for metric in metrics
        if (value := _number(metric.get("mapped_requirements"))) is not None
    ]
    gaps: list[str] = []
    for metric in metrics:
        for item in metric.get("gaps", []) if isinstance(metric.get("gaps"), list) else []:
            text = str(item).strip()
            if text and text not in gaps:
                gaps.append(text)
    complete = bool(metrics) and len(percentages) == len(metrics)
    if not complete:
        gaps.append("one or more team workers did not report review coverage")
    return (
        {
            "total_requirements": max(totals, default=0),
            "mapped_requirements": min(mapped, default=0),
            "percentage": min(percentages, default=0.0) if complete else 0.0,
            "gaps": gaps,
            "complete": complete,
        },
        complete,
    )


def _verify_metrics(
    payloads: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], bool]:
    scopes = [metric for payload in payloads if (metric := _metric(payload, "scope"))]
    compliances = [
        metric for payload in payloads if (metric := _metric(payload, "compliance"))
    ]
    qualities = [metric for payload in payloads if (metric := _metric(payload, "quality"))]
    violations = [
        int(value)
        for metric in scopes
        if (value := _number(metric.get("violations"))) is not None
    ]
    failures = [
        int(value)
        for metric in compliances
        if (value := _number(metric.get("fail"))) is not None
    ]
    percentages = [
        value
        for metric in compliances
        if (value := _number(metric.get("percentage"))) is not None
    ]
    critical = [
        int(value)
        for metric in qualities
        if (value := _number(metric.get("critical"))) is not None
    ]
    complete = (
        bool(payloads)
        and len(scopes) == len(payloads)
        and len(compliances) == len(payloads)
        and len(qualities) == len(payloads)
        and len(violations) == len(scopes)
        and len(failures) == len(compliances)
        and len(percentages) == len(compliances)
        and len(critical) == len(qualities)
    )
    return (
        {"violations": max(violations, default=0), "complete": complete},
        {
            "fail": max(failures, default=0),
            "percentage": min(percentages, default=0.0) if complete else 0.0,
            "complete": complete,
        },
        {"critical": max(critical, default=0), "complete": complete},
        complete,
    )


def _build_combined_output(
    bb: Blackboard,
    findings: list[TeamFinding],
    *,
    phase: str,
    turn: int,
    verdict: str,
) -> str:
    """Build a gate-compatible structured result from the latest team turn."""
    payloads = _latest_worker_payloads(bb, turn)
    risks = _collect_strings(payloads, "risks")
    output: dict[str, Any] = {
        "conclusion": verdict,
        "findings": [finding.to_dict() for finding in findings],
        "evidence": _collect_strings(payloads, "evidence"),
        "risks": risks,
        "action_items": _collect_strings(payloads, "action_items"),
        "team_summary": bb.summary(),
    }
    if phase == "review":
        coverage, complete = _review_coverage(payloads)
        output["coverage"] = coverage
        if not complete:
            risks.append("review coverage is incomplete; the gate must fail closed")
    elif phase == "verify":
        scope, compliance, quality, complete = _verify_metrics(payloads)
        output.update(scope=scope, compliance=compliance, quality=quality)
        if not complete:
            risks.append("verify metrics are incomplete; the gate must fail closed")
    return json.dumps(output, ensure_ascii=False, indent=2)


# --- Logging & Events ---


def _log(msg: str) -> None:
    """Print team runner status to stderr."""
    print(f"  [team] {msg}", file=sys.stderr)


def _emit_turn_event(
    processor, phase: str, turn: int, action: str, *, data: dict[str, Any] | None = None,
) -> None:
    """Emit turn start/complete events if processor is available."""
    if not processor:
        return
    from awf.core.events import EventType
    event_type = EventType.TEAM_TURN_STARTED if action == "started" else EventType.TEAM_TURN_COMPLETED
    event_data = {"phase": phase, "turn": turn, **(data or {})}
    processor.emit(
        event_type=event_type,
        task_id=f"team-{phase}",
        source="team_runner",
        data=event_data,
    )


def _emit_worker_event(
    processor, phase: str, turn: int, role: str, action: str, *, data: dict[str, Any] | None = None,
) -> None:
    """Emit worker spawn/complete events if processor is available."""
    if not processor:
        return
    from awf.core.events import EventType
    event_type = EventType.WORKER_SPAWNED if action == "spawned" else EventType.WORKER_COMPLETED
    event_data = {"worker_id": f"{role}-t{turn}", "role": role, "turn": turn, **(data or {})}
    processor.emit(
        event_type=event_type,
        task_id=f"team-{phase}",
        source="team_runner",
        data=event_data,
    )
