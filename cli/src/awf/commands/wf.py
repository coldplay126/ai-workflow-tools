from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from awf.core.config import load_awf_config, resolve_runtime_paths
from awf.core.artifact_manager import ArtifactManager
from awf.core.events import EventType
from awf.core.event_processor import EventProcessor, run_complete_with_events
from awf.core.event_sync_summary import summarize_event_sync
from awf.core.gateway_runner import run_native_provider_task
from awf.core.permissions import PermissionDeniedError, build_permission_ruleset, check_permission, provider_permission_name
from awf.core.output_schemas import (
    workflow_result_envelope_schema,
    workflow_result_envelope_schema_json,
    write_temp_schema_file,
)
from awf.core.progress import ProgressDisplay
from awf.core.readiness import maybe_doctor_hint
from awf.core.state_updater import WorkflowStateUpdater
from awf.core.state import (
    abort_workflow,
    apply_gate_result,
    initialize_workflow,
    build_workflow_prompt,
    continue_workflow,
    load_workflow_state,
    load_workflow_provider_config,
    mark_phase_in_progress,
    replan_workflow,
    reset_workflow,
    resolve_next_phase,
    resolve_repo_root,
    record_orchestrator_decision,
    record_phase_escape,
    save_workflow_result,
    save_workflow_prompt,
    record_workflow_synthesis,
    summarize_workflow_state,
)
from awf.core.judge import explain_judge_reasons, synthesize_workflow_multi_provider_results
from awf.core.task import TaskConstraints, TaskContext, TaskDefinition, TaskType, resolve_execution_mode
from awf.core.workflow_results import apply_workflow_result
from awf.core.workflow_envelope import normalize_worker_result
from awf.commands.ready_gate import enforce_ready_gate
from awf.providers.registry import ProviderRegistry, UnknownProviderError


def _workflow_mode_prompt_note(mode: str | None) -> str:
    if mode == "critical":
        return (
            "\n=== EXECUTION MODE ===\n"
            "mode: critical\n"
            "- Apply the strictest safe interpretation of the gate rules.\n"
            "- Prefer explicit FAIL over ambiguous PASS.\n"
            "- Highlight production-impacting risks first.\n"
        )
    return ""


def _run_provider_with_heartbeat(provider, prompt: str, cwd: str, label: str, *, processor: EventProcessor | None = None, task_id: str | None = None):
    processor = processor or EventProcessor(handlers=[ProgressDisplay().handle])
    return run_complete_with_events(
        provider=provider,
        prompt=prompt,
        cwd=cwd,
        add_dirs=None,
        label=label,
        task_type="wf_phase",
        task_id=task_id or str(uuid.uuid4()),
        source=getattr(provider, "name", "provider"),
        processor=processor,
    )


_CODEX_READ_ONLY_PHASES = {"plan", "review", "verify", "test"}


def _apply_provider_permission_mode(provider, *, yolo: bool) -> None:
    if yolo and hasattr(provider, "set_permission_mode"):
        provider.set_permission_mode("bypassPermissions")


def _apply_phase_sandbox(provider, phase: str) -> None:
    if not hasattr(provider, "set_sandbox"):
        return
    provider.set_sandbox("read-only" if phase in _CODEX_READ_ONLY_PHASES else "workspace-write")


def _apply_workflow_output_schema(provider, phase: str) -> str | None:
    if hasattr(provider, "output_schema_path") and not getattr(provider, "output_schema_path", None):
        path = write_temp_schema_file(
            workflow_result_envelope_schema(phase),
            prefix=f"awf-wf-{phase}-schema-",
        )
        provider.output_schema_path = path
        return path
    if hasattr(provider, "json_schema") and not getattr(provider, "json_schema", None):
        provider.json_schema = workflow_result_envelope_schema_json(phase)
    return None


def _workflow_replan_budget(state: dict) -> tuple[int, int]:
    loop = state.get("loop") or {}
    replan_count = int(loop.get("replanCount", 0) or 0)
    max_replans = int(loop.get("maxReplans", 3) or 3)
    return replan_count, max_replans


def _workflow_idempotency_key(state: dict, phase: str) -> str:
    replan_count, _ = _workflow_replan_budget(state)
    return f"{state.get('id', 'wf')}:{phase}:{replan_count}"


_GATE_SUPPORTED_PHASES = {"plan", "review", "verify", "impl", "test"}


def run_wf_gate(args: argparse.Namespace) -> int:
    """Run deterministic gate evaluation for a workflow phase.

    Evaluation-only: prints PASS/FAIL and details. Does NOT update state.json
    or apply gate routing. The caller (orchestrator) handles state transitions.

    Supported phases: plan (artifact-based), review, verify (result-based).
    """
    from awf.core.gates import evaluate_gate, evaluate_plan_gate

    phase = args.phase
    repo_root = resolve_repo_root(args.repo_root)

    if phase not in _GATE_SUPPORTED_PHASES:
        print(f"error: deterministic gate not yet implemented for '{phase}'", file=sys.stderr)
        print(f"  supported: {', '.join(sorted(_GATE_SUPPORTED_PHASES))}", file=sys.stderr)
        return 2

    if phase == "plan":
        passed, evaluations = evaluate_plan_gate(repo_root)
    else:
        # For review/verify/impl/test, read result JSON from --result-file or stdin.
        # Uses the same parser as awf wf next to handle prose-wrapped + stream-json results.
        from awf.core.workflow_results import load_result_json
        result_data: dict = {}
        if args.result_file:
            try:
                result_data = load_result_json(args.result_file)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                print(f"error: cannot parse result file: {exc}", file=sys.stderr)
                return 2
        elif not sys.stdin.isatty():
            import tempfile
            raw = sys.stdin.read()
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
                f.write(raw)
                f.flush()
                try:
                    result_data = load_result_json(f.name)
                except (json.JSONDecodeError, ValueError) as exc:
                    print(f"error: invalid JSON from stdin: {exc}", file=sys.stderr)
                    return 2
        else:
            print(f"error: {phase} gate requires --result-file or stdin JSON", file=sys.stderr)
            return 2

        try:
            state = load_workflow_state(args.repo_root)
        except Exception:
            state = {}
        change_class = state.get("changeClass", "standard")
        passed, evaluations = evaluate_gate(args.repo_root, phase, result_data, change_class)

    # Output
    for ev in evaluations:
        mark = "\u2713" if ev["passed"] else "\u2717"
        print(f"  {mark} {ev['condition']}: {ev['detail']}")

    verdict = "PASS" if passed else "FAIL"
    print(f"\nG-{phase}: {verdict}")

    if args.json:
        print(json.dumps({"phase": phase, "passed": passed, "evaluations": evaluations}, ensure_ascii=False, indent=2))

    return 0 if passed else 1


_SKIP_PHASES = {"small": ["review", "approve", "verify"]}
_HIL_PHASES = {"high_risk": ["approve", "done"], "standard": ["approve", "done"], "small": ["done"]}
_PATHS = {
    "small": ["plan", "impl", "test", "done"],
    "standard": ["plan", "review", "approve", "impl", "verify", "test", "done"],
    "high_risk": ["plan", "review", "approve", "impl", "verify", "test", "done"],
}


def run_wf_detect_class(args: argparse.Namespace) -> int:
    """Detect change class from concept text — deterministic classification.

    Evaluation-only: prints classification and routing details.
    Reads from positional arg, --concept-file, or stdin.
    """
    from awf.core.state import detect_change_class, get_risk_investment

    # Resolve concept text: positional > --concept-file > stdin
    concept = args.concept
    if not concept and args.concept_file:
        try:
            concept = open(args.concept_file, encoding="utf-8").read().strip()
        except OSError as exc:
            print(f"error: cannot read concept file: {exc}", file=sys.stderr)
            return 2
    if not concept and not sys.stdin.isatty():
        concept = sys.stdin.read().strip()
    if not concept:
        print("error: concept text required (positional, --concept-file, or stdin)", file=sys.stderr)
        return 2

    change_class = detect_change_class(concept)

    if args.json:
        investment = {}
        for phase in ("review", "verify", "test"):
            investment[phase] = {**get_risk_investment(change_class, phase)}
            for k, v in investment[phase].items():
                if isinstance(v, set):
                    investment[phase][k] = sorted(v)
        print(json.dumps({
            "concept": concept,
            "change_class": change_class,
            "skip_phases": _SKIP_PHASES.get(change_class, []),
            "hil_phases": _HIL_PHASES.get(change_class, []),
            "path": _PATHS.get(change_class, _PATHS["standard"]),
            "risk_investment": investment,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"change_class: {change_class}")
        skip = _SKIP_PHASES.get(change_class, [])
        if skip:
            print(f"  skip: {', '.join(skip)}")
        hil = _HIL_PHASES.get(change_class, [])
        if any(p != "done" for p in hil):
            print(f"  hil: {', '.join(p for p in hil if p != 'done')} (mandatory)")
        path = _PATHS.get(change_class, _PATHS["standard"])
        print(f"  path: {' → '.join(path)}")

    return 0


def run_wf_status(args: argparse.Namespace) -> int:
    try:
        state = load_workflow_state(args.repo_root)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    print(summarize_workflow_state(state))

    # K5: Show work_history sessions
    from awf.core.work_history import list_sessions
    try:
        repo_root = resolve_repo_root(args.repo_root)
        sessions = list_sessions(repo_root)
        if sessions:
            print(f"\n=== Work History ({len(sessions)} sessions) ===")
            for s in sessions[-5:]:  # Show last 5
                artifacts = f", {s['artifact_count']} artifacts" if s["artifact_count"] else ""
                print(f"  {s['name']}: {s['concept']}{artifacts}")
            if len(sessions) > 5:
                print(f"  ... and {len(sessions) - 5} more")
    except Exception:
        pass

    return 0


def run_wf_init(args: argparse.Namespace) -> int:
    gate_rc = enforce_ready_gate(args, "workflow-init")
    if gate_rc != 0:
        return gate_rc

    try:
        state = initialize_workflow(args.repo_root, args.concept, force=args.force)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # K5: Create work_history session
    from awf.core.work_history import create_work_history_session
    try:
        repo_root = resolve_repo_root(args.repo_root)
        session_dir = create_work_history_session(
            repo_root,
            args.concept,
            workflow_id=state.get("id", ""),
        )
        print(f"work_history: {session_dir}")
    except Exception as exc:
        print(f"warning: work_history creation failed: {exc}", file=sys.stderr)

    print("workflow initialized")
    print(f"id: {state.get('id')}")
    print(f"repo: {state.get('repo')}")
    print(f"branch: {state.get('branch')}")
    print(f"current_phase: {state.get('currentPhase')}")
    return 0


def run_wf_reset(args: argparse.Namespace) -> int:
    try:
        state = reset_workflow(args.repo_root, args.concept)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print("workflow reset")
    print(f"id: {state.get('id')}")
    print(f"repo: {state.get('repo')}")
    print(f"branch: {state.get('branch')}")
    print(f"current_phase: {state.get('currentPhase')}")
    return 0


def run_wf_decide(args: argparse.Namespace) -> int:
    try:
        state = load_workflow_state(args.repo_root)
        phase = args.phase or str(state.get("currentPhase", "") or "")
        if not phase:
            raise ValueError("No current workflow phase found.")
        phase_state = ((state.get("phases") or {}).get(phase) or {})
        current_status = str(phase_state.get("status") or "")
        # §1.6: --force-from lets the operator decide from non-deciding states.
        # The decision is logged via history append below so the override is auditable.
        force_from = getattr(args, "force_from", None)
        if current_status != "deciding":
            if force_from == "any" or force_from == current_status:
                from awf.core.state import _now_iso, _save_workflow_state  # local import

                state.setdefault("history", []).append({
                    "phase": phase,
                    "action": "force_decide",
                    "timestamp": _now_iso(),
                    "details": (
                        f"force_from={force_from} prior_status={current_status} "
                        f"decision={args.decision}"
                    ),
                })
                _save_workflow_state(args.repo_root, state)
                print(
                    f"force_decide: phase `{phase}` was {current_status}, overriding to apply decision "
                    f"`{args.decision}` (--force-from={force_from})",
                    file=sys.stderr,
                )
            else:
                msg = f"Phase `{phase}` is not in deciding state (current: {current_status})."
                if force_from is None:
                    msg += " Pass `--force-from <status|any>` to override."
                raise ValueError(msg)

        if args.decision == "replan":
            target_phase = args.target or "plan"
            new_state = replan_workflow(args.repo_root, phase, target_phase)
            print(f"decision_applied: replan -> {target_phase}")
            print(f"current_phase: {new_state.get('currentPhase')}")
            print("next_step: update workflow artifacts and rerun wf next from the replanned phase")
            return 0
        if args.decision == "continue":
            new_state = continue_workflow(args.repo_root, phase)
            print("decision_applied: continue")
            print(f"current_phase: {new_state.get('currentPhase')}")
            print("next_step: rerun wf next for the current phase to continue execution")
            return 0
        if args.decision == "abort":
            new_state = abort_workflow(args.repo_root, phase)
            print("decision_applied: abort")
            print(f"current_phase: {new_state.get('currentPhase')}")
            print("next_step: inspect workflow history before resetting or restarting")
            return 0
        raise ValueError(f"Unsupported decision `{args.decision}`.")
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def run_wf_next(args: argparse.Namespace) -> int:
    non_interactive = bool(getattr(args, "non_interactive", False))
    auto_apply = bool(args.auto_apply)
    dry_run = bool(getattr(args, "dry_run", False))
    if not dry_run:
        gate_rc = enforce_ready_gate(
            args,
            "workflow-run",
            json_output=getattr(args, "output_format", "text") == "json",
        )
        if gate_rc != 0:
            return gate_rc
    try:
        config = load_awf_config(args.repo_root)
        state = load_workflow_state(args.repo_root)
        provider_config = load_workflow_provider_config(args.repo_root)
        phase = resolve_next_phase(state, args.phase)
        # Save state after resolve (policy skips may have been applied in-memory)
        if not dry_run:
            from awf.core.state import save_workflow_state_snapshot
            save_workflow_state_snapshot(args.repo_root, state)
        # Log skipped phases
        skip_phases = [p for p in state.get("phases", {}) if state["phases"][p].get("status") == "skipped"]
        if skip_phases:
            print(f"policy_skip: {', '.join(skip_phases)} (changeClass={state.get('changeClass', 'unknown')})", file=sys.stderr)
        prompt = build_workflow_prompt(args.repo_root, state, provider_config, phase)
        prompt += _workflow_mode_prompt_note(getattr(args, "mode", None))
        provider_name = _resolve_phase_provider(args.provider, provider_config, phase, config)
        fallback_chain = _resolve_fallback_chain(provider_name, provider_config, getattr(args, "mode", None))
        prompt_path = (
            None
            if dry_run
            else save_workflow_prompt(args.repo_root, phase, provider_name, prompt)
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if non_interactive and phase in {"review", "verify"}:
        auto_apply = True

    if args.print_prompt or args.dry_run:
        phase_models = provider_config.get("phase_models", {}).get(phase, {})
        prompt_file = (
            str(prompt_path)
            if prompt_path is not None
            else "(dry-run, not written)"
        )
        if dry_run and getattr(args, "output_format", "text") == "json":
            print(json.dumps({
                "phase": phase,
                "provider": provider_name,
                "phase_models": phase_models,
                "execution_mode": getattr(args, "mode", None) or "default",
                "non_interactive": non_interactive,
                "fallback_chain": fallback_chain,
                "prompt_file": prompt_file,
                "prompt": prompt,
            }, ensure_ascii=False, indent=2))
            return 0
        print(f"=== awf wf next ===")
        print(f"phase: {phase}")
        print(f"provider: {provider_name}")
        if phase_models:
            print(f"phase_models: {json.dumps(phase_models, ensure_ascii=False)}")
        print(f"execution_mode: {getattr(args, 'mode', None) or 'default'}")
        print(f"non_interactive: {non_interactive}")
        print(f"fallback_chain: {json.dumps(fallback_chain, ensure_ascii=False)}")
        print(f"prompt_file: {prompt_file}")
        print()
        print(prompt)
        if dry_run:
            return 0

    repo_root = str(resolve_repo_root(args.repo_root))
    registry = ProviderRegistry(config)
    ruleset = build_permission_ruleset(config.raw, yolo=getattr(args, "yolo", False))
    artifact_manager = ArtifactManager()
    state_updater = WorkflowStateUpdater(args.repo_root)
    processor = EventProcessor(handlers=[ProgressDisplay().handle, artifact_manager.handle, state_updater.handle])

    # HIL phase check: warn if auto-executing a human-in-the-loop phase
    from awf.core.workflow_prompt import is_hil_phase as _is_hil
    try:
        import json as _json
        _ac_path = resolve_repo_root(args.repo_root) / ".workflow" / "agent-cards" / f"{phase}.json"
        _change_class = state.get("changeClass")
        if _ac_path.exists():
            _ac = _json.loads(_ac_path.read_text(encoding="utf-8"))
            if _is_hil(_ac, change_class=_change_class):
                if non_interactive:
                    print(
                        f"warning: phase '{phase}' is marked as human-in-the-loop (hil=true). "
                        f"Running in non-interactive mode — approval will be automated.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"info: phase '{phase}' requires human approval (hil=true). "
                        f"Review the output carefully before proceeding.",
                        file=sys.stderr,
                    )
    except Exception:
        pass

    # §3.2: detect verify fix-loop overruns BEFORE marking the phase
    # in_progress. The BLIP Gem cycle hit 7 verify rounds because nothing
    # in the toolchain capped repeated executions; with this guard the
    # operator gets a warning at 3 and a hard abort with hint at 5.
    fix_loop_status, projected_executions = _verify_fix_loop_status(state, phase)
    if fix_loop_status == "abort":
        force = bool(getattr(args, "force", False))
        if not force:
            print(
                f"error: verify phase has reached the fix-loop hard limit "
                f"(executions would become {projected_executions}, max="
                f"{VERIFY_FIX_LOOP_HARD_LIMIT}).",
                file=sys.stderr,
            )
            print(
                "  Repeated verify failures usually indicate the spec needs "
                "replanning or remaining issues should be accepted as debt.",
                file=sys.stderr,
            )
            print(
                "  options:",
                file=sys.stderr,
            )
            print(
                "    - `awf wf decide replan` to revise the plan",
                file=sys.stderr,
            )
            print(
                "    - `awf wf decide continue` to accept current state",
                file=sys.stderr,
            )
            print(
                "    - re-run `awf wf next` with `--force` to bypass this guard "
                "(record why in history).",
                file=sys.stderr,
            )
            return 2
        print(
            f"warning: verify fix-loop hard limit bypassed via --force "
            f"(execution #{projected_executions}).",
            file=sys.stderr,
        )
    elif fix_loop_status == "warn":
        print(
            f"warning: verify has been executed {projected_executions} times "
            f"this cycle (warn threshold={VERIFY_FIX_LOOP_WARN_THRESHOLD}, "
            f"hard limit={VERIFY_FIX_LOOP_HARD_LIMIT}). Consider "
            "`awf wf decide replan` or accepting remaining findings as debt.",
            file=sys.stderr,
        )

    mark_phase_in_progress(args.repo_root, phase)
    processor.emit(
        event_type=EventType.PHASE_STARTED,
        task_id=f"wf-{phase}",
        source="cli",
        data={"phase": phase, "description": "workflow phase execution"},
    )
    processor.emit(
        event_type=EventType.STAGE_STARTED,
        task_id=f"wf-{phase}",
        source="cli",
        data={"stage": "prepare", "description": "prepare workflow prompt and execution context"},
    )
    if state.get("currentPhase") == phase and state.get("phases", {}).get(phase, {}).get("status") == "in_progress":
        # §1.4: avoid silently re-running a multi-minute executor when the
        # caller may not have realised the phase is still in_progress. If a
        # recent result file exists, abort with a hint; without --force the
        # operator must apply-result or explicitly override.
        force = bool(getattr(args, "force", False))
        fresh_result = _find_fresh_result_file(args.repo_root, phase, max_age_sec=1800)
        if fresh_result is not None and not force:
            print(
                f"error: phase `{phase}` is already in_progress and a fresh result file exists.",
                file=sys.stderr,
            )
            print(f"  result_file: {fresh_result}", file=sys.stderr)
            print(
                f"  apply it with `awf wf apply-result {phase} {fresh_result}`,",
                file=sys.stderr,
            )
            print(
                "  or re-execute with `--force` if the previous result is stale.",
                file=sys.stderr,
            )
            return 2
        if force:
            print(
                f"warning: phase `{phase}` is already in_progress; --force re-running delegated execution.",
                file=sys.stderr,
            )
        else:
            print(
                f"warning: phase `{phase}` is already in_progress (no fresh result detected); re-running delegated execution.",
                file=sys.stderr,
            )
    print(f"prompt_file: {prompt_path}")
    processor.emit(
        event_type=EventType.ARTIFACT_CREATED,
        task_id=f"wf-{phase}",
        source="cli",
        data={
            "path": str(prompt_path),
            "kind": "wf_prompt",
            "producer": "cli",
            "status": "final",
            "replaces": None,
        },
    )
    processor.emit(
        event_type=EventType.STAGE_COMPLETED,
        task_id=f"wf-{phase}",
        source="cli",
        data={"stage": "prepare"},
    )
    execute_started_at = time.monotonic()
    processor.emit(
        event_type=EventType.STAGE_STARTED,
        task_id=f"wf-{phase}",
        source="cli",
        data={"stage": "execute", "description": "run delegated workflow phase provider"},
    )
    result_path = None
    last_returncode = 2
    selected_provider = None
    secondary_result_path = None
    secondary_provider = None
    for candidate in fallback_chain:
        if not registry.supports(candidate):
            continue
        try:
            provider = registry.get(candidate)
            _apply_phase_effort(provider, provider_config, phase)
            _apply_phase_sandbox(provider, phase)
            _apply_provider_permission_mode(provider, yolo=bool(getattr(args, "yolo", False)))
        except UnknownProviderError:
            continue
        try:
            check_permission(ruleset, provider_permission_name(candidate, config.raw.get("provider", {}).get("aliases")), f"wf:{phase}")
        except PermissionDeniedError as exc:
            print(f"permission_skip: {candidate} ({exc})", file=sys.stderr)
            continue
        selected_provider = candidate
        timeout_sec = getattr(provider, "timeout_sec", None)
        schema_cleanup_path = _apply_workflow_output_schema(provider, phase)
        # §12.5 routing log — surface the dispatch path so operators can see whether
        # the worker ran inline (subprocess), via cmux-agent broker, or fell back.
        # The multi-agent code emits its own per-mode line for cross/critical/precise;
        # this covers the default single-agent path.
        dispatch_pref = "inline"
        try:
            from awf.core.dispatch import resolve_preference_from_config

            dispatch_pref = resolve_preference_from_config(provider_config)
        except Exception:
            pass
        if timeout_sec is not None:
            print(
                f"provider_running: {candidate} surface={dispatch_pref} (timeout: {timeout_sec}s)",
                file=sys.stderr,
            )
        else:
            print(
                f"provider_running: {candidate} surface={dispatch_pref}",
                file=sys.stderr,
            )
        native_task = TaskDefinition(
            task_id=str(uuid.uuid4()),
            parent_task_id=None,
            correlation_id=state.get("id", f"wf:{phase}"),
            idempotency_key=_workflow_idempotency_key(state, phase),
            type=TaskType.WF_PHASE,
            params={
                "phase": phase,
                "prompt": prompt,
                "mode": getattr(args, "mode", None) or "default",
                "auto_apply": auto_apply,
                "fallback_chain": list(fallback_chain),
                "replan_count": _workflow_replan_budget(state)[0],
                "escape_budget": _workflow_replan_budget(state)[1],
            },
            constraints=TaskConstraints(
                timeout_sec=timeout_sec,
                max_retries=2,
                mode=getattr(args, "mode", None),
                non_interactive=non_interactive,
                dry_run=bool(args.dry_run),
            ),
            context=TaskContext(
                cwd=repo_root,
                repo_root=repo_root,
                docs_root=None,
                github_root=None,
                config=config,
                provider_name=candidate,
            ),
        )
        try:
            execution_mode_name = resolve_execution_mode(provider, native_task)
            if execution_mode_name == "native":
                print(f"provider_execution_mode: {candidate} native", file=sys.stderr)
                result, elapsed = run_native_provider_task(provider=provider, task=native_task, processor=processor)
            else:
                result, elapsed = _run_provider_with_heartbeat(
                    provider,
                    prompt,
                    repo_root,
                    candidate,
                    processor=processor,
                    task_id=str(uuid.uuid4()),
                )
        finally:
            if schema_cleanup_path:
                try:
                    os.unlink(schema_cleanup_path)
                except FileNotFoundError:
                    pass
        captured_output = result.stdout if (result.stdout or "").strip() else (result.stderr or "")
        # §8.7-P1: record phase telemetry (best-effort, never blocks the workflow).
        try:
            from awf.core.state import record_phase_telemetry
            from awf.core.usage import estimate_cost

            usage = getattr(result, "usage", None)
            in_tok = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
            out_tok = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
            if in_tok or out_tok:
                cost = estimate_cost(candidate, in_tok, out_tok)
                record_phase_telemetry(
                    args.repo_root,
                    phase,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cost_usd=cost,
                    provider=candidate,
                )
        except Exception:
            pass
        result_path = save_workflow_result(
            args.repo_root,
            phase,
            candidate,
            captured_output,
        )
        processor.emit(
            event_type=EventType.ARTIFACT_CREATED,
            task_id=f"wf-{phase}",
            source="cli",
            data={
                "path": str(result_path),
                "kind": "wf_result",
                "producer": candidate,
                "status": "final",
                "replaces": None,
            },
        )
        print(f"  provider: {candidate}", file=sys.stderr)
        print(f"  result: {result_path}", file=sys.stderr)
        if result.stdout:
            try:
                parsed = json.loads(result.stdout)
                conclusion = parsed.get("result", parsed).get("conclusion", parsed.get("conclusion", ""))
                findings = parsed.get("result", parsed).get("findings", parsed.get("findings", []))
                if conclusion:
                    icon = "✓" if "PASS" in str(conclusion).upper() else "✗"
                    print(f"\n  {icon} 결론: {str(conclusion)[:150]}", file=sys.stderr)
                if findings:
                    critical = sum(1 for f in findings if str(f.get("severity", "")).upper() in {"CRITICAL", "HIGH"})
                    major = sum(1 for f in findings if str(f.get("severity", "")).upper() in {"MAJOR", "MEDIUM"})
                    print(f"  발견: {len(findings)}건 (critical/high={critical}, major/medium={major})", file=sys.stderr)
                    for f in findings[:3]:
                        desc = str(f.get("summary", f.get("description", "")))[:100]
                        sev = f.get("severity", "?")
                        if desc:
                            print(f"    - [{sev}] {desc}", file=sys.stderr)
                    if len(findings) > 3:
                        print(f"    ... (+{len(findings) - 3}건)", file=sys.stderr)
                elif conclusion:
                    print(f"  발견: 이슈 없음", file=sys.stderr)
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
            hint = maybe_doctor_hint(candidate, result.stderr)
            if hint:
                print(hint, file=sys.stderr)
        last_returncode = result.returncode
        try:
            normalized_result = normalize_worker_result(
                json.loads(captured_output),
                phase=phase,
                provider=candidate,
            )
        except Exception:
            normalized_result = None
        if normalized_result is not None and normalized_result.get("status") != "completed":
            worker_status = str(normalized_result.get("status", "failed"))
            print(f"worker_status: {worker_status}", file=sys.stderr)
            escape = normalized_result.get("escape")
            if isinstance(escape, dict):
                summary = str(escape.get("summary", "") or "").strip()
                reason = str(escape.get("reason", "") or "").strip()
                if reason:
                    print(f"escape_reason: {reason}", file=sys.stderr)
                if summary:
                    print(f"escape_summary: {summary}", file=sys.stderr)
            if worker_status == "escaped":
                processor.emit(
                    event_type=EventType.ESCAPE_TRIGGERED,
                    task_id=f"wf-{phase}",
                    source=candidate,
                    data={
                        "phase": phase,
                        "provider": candidate,
                        "reason": str((escape or {}).get("reason", "") or ""),
                        "summary": str((escape or {}).get("summary", "") or ""),
                        "recommended_action": str((escape or {}).get("recommended_action", "") or ""),
                    },
                )
                record_phase_escape(
                    args.repo_root,
                    phase,
                    provider=candidate,
                    escape=escape if isinstance(escape, dict) else {},
                )
                decision, decision_reason, replan_target = _orchestrator_decision_from_escape(
                    escape if isinstance(escape, dict) else None,
                    state=state,
                )
                processor.emit(
                    event_type=EventType.ORCHESTRATOR_DECIDED,
                    task_id=f"wf-{phase}",
                    source="cli",
                    data={
                        "phase": phase,
                        "decision": decision,
                        "reason": decision_reason,
                        "replan_target": replan_target,
                    },
                )
                record_orchestrator_decision(
                    args.repo_root,
                    phase,
                    decision=decision,
                    reason=decision_reason,
                    replan_target=replan_target,
                )
                if decision == "continue":
                    continue_workflow(args.repo_root, phase)
                    print("decision_applied: continue", file=sys.stderr)
                    print("next_step: rerun wf next for the current phase to continue execution", file=sys.stderr)
                elif decision == "replan":
                    target_phase = replan_target or "plan"
                    replanned_state = replan_workflow(args.repo_root, phase, target_phase)
                    print(f"decision_applied: replan -> {target_phase}", file=sys.stderr)
                    print(f"current_phase: {replanned_state.get('currentPhase')}", file=sys.stderr)
                    print(
                        "next_step: update workflow artifacts and rerun wf next from the replanned phase",
                        file=sys.stderr,
                    )
                elif decision == "abort":
                    aborted_state = abort_workflow(args.repo_root, phase)
                    print("decision_applied: abort", file=sys.stderr)
                    print(f"current_phase: {aborted_state.get('currentPhase')}", file=sys.stderr)
                    print(
                        "next_step: inspect workflow history before resetting or restarting",
                        file=sys.stderr,
                    )
                else:
                    print(
                        "next_step: review wf status and decide whether to replan, continue, or abort",
                        file=sys.stderr,
                    )
                processor.emit(
                    event_type=EventType.TASK_FAILED,
                    task_id=f"wf-{phase}",
                    source="cli",
                    data={"error": "worker_result_status:escaped"},
                )
                last_returncode = 5
            else:
                processor.emit(
                    event_type=EventType.TASK_FAILED,
                    task_id=f"wf-{phase}",
                    source="cli",
                    data={"error": f"worker_result_status:{worker_status}"},
                )
                last_returncode = 5
            break
        if result.returncode == 0:
            break

    processor.emit(
        event_type=EventType.STAGE_COMPLETED,
        task_id=f"wf-{phase}",
        source="cli",
        data={"stage": "execute", "duration_sec": round(time.monotonic() - execute_started_at, 3)},
    )

    if last_returncode == 5:
        refreshed_state = load_workflow_state(args.repo_root)
        event_summary = summarize_event_sync(refreshed_state.get("eventSync"))
        if event_summary:
            print(event_summary, file=sys.stderr)
        return last_returncode

    if selected_provider is None or result_path is None:
        print("error: no supported provider available for wf next.", file=sys.stderr)
        return 2

    # Multi-agent execution for non-solo modes. Phase 4: auto-promote
    # solo → cross for review/verify when the user did not pass --mode,
    # because synthesize_workflow_multi_provider_results is already tuned
    # to combine those two phases' results meaningfully.
    user_mode = getattr(args, "mode", None)
    exec_mode, _dual_strategy_promoted = _maybe_auto_promote_dual_strategy(
        user_mode=user_mode,
        phase=phase,
        provider_config=provider_config,
        repo_root=repo_root,
    )
    if exec_mode and exec_mode != "solo" and last_returncode == 0:
        from awf.core.multi_agent import run_phase, auto_promote
        from awf.core.team_config import get_phase_pattern

        # Select synthesis pattern based on phase (SKILL.md §5C Dual Execution)
        synthesis_pattern = _select_synthesis_pattern(phase, exec_mode)
        phase_pattern = get_phase_pattern(provider_config, phase)
        print(f"=== multi-agent: {exec_mode} mode, pattern: {synthesis_pattern}, routing: {phase_pattern} ===", file=sys.stderr)
        processor.emit(
            event_type=EventType.STAGE_STARTED,
            task_id=f"wf-{phase}-multi-agent",
            source="cli",
            data={
                "stage": "multi_agent",
                "description": f"{exec_mode} mode {synthesis_pattern}",
                "mode": exec_mode,
                "pattern": synthesis_pattern,
                "phase_pattern": phase_pattern,
            },
        )
        primary = registry.get(selected_provider)
        _apply_phase_effort(primary, provider_config, phase)
        multi_result = run_phase(
            mode=exec_mode,
            prompt=prompt,
            primary_provider=primary,
            registry=registry,
            provider_config=provider_config,
            cwd=repo_root,
            phase=phase,
            processor=processor,
        )
        # Store secondary results in multi-agent directory
        from pathlib import Path
        import json as json_mod
        ma_dir = Path(resolve_repo_root(args.repo_root)) / ".workflow" / "tmp" / "multi-agent"
        ma_dir.mkdir(parents=True, exist_ok=True)
        is_team = multi_result.mode.startswith("team:")

        if is_team:
            # Team mode: persist combined team output as a single normalized artifact
            if multi_result.combined_output:
                team_file = ma_dir / f"team-{phase}-result.json"
                team_file.write_text(multi_result.combined_output, encoding="utf-8")
                secondary_result_path = str(team_file)
                secondary_provider = multi_result.selected_agent
                processor.emit(
                    event_type=EventType.ARTIFACT_CREATED,
                    task_id=f"wf-{phase}-team",
                    source="cli",
                    data={"path": secondary_result_path, "kind": "wf_result", "producer": "team_runner", "status": "final", "replaces": None},
                )
                print(f"team_result: {multi_result.selected_agent} ({len(multi_result.agents)} workers)", file=sys.stderr)
        else:
            # Subagent mode: persist per-agent secondary results
            for agent in multi_result.agents:
                if agent.role != "primary" and agent.stdout.strip():
                    sec_file = ma_dir / f"{agent.provider_name}-{agent.role}.txt"
                    sec_file.write_text(agent.stdout, encoding="utf-8")
                    sec_path = str(sec_file)
                    processor.emit(
                        event_type=EventType.ARTIFACT_CREATED,
                        task_id=f"wf-{phase}-{agent.role}",
                        source="cli",
                        data={"path": str(sec_path), "kind": "wf_result", "producer": agent.provider_name, "status": "final", "replaces": None},
                    )
                    secondary_result_path = str(sec_path)
                    secondary_provider = agent.provider_name
                    print(f"multi_agent_{agent.role}: {agent.provider_name} ({agent.elapsed_sec:.1f}s)", file=sys.stderr)

        # Pattern-specific post-processing: finding-based feedback (subagent only)
        # Team results use their own iterative turn loop for feedback; skip here
        if not is_team and synthesis_pattern in ("generate_then_validate", "implement_then_review"):
            _run_finding_feedback_loop(
                multi_result=multi_result,
                phase=phase,
                pattern=synthesis_pattern,
                state=state,
                processor=processor,
            )
            # Persist audit trail added by _run_finding_feedback_loop
            from awf.core.state import save_workflow_state_snapshot
            save_workflow_state_snapshot(args.repo_root, state)

        # Save judge verdict
        verdict_file = ma_dir / "judge-verdict.json"
        verdict_file.write_text(json_mod.dumps({
            "mode": multi_result.mode,
            "verdict": multi_result.judge_verdict,
            "reason": multi_result.judge_reason,
            "selected_agent": multi_result.selected_agent,
            "pattern": synthesis_pattern,
            "phase_pattern": phase_pattern,
            "agents": [{"provider": a.provider_name, "role": a.role, "ok": a.ok, "elapsed_sec": round(a.elapsed_sec, 1)} for a in multi_result.agents],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"multi_agent_judge: {multi_result.judge_verdict} ({multi_result.judge_reason})", file=sys.stderr)
        print(f"=== multi-agent: {exec_mode} complete ===", file=sys.stderr)
        processor.emit(
            event_type=EventType.STAGE_COMPLETED,
            task_id=f"wf-{phase}-multi-agent",
            source="cli",
            data={"stage": "multi_agent", "verdict": multi_result.judge_verdict, "pattern": synthesis_pattern},
        )
        if not multi_result.ok:
            last_returncode = 1

    if auto_apply and phase in {"review", "verify"} and last_returncode == 0:
        apply_started_at = time.monotonic()
        processor.emit(
            event_type=EventType.STAGE_STARTED,
            task_id=f"wf-{phase}",
            source="cli",
            data={"stage": "apply", "description": "apply workflow result and evaluate gate"},
        )
        try:
            selected_result_path = str(result_path)
            selected_provider = selected_provider
            gate_passed = False
            if secondary_result_path is not None:
                synthesis = synthesize_workflow_multi_provider_results(
                    args.repo_root,
                    phase,
                    str(result_path),
                    str(secondary_result_path),
                    primary_provider=selected_provider,
                    secondary_provider=secondary_provider or "secondary",
                    change_class=state.get("changeClass", "standard"),
                )
                selected_result_path = str(synthesis["selected_result_path"])
                selected_provider = str(synthesis["selected_provider"])
                print(f"judge_secondary_provider: {secondary_provider}")
                print(f"judge_result: {'PASS' if synthesis['judge_passed'] else 'FAIL'}")
                if synthesis["judge_reasons"]:
                    print("judge_reasons: " + ", ".join(synthesis["judge_reasons"]))
                    print("judge_reason_details: " + " | ".join(explain_judge_reasons(synthesis["judge_reasons"])))
                print(f"synthesis_selected_provider: {selected_provider}")
                print(f"synthesis_result: {'PASS' if synthesis['final_passed'] else 'FAIL'}")
                if synthesis["synthesis_reasons"]:
                    print("synthesis_reasons: " + ", ".join(synthesis["synthesis_reasons"]))
                    print(
                        "synthesis_reason_details: "
                        + " | ".join(explain_judge_reasons(synthesis["synthesis_reasons"]))
                    )
                if synthesis.get("selection_summary"):
                    print(f"synthesis_selection_basis: {synthesis['selection_summary']}")
                record_workflow_synthesis(
                    args.repo_root,
                    phase,
                    selected_provider=selected_provider,
                    selected_result_path=selected_result_path,
                    judge_passed=bool(synthesis["judge_passed"]),
                    judge_reasons=list(synthesis["judge_reasons"]),
                    synthesis_passed=bool(synthesis["final_passed"]),
                    synthesis_reasons=list(synthesis["synthesis_reasons"]),
                    selection_summary=str(synthesis.get("selection_summary", "") or ""),
                    secondary_provider=secondary_provider,
                )
            synthesis_summary = None
            if secondary_result_path is not None:
                synthesis_summary = {
                    "selected_provider": selected_provider,
                    "secondary_provider": secondary_provider,
                    "judge_passed": bool(synthesis["judge_passed"]),
                    "judge_reasons": list(synthesis["judge_reasons"]),
                    "synthesis_passed": bool(synthesis["final_passed"]),
                    "synthesis_reasons": list(synthesis["synthesis_reasons"]),
                    "selection_summary": str(synthesis.get("selection_summary", "") or ""),
                }
            has_synthesis = secondary_result_path is not None
            artifact_path, gate_passed = apply_workflow_result(
                args.repo_root,
                phase,
                selected_result_path,
                synthesis_summary=synthesis_summary,
                skip_gate_apply=has_synthesis,
                change_class=state.get("changeClass", "standard"),
            )
            processor.emit(
                event_type=EventType.ARTIFACT_CREATED,
                task_id=f"wf-{phase}",
                source="cli",
                data={
                    "path": str(artifact_path),
                    "kind": "wf_artifact",
                    "producer": "cli",
                    "status": "final",
                    "replaces": None,
                },
            )
            if has_synthesis:
                gate_passed = gate_passed and bool(synthesis["final_passed"])
                apply_gate_result(args.repo_root, phase, gate_passed)
            processor.emit(
                event_type=EventType.GATE_EVALUATED,
                task_id=f"wf-{phase}",
                source="cli",
                data={"gate": f"G{2 if phase == 'review' else 5}", "passed": gate_passed},
            )
            processor.emit(
                event_type=EventType.STAGE_COMPLETED,
                task_id=f"wf-{phase}",
                source="cli",
                data={"stage": "apply", "duration_sec": round(time.monotonic() - apply_started_at, 3)},
            )
            processor.emit(
                event_type=EventType.PHASE_COMPLETED,
                task_id=f"wf-{phase}",
                source="cli",
                data={"phase": phase},
            )

            # K5: Copy artifacts to work_history
            from awf.core.work_history import record_phase_completion
            try:
                repo_root = resolve_repo_root(args.repo_root)
                copied = record_phase_completion(repo_root, phase)
                if copied:
                    print(f"work_history: {len(copied)} artifact(s) archived", file=sys.stderr)
            except Exception:
                pass

            print(f"applied_artifact: {artifact_path}")
            print(f"applied_gate: {'PASS' if gate_passed else 'FAIL'}")
            refreshed_state = load_workflow_state(args.repo_root)
            event_summary = summarize_event_sync(refreshed_state.get("eventSync"))
            if event_summary:
                print(event_summary, file=sys.stderr)
            if gate_passed and not non_interactive:
                print(f"next_step: review the updated workflow state and continue to the next phase")
            elif not gate_passed and not non_interactive:
                print(f"next_step: inspect {artifact_path} and update workflow artifacts before retrying")
            return 0 if gate_passed else 3
        except Exception as exc:
            processor.emit(
                event_type=EventType.TASK_FAILED,
                task_id=f"wf-{phase}",
                source="cli",
                data={"error": f"auto-apply failed: {exc}"},
            )
            print(f"error: auto-apply failed: {exc}", file=sys.stderr)
            return 4

    return last_returncode


_INLINE_MODEL_ALIASES: dict[str, str] = {
    "sonnet": "claude:sonnet",
    "opus": "claude-code",
    "haiku": "claude-sdk",
    "codex": "codex",
}


# --- Multi-agent synthesis patterns (SKILL.md §5C) ---

# Pattern selection per phase
_PHASE_SYNTHESIS_PATTERNS: dict[str, str] = {
    "review": "parallel_evaluate",
    "verify": "parallel_evaluate",
    "plan": "generate_then_validate",
    "test": "generate_then_validate",
    "impl": "implement_then_review",
    "approve": "parallel_evaluate",  # HIL phase — secondary provides recommendation
    "done": "parallel_evaluate",
}

# Phases for which `parallel_evaluate` should engage automatically when the
# user did not pass `--mode`. The synthesis policy in
# ``synthesize_workflow_multi_provider_results`` is already tuned for these
# phases (review = coverage-based selection, verify = compliance-based), so
# auto-promoting solo → cross gives the gate the dual-evaluator quality
# without forcing every project to remember the flag.
_DEFAULT_DUAL_STRATEGY_PHASES: tuple[str, ...] = ("review", "verify")


def _resolve_dual_strategy_phases(provider_config: dict) -> list[str]:
    """Read ``wf.dual_strategy_phases`` from provider-config.json.

    Falls back to ``_DEFAULT_DUAL_STRATEGY_PHASES`` when the section is
    missing or malformed. Setting an explicit empty list disables the
    auto-promotion entirely (opt-out at the project level).
    """
    section = (provider_config or {}).get("wf", {})
    if not isinstance(section, dict):
        return list(_DEFAULT_DUAL_STRATEGY_PHASES)
    raw = section.get("dual_strategy_phases", _DEFAULT_DUAL_STRATEGY_PHASES)
    if not isinstance(raw, list):
        return list(_DEFAULT_DUAL_STRATEGY_PHASES)
    return [str(item).strip() for item in raw if isinstance(item, str) and item.strip()]


def _maybe_auto_promote_dual_strategy(
    *,
    user_mode: str | None,
    phase: str,
    provider_config: dict,
    repo_root: str,
) -> tuple[str | None, bool]:
    """Decide whether to auto-promote ``solo`` → ``cross`` for this phase.

    Returns a ``(promoted_mode, was_promoted)`` tuple. If the user passed
    ``--mode`` explicitly we never touch their choice — including
    ``--mode solo`` which is the documented opt-out path.
    """
    if user_mode is not None:
        return user_mode, False
    phases = _resolve_dual_strategy_phases(provider_config)
    if phase not in phases:
        return user_mode, False
    print(
        f"dual_strategy_auto_promote: {phase} phase → cross "
        f"(pass --mode solo to opt out)",
        file=sys.stderr,
    )
    try:
        from awf.core.operational_metrics import record_event
        from awf.core.wiki import log_event

        record_event(
            repo_root,
            "dual_strategy_engaged",
            {"phase": phase, "promoted_from": "solo", "promoted_to": "cross"},
        )
        log_event(
            repo_root,
            "dual_strategy_engaged",
            f"{phase}: solo → cross",
        )
    except Exception:
        # Telemetry is best-effort: never block phase execution.
        pass
    return "cross", True


def _select_synthesis_pattern(phase: str, exec_mode: str) -> str:
    """Select multi-agent synthesis pattern based on phase.

    Patterns:
    - parallel_evaluate: Both LLMs evaluate independently → merge (review, verify)
    - generate_then_validate: Primary generates → Secondary pre-validates → feedback (plan, test)
    - implement_then_review: Primary implements → Secondary post-reviews → feedback (impl)
    """
    return _PHASE_SYNTHESIS_PATTERNS.get(phase, "parallel_evaluate")


def _run_finding_feedback_loop(
    *,
    multi_result,
    phase: str,
    pattern: str,
    state: dict,
    processor,
) -> None:
    """Process secondary agent findings and record feedback history.

    For generate_then_validate and implement_then_review patterns,
    CRITICAL/HIGH findings from secondary agents are flagged for user review.
    """
    secondary_findings: list[dict] = []
    for agent in multi_result.agents:
        if agent.role == "primary":
            continue
        for finding in agent.findings:
            severity = str(finding.get("severity", "")).upper()
            if severity in ("CRITICAL", "HIGH"):
                secondary_findings.append({
                    "provider": agent.provider_name,
                    "role": agent.role,
                    "severity": severity,
                    "description": str(finding.get("description", finding.get("summary", "")))[:200],
                    "location": str(finding.get("location", finding.get("locations", "")))[:100],
                })

    if not secondary_findings:
        return

    action_label = "pre-validated" if pattern == "generate_then_validate" else "post-reviewed"
    print(
        f"multi_agent_{action_label}: {len(secondary_findings)} CRITICAL/HIGH findings "
        f"from secondary agent(s)",
        file=sys.stderr,
    )
    for f in secondary_findings[:5]:
        print(
            f"  ⚠ [{f['severity']}] {f['provider']}/{f['role']}: {f['description']}",
            file=sys.stderr,
        )
    if len(secondary_findings) > 5:
        print(f"  ... and {len(secondary_findings) - 5} more", file=sys.stderr)

    # Record in history for audit trail
    history = state.setdefault("history", [])
    from awf.core.state import _now_iso
    history.append({
        "phase": phase,
        "action": action_label,
        "timestamp": _now_iso(),
        "details": (
            f"pattern={pattern} findings={len(secondary_findings)} "
            f"providers={','.join(set(f['provider'] for f in secondary_findings))}"
        ),
    })

    # Emit event for observability
    if processor:
        processor.emit(
            event_type=EventType.STAGE_COMPLETED,
            task_id=f"wf-{phase}-feedback",
            source="cli",
            data={
                "stage": f"{action_label}_feedback",
                "finding_count": len(secondary_findings),
                "pattern": pattern,
            },
        )


VERIFY_FIX_LOOP_WARN_THRESHOLD = 3
VERIFY_FIX_LOOP_HARD_LIMIT = 5


def _verify_fix_loop_status(state: dict, phase: str) -> tuple[str, int]:
    """§3.2: classify the current verify-loop depth.

    Returns ("ok" | "warn" | "abort", executions). Only the verify phase is
    subject to the cap — other phases keep the existing retry budget logic.
    Both thresholds are intentionally hardcoded for now; a later cycle can
    promote them to `provider-config.json` once we have telemetry to pick
    sensible per-team defaults.
    """
    if phase != "verify":
        return ("ok", 0)
    executions = int(state.get("phases", {}).get(phase, {}).get("executions", 0) or 0)
    # `executions` is incremented inside mark_phase_in_progress; at the
    # decision point we look at the count BEFORE the upcoming increment,
    # so the next run will be executions+1.
    next_executions = executions + 1
    if next_executions > VERIFY_FIX_LOOP_HARD_LIMIT:
        return ("abort", next_executions)
    if next_executions >= VERIFY_FIX_LOOP_WARN_THRESHOLD:
        return ("warn", next_executions)
    return ("ok", next_executions)


def _find_fresh_result_file(
    explicit_root: Optional[str],
    phase: str,
    *,
    max_age_sec: int = 1800,
) -> Optional[Path]:
    """Return the newest .workflow/tmp/result-{phase}-*.txt if it is fresh.

    §1.4 helper: when the operator re-runs `awf wf next` on an in_progress
    phase, this detects whether a recent executor result already exists so we
    can suggest applying it instead of re-executing. `max_age_sec` defaults
    to 30 minutes — well over a typical verify executor's runtime but short
    enough that genuinely stalled phases will fall through.
    """
    try:
        root = resolve_repo_root(explicit_root)
    except Exception:
        return None
    tmp_dir = root / ".workflow" / "tmp"
    if not tmp_dir.is_dir():
        return None
    candidates = sorted(
        (p for p in tmp_dir.glob(f"result-{phase}-*.txt") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    newest = candidates[0]
    age = time.time() - newest.stat().st_mtime
    return newest if age <= max_age_sec else None


def _resolve_phase_effort(provider_config: dict, phase: str) -> dict[str, str | None]:
    """Resolve effort settings for a phase from phase_models."""
    phase_models = provider_config.get("phase_models", {})
    phase_model = phase_models.get(phase, {})
    return {
        "effort": phase_model.get("effort"),
        "codex_reasoning": phase_model.get("codex_reasoning"),
        "inline_model": phase_model.get("inline_model"),
    }


def _apply_phase_effort(provider, provider_config: dict, phase: str) -> None:
    """Apply phase-specific effort and model settings to a provider instance.

    Without set_model wiring, `phase_models.{phase}.inline_model` would only
    flip the provider alias (claude-code → claude:sonnet) inside
    _resolve_phase_provider; on the actual CLI command it would never produce
    a `--model` flag when the global provider was already `claude-code`.
    That left impl/test phases on the opus default, which is the cost spike
    described in 2026-05-13 BLIP Gem cycle issues §8.
    """
    effort = _resolve_phase_effort(provider_config, phase)
    if hasattr(provider, "effort") and effort.get("effort"):
        provider.effort = effort["effort"]
    if hasattr(provider, "reasoning_effort") and effort.get("codex_reasoning"):
        provider.reasoning_effort = effort["codex_reasoning"]
    inline_model = effort.get("inline_model")
    if inline_model and hasattr(provider, "set_model"):
        provider.set_model(str(inline_model))


def _resolve_phase_provider(
    explicit: str | None,
    provider_config: dict,
    phase: str,
    config: "AwfConfig",
) -> str:
    """Resolve provider for a phase.  Priority: CLI --provider > phase_models > global default."""
    if explicit:
        return explicit
    phase_models = provider_config.get("phase_models", {})
    phase_model = phase_models.get(phase, {})
    inline_model = phase_model.get("inline_model")
    if inline_model:
        return _INLINE_MODEL_ALIASES.get(str(inline_model), str(inline_model))
    return config.provider_name()


def _resolve_fallback_chain(provider_name: str, provider_config: dict, mode: str | None = None) -> list[str]:
    if mode == "critical":
        configured = ["codex", "claude:sonnet", *provider_config.get("fallback_chain", [])]
    else:
        configured = provider_config.get("fallback_chain", [])
    chain = [provider_name]
    for item in configured:
        if item not in chain:
            chain.append(item)
    return chain


def _orchestrator_decision_from_escape(escape: dict | None, *, state: dict | None = None) -> tuple[str, str, str | None]:
    if not isinstance(escape, dict):
        return "escalate_user", "worker escape requires operator review", None
    state = state or {}
    recommended = str(escape.get("recommended_action", "") or "").strip()
    reason = str(escape.get("reason", "") or "").strip() or "worker_escape"
    severity = str(escape.get("severity", "") or "").strip().lower()
    replan_target = str(escape.get("replan_target", "") or escape.get("replanTarget", "") or "").strip() or "plan"
    replan_count, max_replans = _workflow_replan_budget(state)

    if severity == "advisory":
        return "continue", f"{reason} marked advisory", None
    if severity == "degraded" and reason == "quality_threshold":
        return "continue", f"{reason} accepted as degraded", None
    if severity == "blocking" and reason == "constraint_violation":
        return "abort", f"{reason} violates workflow constraints", None
    if replan_count >= max_replans and (severity == "blocking" or recommended == "replan" or reason in {"spec_divergence", "scope_divergence", "scope_expansion", "ambiguous_requirement"}):
        return "escalate_user", f"{reason} exceeded replan budget {replan_count}/{max_replans}", replan_target
    if reason in {"spec_divergence", "scope_divergence", "scope_expansion", "ambiguous_requirement"}:
        return "replan", f"{reason} requires plan revision", replan_target
    if severity == "blocking" and reason in {"dependency_missing", "external_blocker"}:
        return "escalate_user", f"{reason} requires operator intervention", None
    if recommended == "abort":
        return "abort", f"{reason} requested abort", None
    if recommended == "replan":
        if replan_count >= max_replans:
            return "escalate_user", f"{reason} exceeded replan budget {replan_count}/{max_replans}", replan_target
        return "replan", f"{reason} requested replan", replan_target
    if recommended == "continue":
        return "continue", f"{reason} requested continue", None
    if recommended == "user_decision":
        return "escalate_user", f"{reason} requires user decision", None
    return "escalate_user", f"{reason} requires operator review", None


def run_wf_expand_scope(args: argparse.Namespace) -> int:
    """Expand `.workflow/artifacts/allowed-files.json` using saved import graphs.

    Reads the planned_files list, looks each path up in the per-unit
    import graphs written by `awf analyze`, and either prints a diff
    (--dry-run) or writes back the merged payload with an audit trail.
    """
    from awf.core.wf_scope import (
        VALID_DIRECTIONS,
        apply_expansion_to_payload,
        expand_allowed_files,
        load_allowed_files,
        planned_files_from_payload,
        save_allowed_files,
    )

    try:
        repo_root = resolve_repo_root(args.repo_root)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.direction not in VALID_DIRECTIONS:
        print(
            f"error: --direction must be one of {VALID_DIRECTIONS}",
            file=sys.stderr,
        )
        return 2

    try:
        payload = load_allowed_files(repo_root)
    except FileNotFoundError as exc:
        print(f"error: allowed-files.json not found at {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: allowed-files.json is not valid JSON: {exc}", file=sys.stderr)
        return 2

    planned = planned_files_from_payload(payload)
    if not planned:
        print("warning: planned_files is empty; nothing to expand", file=sys.stderr)
        return 0

    try:
        docs_root = Path(resolve_runtime_paths(str(repo_root))["analysis_docs"])
    except Exception as exc:
        print(f"error: cannot resolve docs_root: {exc}", file=sys.stderr)
        return 2

    services = list(args.service) if args.service else None
    depth = None if args.depth in (None, 0) else int(args.depth)

    result = expand_allowed_files(
        planned,
        docs_root,
        services=services,
        direction=args.direction,
        depth=depth,
        runtime_only=bool(args.runtime_only),
    )

    if args.json:
        print(
            json.dumps(
                {
                    "planned": list(result.planned),
                    "added": list(result.added),
                    "entries": [{"path": e.path, "reason": e.reason} for e in result.entries],
                    "coverage": dict(result.coverage),
                    "direction": result.direction,
                    "depth": result.depth,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"planned: {len(result.planned)} file(s)")
        print(
            f"added:   {len(result.added)} file(s) "
            f"(direction={result.direction}, depth={result.depth})"
        )
        for entry in result.entries[:30]:
            print(f"  + {entry.path}  ({entry.reason})")
        if len(result.entries) > 30:
            print(f"  ... and {len(result.entries) - 30} more")
        ungraphed = sorted(
            p for p, status in result.coverage.items() if status != "no_graph" and not status.startswith("found_in:")
        )
        if ungraphed:
            print(
                f"\nnote: {len(ungraphed)} planned file(s) not found in any saved graph "
                "(run `awf analyze` for the relevant units to improve coverage)"
            )
            for path in ungraphed[:10]:
                print(f"  · {path}")
            if len(ungraphed) > 10:
                print(f"  ... and {len(ungraphed) - 10} more")

    if args.dry_run:
        return 0

    new_payload = apply_expansion_to_payload(payload, result)
    out_path = save_allowed_files(repo_root, new_payload)
    print(f"\nwrote: {out_path}")
    return 0


def run_wf_scope_check(args: argparse.Namespace) -> int:
    """Deterministic G5 scope check: git diff vs allowed-files.json.

    Compares changed files against `planned_files` (and `expanded_files`
    unless --no-expanded) and prints per-file classification. Exit code
    is 1 if any violation is found, 0 otherwise — suitable for direct
    consumption by the verify SKILL or a CI gate.
    """
    from awf.core.wf_scope import (
        STATUS_EXPANDED,
        STATUS_PLANNED,
        STATUS_VIOLATION,
        check_scope_violations,
    )

    try:
        repo_root = resolve_repo_root(args.repo_root)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        result = check_scope_violations(
            repo_root,
            base_branch=args.base_branch,
            include_expanded=not args.no_expanded,
        )
    except FileNotFoundError as exc:
        print(f"error: allowed-files.json not found at {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: allowed-files.json is not valid JSON: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        # Malformed sibling_repos in manifest.json. See multi-repo-scope spec §5.
        print(f"error: manifest.json sibling_repos invalid: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        from awf.core.operational_metrics import record_scope_check
        from awf.core.wiki import log_event

        record_scope_check(repo_root, result)
        log_event(
            repo_root,
            "scope_check",
            f"violations={result.violation_count} "
            f"changed={len(result.changed_files)} "
            f"planned={len(result.planned_set)}",
        )
    except Exception as exc:
        print(f"warning: operations metrics record failed: {exc}", file=sys.stderr)

    if args.json:
        print(json.dumps(result.to_json(), ensure_ascii=False, indent=2))
        return _scope_check_exit_code(result)

    is_multi = len(result.per_repo) > 1
    if is_multi:
        sibling_count = len(result.per_repo) - 1
        print(
            f"=== Scope Check (multi-repo: 1 root + {sibling_count} sibling"
            f"{'s' if sibling_count != 1 else ''}) ==="
        )
    else:
        print(f"=== Scope Check (base: {result.base_branch}) ===")
    print(
        f"planned: {len(result.planned_set)}, "
        f"expanded: {len(result.expanded_set)}, "
        f"changed: {len(result.changed_files)}"
    )
    counts: dict[str, int] = {STATUS_PLANNED: 0, STATUS_EXPANDED: 0, STATUS_VIOLATION: 0}
    for c in result.classifications:
        counts[c.status] = counts.get(c.status, 0) + 1
    verdict_line = (
        f"verdict: {counts[STATUS_PLANNED]} planned, "
        f"{counts[STATUS_EXPANDED]} expanded, "
        f"{counts[STATUS_VIOLATION]} violation(s)"
    )
    if result.repo_error_count:
        verdict_line += f", {result.repo_error_count} repo error(s)"
    print(verdict_line)

    icon = {STATUS_PLANNED: "✓", STATUS_EXPANDED: "+", STATUS_VIOLATION: "✗"}
    if is_multi:
        for r in result.per_repo:
            header = f"[{r.name or 'root'} @ {r.base_branch or '?'}]"
            print(f"\n{header}")
            if r.error:
                print(f"  ERROR: {r.error} — path {r.path}")
                continue
            print(
                f"  planned: {len([p for p in result.planned_set if _belongs_to(p, r.name)])}, "
                f"changed: {len(r.changed_files)}"
            )
            for c in r.classifications:
                print(f"  {icon.get(c.status, '?')} {c.status:<10} {c.path}  ({c.reason})")
        if any(v.reason.startswith("unknown sibling") for v in result.violations):
            print("\n[unknown sibling prefixes in allowed-files.json]")
            for v in result.violations:
                if v.reason.startswith("unknown sibling"):
                    print(f"  ✗ violation  {v.path}  ({v.reason})")
    else:
        for c in result.classifications:
            print(f"  {icon.get(c.status, '?')} {c.status:<10} {c.path}  ({c.reason})")

    if result.planned_not_changed:
        print(
            f"\nplanned but not changed ({len(result.planned_not_changed)}): "
            "possible missing implementation"
        )
        for path in result.planned_not_changed[:10]:
            print(f"  · {path}")
        if len(result.planned_not_changed) > 10:
            print(f"  ... and {len(result.planned_not_changed) - 10} more")

    return _scope_check_exit_code(result)


def _belongs_to(prefixed_path: str, repo_name: str) -> bool:
    if not repo_name:
        return not prefixed_path.startswith("@")
    return prefixed_path.startswith(f"@{repo_name}/")


def _scope_check_exit_code(result) -> int:
    # Repo-level errors are config mistakes (missing path, branch ambiguous,
    # not-a-git-repo). Distinguish from scope violations via exit code 2.
    if result.repo_error_count > 0:
        return 2
    return 1 if result.violation_count > 0 else 0
