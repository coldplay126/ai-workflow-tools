from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

from awf.core.paths import find_repo_root


def _read_optional_text(path: Path) -> Optional[str]:
    return path.read_text(encoding="utf-8") if path.exists() else None


def _read_runtime_optional_context(root: Path, item: dict) -> Optional[str]:
    key = str(item.get("key", ""))
    if key != "git_diff":
        return None
    try:
        stat_completed = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
        )
        patch_completed = subprocess.run(
            ["git", "diff", "--", "."],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return None

    stat_text = stat_completed.stdout.strip() or "(no diff stat)"
    patch_text = patch_completed.stdout.strip() or "(no diff patch)"
    patch_limit = 12000
    if len(patch_text) > patch_limit:
        patch_text = patch_text[:patch_limit] + "\n...[git diff truncated by awf-cli]..."
    return "\n".join(
        [
            "git diff --stat",
            stat_text,
            "",
            "git diff -- .",
            patch_text,
        ]
    )


def _omp_followup_context(root: Path) -> list[dict]:
    dispatch_dir = root / ".workflow" / "artifacts" / "dispatch"
    if not dispatch_dir.is_dir():
        return []
    context: list[dict] = []
    for path in sorted(dispatch_dir.glob("*.json"))[-20:]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("backend") != "omp"
            or payload.get("mode") != "agents:followup-omp"
        ):
            continue
        agents: list[dict] = []
        for raw_agent in payload.get("agents", []):
            if not isinstance(raw_agent, dict):
                continue
            agents.append(
                {
                    key: raw_agent.get(key)
                    for key in (
                        "status",
                        "task_id",
                        "agent_uri",
                        "history_uri",
                        "output_sha256",
                        "lineage",
                        "followup_evidence",
                    )
                }
            )
        context.append(
            {
                "run_id": payload.get("run_id"),
                "created_at": payload.get("created_at"),
                "status": payload.get("status"),
                "parent_run_id": payload.get("parent_run_id"),
                "parent_task_id": payload.get("parent_task_id"),
                "message_sha256": payload.get("message_sha256"),
                "agents": agents,
            }
        )
    return context


def _structured_result_envelope(phase: str, structured_result: dict) -> dict:
    envelope: dict = {
        "conclusion": "PASS|FAIL + short summary",
        "evidence": [{"id": "E1", "detail": "judgment evidence"}],
        "risks": [{"id": "R1", "severity": "HIGH|MEDIUM|LOW", "detail": "risk or edge case"}],
        "action_items": [{"id": "A1", "action": "recommended next step"}],
    }
    if phase == "review":
        envelope["findings"] = [
            {
                "id": "F1",
                "category": "duplication|ambiguity|coverage_gap|inconsistency|domain_conflict",
                "severity": "CRITICAL|HIGH|MEDIUM|LOW",
                "locations": ["file:line or artifact section"],
                "summary": "finding summary",
                "recommendation": "optional recommendation",
            }
        ]
    envelope.update(structured_result)
    return envelope


def _validate_preconditions(state: dict, agent_card: dict, phase: str) -> None:
    """Check that required predecessor gates have passed before running this phase.

    Supports agent card structure: input.required_state.gates = { "G1": {"passed": true} }
    Also supports legacy: preconditions.required_gates = ["G1"]
    """
    gates = state.get("gates", {})

    # Primary: input.required_state.gates (agent card standard structure)
    required_state = agent_card.get("input", {}).get("required_state", {})
    required_gates_map = required_state.get("gates", {})
    if required_gates_map:
        for gate_id, expected in required_gates_map.items():
            gate = gates.get(gate_id, {})
            if expected.get("passed") and gate.get("passed") is not True:
                raise ValueError(
                    f"Precondition failed for phase '{phase}': "
                    f"gate {gate_id} must pass first (current: {gate.get('passed')})"
                )
        return

    # Fallback: preconditions.required_gates (legacy list format)
    preconditions = agent_card.get("preconditions", {})
    required_gates = preconditions.get("required_gates", [])
    for gate_id in required_gates:
        gate = gates.get(gate_id, {})
        if gate.get("passed") is not True:
            raise ValueError(
                f"Precondition failed for phase '{phase}': "
                f"gate {gate_id} must pass first (current: {gate.get('passed')})"
            )


def is_hil_phase(agent_card: dict, change_class: str | None = None) -> bool:
    """Check if this phase requires human-in-the-loop approval.

    HIL is determined by policy/change class (invariant I2):
    - Agent card hil=true is the baseline, but change class can override.
    - high_risk: hil=true phases remain true (no override)
    - standard: hil=true phases remain true (no override)
    - small: hil=true phases become false (auto-approval allowed)
      EXCEPT for 'done' phase which always respects agent card.
    """
    card_hil = bool(agent_card.get("hil", False))
    if not card_hil:
        return False
    # Policy override: small change class allows auto-approval
    if change_class == "small":
        phase_name = agent_card.get("name", "")
        # done phase always requires human confirmation regardless of change class
        if "done" in phase_name:
            return True
        return False
    return True


def build_workflow_prompt(explicit_root: Optional[str], state: dict, provider_config: dict, phase: str) -> str:
    root = find_repo_root(explicit_root)
    wf_dir = root / ".workflow"
    agent_card_path = wf_dir / "agent-cards" / f"{phase}.json"
    if not agent_card_path.exists():
        raise FileNotFoundError(f"Missing agent card: {agent_card_path}")

    agent_card = json.loads(agent_card_path.read_text(encoding="utf-8"))

    # Validate preconditions before building prompt
    _validate_preconditions(state, agent_card, phase)
    routing = provider_config.get("phase_routing", {}).get(phase, {})
    rules_files = [root / "AGENTS.md", root / "CLAUDE.md", root / "codex" / "AGENTS.md"]

    from awf.core.spec_loader import load_prompt_optional
    artifact_instruction = (
        "Return the required structured result only. Do not write workflow artifacts; "
        "the orchestrator will materialize the documented outputs."
        if phase in {"review", "verify"}
        else "Use the workflow artifacts below as the source of truth and write any "
        "required outputs to the documented paths."
    )

    # Load base prompt from external template (falls back to inline)
    base_prompt = load_prompt_optional("wf-orchestrator", "base",
        repo=state.get("repo", root.name),
        branch=state.get("branch", "-"),
        phase=phase,
        phase_mode=routing.get("mode", provider_config.get("defaults", {}).get("mode", "inline")),
        recommended_protocol=agent_card.get("capabilities", {}).get("protocol_hint", {}).get("recommended_mode", "solo"),
        task_description=agent_card.get("description", ""),
        artifact_instruction=artifact_instruction,
    )
    if not base_prompt:
        base_prompt = load_prompt_optional("wf-orchestrator", "phase-fallback",
            repo=state.get("repo", root.name),
            branch=state.get("branch", "-"),
            phase=phase,
            task_description=agent_card.get("description", ""),
        )
    if not base_prompt:
        base_prompt = f"You are executing a workflow phase.\nphase: {phase}\n"

    parts: list[str] = [
        base_prompt,
        "",
    ]

    # Inject risk context based on change class (§2: 위험 비례 투자)
    change_class = state.get("changeClass", "standard")
    from awf.core.state import get_risk_investment
    risk = get_risk_investment(change_class, phase)
    if risk:
        parts.append("=== RISK CONTEXT ===")
        parts.append(f"change_class: {change_class}")
        if risk.get("instruction"):
            parts.append(f"지시: {risk['instruction']}")
        if risk.get("depth"):
            parts.append(f"review_depth: {risk['depth']}")
        if risk.get("strictness"):
            parts.append(f"verify_strictness: {risk['strictness']}")
        if risk.get("range"):
            parts.append(f"test_range: {risk['range']}")
        parts.append("")

    parts.append("=== REQUIRED ARTIFACTS ===")

    for item in agent_card.get("input", {}).get("required_artifacts", []):
        rel_path = item.get("path")
        if not rel_path:
            continue
        full_path = (wf_dir / rel_path).resolve()
        content = _read_optional_text(full_path)
        if content is None:
            raise FileNotFoundError(f"Missing required workflow artifact: {full_path}")
        parts.append(f"--- {item.get('key', rel_path)} ({rel_path}) ---")
        parts.append(content)
        parts.append("")

    optional_context = agent_card.get("input", {}).get("optional_context", [])
    if optional_context:
        parts.extend(["=== OPTIONAL CONTEXT ==="])
        for item in optional_context:
            rel_path = item.get("path")
            if rel_path is None:
                runtime_content = _read_runtime_optional_context(root, item)
                if runtime_content is None:
                    continue
                parts.append(f"--- {item.get('key', 'runtime_context')} (runtime generated) ---")
                parts.append(runtime_content)
                parts.append("")
                continue
            if not rel_path:
                continue
            full_path = (wf_dir / rel_path).resolve()
            content = _read_optional_text(full_path)
            if content is None:
                continue
            parts.append(f"--- {item.get('key', rel_path)} ({rel_path}) ---")
            parts.append(content)
            parts.append("")

    omp_followups = _omp_followup_context(root)
    if omp_followups:
        parts.extend(
            [
                "=== OMP FOLLOW-UP EVIDENCE ===",
                "Redacted event-proven lineage from prior OMP follow-ups. "
                "Use status, handles, and hashes as phase evidence; response bodies "
                "are intentionally excluded.",
                json.dumps(omp_followups, ensure_ascii=False, indent=2),
                "",
            ]
        )

    existing_rules = [path for path in rules_files if path.exists()]
    if existing_rules:
        parts.extend(["=== PROJECT RULES ==="])
        for rule_path in existing_rules:
            content = _read_optional_text(rule_path)
            if not content:
                continue
            parts.append(f"--- {rule_path.relative_to(root)} ---")
            parts.append(content)
            parts.append("")

    output_artifacts = agent_card.get("output", {}).get("artifacts", [])
    if output_artifacts:
        parts.extend(["=== OUTPUTS ==="])
        for item in output_artifacts:
            parts.append(f"- {item.get('key')}: .workflow/{item.get('path')} ({item.get('format')})")
        parts.append("")

    structured_result = agent_card.get("output", {}).get("structured_result")
    if structured_result:
        result_schema = _structured_result_envelope(phase, structured_result)

        # Load envelope schema instructions from external template
        envelope_instructions = load_prompt_optional("wf-orchestrator", "envelope-schema") or ""

        # Load gate-specific instructions if applicable
        gate_instructions = ""
        if phase in ("review", "verify"):
            gate_instructions = load_prompt_optional("wf-orchestrator", f"{phase}-gate") or ""

        parts.extend([
            "=== STRUCTURED RESULT ===",
            envelope_instructions,
            gate_instructions,
            "Use this result body schema inside the worker envelope:",
            json.dumps(result_schema, ensure_ascii=False, indent=2),
            "",
            "Use this worker envelope:",
            json.dumps(
                {
                    "status": "completed|escaped|failed",
                    "phase": phase,
                    "provider": "provider-name",
                    "result": result_schema,
                    "escape": {
                        "severity": "blocking|degraded|advisory",
                        "reason": "scope_divergence|missing_dependency|contract_conflict|unsafe_change|missing_input|decision_selection_required|unknown",
                        "summary": "short description",
                        "evidence": [{"kind": "file|symbol|log|note", "value": "evidence"}],
                        "affected_files": ["path/to/file"],
                        "recommended_action": "replan|user_decision|abort|continue",
                    },
                    "meta": {"format_version": 1},
                },
                ensure_ascii=False,
                indent=2,
            ),
            "",
        ])

    gate = agent_card.get("gate", {})
    if gate:
        parts.extend(["=== GATE ===", f"id: {gate.get('id', '-')}", "pass_conditions:"])
        for condition in gate.get("pass_conditions", []):
            parts.append(f"- {condition}")
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def save_workflow_prompt(explicit_root: Optional[str], phase: str, provider_name: str, prompt: str) -> Path:
    root = find_repo_root(explicit_root)
    tmp_dir = root / ".workflow" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    safe_provider = provider_name.replace(":", "_")
    prompt_path = tmp_dir / f"prompt-{phase}-{safe_provider}.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    return prompt_path


def save_workflow_result(
    explicit_root: Optional[str],
    phase: str,
    provider_name: str,
    content: str,
    *,
    round_index: int | None = None,
) -> Path:
    """Persist a worker result file under `.workflow/tmp/`.

    §3.5: filenames now accumulate per round so re-execution does not
    overwrite the previous run's output. Pattern:
        result-{phase}-r{round}-{epoch_ms}-{safe_provider}.txt
    `round_index` defaults to the current `phases[phase].retries` value
    when not explicitly provided; the legacy overwrite path is no longer
    used. `_find_fresh_result_file` already globs `result-{phase}-*.txt`
    by mtime, so both legacy and new filenames are picked up transparently.
    """
    import time as _time

    root = find_repo_root(explicit_root)
    tmp_dir = root / ".workflow" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    safe_provider = provider_name.replace(":", "_")

    if round_index is None:
        try:
            from awf.core.state import load_workflow_state

            state = load_workflow_state(explicit_root)
            round_index = int(
                state.get("phases", {}).get(phase, {}).get("retries", 0) or 0
            )
        except Exception:
            round_index = 0
    round_index = max(int(round_index), 0)

    epoch_ms = int(_time.time() * 1000)
    result_path = tmp_dir / f"result-{phase}-r{round_index}-{epoch_ms}-{safe_provider}.txt"
    result_path.write_text(content, encoding="utf-8")
    return result_path
