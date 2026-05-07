"""TeamRunner — 3-layer agent team orchestrator.

Layer 1 (Python): Deterministic flow — turn ordering, termination, timeout, state.
Layer 2 (Leader): Mission building — task analysis, worker instructions, iteration context.
Layer 3 (Workers): Autonomous execution — read board, write discussion, produce findings.

All worker communication goes through the Blackboard (file-based).
Python evaluates termination deterministically from discussion JSON.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Any

from awf.core.agent_runner import AgentResult, MultiAgentResult, run_agent
from awf.core.blackboard import Blackboard, TeamFinding


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
        combined_output=_build_combined_output(bb, all_findings),
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
) -> list[AgentResult]:
    """Execute all workers for a turn.

    Sequential: one by one (each sees previous worker's output).
    Parallel: all at once via ThreadPoolExecutor.
    """
    if config.execution == "parallel":
        return _execute_parallel(bb, turn, config, registry, cwd,
                                 timeout_sec=timeout_sec, add_dirs=add_dirs,
                                 processor=processor, phase=phase,
                                 effort=effort, codex_reasoning=codex_reasoning)
    return _execute_sequential(bb, turn, config, registry, cwd,
                               timeout_sec=timeout_sec, add_dirs=add_dirs,
                               processor=processor, phase=phase,
                               effort=effort, codex_reasoning=codex_reasoning)


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
) -> list[AgentResult]:
    """Run workers one at a time. Each can read previous workers' output."""
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

        result = run_agent(
            provider,
            worker_prompt,
            role_cfg.id,
            cwd,
            timeout_sec=actual_timeout,
            require_json=True,
            add_dirs=add_dirs,
        )
        results.append(result)

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
) -> list[AgentResult]:
    """Run workers concurrently. Workers see board/ but not each other's discussion."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: list[AgentResult] = []
    futures = {}

    with ThreadPoolExecutor(max_workers=max(len(config.roles), 1)) as pool:
        for role_cfg in config.roles:
            provider = _resolve_provider(registry, role_cfg.provider, effort=effort, codex_reasoning=codex_reasoning)
            if provider is None:
                _log(f"  skip {role_cfg.id}: provider '{role_cfg.provider}' unavailable")
                _save_skipped_worker(bb, turn, role_cfg)
                continue

            worker_prompt = _build_worker_prompt(bb, turn, role_cfg)

            _emit_worker_event(processor, phase, turn, role_cfg.id, "spawned")

            future = pool.submit(
                run_agent,
                provider,
                worker_prompt,
                role_cfg.id,
                cwd,
                timeout_sec=timeout_sec,
                require_json=True,
                add_dirs=add_dirs,
            )
            futures[future] = role_cfg

        for future in as_completed(futures, timeout=timeout_sec + 10):
            role_cfg = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = AgentResult(
                    provider_name=role_cfg.provider,
                    role=role_cfg.id,
                    stdout="",
                    stderr=str(exc),
                    returncode=2,
                    elapsed_sec=0.0,
                )
            results.append(result)
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


def _build_combined_output(bb: Blackboard, findings: list[TeamFinding]) -> str:
    """Build human-readable combined output from team execution."""
    import json

    summary = bb.summary()
    output = {
        "team_summary": summary,
        "findings": [f.to_dict() for f in findings],
    }
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
