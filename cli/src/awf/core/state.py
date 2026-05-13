from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from copy import deepcopy
from datetime import datetime, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Optional

from awf.core.paths import find_repo_root
from awf.core.workflow_loop import (
    abort_workflow,
    continue_workflow,
    record_orchestrator_decision,
    record_phase_escape,
    record_workflow_synthesis,
    replan_workflow,
)
from awf.core.workflow_prompt import (
    build_workflow_prompt,
    save_workflow_prompt,
    save_workflow_result,
)
from awf.core.workflow_status import summarize_workflow_state

# --- Risk-based routing per SKILL.md §Risk-Based Routing ---

HIGH_RISK_PATTERNS = {
    "auth", "authentication", "authorization", "인증", "인가",
    "payment", "결제", "billing",
    "delete", "drop", "삭제", "truncate",
    "migration", "마이그레이션",
    "secret", "credential", "token", "비밀키",
    "infra", "infrastructure", "terraform", "k8s", "kubernetes",
    "production", "prod", "프로덕션",
}


def detect_change_class(concept: str) -> str:
    """Detect change risk class from workflow concept text.

    Returns: 'small', 'standard', or 'high_risk'
    """
    lower = concept.lower()
    if any(pattern in lower for pattern in HIGH_RISK_PATTERNS):
        return "high_risk"
    # Heuristic: longer descriptions suggest more complex changes
    # Use char count for CJK-friendly measurement (Korean/Japanese/Chinese)
    char_count = len(lower)
    if char_count <= 30:
        return "small"
    return "standard"


PHASE_ORDER = ["plan", "review", "approve", "impl", "verify", "test", "done"]
PHASE_GATE = {
    "plan": "G1",
    "review": "G2",
    "approve": "G3",
    "impl": "G4",
    "verify": "G5",
    "test": "G6",
}
MAX_TOTAL_EXECUTIONS = 30

# Phase skip policy per change class (I3)
CHANGE_CLASS_SKIP_PHASES: dict[str, set[str]] = {
    "small": {"review", "approve", "verify"},
    "standard": set(),
    "high_risk": set(),
}

# Risk investment policy per change class (§2: 위험 비례 투자)
# Defines per-phase differentiation: review depth, verify strictness, test range, gate scope.
# Instruction text loaded from wf-orchestrator/prompts/risk-*.md
RISK_INVESTMENT: dict[str, dict[str, dict]] = {
    "small": {
        "review": {"depth": "minimal"},
        "verify": {"strictness": "scope_only", "skip_checks": {"compliance", "quality"}},
        "test": {"range": "related_only"},
    },
    "standard": {
        "review": {"depth": "standard"},
        "verify": {"strictness": "scope_compliance", "skip_checks": {"quality"}},
        "test": {"range": "regression_acceptance"},
    },
    "high_risk": {
        "review": {"depth": "deep"},
        "verify": {"strictness": "full", "skip_checks": set()},
        "test": {"range": "full_with_manual"},
    },
}

# Map risk class keys to prompt file names
_RISK_PROMPT_NAMES: dict[str, str] = {
    "small": "risk-small",
    "standard": "risk-standard",
    "high_risk": "risk-high",
}


def _load_risk_instructions(change_class: str) -> dict[str, str]:
    """Load per-phase instruction text from wf-orchestrator/prompts/risk-*.md.

    File format: '## phase' headers followed by instruction text.
    Returns {phase: instruction_text}.
    """
    prompt_name = _RISK_PROMPT_NAMES.get(change_class)
    if not prompt_name:
        return {}
    try:
        from awf.core.spec_loader import load_prompt
        content = load_prompt("wf-orchestrator", prompt_name)
    except FileNotFoundError:
        return {}
    instructions: dict[str, str] = {}
    current_phase: str | None = None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            current_phase = stripped[3:].strip()
        elif current_phase and stripped:
            instructions[current_phase] = stripped
    return instructions


def get_risk_investment(change_class: str, phase: str) -> dict:
    """Return risk investment settings for a phase at the given change class."""
    effective_class = change_class if change_class in RISK_INVESTMENT else "standard"
    result = RISK_INVESTMENT[effective_class].get(phase, {})
    if not result:
        return result
    # Inject instruction from external prompt if available
    if "instruction" not in result:
        instructions = _load_risk_instructions(effective_class)
        if phase in instructions:
            result = {**result, "instruction": instructions[phase]}
    return result


def _get_skip_phases(state: dict) -> set[str]:
    """Return phases to skip based on current change class."""
    change_class = state.get("changeClass", "standard")
    return CHANGE_CLASS_SKIP_PHASES.get(change_class, set())


def _is_phase_skipped_by_policy(state: dict, phase: str) -> bool:
    return phase in _get_skip_phases(state)


def _record_auto_pass_gate(state: dict, phase: str, change_class: str) -> None:
    """Record auto-pass gate for a skipped phase (I4)."""
    gate_id = PHASE_GATE.get(phase)
    if not gate_id:
        return
    gates = state.setdefault("gates", {})
    gates[gate_id] = {
        "passed": True,
        "auto_pass": True,
        "provider": "policy",
        "provider_status": "skipped",
        "checkedAt": _now_iso(),
        "skip_reason": f"policy:change_class={change_class}",
    }


def _mark_phase_skipped(state: dict, phase: str, change_class: str) -> None:
    """Mark a phase as skipped and record auto-pass gate."""
    phases = state.setdefault("phases", {})
    phase_state = phases.setdefault(phase, {"status": "pending", "retries": 0})
    if phase_state.get("status") == "skipped":
        return  # already skipped, no-op
    phase_state["status"] = "skipped"
    phase_state["skippedAt"] = _now_iso()
    phase_state["skipReason"] = f"policy:change_class={change_class}"
    _record_auto_pass_gate(state, phase, change_class)
    state.setdefault("history", []).append({
        "phase": phase,
        "action": "skipped",
        "timestamp": _now_iso(),
        "details": f"policy skip changeClass={change_class} gate={PHASE_GATE.get(phase, 'none')} auto_pass=true",
    })


def _apply_policy_skips(state: dict) -> list[str]:
    """Apply skip policy to all pending skippable phases. Returns list of skipped phase names."""
    skip_phases = _get_skip_phases(state)
    if not skip_phases:
        return []
    change_class = state.get("changeClass", "standard")
    skipped = []
    for phase in PHASE_ORDER:
        if phase in skip_phases:
            phase_status = state.get("phases", {}).get(phase, {}).get("status", "pending")
            if phase_status == "pending":
                _mark_phase_skipped(state, phase, change_class)
                skipped.append(phase)
    return skipped

DEFAULT_MANIFEST = {
    "version": "1.0.0",
    "language": None,
    "framework": None,
    "test_command": None,
    "lint_command": None,
    "build_command": None,
    "branch_pattern": None,
    "constitution_path": None,
    "context_providers": [],
    "speckit_available": False,
    "test_structure": {
        "unit": None,
        "integration": None,
        "e2e": None,
    },
}

def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _slugify_concept(value: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    slug = "-".join(part for part in slug.split("-") if part)
    return slug[:48] or "workflow"


def _detect_current_branch(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
        )
        branch = completed.stdout.strip()
        return branch or "HEAD"
    except Exception:
        return "unknown"


def _detect_manifest(root: Path) -> dict:
    from awf.core.scanner import PYTHON_PROJECT_MARKERS

    manifest = deepcopy(DEFAULT_MANIFEST)
    if (root / "package.json").exists():
        manifest["language"] = "javascript"
    elif any((root / marker).exists() for marker in PYTHON_PROJECT_MARKERS):
        manifest["language"] = "python"
        manifest["test_command"] = "pytest"
    elif (root / "go.mod").exists():
        manifest["language"] = "go"
        manifest["test_command"] = "go test ./..."
    elif (root / "Cargo.toml").exists():
        manifest["language"] = "rust"
        manifest["test_command"] = "cargo test"

    if (root / "tsconfig.json").exists():
        manifest["language"] = "typescript"

    context_providers: list[str] = []
    if (root / ".mcp.json").exists():
        context_providers.append(".mcp.json")
    if (root / "AGENTS.md").exists():
        context_providers.append("AGENTS.md")
    if (root / "CLAUDE.md").exists():
        context_providers.append("CLAUDE.md")
    manifest["context_providers"] = context_providers
    manifest["speckit_available"] = (root / ".specify").exists()
    return manifest


def _initial_workflow_state(root: Path, concept: str) -> dict:
    workflow_id = f"{datetime.now().strftime('%Y-%m-%d')}-{_slugify_concept(concept)}"
    return {
        "id": workflow_id,
        "repo": root.name,
        "branch": _detect_current_branch(root),
        "currentPhase": "plan",
        "createdAt": _now_iso(),
        "phases": {phase: {"status": "pending", "retries": 0} for phase in PHASE_ORDER},
        "gates": {
            "G1": {"passed": None},
            "G2": {"passed": None, "provider": None, "provider_status": None},
            "G3": {"passed": None, "scope_hash": None},
            "G4": {"passed": None},
            "G5": {"passed": None, "provider": None, "provider_status": None},
            "G6": {"passed": None},
        },
        "totalExecutions": 0,
        "loop": {
            "replanCount": 0,
            "maxReplans": 3,
        },
        "history": [],
    }


def _concept_markdown(concept: str) -> str:
    return "\n".join(
        [
            "# Concept",
            "",
            "## 요구사항",
            concept.strip(),
            "",
            "## 작성일",
            datetime.now().strftime("%Y-%m-%d"),
            "",
        ]
    )


def _extract_concept_text(markdown: str) -> str:
    lines = markdown.splitlines()
    inside_requirements = False
    collected: list[str] = []
    for line in lines:
        if line.strip() == "## 요구사항":
            inside_requirements = True
            continue
        if inside_requirements and line.startswith("## "):
            break
        if inside_requirements:
            collected.append(line)
    text = "\n".join(collected).strip()
    return text or markdown.strip()


def resolve_repo_root(explicit_root: Optional[str] = None) -> Path:
    return find_repo_root(explicit_root)


def _find_workflow_template_root(root: Path) -> Path | None:
    env_dir = os.environ.get("AWF_WORKFLOW_TEMPLATE_DIR", "").strip()
    candidates: list[Path] = []
    if env_dir:
        candidates.append(Path(env_dir).expanduser())
    candidates.append(root / "claude" / "skills" / "wf-orchestrator" / "templates")
    candidates.extend(
        base / "claude" / "skills" / "wf-orchestrator" / "templates"
        for base in Path(__file__).resolve().parents
    )
    for candidate in candidates:
        resolved = candidate.resolve()
        if (resolved / "agent-cards").is_dir() and (resolved / "provider-config.default.json").is_file():
            return resolved
    return None


def _copy_missing_tree(source: Path, target: Path) -> None:
    for source_path in source.rglob("*"):
        relative = source_path.relative_to(source)
        target_path = target / relative
        if source_path.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
        elif not target_path.exists():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)


def _bootstrap_workflow_runtime(root: Path, wf_dir: Path) -> None:
    template_root = _find_workflow_template_root(root)
    if template_root is None:
        return
    _copy_missing_tree(template_root / "agent-cards", wf_dir / "agent-cards")
    provider_config = wf_dir / "provider-config.json"
    if not provider_config.exists():
        shutil.copy2(template_root / "provider-config.default.json", provider_config)
    schema = template_root / "agent-card.schema.json"
    if schema.is_file() and not (wf_dir / "agent-card.schema.json").exists():
        shutil.copy2(schema, wf_dir / "agent-card.schema.json")


def initialize_workflow(explicit_root: Optional[str], concept: str, force: bool = False) -> dict:
    root = find_repo_root(explicit_root)
    wf_dir = root / ".workflow"
    state_path = wf_dir / "state.json"
    if state_path.exists() and not force:
        raise FileExistsError(f"Workflow already exists: {state_path}")

    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (wf_dir / "tmp").mkdir(parents=True, exist_ok=True)
    _bootstrap_workflow_runtime(root, wf_dir)

    manifest_path = wf_dir / "manifest.json"
    concept_path = wf_dir / "concept.md"

    manifest = _detect_manifest(root)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    concept_path.write_text(_concept_markdown(concept), encoding="utf-8")

    state = _initial_workflow_state(root, concept)
    state["changeClass"] = detect_change_class(concept)
    _save_workflow_state(explicit_root, state)
    return state


def reset_workflow(explicit_root: Optional[str], concept: Optional[str] = None) -> dict:
    root = find_repo_root(explicit_root)
    wf_dir = root / ".workflow"
    state = load_workflow_state(explicit_root)
    concept_path = wf_dir / "concept.md"
    replacement_concept = concept is not None
    if concept is None:
        if not concept_path.exists():
            raise FileNotFoundError(f"Missing workflow concept: {concept_path}")
        concept = _extract_concept_text(concept_path.read_text(encoding="utf-8"))

    new_state = _initial_workflow_state(root, concept)
    new_state["changeClass"] = detect_change_class(concept)
    # Keep existing concept/manifest/provider config/agent cards/artifacts files intact.
    if replacement_concept:
        concept_path.write_text(_concept_markdown(concept), encoding="utf-8")
    _save_workflow_state(explicit_root, new_state)
    return new_state


def load_workflow_state(explicit_root: Optional[str] = None) -> dict:
    root = find_repo_root(explicit_root)
    state_path = root / ".workflow" / "state.json"
    if not state_path.exists():
        raise FileNotFoundError(f"Missing workflow state: {state_path}")
    raw = state_path.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except JSONDecodeError:
        # Event handlers may observe the file while another process is replacing it.
        retry_raw = state_path.read_text(encoding="utf-8")
        return json.loads(retry_raw)


def load_workflow_provider_config(explicit_root: Optional[str] = None) -> dict:
    root = find_repo_root(explicit_root)
    provider_path = root / ".workflow" / "provider-config.json"
    if not provider_path.exists():
        raise FileNotFoundError(f"Missing workflow provider config: {provider_path}")
    return json.loads(provider_path.read_text(encoding="utf-8"))


def resolve_next_phase(state: dict, explicit_phase: Optional[str] = None) -> str:
    if explicit_phase:
        if explicit_phase not in PHASE_ORDER:
            supported = ", ".join(PHASE_ORDER)
            raise ValueError(f"Unknown phase `{explicit_phase}`. Supported: {supported}")
        return explicit_phase

    current_phase = state.get("currentPhase")

    # Workflow is in terminal states — cannot advance
    if current_phase == "aborted":
        raise ValueError("Workflow is aborted. Use `awf wf reset` to restart.")
    if current_phase == "completed":
        raise ValueError("Workflow is already completed.")

    # Apply policy skips before determining next phase (in-memory only; caller saves)
    _apply_policy_skips(state)

    phases = state.get("phases", {})
    for phase in PHASE_ORDER:
        status = phases.get(phase, {}).get("status")
        if status not in ("completed", "skipped"):
            return phase

    if current_phase in PHASE_ORDER:
        return current_phase
    raise ValueError("No pending workflow phase found. Pass --phase to force a delegated prompt.")


def _save_workflow_state(explicit_root: Optional[str], state: dict) -> Path:
    root = find_repo_root(explicit_root)
    state_path = root / ".workflow" / "state.json"
    tmp_path = state_path.with_name(
        f"{state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(state_path)
    return state_path


def save_workflow_state_snapshot(explicit_root: Optional[str], state: dict) -> Path:
    return _save_workflow_state(explicit_root, state)


def _clear_phase_runtime_markers(phase_state: dict) -> None:
    for key in (
        "completedAt",
        "escapedAt",
        "abortedAt",
        "provider",
        "escapeReason",
        "escapeSummary",
        "recommendedAction",
        "decision",
        "decisionAt",
        "decisionReason",
        "replanTarget",
        "synthesis",
    ):
        phase_state.pop(key, None)


def mark_phase_in_progress(explicit_root: Optional[str], phase: str) -> dict:
    state = load_workflow_state(explicit_root)

    # Enforce execution limit before starting any phase
    _check_execution_limit(state)
    _increment_execution_counter(state)

    phases = state.setdefault("phases", {})
    phase_state = phases.setdefault(phase, {"status": "pending", "retries": 0})
    _clear_phase_runtime_markers(phase_state)
    # §3.2: per-phase execution counter for fix-loop detection. Distinct from
    # `retries` (which only increments on apply_gate_result FAIL) — `executions`
    # tracks every time `awf wf next` starts this phase regardless of outcome.
    phase_state["executions"] = int(phase_state.get("executions", 0) or 0) + 1
    phase_state["status"] = "in_progress"
    phase_state["startedAt"] = _now_iso()
    state["currentPhase"] = phase
    loop = state.setdefault("loop", {})
    pending_decision = loop.get("pendingDecision")
    if isinstance(pending_decision, dict) and pending_decision.get("phase") == phase:
        loop.pop("pendingDecision", None)
    last_escape = loop.get("lastEscape")
    if isinstance(last_escape, dict) and last_escape.get("phase") == phase:
        loop.pop("lastEscape", None)
    history = state.setdefault("history", [])
    history.append(
        {
            "phase": phase,
            "action": "started",
            "timestamp": _now_iso(),
            "details": "awf-cli delegated execution started",
        }
    )
    _save_workflow_state(explicit_root, state)
    return state


def _check_execution_limit(state: dict) -> None:
    """Enforce MAX_TOTAL_EXECUTIONS to prevent infinite loops."""
    total = state.setdefault("totalExecutions", 0)
    if total >= MAX_TOTAL_EXECUTIONS:
        raise RuntimeError(
            f"Workflow execution limit reached ({MAX_TOTAL_EXECUTIONS}). "
            "Aborting to prevent infinite loop. Use `awf wf reset` to restart."
        )


def _increment_execution_counter(state: dict) -> int:
    """Increment and return total execution count."""
    state["totalExecutions"] = int(state.get("totalExecutions", 0) or 0) + 1
    return state["totalExecutions"]


def _default_next_phase(phase: str) -> Optional[str]:
    """Fallback phase ordering when agent card is missing."""
    try:
        idx = PHASE_ORDER.index(phase)
        if idx + 1 < len(PHASE_ORDER):
            return PHASE_ORDER[idx + 1]
    except ValueError:
        pass
    return None


def _agent_card_retry_max(agent_card: dict, default: int = 3) -> int:
    retry = agent_card.get("retry")
    if isinstance(retry, dict) and isinstance(retry.get("max"), int):
        return retry["max"]

    legacy_retry = (agent_card.get("gate") or {}).get("retry")
    if isinstance(legacy_retry, dict) and isinstance(legacy_retry.get("max"), int):
        return legacy_retry["max"]

    return default


def _agent_card_on_fail_action(
    on_fail: dict,
    failure_context: Optional[dict],
) -> object | None:
    if not isinstance(on_fail, dict):
        return None

    ctx = failure_context or {}
    if ctx.get("has_critical") and "critical_found" in on_fail:
        return on_fail["critical_found"]

    high_count = ctx.get("high_count", 0)
    if isinstance(high_count, int) and high_count > 0 and "high_only" in on_fail:
        return on_fail["high_only"]

    failure_type = ctx.get("failure_type")
    if isinstance(failure_type, str) and failure_type in on_fail:
        return on_fail[failure_type]

    if "default" in on_fail:
        return on_fail["default"]

    return None


def _agent_card_on_fail_next_phase(
    on_fail: dict,
    failure_context: Optional[dict],
) -> str | None:
    action = _agent_card_on_fail_action(on_fail, failure_context)

    if isinstance(action, dict):
        next_phase = action.get("next_phase") or action.get("target_phase")
        if isinstance(next_phase, str) and next_phase in PHASE_ORDER:
            return next_phase
        return None

    if action == "replan":
        target_phase = on_fail.get("target_phase")
        if isinstance(target_phase, str) and target_phase in PHASE_ORDER:
            return target_phase
        return None

    return None


def _agent_card_on_fail_prompts_user(
    on_fail: dict,
    failure_context: Optional[dict],
) -> bool:
    action = _agent_card_on_fail_action(on_fail, failure_context)
    if isinstance(action, dict):
        return action.get("prompt_user") is True
    return action == "prompt_user"


def apply_gate_result(
    explicit_root: Optional[str],
    phase: str,
    passed: bool,
    *,
    failure_context: Optional[dict] = None,
) -> dict:
    state = load_workflow_state(explicit_root)

    # --- Execution counter enforcement ---
    _check_execution_limit(state)
    _increment_execution_counter(state)

    phases = state.setdefault("phases", {})
    phase_state = phases.setdefault(phase, {"status": "pending", "retries": 0})
    phase_state["completedAt"] = _now_iso()

    # --- Record gate result for ALL phases ---
    gate_id = PHASE_GATE.get(phase)
    if gate_id:
        gates = state.setdefault("gates", {})
        gates[gate_id] = {
            "passed": passed,
            "checkedAt": _now_iso(),
            "provider": "awf-cli",
            "provider_status": "completed",
        }

    # --- Load agent card (with fallback) ---
    agent_card: Optional[dict] = None
    agent_card_path = find_repo_root(explicit_root) / ".workflow" / "agent-cards" / f"{phase}.json"
    if agent_card_path.exists():
        agent_card = json.loads(agent_card_path.read_text(encoding="utf-8"))

    if passed:
        phase_state["status"] = "completed"
        # Determine next phase: agent card > default ordering
        next_phase = None
        if agent_card:
            next_phase = agent_card.get("gate", {}).get("on_pass", {}).get("next_phase")
        if not next_phase:
            next_phase = _default_next_phase(phase)

        # Apply policy skips: skip phases between current and next executable
        _apply_policy_skips(state)

        if next_phase:
            # Find the actual next non-skipped phase
            try:
                start_idx = PHASE_ORDER.index(next_phase)
            except ValueError:
                start_idx = 0
            actual_next = None
            for p in PHASE_ORDER[start_idx:]:
                p_status = state.get("phases", {}).get(p, {}).get("status")
                if p_status not in ("completed", "skipped"):
                    actual_next = p
                    break
            state["currentPhase"] = actual_next or "completed"
        elif phase == PHASE_ORDER[-1]:
            state["currentPhase"] = "completed"
        else:
            state["currentPhase"] = phase
    else:
        # --- Retry budget enforcement ---
        retries = int(phase_state.get("retries", 0) or 0) + 1
        phase_state["retries"] = retries

        max_retries = 3  # default
        if agent_card:
            max_retries = _agent_card_retry_max(agent_card, default=max_retries)

        if retries > max_retries:
            # Retry budget exhausted — abort workflow
            phase_state["status"] = "aborted"
            phase_state["abortedAt"] = _now_iso()
            state["currentPhase"] = "aborted"
            state.setdefault("history", []).append(
                {
                    "phase": phase,
                    "action": "retry_exhausted",
                    "timestamp": _now_iso(),
                    "details": f"retries={retries} exceeded max={max_retries}, workflow halted",
                }
            )
            _save_workflow_state(explicit_root, state)
            return state

        # --- on_fail routing from agent card ---
        on_fail_target = None
        on_fail_prompts_user = False
        if agent_card:
            on_fail = agent_card.get("gate", {}).get("on_fail", {})
            on_fail_target = _agent_card_on_fail_next_phase(on_fail, failure_context)
            on_fail_prompts_user = _agent_card_on_fail_prompts_user(
                on_fail,
                failure_context,
            )

        if on_fail_target:
            phase_state["status"] = "failed"
            # Delegate to replan_workflow for proper state cleanup
            _save_workflow_state(explicit_root, state)
            return replan_workflow(explicit_root, phase, on_fail_target)
        elif on_fail_prompts_user:
            phase_state["status"] = "failed"
            _save_workflow_state(explicit_root, state)
            return record_orchestrator_decision(
                explicit_root,
                phase,
                decision="escalate_user",
                reason="agent-card on_fail.prompt_user matched",
            )
        else:
            # Default: mark failed, stay on current phase for retry or user decision
            phase_state["status"] = "failed"
            state["currentPhase"] = phase

    history = state.setdefault("history", [])
    history.append(
        {
            "phase": phase,
            "action": "completed" if passed else "failed",
            "timestamp": _now_iso(),
            "details": (
                f"awf-cli apply-result gate={'PASS' if passed else 'FAIL'} "
                f"retries={phase_state.get('retries', 0)}"
            ),
        }
    )
    _save_workflow_state(explicit_root, state)
    return state
