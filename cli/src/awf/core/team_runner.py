"""TeamRunner — 3-layer agent team orchestrator.

Layer 1 (Python): Deterministic flow — turn ordering, termination, timeout, state.
Layer 2 (Leader): Mission building — task analysis, worker instructions, iteration context.
Layer 3 (Workers): Autonomous execution — read board, write discussion, produce findings.

All worker communication goes through the Blackboard (file-based).
Python evaluates termination deterministically from discussion JSON.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
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
    try:
        from awf.core.dispatch_provenance import write_omp_dispatch_provenance

        write_omp_dispatch_provenance(
            cwd,
            strategy=strategy,
            mode=f"team:{phase}:turn-{turn}",
            agents=agents,
            elapsed_sec=elapsed_sec,
        )
    except Exception as exc:
        _log(f"  warning: OMP provenance record failed: {exc}")


@dataclass
class RoleConfig:
    """Single role definition within a team."""

    id: str
    provider: str  # e.g., "claude-code", "codex"
    protocol: str = ""  # protocol file name (defaults to id)
    write_scope: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RoleConfig:
        return cls(
            id=data.get("id", ""),
            provider=data.get("provider", "claude-code"),
            protocol=data.get("protocol", "") or data.get("id", ""),
            write_scope=data.get("write_scope", []),
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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TeamConfig:
        roles = [RoleConfig.from_dict(r) for r in data.get("roles", [])]
        return cls(
            name=data.get("name", "team"),
            roles=roles,
            execution=data.get("execution", "sequential"),
            max_turns=data.get("max_turns", 3),
            timeout_sec=data.get("timeout_sec", 600),
            workspace=data.get("workspace", ""),
        )


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

    # Resolve phase-specific effort for providers
    _pm = (provider_config or {}).get("phase_models", {}).get(phase, {})
    _effort = _pm.get("effort")
    _codex_re = _pm.get("codex_reasoning")

    if not config.roles:
        _log(f"team: {config.name} — no roles configured, returning FAIL")
        return MultiAgentResult(
            mode=f"team:{config.name}",
            judge_verdict="FAIL",
            judge_reason="no roles configured in team",
            selected_agent=config.name,
        )

    bb = Blackboard.create(cwd, phase, team_config=team_config)

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

        spec = WorkerSpec(
            role=role_cfg.id,
            provider=provider,
            prompt=worker_prompt,
            timeout_sec=actual_timeout,
            require_json=True,
            add_dirs=tuple(add_dirs or ()),
            isolated=True if role_cfg.write_scope else None,
        )
        dispatch = _resolve_team_dispatch(
            cwd=cwd,
            worker_count=1,
            estimated_seconds=float(actual_timeout),
            provider_config=provider_config,
            workers=[spec],
        )
        result = dispatch.run([spec], cwd=cwd, strategy="sequential")[0]
        _enforce_worker_write_scope(bb, role_cfg, result, cwd)
        results.append(result)
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

    specs = [
        WorkerSpec(
            role=role_cfg.id,
            provider=provider,
            prompt=prompt,
            timeout_sec=timeout_sec,
            require_json=True,
            add_dirs=tuple(add_dirs or ()),
            isolated=True if role_cfg.write_scope else None,
        )
        for role_cfg, provider, prompt in available
    ]
    dispatch = _resolve_team_dispatch(
        cwd=cwd,
        worker_count=len(specs),
        estimated_seconds=float(timeout_sec),
        provider_config=provider_config,
        workers=specs,
    )
    dispatch_started_at = time.monotonic()

    try:
        results = list(dispatch.run(specs, cwd=cwd, strategy="parallel"))
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

    for (role_cfg, _, _), result in zip(available, results):
        _enforce_worker_write_scope(bb, role_cfg, result, cwd)
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
    """Return both source and destination paths touched by a git patch."""
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
        if index + 1 >= len(chunks):
            raise ValueError("cannot inspect isolated patch: incomplete rename paths")
        paths.extend(
            (
                Path(chunks[index].decode("utf-8", errors="strict")),
                Path(chunks[index + 1].decode("utf-8", errors="strict")),
            )
        )
        index += 2
    if not paths:
        raise ValueError("cannot inspect isolated patch: no changed paths")
    return paths


def _enforce_worker_write_scope(
    bb: Blackboard,
    role_cfg: RoleConfig,
    result: AgentResult,
    cwd: str,
) -> None:
    """Validate and apply an isolated OMP patch only within the role's scope."""
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
    elif result.parsed:
        bb.write_findings(turn, role, result.parsed)
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
