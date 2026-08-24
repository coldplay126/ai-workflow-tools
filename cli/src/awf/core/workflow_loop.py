from __future__ import annotations

from typing import Optional

# --- Error classification per SKILL.md §Error Classification ---

ERROR_TYPES = {
    "format_error": {"recoverable": True, "action": "retry", "backoff_sec": 0},
    "timeout": {"recoverable": True, "action": "retry", "backoff_sec": 10},
    "rate_limited": {"recoverable": True, "action": "retry", "backoff_sec": 60},
    "budget_exceeded": {"recoverable": False, "action": "abort", "backoff_sec": 0},
    "provider_unavailable": {"recoverable": True, "action": "fallback", "backoff_sec": 5},
    "permission_denied": {"recoverable": False, "action": "abort", "backoff_sec": 0},
    "invalid_state": {"recoverable": False, "action": "abort", "backoff_sec": 0},
    "unknown": {"recoverable": True, "action": "escalate_user", "backoff_sec": 0},
}


def classify_error(error: str | Exception) -> dict:
    """Classify an error string/exception into a typed error with recovery action."""
    msg = str(error).lower()
    if "timeout" in msg or "timed out" in msg:
        return {**ERROR_TYPES["timeout"], "type": "timeout"}
    if "rate" in msg and "limit" in msg:
        return {**ERROR_TYPES["rate_limited"], "type": "rate_limited"}
    if "budget" in msg or "token limit" in msg or "context length" in msg:
        return {**ERROR_TYPES["budget_exceeded"], "type": "budget_exceeded"}
    if "format" in msg or "json" in msg or "parse" in msg:
        return {**ERROR_TYPES["format_error"], "type": "format_error"}
    if "permission" in msg or "denied" in msg or "forbidden" in msg:
        return {**ERROR_TYPES["permission_denied"], "type": "permission_denied"}
    if "not found" in msg or "unavailable" in msg or "connection" in msg:
        return {**ERROR_TYPES["provider_unavailable"], "type": "provider_unavailable"}
    if "invalid state" in msg or "missing workflow" in msg:
        return {**ERROR_TYPES["invalid_state"], "type": "invalid_state"}
    return {**ERROR_TYPES["unknown"], "type": "unknown"}


def _state_deps():
    from awf.core.state import (
        PHASE_GATE,
        PHASE_ORDER,
        _clear_phase_runtime_markers,
        _initial_skipped_gate_state,
        _now_iso,
        _save_workflow_state,
        load_workflow_state,
    )

    return {
        "PHASE_GATE": PHASE_GATE,
        "PHASE_ORDER": PHASE_ORDER,
        "clear_runtime": _clear_phase_runtime_markers,
        "initial_gate_state": _initial_skipped_gate_state,
        "now_iso": _now_iso,
        "save_state": _save_workflow_state,
        "load_state": load_workflow_state,
    }


def _record_planning_options_marker(
    state: dict,
    deps: dict,
    *,
    artifact_hash: str,
    decision_id: str,
    option_id: str,
    action: str,
) -> None:
    state["planningOptions"] = {
        "artifactHash": artifact_hash,
        "decision": decision_id,
        "option": option_id,
        "appliedAt": deps["now_iso"](),
        "action": action,
    }


def record_planning_option_selection(
    explicit_root: Optional[str],
    *,
    artifact_hash: str,
    decision_id: str,
    option_id: str,
    action: str,
) -> dict:
    """Persist a sanitized selection marker when Plan remains deciding."""

    deps = _state_deps()
    state = deps["load_state"](explicit_root)
    _record_planning_options_marker(
        state,
        deps,
        artifact_hash=artifact_hash,
        decision_id=decision_id,
        option_id=option_id,
        action=action,
    )
    deps["save_state"](explicit_root, state)
    return state


def record_phase_escape(
    explicit_root: Optional[str],
    phase: str,
    *,
    provider: str,
    escape: dict,
) -> dict:
    deps = _state_deps()
    state = deps["load_state"](explicit_root)
    phases = state.setdefault("phases", {})
    phase_state = phases.setdefault(phase, {"status": "pending", "retries": 0})
    phase_state["status"] = "escaped"
    phase_state["escapedAt"] = deps["now_iso"]()
    phase_state["provider"] = provider
    phase_state["escapeReason"] = str(escape.get("reason", "") or "")
    phase_state["escapeSummary"] = str(escape.get("summary", "") or "")
    if "recommended_action" in escape:
        phase_state["recommendedAction"] = str(escape.get("recommended_action", "") or "")

    loop = state.setdefault("loop", {})
    loop["lastEscape"] = {
        "phase": phase,
        "provider": provider,
        "reason": str(escape.get("reason", "") or ""),
        "summary": str(escape.get("summary", "") or ""),
        "recommendedAction": str(escape.get("recommended_action", "") or ""),
        "escapedAt": phase_state["escapedAt"],
    }
    state["currentPhase"] = phase

    history = state.setdefault("history", [])
    history.append(
        {
            "phase": phase,
            "action": "escaped",
            "timestamp": deps["now_iso"](),
            "details": (
                f"provider={provider} "
                f"reason={phase_state.get('escapeReason') or 'unknown'} "
                f"action={phase_state.get('recommendedAction') or 'n/a'}"
            ),
        }
    )
    deps["save_state"](explicit_root, state)
    return state


def record_orchestrator_decision(
    explicit_root: Optional[str],
    phase: str,
    *,
    decision: str,
    reason: str,
    replan_target: Optional[str] = None,
) -> dict:
    deps = _state_deps()
    state = deps["load_state"](explicit_root)
    phases = state.setdefault("phases", {})
    phase_state = phases.setdefault(phase, {"status": "pending", "retries": 0})
    phase_state["status"] = "deciding"
    phase_state["decision"] = decision
    phase_state["decisionAt"] = deps["now_iso"]()
    phase_state["decisionReason"] = reason
    if replan_target:
        phase_state["replanTarget"] = replan_target
    state["currentPhase"] = phase

    loop = state.setdefault("loop", {})
    loop["pendingDecision"] = {
        "phase": phase,
        "decision": decision,
        "reason": reason,
        "replanTarget": replan_target,
        "recordedAt": phase_state["decisionAt"],
    }

    history = state.setdefault("history", [])
    history.append(
        {
            "phase": phase,
            "action": "deciding",
            "timestamp": deps["now_iso"](),
            "details": (
                f"decision={decision} "
                f"reason={reason or 'n/a'} "
                f"replan_target={replan_target or 'n/a'}"
            ),
        }
    )
    deps["save_state"](explicit_root, state)
    return state


def continue_workflow(
    explicit_root: Optional[str],
    phase: str,
    *,
    planning_options: Optional[tuple[str, str, str]] = None,
) -> dict:
    deps = _state_deps()
    state = deps["load_state"](explicit_root)
    phases = state.setdefault("phases", {})
    phase_state = phases.setdefault(phase, {"status": "pending", "retries": 0})
    deps["clear_runtime"](phase_state)
    phase_state["status"] = "in_progress"
    phase_state["startedAt"] = deps["now_iso"]()
    state["currentPhase"] = phase

    loop = state.setdefault("loop", {})
    pending_decision = loop.get("pendingDecision")
    decision_reason = ""
    if isinstance(pending_decision, dict) and pending_decision.get("phase") == phase:
        decision_reason = str(pending_decision.get("reason", "") or "")
        loop.pop("pendingDecision", None)
    loop_history = loop.setdefault("history", [])
    loop_history.append(
        {
            "fromPhase": phase,
            "toPhase": None,
            "decision": "continue",
            "reason": decision_reason,
            "at": deps["now_iso"](),
        }
    )

    history = state.setdefault("history", [])
    history.append(
        {
            "phase": phase,
            "action": "continued",
            "timestamp": deps["now_iso"](),
            "details": "workflow resumed from deciding state",
        }
    )
    if planning_options is not None:
        artifact_hash, decision_id, option_id = planning_options
        _record_planning_options_marker(
            state,
            deps,
            artifact_hash=artifact_hash,
            decision_id=decision_id,
            option_id=option_id,
            action="continued",
        )
    deps["save_state"](explicit_root, state)
    return state


def abort_workflow(explicit_root: Optional[str], phase: str) -> dict:
    deps = _state_deps()
    state = deps["load_state"](explicit_root)
    phases = state.setdefault("phases", {})
    phase_state = phases.setdefault(phase, {"status": "pending", "retries": 0})
    phase_state["status"] = "aborted"
    phase_state["abortedAt"] = deps["now_iso"]()
    state["currentPhase"] = "aborted"

    loop = state.setdefault("loop", {})
    pending_decision = loop.get("pendingDecision")
    decision_reason = ""
    if isinstance(pending_decision, dict) and pending_decision.get("phase") == phase:
        decision_reason = str(pending_decision.get("reason", "") or "")
        loop.pop("pendingDecision", None)
    loop_history = loop.setdefault("history", [])
    loop_history.append(
        {
            "fromPhase": phase,
            "toPhase": None,
            "decision": "abort",
            "reason": decision_reason,
            "at": deps["now_iso"](),
        }
    )

    history = state.setdefault("history", [])
    history.append(
        {
            "phase": phase,
            "action": "aborted",
            "timestamp": deps["now_iso"](),
            "details": "workflow aborted from deciding state",
        }
    )
    deps["save_state"](explicit_root, state)
    return state


def replan_workflow(
    explicit_root: Optional[str],
    phase: str,
    target_phase: str,
    *,
    planning_options: Optional[tuple[str, str, str]] = None,
) -> dict:
    deps = _state_deps()
    phase_order = deps["PHASE_ORDER"]
    phase_gate = deps["PHASE_GATE"]
    state = deps["load_state"](explicit_root)
    if target_phase not in phase_order:
        supported = ", ".join(phase_order)
        raise ValueError(f"Unknown target phase `{target_phase}`. Supported: {supported}")
    # Replan must go backwards (or same phase for retry), never forward
    current_index = phase_order.index(phase) if phase in phase_order else len(phase_order)
    target_index_check = phase_order.index(target_phase)
    if target_index_check > current_index:
        raise ValueError(
            f"Cannot replan forward: {phase} → {target_phase}. "
            "Replan target must be the current phase or earlier."
        )
    phases = state.setdefault("phases", {})
    target_index = phase_order.index(target_phase)
    for phase_name in phase_order[target_index:]:
        phase_state = phases.setdefault(phase_name, {"status": "pending", "retries": 0})
        deps["clear_runtime"](phase_state)
        phase_state["status"] = "pending"
        phase_state.pop("startedAt", None)
        for gate_phase, gate_id in phase_gate.items():
            if gate_phase == phase_name:
                gates = state.setdefault("gates", {})
                gates[gate_id] = deps["initial_gate_state"](gate_id)
    state["currentPhase"] = target_phase

    loop = state.setdefault("loop", {})
    loop["replanCount"] = int(loop.get("replanCount", 0) or 0) + 1
    loop.setdefault("maxReplans", 3)
    pending_decision = loop.get("pendingDecision")
    if isinstance(pending_decision, dict) and pending_decision.get("phase") == phase:
        loop.pop("pendingDecision", None)
    history_items = loop.setdefault("history", [])
    history_items.append(
        {
            "fromPhase": phase,
            "toPhase": target_phase,
            "reason": ((state.get("loop") or {}).get("lastEscape") or {}).get("reason", ""),
            "decision": "replan",
            "at": deps["now_iso"](),
        }
    )

    history = state.setdefault("history", [])
    history.append(
        {
            "phase": phase,
            "action": "replanned",
            "timestamp": deps["now_iso"](),
            "details": f"workflow rolled back to {target_phase}",
        }
    )
    if planning_options is not None:
        artifact_hash, decision_id, option_id = planning_options
        _record_planning_options_marker(
            state,
            deps,
            artifact_hash=artifact_hash,
            decision_id=decision_id,
            option_id=option_id,
            action="replanned",
        )
    deps["save_state"](explicit_root, state)
    return state


def record_workflow_synthesis(
    explicit_root: Optional[str],
    phase: str,
    *,
    selected_provider: str,
    selected_result_path: str,
    judge_passed: bool,
    judge_reasons: list[str],
    synthesis_passed: bool,
    synthesis_reasons: list[str],
    selection_summary: str = "",
    secondary_provider: str | None = None,
) -> dict:
    deps = _state_deps()
    state = deps["load_state"](explicit_root)
    phases = state.setdefault("phases", {})
    phase_state = phases.setdefault(phase, {"status": "pending", "retries": 0})
    phase_state["synthesis"] = {
        "selectedProvider": selected_provider,
        "selectedResultPath": selected_result_path,
        "secondaryProvider": secondary_provider,
        "judgePassed": judge_passed,
        "judgeReasons": list(judge_reasons),
        "synthesisPassed": synthesis_passed,
        "synthesisReasons": list(synthesis_reasons),
        "selectionSummary": selection_summary,
        "recordedAt": deps["now_iso"](),
    }

    history = state.setdefault("history", [])
    history.append(
        {
            "phase": phase,
            "action": "synthesis",
            "timestamp": deps["now_iso"](),
            "details": (
                f"selected={selected_provider} "
                f"judge={'PASS' if judge_passed else 'FAIL'} "
                f"synthesis={'PASS' if synthesis_passed else 'FAIL'} "
                f"basis={selection_summary or 'n/a'}"
            ),
        }
    )
    deps["save_state"](explicit_root, state)
    return state
