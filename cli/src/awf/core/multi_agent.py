"""Multi-agent orchestrator — 5 subagent modes + team pattern routing."""
from __future__ import annotations

import re
import sys
import time
from typing import Any

from awf.core.agent_runner import AgentResult, MultiAgentResult, run_agent


def _record_dispatch_complete_safe(
    cwd: str,
    *,
    backend: str,
    strategy: str,
    mode: str,
    worker_count: int,
    agents: list,
    started_at: float,
) -> None:
    """Persist a dispatch_complete summary keyed off ``cwd`` (project root).

    Failures are swallowed: telemetry must not block multi-agent flows.
    """
    try:
        from awf.core.operational_metrics import record_event
        from awf.core.wiki import log_event

        elapsed = time.monotonic() - started_at
        success_count = sum(1 for a in agents if getattr(a, "ok", False))
        timed_out = sum(1 for a in agents if getattr(a, "timed_out", False))
        provenance_path = None
        if backend == "omp":
            from awf.core.dispatch_provenance import write_omp_dispatch_provenance

            provenance = write_omp_dispatch_provenance(
                cwd,
                strategy=strategy,
                mode=mode,
                agents=agents,
                elapsed_sec=elapsed,
            )
            provenance_path = str(provenance) if provenance is not None else None
        payload = {
            "backend": backend,
            "strategy": strategy,
            "mode": mode,
            "worker_count": worker_count,
            "success_count": success_count,
            "timed_out_count": timed_out,
            "total_seconds": round(elapsed, 2),
            "provenance_path": provenance_path,
        }
        record_event(cwd, "dispatch_complete", payload)
        log_event(
            cwd,
            "dispatch_complete",
            f"mode={mode} backend={backend} strategy={strategy} "
            f"workers={worker_count} success={success_count} elapsed={elapsed:.1f}s",
        )
    except Exception as exc:
        print(f"warning: dispatch_complete record failed: {exc}", file=sys.stderr)


# Only these prefixes are shown — everything else is hidden
_SHOW_PREFIXES = (
    "openai codex", "codex-cli",
    "model:", "provider:",
)


def _is_useful_stderr(line: str) -> bool:
    """Strict allowlist: only show lines that match known agent status patterns."""
    if not line:
        return False
    stripped = line.strip()
    if not stripped:
        return False
    lower = stripped.lower()
    return any(lower.startswith(p) for p in _SHOW_PREFIXES)


def _make_progress_callback(provider_name: str, role: str):
    """Create a progress callback that shows real-time agent activity."""
    last_display = [""]
    last_update = [0.0]

    def callback(elapsed_sec: float, stderr_line: str | None):
        now = time.monotonic()
        if now - last_update[0] < 2.0 and stderr_line is None:
            return
        last_update[0] = now

        label = f"{provider_name}/{role}"
        if stderr_line and _is_useful_stderr(stderr_line):
            clean = stderr_line.strip()
            if clean and clean != last_display[0]:
                last_display[0] = clean
                if len(clean) > 60:
                    clean = clean[:57] + "..."
                if sys.stderr.isatty():
                    line = f"    ⟳ {label} · {elapsed_sec:.0f}s · {clean}"
                    sys.stderr.write(f"\r{line}" + " " * 10 + "\r")
                    sys.stderr.flush()
                else:
                    print(f"    ⟳ {label} · {elapsed_sec:.0f}s · {clean}", file=sys.stderr)
        elif stderr_line is None:
            if sys.stderr.isatty():
                line = f"    ⟳ {label} · {elapsed_sec:.0f}s"
                sys.stderr.write(f"\r{line}" + " " * 20 + "\r")
                sys.stderr.flush()

    return callback


# Default timeouts per mode and role
DEFAULT_TIMEOUTS: dict[str, dict[str, int]] = {
    "quick": {"codex": 45},
    "precise": {"codex": 90, "primary": 120},
    "cross": {"codex": 90, "sonnet": 90},
    "critical": {"codex": 90, "sonnet": 60, "primary": 120},
}

# Security keywords for auto-promotion
SECURITY_KEYWORDS = {
    "iam", "auth", "security", "permission", "rbac", "acl",
    "secret", "credential", "token", "certificate", "ssl", "tls",
    "firewall", "waf", "sg", "security-group", "인증", "인가", "보안",
}

PRODUCTION_KEYWORDS = {
    "deploy", "rollback", "migration", "production", "prod",
    "배포", "롤백", "마이그레이션", "프로덕션", "삭제", "delete", "drop",
}


def run_phase(
    mode: str,
    prompt: str,
    primary_provider,
    registry,
    provider_config: dict,
    *,
    cwd: str,
    phase: str,
    timeout_overrides: dict[str, int] | None = None,
    add_dirs: list[str] | None = None,
    processor=None,
) -> MultiAgentResult:
    """Top-level phase executor — routes to team or subagent based on provider-config.

    Checks provider_config for phase pattern:
    - pattern == "team" → run_team() with team config
    - pattern == "subagent" (default) → run_multi_agent() with mode

    On team failure, falls back to subagent mode if fallback is configured.
    """
    from awf.core.team_config import get_phase_pattern, get_team_config, get_team_fallback

    pattern = get_phase_pattern(provider_config, phase)

    if pattern == "team":
        team_config = get_team_config(provider_config, phase)
        if team_config:
            team_name = team_config.get("name", "unnamed")
            print(f"pattern: team ({team_name})", file=sys.stderr)

            # Run team with exception guard — treat exceptions as team failure
            result: MultiAgentResult | None = None
            try:
                from awf.core.team_runner import run_team

                result = run_team(
                    phase=phase,
                    prompt=prompt,
                    team_config=team_config,
                    registry=registry,
                    cwd=cwd,
                    processor=processor,
                    add_dirs=add_dirs,
                    provider_config=provider_config,
                )
            except Exception as exc:
                print(f"team exception: {exc}", file=sys.stderr)
                result = MultiAgentResult(
                    mode=f"team:{team_name}",
                    judge_verdict="FAIL",
                    judge_reason=f"team exception: {exc}",
                    selected_agent=team_name,
                )

            # If team passed, return directly
            if result.ok:
                return result

            # Team failed — try fallback to subagent
            fallback = get_team_fallback(provider_config, phase)
            if fallback is not None:
                fallback_mode = fallback.get("mode") or mode
                if fallback_mode not in VALID_MODES:
                    fallback_mode = mode
                print(
                    f"team FAIL ({result.judge_reason}) → fallback: {fallback_mode}",
                    file=sys.stderr,
                )
                return run_multi_agent(
                    mode=fallback_mode,
                    prompt=prompt,
                    primary_provider=primary_provider,
                    registry=registry,
                    config=provider_config,
                    cwd=cwd,
                    phase=phase,
                    timeout_overrides=timeout_overrides,
                    add_dirs=add_dirs,
                    processor=processor,
                )

            # No fallback configured — return team failure as-is
            return result

        # team config missing/invalid — fall through to subagent
        print(f"warning: pattern=team but no valid team config for phase '{phase}', using subagent", file=sys.stderr)

    # Default: subagent mode
    return run_multi_agent(
        mode=mode,
        prompt=prompt,
        primary_provider=primary_provider,
        registry=registry,
        config=provider_config,
        cwd=cwd,
        phase=phase,
        timeout_overrides=timeout_overrides,
        add_dirs=add_dirs,
        processor=processor,
    )


_MODE_REQUIRED_PROVIDERS: dict[str, list[str]] = {
    "quick": ["codex"],
    "precise": ["codex"],
    "cross": ["codex", "claude:sonnet"],
    "critical": ["codex", "claude:sonnet"],
}


def validate_providers_for_mode(mode: str, registry) -> list[str]:
    """Pre-check that required providers are available for a mode.

    Returns list of missing provider names. Empty list = all available.
    """
    required = _MODE_REQUIRED_PROVIDERS.get(mode, [])
    missing = []
    for name in required:
        try:
            if not registry.supports(name):
                missing.append(name)
        except Exception:
            missing.append(name)
    return missing


def run_multi_agent(
    mode: str,
    prompt: str,
    primary_provider,
    registry,
    config: dict,
    *,
    cwd: str,
    phase: str | None = None,
    timeout_overrides: dict[str, int] | None = None,
    add_dirs: list[str] | None = None,
    processor=None,
) -> MultiAgentResult:
    """Run multi-agent orchestration in the specified mode.

    Modes:
        solo:     Primary only (passthrough)
        quick:    Codex only (read-only, fast)
        precise:  Codex → Primary (sequential verification)
        cross:    Codex + Sonnet parallel → Judge
        critical: Codex → Sonnet → Primary (sequential, chained input)
    """
    # Pre-flight: check required providers exist for the requested mode
    missing = validate_providers_for_mode(mode, registry)
    if missing:
        print(f"warning: mode '{mode}' requires providers {missing} but they are unavailable; falling back to solo", file=sys.stderr)
        mode = "solo"

    timeouts = {**DEFAULT_TIMEOUTS.get(mode, {}), **(timeout_overrides or {})}

    # Resolve phase-specific effort for secondary providers
    _pm = config.get("phase_models", {}).get(phase, {}) if phase and config else {}
    _effort = _pm.get("effort")
    _codex_re = _pm.get("codex_reasoning")

    if mode == "solo":
        result = _run_solo(primary_provider, prompt, cwd, timeouts, add_dirs)
    elif mode == "quick":
        result = _run_quick(registry, prompt, cwd, timeouts, add_dirs, codex_reasoning=_codex_re)
    elif mode == "precise":
        result = _run_precise(registry, primary_provider, prompt, cwd, timeouts, add_dirs, codex_reasoning=_codex_re)
    elif mode == "cross":
        from awf.core.dispatch import resolve_preference_from_config

        result = _run_cross(
            registry, prompt, cwd, timeouts, add_dirs,
            effort=_effort,
            codex_reasoning=_codex_re,
            dispatch_preference=resolve_preference_from_config(config),
            provider_config=config,
        )
    elif mode == "critical":
        from awf.core.dispatch import resolve_preference_from_config

        result = _run_critical(
            registry, primary_provider, prompt, cwd, timeouts, add_dirs,
            effort=_effort,
            codex_reasoning=_codex_re,
            dispatch_preference=resolve_preference_from_config(config),
            provider_config=config,
        )
    else:
        raise ValueError(
            f"Unknown multi-agent mode '{mode}'. "
            f"Valid modes: {', '.join(sorted(VALID_MODES))}"
        )

    # Print token usage summary
    entries = result.usage_entries()
    if entries:
        from awf.core.usage import summarize_usage
        summary = summarize_usage(entries)
        print(summary.format_text(), file=sys.stderr)

    # Emit events if processor available (including solo mode for observability)
    if processor:
        from awf.core.events import EventType
        task_id = f"multi-agent-{phase or 'default'}"
        for agent in result.agents:
            processor.emit(
                event_type=EventType.AGENT_COMPLETED,
                task_id=task_id,
                source="multi_agent",
                data={
                    "provider": agent.provider_name,
                    "role": agent.role,
                    "elapsed_sec": round(agent.elapsed_sec, 1),
                    "ok": agent.ok,
                    "input_tokens": agent.input_tokens,
                    "output_tokens": agent.output_tokens,
                },
            )
        processor.emit(
            event_type=EventType.JUDGE_VERDICT,
            task_id=task_id,
            source="multi_agent",
            data={
                "mode": mode,
                "verdict": result.judge_verdict,
                "reason": result.judge_reason,
                "selected_agent": result.selected_agent,
                "agent_count": len(result.agents),
            },
        )

    # Auto-save review output to docs/working/codex-reviews/
    if mode != "solo" and result.agents:
        try:
            from awf.core.review_output import save_review_output
            topic = phase or mode
            save_review_output(result, topic=topic, cwd=cwd)
        except Exception as exc:
            print(f"warning: review output save failed: {exc}", file=sys.stderr)

    return result


# Keywords that indicate low-risk tasks suitable for de-escalation
DEESCALATION_KEYWORDS = {
    "readme", "comment", "typo", "documentation", "doc", "문서",
    "주석", "오타", "format", "lint", "style",
}


def auto_promote(mode: str, prompt: str, phase: str | None = None) -> str:
    """Promote or demote mode based on risk detection in prompt."""
    prompt_lower = prompt.lower()

    # De-escalation: if mode was explicitly set high but prompt is low-risk
    if mode in ("cross", "critical"):
        if any(kw in prompt_lower for kw in DEESCALATION_KEYWORDS):
            has_risk = (
                any(kw in prompt_lower for kw in PRODUCTION_KEYWORDS)
                or any(kw in prompt_lower for kw in SECURITY_KEYWORDS)
            )
            if not has_risk:
                print(f"auto_demote: {mode} → solo (low-risk keywords detected)", file=sys.stderr)
                return "solo"

    # Promotion: only from solo
    if mode != "solo":
        return mode
    if any(kw in prompt_lower for kw in PRODUCTION_KEYWORDS):
        print("auto_promote: solo → critical (production keywords detected)", file=sys.stderr)
        return "critical"
    if any(kw in prompt_lower for kw in SECURITY_KEYWORDS):
        print("auto_promote: solo → cross (security keywords detected)", file=sys.stderr)
        return "cross"
    return mode


VALID_MODES = {"solo", "quick", "precise", "cross", "critical"}

# Escalation chain: when a downgraded mode also fails, try the next option
_ESCALATION_FALLBACK: dict[str, list[str]] = {
    "cross": ["precise", "solo"],
    "critical": ["cross", "precise", "solo"],
    "precise": ["solo"],
    "quick": ["solo"],
}


def auto_downgrade(mode: str, agent_results: list[AgentResult]) -> str | None:
    """Return downgraded mode if agents failed, or None to keep current mode.

    Uses a fallback chain instead of always dropping to solo,
    preserving multi-agent verification when possible.
    """
    if not agent_results:
        return "solo"
    all_failed = all(r.timed_out or r.parse_error or not r.stdout.strip() for r in agent_results)
    if all_failed:
        reason = "all agents failed"
        if any(r.timed_out for r in agent_results):
            reason = "timeout"
        elif any(not r.stdout.strip() for r in agent_results):
            reason = "empty response (file structure discovery failure)"

        fallback_chain = _ESCALATION_FALLBACK.get(mode, ["solo"])
        target = fallback_chain[0] if fallback_chain else "solo"
        print(f"auto_downgrade: {mode} → {target} ({reason})", file=sys.stderr)
        return target
    return None


_EVIDENCE_SCORE_THRESHOLD = 3
_CONFIDENCE_POINTS = {"high": 2, "medium": 1}
_EMPTY_EVIDENCE = {"", "?", "-", "[]", "{}", "n/a", "na", "none", "null", "unknown"}
_REPRODUCIBILITY_RE = re.compile(
    r"(?:\b[\w./\\-]+:\d+\b|"
    r"\b(?:pytest|unittest|npm\s+test|cargo\s+test|go\s+test)\b|"
    r"\b\d+\s+(?:passed|failed)\b|"
    r"\b(?:returncode|exit(?:\s+code)?)\s*[:=]?\s*-?\d+\b)",
    re.IGNORECASE,
)
_REPRODUCIBILITY_KEYS = {
    "command", "exit_code", "returncode", "test", "test_result",
}


def _agent_findings(agent: AgentResult) -> list[dict[str, Any]]:
    parsed = agent.parsed
    if not isinstance(parsed, dict):
        return []
    findings = parsed.get("findings", [])
    if not isinstance(findings, list):
        return []
    return [finding for finding in findings if isinstance(finding, dict)]


def _agent_conclusion(agent: AgentResult) -> str:
    parsed = agent.parsed
    if not isinstance(parsed, dict):
        return ""
    return str(parsed.get("conclusion", "")).upper()


def _evidence_text(value: Any) -> str:
    """Flatten evidence-bearing values while ignoring envelope identifiers."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return " ".join(
            _evidence_text(item)
            for key, item in value.items()
            if str(key).lower() not in {"id", "kind", "severity", "category", "confidence"}
        ).strip()
    if isinstance(value, (list, tuple)):
        return " ".join(_evidence_text(item) for item in value).strip()
    if value is None:
        return ""
    return str(value).strip()


def _has_evidence(*values: Any) -> bool:
    return any(
        text.lower() not in _EMPTY_EVIDENCE
        for value in values
        if (text := _evidence_text(value))
    )


def _has_reproducibility(value: Any) -> bool:
    if isinstance(value, dict):
        if any(
            str(key).lower() in _REPRODUCIBILITY_KEYS and _has_evidence(item)
            for key, item in value.items()
        ):
            return True
        return any(_has_reproducibility(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_reproducibility(item) for item in value)
    return bool(_REPRODUCIBILITY_RE.search(_evidence_text(value)))


def _confidence_points(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        confidence = float(value)
        if confidence > 1:
            confidence /= 100
        return 2 if confidence >= 0.8 else 1 if confidence >= 0.5 else 0
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return _CONFIDENCE_POINTS.get(normalized, 0)


def _failing_evidence_score(agent: AgentResult) -> tuple[int, bool, bool, str]:
    """Return score, execution validity, grounding, and a transparent breakdown."""
    parsed = agent.parsed if isinstance(agent.parsed, dict) else {}
    top_evidence = parsed.get("evidence")
    top_confidence = parsed.get("confidence")
    valid = agent.ok and not agent.parse_error
    best = (1 if valid else 0, 0, 0, 0)

    candidates = _agent_findings(agent) or [{}]
    for finding in candidates:
        confidence = max(
            _confidence_points(finding.get("confidence")),
            _confidence_points(top_confidence),
        )
        evidence_values = (
            finding.get("location"),
            finding.get("locations"),
            finding.get("evidence"),
            top_evidence,
        )
        has_evidence = _has_evidence(*evidence_values)
        reproducible = _has_reproducibility(
            (
                *evidence_values,
                finding.get("description"),
                finding.get("summary"),
            )
        )
        candidate = (
            1 if valid else 0,
            confidence,
            1 if has_evidence else 0,
            1 if reproducible else 0,
        )
        if sum(candidate) > sum(best):
            best = candidate

    score = sum(best)
    grounded = bool(best[2] or best[3])
    breakdown = (
        f"valid={best[0]},confidence={best[1]},"
        f"evidence={best[2]},reproducible={best[3]}"
    )
    return score, valid, grounded, breakdown


def judge(agents: list[AgentResult]) -> tuple[str, str]:
    """Apply deterministic evidence-aware Judge Rules v2."""
    if not agents:
        return "FAIL", "no agent results"

    # Rule 1: CRITICAL/HIGH is fail-closed, independent of execution validity.
    for agent in agents:
        if any(
            str(finding.get("severity", "")).upper() in {"CRITICAL", "HIGH"}
            for finding in _agent_findings(agent)
        ):
            return "FAIL", f"critical finding from {agent.provider_name} ({agent.role})"

    # Rule 2: Two distinct MAJOR/MEDIUM findings are fail-closed.
    severity_rank = {"CRITICAL": 4, "HIGH": 3, "MAJOR": 2, "MEDIUM": 1, "LOW": 0}
    seen_findings: dict[str, str] = {}
    for agent in agents:
        for finding in _agent_findings(agent):
            severity = str(finding.get("severity", "")).upper()
            category = str(finding.get("category", ""))
            location = str(finding.get("location", ""))
            key = f"{category}:{location}"
            existing = seen_findings.get(key)
            if existing is None or severity_rank.get(severity, 0) > severity_rank.get(existing, 0):
                seen_findings[key] = severity
    total_major = sum(
        1 for severity in seen_findings.values()
        if severity in {"MAJOR", "MEDIUM"}
    )
    if total_major >= 2:
        return "FAIL", f"major findings total {total_major} >= 2 (after dedup)"

    # Rule 3: Resolve PASS/FAIL disagreement from the strongest FAIL evidence.
    conclusions: dict[str, list[str]] = {}
    failing_agents: list[AgentResult] = []
    for agent in agents:
        conclusion = _agent_conclusion(agent)
        if "PASS" in conclusion:
            conclusions.setdefault("PASS", []).append(agent.provider_name)
        elif "FAIL" in conclusion:
            conclusions.setdefault("FAIL", []).append(agent.provider_name)
            failing_agents.append(agent)
    if len(conclusions) > 1:
        strongest_agent = failing_agents[0]
        (
            strongest_score,
            strongest_valid,
            strongest_grounded,
            strongest_breakdown,
        ) = _failing_evidence_score(strongest_agent)
        for agent in failing_agents[1:]:
            score, valid, grounded, breakdown = _failing_evidence_score(agent)
            if (valid, grounded, score) > (
                strongest_valid,
                strongest_grounded,
                strongest_score,
            ):
                strongest_agent = agent
                strongest_score = score
                strongest_valid = valid
                strongest_grounded = grounded
                strongest_breakdown = breakdown

        disagreement = (
            f"PASS={conclusions.get('PASS', [])}, "
            f"FAIL={conclusions.get('FAIL', [])}"
        )
        if (
            strongest_valid
            and strongest_grounded
            and strongest_score >= _EVIDENCE_SCORE_THRESHOLD
        ):
            return "FAIL", (
                f"grounded disagreement: {strongest_agent.provider_name} "
                f"FAIL evidence score {strongest_score}/5 "
                f"({strongest_breakdown}); {disagreement}"
            )
        return "ESCALATE", (
            f"revalidation_required: weak or invalid FAIL evidence from "
            f"{strongest_agent.provider_name} scored {strongest_score}/5 "
            f"({strongest_breakdown}; threshold={_EVIDENCE_SCORE_THRESHOLD}); "
            f"{disagreement}"
        )

    # Rule 4: An explicit unanimous FAIL is still a failure. The legacy judge
    # accidentally fell through to PASS when no finding crossed a severity gate.
    if failing_agents and "PASS" not in conclusions:
        return "FAIL", "all failing agents agree"

    invalid_agents = [
        agent
        for agent in agents
        if not agent.ok or agent.parse_error or not _agent_conclusion(agent)
    ]
    if "PASS" in conclusions and invalid_agents:
        invalid_names = [agent.provider_name for agent in invalid_agents]
        return "ESCALATE", (
            "revalidation_required: incomplete validation from "
            f"{invalid_names}"
        )
    if not conclusions and invalid_agents:
        return "FAIL", "no valid agent conclusion"

    # Rule 5: Preserve the existing detailed-agent preference.
    if len(agents) > 1:
        agents_with_findings = [agent for agent in agents if _agent_findings(agent)]
        agents_without = [agent for agent in agents if not _agent_findings(agent)]
        if agents_with_findings and agents_without:
            detailed = agents_with_findings[0]
            if "FAIL" in _agent_conclusion(detailed):
                return "FAIL", (
                    f"detailed agent {detailed.provider_name} concluded FAIL "
                    f"with {len(_agent_findings(detailed))} finding(s)"
                )
            return "PASS", (
                f"detailed agent {detailed.provider_name} found "
                f"{len(_agent_findings(detailed))} minor issue(s) but concluded PASS"
            )

    # Rule 6: Preserve unanimous explicit PASS and legacy unstructured success.
    return "PASS", "all agents agree"


def _print_agent_summary(agent: AgentResult) -> None:
    """Print human-readable summary of what an agent found."""
    icon = "✓" if agent.ok else "✗"
    label = f"{agent.provider_name}/{agent.role}"

    # Conclusion
    conclusion = agent.conclusion
    if not conclusion and agent.stdout:
        # Try to extract first meaningful line
        for line in agent.stdout.splitlines()[:5]:
            stripped = line.strip()
            if stripped and not stripped.startswith("{") and not stripped.startswith('"'):
                conclusion = stripped[:120]
                break

    findings = agent.findings
    critical = sum(1 for f in findings if str(f.get("severity", "")).upper() in {"CRITICAL", "HIGH"})
    major = sum(1 for f in findings if str(f.get("severity", "")).upper() in {"MAJOR", "MEDIUM"})
    minor = sum(1 for f in findings if str(f.get("severity", "")).upper() in {"LOW", "MINOR", "INFO"})

    print(f"  {icon} {label} ({agent.elapsed_sec:.0f}s)", file=sys.stderr)
    if conclusion:
        # Truncate long conclusions
        if len(conclusion) > 100:
            conclusion = conclusion[:97] + "..."
        print(f"    결론: {conclusion}", file=sys.stderr)
    if findings:
        severity_text = []
        if critical:
            severity_text.append(f"critical={critical}")
        if major:
            severity_text.append(f"major={major}")
        if minor:
            severity_text.append(f"minor={minor}")
        print(f"    발견: {len(findings)}건 ({', '.join(severity_text)})", file=sys.stderr)
        # Show top finding
        top = next((f for f in findings if str(f.get("severity", "")).upper() in {"CRITICAL", "HIGH"}), None)
        if not top and findings:
            top = findings[0]
        if top:
            desc = str(top.get("description", top.get("summary", "")))[:100]
            loc = str(top.get("location", top.get("locations", "")))[:60]
            if desc:
                print(f"    주요: {desc}", file=sys.stderr)
                if loc and loc != "[]":
                    print(f"          위치: {loc}", file=sys.stderr)
    elif agent.ok and not findings:
        print(f"    발견: 이슈 없음", file=sys.stderr)


def _print_judge_summary(verdict: str, reason: str, agents: list[AgentResult]) -> None:
    """Print human-readable judge verdict."""
    icon = "✓" if verdict == "PASS" else "✗"
    print(f"  {icon} 판정: {verdict} — {reason}", file=sys.stderr)
    if verdict == "FAIL":
        # Show which agents caused the failure
        for a in agents:
            if a.has_critical:
                print(f"    ⚠ {a.provider_name}/{a.role}: critical 이슈 발견", file=sys.stderr)


def _get_codex_provider(registry, *, reasoning_effort: str | None = None):
    """Get codex provider, return None if unavailable."""
    try:
        if registry.supports("codex"):
            provider = registry.get("codex")
            if reasoning_effort and hasattr(provider, "reasoning_effort"):
                provider.reasoning_effort = reasoning_effort
            return provider
    except Exception:
        pass
    return None


def _get_sonnet_provider(registry, *, effort: str | None = None):
    """Get claude:sonnet provider, return None if unavailable."""
    try:
        if registry.supports("claude:sonnet"):
            provider = registry.get("claude:sonnet")
            if effort and hasattr(provider, "effort"):
                provider.effort = effort
            return provider
    except Exception:
        pass
    return None


_PROTOCOL_CACHE: dict[str, tuple[str, float]] = {}  # role → (content, loaded_at)
_PROTOCOL_CACHE_TTL = 300.0  # 5 minutes


def _load_protocol(role: str) -> str:
    """Load agent instructions for a role.

    Search order:
    1. agents/{agent_name}.md — new unified agent definitions (body only)
    2. multi-agent/protocols/{role}.md — legacy protocol files
    3. Minimal built-in fallback

    Uses TTL-based cache to avoid repeated disk I/O.
    """
    cached = _PROTOCOL_CACHE.get(role)
    if cached is not None:
        content, loaded_at = cached
        if time.monotonic() - loaded_at < _PROTOCOL_CACHE_TTL:
            return content

    # 1. Try agent definition (agents/*.md) — search by role mapping
    try:
        from awf.core.spec_loader import resolve_agent_for_role, load_agent_instructions
        agent_name = resolve_agent_for_role(role)
        if agent_name:
            content = load_agent_instructions(agent_name)
            if content.strip():
                _PROTOCOL_CACHE[role] = (content.strip(), time.monotonic())
                return content.strip()
    except (FileNotFoundError, ValueError, ImportError):
        pass

    # 2. Try legacy protocol file
    try:
        from awf.core.spec_loader import load_skill_resource
        content = load_skill_resource("multi-agent", "protocols", role)
        if isinstance(content, str) and content.strip():
            content = content.strip()
            _PROTOCOL_CACHE[role] = (content, time.monotonic())
            return content
    except (FileNotFoundError, ValueError):
        pass

    # 3. Try external fallback prompt
    try:
        from awf.core.spec_loader import load_prompt
        fallback = load_prompt("multi-agent", "fallback-protocol", role=role)
    except FileNotFoundError:
        fallback = f"분석을 수행하세요. (role: {role})\n"
    print(f"warning: no agent or protocol found for role '{role}', using minimal fallback", file=sys.stderr)
    _PROTOCOL_CACHE[role] = (fallback, time.monotonic())
    return fallback


def _make_slave_prompt(prompt: str, role: str) -> str:
    """Wrap prompt with role-specific instructions for slave agents."""
    protocol = _load_protocol(role)
    return f"{protocol}\n\n{prompt}"


# --- Mode implementations ---

def _run_solo(primary_provider, prompt: str, cwd: str, timeouts: dict, add_dirs: list[str] | None) -> MultiAgentResult:
    """Solo mode: primary only."""
    result = run_agent(
        primary_provider, prompt, "primary", cwd,
        timeout_sec=timeouts.get("primary", 900),
        add_dirs=add_dirs,
    )
    # Check parsed conclusion first, fall back to process exit code
    verdict = "PASS" if result.ok else "FAIL"
    reason = "solo mode"
    if result.conclusion:
        upper = result.conclusion.upper()
        if "FAIL" in upper:
            verdict = "FAIL"
            reason = f"solo: agent concluded FAIL"
        elif "PASS" in upper:
            verdict = "PASS"
            reason = f"solo: agent concluded PASS"

    return MultiAgentResult(
        mode="solo",
        agents=[result],
        judge_verdict=verdict,
        judge_reason=reason,
        selected_agent=result.provider_name,
        combined_output=result.stdout,
    )


def _force_codex_read_only(provider) -> None:
    if hasattr(provider, "set_sandbox"):
        provider.set_sandbox("read-only")


def _run_quick(registry, prompt: str, cwd: str, timeouts: dict, add_dirs: list[str] | None, *, codex_reasoning: str | None = None) -> MultiAgentResult:
    """Quick mode: codex only (read-only, fast)."""
    codex = _get_codex_provider(registry, reasoning_effort=codex_reasoning)
    if not codex:
        print("warning: codex not available for quick mode", file=sys.stderr)
        return MultiAgentResult(
            mode="quick",
            agents=[],
            judge_verdict="FAIL",
            judge_reason="codex provider unavailable — install codex or use a different mode",
        )

    _force_codex_read_only(codex)
    print("mode: quick (codex read-only)", file=sys.stderr)
    result = run_agent(
        codex, _make_slave_prompt(prompt, "speed"), "speed", cwd,
        timeout_sec=timeouts.get("codex", 45),
        require_json=True,
        add_dirs=add_dirs,
    )
    _print_agent_summary(result)

    verdict, reason = ("PASS", "quick mode") if result.ok else ("FAIL", f"codex failed: {result.stderr[:100]}")
    return MultiAgentResult(
        mode="quick",
        agents=[result],
        judge_verdict=verdict,
        judge_reason=reason,
        selected_agent=result.provider_name,
        combined_output=result.stdout,
    )


def _run_precise(registry, primary_provider, prompt: str, cwd: str, timeouts: dict, add_dirs: list[str] | None, *, codex_reasoning: str | None = None) -> MultiAgentResult:
    """Precise mode: codex analysis → primary verification."""
    codex = _get_codex_provider(registry, reasoning_effort=codex_reasoning)
    if not codex:
        print("warning: codex not available for precise mode, running solo", file=sys.stderr)
        return _run_solo(primary_provider, prompt, cwd, timeouts, add_dirs)

    _force_codex_read_only(codex)
    # Track cumulative timeout budget
    total_budget = sum(timeouts.values()) if timeouts else 210
    budget_start = time.monotonic()

    # Step 1: Codex analysis
    print("mode: precise — step 1: codex analysis", file=sys.stderr)
    step1_timeout = timeouts.get("codex", 90)
    codex_result = run_agent(
        codex, _make_slave_prompt(prompt, "precision"), "precision", cwd,
        timeout_sec=step1_timeout,
        require_json=True,
        add_dirs=add_dirs,
        on_progress=_make_progress_callback("codex", "precision"),
    )
    _print_agent_summary(codex_result)

    # Check for downgrade
    downgrade = auto_downgrade("precise", [codex_result])
    if downgrade:
        return _run_solo(primary_provider, prompt, cwd, timeouts, add_dirs)

    # Step 2: Primary verification with codex result
    # Adjust timeout based on remaining budget
    elapsed_so_far = time.monotonic() - budget_start
    remaining_budget = max(30, total_budget - elapsed_so_far)  # minimum 30s
    from awf.core.spec_loader import load_prompt_optional
    verify_template = load_prompt_optional("multi-agent", "precise-verify", codex_result=codex_result.stdout)
    if verify_template:
        verification_prompt = f"{prompt}\n\n{verify_template}"
    else:
        verification_prompt = f"{prompt}\n\n## Codex 분석 결과 (검증 대상)\n\n{codex_result.stdout}\n\n위 Codex 분석 결과를 검증하고 보완하세요.\n"
    print("mode: precise — step 2: primary verification", file=sys.stderr)
    primary_result = run_agent(
        primary_provider, verification_prompt, "primary", cwd,
        timeout_sec=min(timeouts.get("primary", 120), int(remaining_budget)),
        add_dirs=add_dirs,
    )
    _print_agent_summary(primary_result)

    agents = [codex_result, primary_result]
    verdict, reason = judge(agents)

    return MultiAgentResult(
        mode="precise",
        agents=agents,
        judge_verdict=verdict,
        judge_reason=reason,
        selected_agent=primary_result.provider_name,
        combined_output=primary_result.stdout,
    )


def _run_cross(
    registry,
    prompt: str,
    cwd: str,
    timeouts: dict,
    add_dirs: list[str] | None,
    *,
    effort: str | None = None,
    codex_reasoning: str | None = None,
    dispatch_preference: str = "auto",
    provider_config: dict | None = None,
) -> MultiAgentResult:
    """Cross mode: codex + sonnet parallel → judge."""
    from awf.core.dispatch import (
        WorkerSpec,
        resolve_cmux_options_from_config,
        resolve_omp_options_from_config,
        select_dispatch,
    )

    codex = _get_codex_provider(registry, reasoning_effort=codex_reasoning)
    sonnet = _get_sonnet_provider(registry, effort=effort)
    if codex:
        _force_codex_read_only(codex)

    available = []
    if codex:
        available.append(("codex", codex, "plan_conformance", timeouts.get("codex", 90)))
    if sonnet:
        available.append(("sonnet", sonnet, "quality_validation", timeouts.get("sonnet", 90)))

    if len(available) < 2:
        missing = []
        if not codex:
            missing.append("codex")
        if not sonnet:
            missing.append("sonnet")
        print(f"warning: cross mode needs 2 agents, missing: {missing}. Running available.", file=sys.stderr)
        if not available:
            return MultiAgentResult(mode="cross", judge_verdict="FAIL", judge_reason="no agents available")

    specs: list[WorkerSpec] = []
    for name, provider, role, timeout in available:
        specs.append(
            WorkerSpec(
                role=role,
                provider=provider,
                prompt=_make_slave_prompt(prompt, role),
                timeout_sec=timeout,
                require_json=True,
                add_dirs=tuple(add_dirs or ()),
                on_progress=_make_progress_callback(name, role),
            )
        )

    dispatch = select_dispatch(
        worker_count=len(specs),
        estimated_seconds=max(s.expected_seconds() for s in specs),
        preference=dispatch_preference,  # type: ignore[arg-type]
        cwd=cwd,
        options=resolve_cmux_options_from_config(provider_config),
        workers=specs,
        provider_config=provider_config,
        omp_options=resolve_omp_options_from_config(provider_config),
    )
    print(
        f"mode: cross — {len(specs)} agents parallel via {dispatch.name}",
        file=sys.stderr,
    )

    _cross_started_at = time.monotonic()
    agents: list[AgentResult] = list(dispatch.run(specs, cwd=cwd, strategy="parallel"))
    _record_dispatch_complete_safe(
        cwd,
        backend=dispatch.name,
        strategy="parallel",
        mode="cross",
        worker_count=len(specs),
        agents=agents,
        started_at=_cross_started_at,
    )
    for result in agents:
        if sys.stderr.isatty():
            sys.stderr.write("\r" + " " * 100 + "\r")
            sys.stderr.flush()
        _print_agent_summary(result)

    # Sort by provider name for deterministic ordering (prevents non-deterministic judge input)
    agents.sort(key=lambda a: a.provider_name)

    # Check for downgrade
    downgrade = auto_downgrade("cross", agents)
    if downgrade:
        return MultiAgentResult(
            mode="cross",
            agents=agents,
            judge_verdict="FAIL",
            judge_reason="auto_downgrade to solo",
        )

    # Judge
    verdict, reason = judge(agents)
    _print_judge_summary(verdict, reason, agents)

    # Select best agent result
    selected = agents[0]
    if len(agents) > 1:
        # Prefer the one with more findings (more thorough)
        selected = max(agents, key=lambda a: len(a.findings))

    return MultiAgentResult(
        mode="cross",
        agents=agents,
        judge_verdict=verdict,
        judge_reason=reason,
        selected_agent=selected.provider_name,
        combined_output=selected.stdout,
    )


def _run_critical(
    registry,
    primary_provider,
    prompt: str,
    cwd: str,
    timeouts: dict,
    add_dirs: list[str] | None,
    *,
    effort: str | None = None,
    codex_reasoning: str | None = None,
    dispatch_preference: str = "auto",
    provider_config: dict | None = None,
) -> MultiAgentResult:
    """Critical mode: codex → sonnet → primary (sequential, chained).

    Each step's prompt incorporates the prior steps' outputs. Dispatch is
    routed through ``MultiAgentDispatch.run_chained`` so cmux selection can
    keep all three steps on the same set of pinned workers.
    """
    from awf.core.dispatch import (
        ChainedStep,
        WorkerSpec,
        resolve_cmux_options_from_config,
        resolve_omp_options_from_config,
        select_dispatch,
    )

    codex = _get_codex_provider(registry, reasoning_effort=codex_reasoning)
    sonnet = _get_sonnet_provider(registry, effort=effort)
    if codex:
        _force_codex_read_only(codex)

    total_budget = sum(timeouts.values()) if timeouts else 270
    budget_start = time.monotonic()
    summary_printed: set[int] = set()

    def _flush_prior_summaries(prior: list[AgentResult]) -> None:
        for i, a in enumerate(prior):
            if i not in summary_printed:
                _print_agent_summary(a)
                summary_printed.add(i)

    def _remaining_budget() -> int:
        return max(30, int(total_budget - (time.monotonic() - budget_start)))

    def step1_factory(prior: list[AgentResult]) -> "WorkerSpec | None":
        if codex is None:
            print(
                "warning: codex not available for critical step 1, skipping",
                file=sys.stderr,
            )
            return None
        print(
            "mode: critical — step 1: codex precision analysis",
            file=sys.stderr,
        )
        return WorkerSpec(
            role="precision",
            provider=codex,
            prompt=_make_slave_prompt(prompt, "precision"),
            timeout_sec=timeouts.get("codex", 90),
            require_json=True,
            add_dirs=tuple(add_dirs or ()),
            on_progress=_make_progress_callback("codex", "precision"),
        )

    def step2_factory(prior: list[AgentResult]) -> "WorkerSpec | None":
        _flush_prior_summaries(prior)
        if sonnet is None:
            print(
                "warning: sonnet not available for critical step 2, skipping",
                file=sys.stderr,
            )
            return None
        step2_prompt = prompt
        if prior and prior[-1].ok:
            from awf.core.spec_loader import load_prompt_optional

            impact_template = load_prompt_optional(
                "multi-agent", "critical-impact", step1_result=prior[-1].stdout
            )
            if impact_template:
                step2_prompt = f"{prompt}\n\n{impact_template}"
            else:
                step2_prompt = (
                    f"{prompt}\n\n## Step 1 분석 결과 (Codex Precision)\n\n"
                    f"{prior[-1].stdout}\n\n위 분석 결과를 기반으로 영향도를 분석하세요.\n"
                )
        print(
            "mode: critical — step 2: sonnet impact analysis",
            file=sys.stderr,
        )
        return WorkerSpec(
            role="quality_validation",
            provider=sonnet,
            prompt=_make_slave_prompt(step2_prompt, "quality_validation"),
            timeout_sec=min(timeouts.get("sonnet", 60), _remaining_budget()),
            require_json=True,
            on_progress=_make_progress_callback("sonnet", "quality_validation"),
        )

    def step3_factory(prior: list[AgentResult]) -> "WorkerSpec | None":
        _flush_prior_summaries(prior)
        step3_prompt = prompt
        prior_results = [
            f"## Step {i + 1} 결과 ({a.provider_name}, {a.role})\n\n{a.stdout}"
            for i, a in enumerate(prior) if a.ok
        ]
        if prior_results:
            from awf.core.spec_loader import load_prompt_optional

            joined = "\n\n".join(prior_results)
            final_template = load_prompt_optional(
                "multi-agent", "critical-final", prior_results=joined
            )
            if final_template:
                step3_prompt = f"{prompt}\n\n{final_template}"
            else:
                step3_prompt = (
                    f"{prompt}\n\n{joined}\n\n위 분석 결과를 종합하여 최종 판정하세요.\n"
                )
        print(
            "mode: critical — step 3: primary final judgment",
            file=sys.stderr,
        )
        return WorkerSpec(
            role="primary",
            provider=primary_provider,
            prompt=step3_prompt,
            timeout_sec=min(timeouts.get("primary", 120), _remaining_budget()),
            add_dirs=tuple(add_dirs or ()),
        )

    dispatch = select_dispatch(
        worker_count=3,
        estimated_seconds=float(total_budget),
        preference=dispatch_preference,  # type: ignore[arg-type]
        cwd=cwd,
        options=resolve_cmux_options_from_config(provider_config),
        provider_config=provider_config,
        omp_options=resolve_omp_options_from_config(provider_config),
    )
    print(
        f"mode: critical — chained 3 steps via {dispatch.name}",
        file=sys.stderr,
    )

    _critical_started_at = time.monotonic()
    agents = list(
        dispatch.run_chained(
            [
                ChainedStep(role="precision", factory=step1_factory),
                ChainedStep(role="quality_validation", factory=step2_factory),
                ChainedStep(role="primary", factory=step3_factory),
            ],
            cwd=cwd,
        )
    )
    _record_dispatch_complete_safe(
        cwd,
        backend=dispatch.name,
        strategy="chained",
        mode="critical",
        worker_count=3,
        agents=agents,
        started_at=_critical_started_at,
    )

    _flush_prior_summaries(agents)

    if not agents:
        return MultiAgentResult(
            mode="critical",
            judge_verdict="FAIL",
            judge_reason="no agents available",
        )

    primary_result = agents[-1]
    verdict, reason = judge(agents)
    _print_judge_summary(verdict, reason, agents)

    return MultiAgentResult(
        mode="critical",
        agents=agents,
        judge_verdict=verdict,
        judge_reason=reason,
        selected_agent=primary_result.provider_name,
        combined_output=primary_result.stdout,
    )
