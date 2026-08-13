from __future__ import annotations

import threading
from pathlib import Path
from typing import Sequence

from awf.core.analysis_files import hashes_changed, load_hashes_file

# Per-path locks to protect concurrent resume/finalize operations
_ANALYSIS_LOCKS: dict[str, threading.Lock] = {}
_ANALYSIS_LOCKS_GUARD = threading.Lock()


def _analysis_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _ANALYSIS_LOCKS_GUARD:
        lock = _ANALYSIS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _ANALYSIS_LOCKS[key] = lock
        return lock


def _deps():
    from awf.core.analysis_state import ensure_ai_context_dirs, load_analysis_state, save_analysis_state
    from awf.core.analysis_store import (
        compute_bundle_config_hash,
        output_files_present,
    )
    from awf.core.analysis_state import _now_iso

    return {
        "ensure_dirs": ensure_ai_context_dirs,
        "load_state": load_analysis_state,
        "save_state": save_analysis_state,
        "config_hash": compute_bundle_config_hash,
        "output_files_present": output_files_present,
        "now_iso": _now_iso,
    }


def cleanup_failed_stage2_artifacts(context, state: dict) -> list[Path]:
    artifacts = state.get("artifacts", {})
    removed: list[Path] = []
    for key in ("prompt_file", "result_file", "stage2_draft"):
        relative_path = artifacts.get(key)
        if not relative_path:
            continue
        path = context.ai_context_dir / relative_path
        if path.exists():
            path.unlink()
            removed.append(path)
        artifacts[key] = None
    return removed


def cleanup_orphan_tmp_files(context, state: dict) -> list[Path]:
    deps = _deps()
    _, tmp_dir = deps["ensure_dirs"](context)
    artifacts = state.get("artifacts", {})
    referenced = {
        str(artifacts.get("prompt_file") or ""),
        str(artifacts.get("result_file") or ""),
        str(artifacts.get("stage2_draft") or ""),
        str(artifacts.get("domain_bundle") or ""),
        str(artifacts.get("project_bundle") or ""),
    }
    removed: list[Path] = []
    for path in tmp_dir.iterdir():
        if not path.is_file():
            continue
        relative_path = str(path.relative_to(context.ai_context_dir))
        if relative_path in referenced:
            continue
        if path.name.startswith("prompt-stage2-") or path.name.startswith("result-stage2-") or path.name == "stage2-draft.md":
            path.unlink()
            removed.append(path)
    return removed


def resolve_analysis_resume(context, current_file_entries: list[dict[str, str]] | None = None) -> dict:
    from awf.core.analysis_state import analysis_state_path
    deps = _deps()
    lock = _analysis_lock(analysis_state_path(context))
    lock.acquire()
    try:
        return _resolve_analysis_resume_locked(context, current_file_entries, deps)
    finally:
        lock.release()


def _resolve_analysis_resume_locked(context, current_file_entries, deps) -> dict:
    state = deps["load_state"](context)
    output_status = str(state.get("layers", {}).get("output", {}).get("status", "pending"))
    stage2 = state.get("layers", {}).get("analyze", {}).get("stage2", {})
    messages: list[str] = []
    reused_result = False
    blocked_by_retry_limit = False
    files_changed = False

    cleanup_orphan_tmp_files(context, state)

    config_hash = deps["config_hash"](context)
    bundle_layer = state.get("layers", {}).get("bundle", {})
    saved_hash = str(bundle_layer.get("configHash", "") or "")
    domain_bundle = state.get("artifacts", {}).get("domain_bundle")
    bundle_invalidated = bool(saved_hash and saved_hash != config_hash)
    if bundle_invalidated:
        state["layers"]["bundle"]["status"] = "pending"
    if domain_bundle and bundle_invalidated:
        bundle_path = context.ai_context_dir / domain_bundle
        if bundle_path.exists():
            bundle_path.unlink()
        state["artifacts"]["domain_bundle"] = None
        messages.append("resume: domain bundle invalidated by config change.")

    project_bundle = state.get("artifacts", {}).get("project_bundle")
    if project_bundle and bundle_invalidated:
        project_bundle_path = context.ai_context_dir / project_bundle
        if project_bundle_path.exists():
            project_bundle_path.unlink()
        state["artifacts"]["project_bundle"] = None
        messages.append("resume: project bundle invalidated by config change.")
    if bundle_invalidated:
        deps["save_state"](context, state)

    # K0: Check file hashes before evaluating Stage 3 retry eligibility.
    saved_hashes = load_hashes_file(context)
    if current_file_entries is not None and saved_hashes:
        files_changed = hashes_changed(context, current_file_entries)

    # --- Stage 3 resume (A3: stage-level resume contract) ---
    # Resolve a failed Stage 3 before completed outputs can skip its retry.
    stage3 = state.get("layers", {}).get("analyze", {}).get("stage3", {})
    stage3_status = str(stage3.get("status", "pending"))
    stage3_retry_blocked = False
    stage3_requires_attention = stage3_status == "failed"

    # Normalize stale in_progress to pending (interrupted execution)
    if stage3_status == "in_progress":
        stage3["status"] = "pending"
        stage3["errorMessage"] = ""
        deps["save_state"](context, state)
        messages.append("resume: stage3 was in_progress (interrupted); reset to pending.")

    if stage3_status == "failed":
        stage3_error = str(stage3.get("errorMessage", "") or "")
        state["currentLayer"] = "analyze"
        state["currentStage"] = 3
        state["completedAt"] = None
        state["layers"]["output"]["status"] = "failed"
        state["layers"]["output"]["errorMessage"] = (
            stage3_error
            or str(state["layers"]["output"].get("errorMessage", "") or "")
            or "Stage 3 failed."
        )
        stage3_retry_count = int(stage3.get("retryCount", 0) or 0)
        if files_changed or bundle_invalidated:
            state["layers"]["analyze"]["stage3"]["status"] = "pending"
            state["layers"]["analyze"]["stage3"]["retryCount"] = 0
            deps["save_state"](context, state)
            generation_reason = "source files changed" if files_changed else "bundle config changed"
            messages.append(
                f"resume: stage3 failure belongs to a previous generation ({generation_reason}); retry budget reset."
            )
        elif context.mode != "deep":
            deps["save_state"](context, state)
            messages.append("resume: stage3 retry limit does not block standard mode; Stage 3 will be skipped by policy.")
        elif stage3_retry_count >= 2:
            stage3_retry_blocked = True
            deps["save_state"](context, state)
            messages.append(f"resume: stage3 retryCount={stage3_retry_count}; automatic retry is blocked.")
        else:
            state["layers"]["analyze"]["stage3"]["status"] = "pending"
            deps["save_state"](context, state)
            messages.append(f"resume: stage3 failed previously (retryCount={stage3_retry_count}); will retry stage3.")

    if output_status == "completed" and deps["output_files_present"](context):
        if files_changed or bundle_invalidated:
            generation_reason = "source files changed" if files_changed else "bundle config changed"
            messages.append(f"resume: {generation_reason} since last analysis; re-analyzing.")
            # Reset bundle and analyze layers so stale data is not reused.
            state["layers"]["bundle"]["status"] = "pending"
            state["layers"]["analyze"]["stage2"]["status"] = "pending"
            state["layers"]["analyze"]["stage2"]["retryCount"] = 0
            state["layers"]["output"]["status"] = "pending"
            state["completedAt"] = None
            deps["save_state"](context, state)
        elif saved_hashes and not stage3_requires_attention:
            messages.append("resume: existing completed .ai-context detected; skipping provider run.")
            return {
                "state": state,
                "skip_provider": True,
                "messages": messages,
                "reused_result": False,
                "blocked_by_retry_limit": False,
                "stage3_retry_blocked": False,
                "hashes_changed": False,
            }

    # --- Stage dependency validation: Stage 2 reuse requires Stage 1 completed ---
    stage1 = state.get("layers", {}).get("analyze", {}).get("stage1", {})
    stage1_status = str(stage1.get("status", "pending"))

    result_file = state.get("artifacts", {}).get("result_file")
    result_path = context.ai_context_dir / result_file if result_file else None
    reusable_status = str(stage2.get("status", "pending")) in {"in_progress", "completed"}
    can_reuse_generation = not files_changed and not bundle_invalidated
    if result_path and result_path.exists() and reusable_status and not deps["output_files_present"](context):
        if stage1_status != "completed":
            # Stage 1 not completed — cannot trust Stage 2 results
            messages.append("resume: stage2 result exists but stage1 incomplete; discarding stale result.")
            cleanup_failed_stage2_artifacts(context, state)
            deps["save_state"](context, state)
        elif not can_reuse_generation:
            reason = "source files changed" if files_changed else "bundle config changed"
            messages.append(f"resume: saved stage2 result invalidated because {reason}; discarding stale result.")
            cleanup_failed_stage2_artifacts(context, state)
            deps["save_state"](context, state)
        elif stage3_requires_attention:
            messages.append("resume: stage3 retry is pending; saved stage2 result will not bypass it.")
        else:
            reused_result = True
            messages.append(f"resume: reusing saved stage2 result from {result_path.relative_to(context.ai_context_dir)}.")

    if str(stage2.get("status", "pending")) == "failed":
        retry_count = int(stage2.get("retryCount", 0) or 0)
        if files_changed or bundle_invalidated:
            retry_count = 0
            state["layers"]["analyze"]["stage2"]["retryCount"] = retry_count
            generation_reason = "source files changed" if files_changed else "bundle config changed"
            messages.append(
                f"resume: stage2 failure belongs to a previous generation ({generation_reason}); retry budget reset."
            )
        if retry_count >= 2:
            blocked_by_retry_limit = True
            messages.append(f"resume: stage2 retryCount={retry_count}; automatic retry is blocked.")
        else:
            removed = cleanup_failed_stage2_artifacts(context, state)
            deps["save_state"](context, state)
            if removed:
                messages.append(f"resume: removed {len(removed)} failed stage2 artifact(s) before retry.")
            messages.append(f"resume: stage2 failed previously (retryCount={retry_count}); retrying stage2.")


    return {
        "state": state,
        "skip_provider": False,
        "messages": messages,
        "reused_result": reused_result,
        "blocked_by_retry_limit": blocked_by_retry_limit,
        "stage3_retry_blocked": stage3_retry_blocked,
        "hashes_changed": files_changed,
    }


def finalize_analysis_run(
    context,
    provider_name: str,
    returncode: int,
    error_message: str = "",
    *,
    missing_output_files: Sequence[str] = (),
) -> dict:
    from awf.core.analysis_state import analysis_state_path
    deps = _deps()
    lock = _analysis_lock(analysis_state_path(context))
    with lock:
        return _finalize_analysis_run_locked(
            context,
            provider_name,
            returncode,
            error_message,
            missing_output_files,
            deps,
        )


def _finalize_analysis_run_locked(
    context,
    provider_name: str,
    returncode: int,
    error_message: str,
    missing_output_files: Sequence[str],
    deps,
) -> dict:
    state = deps["load_state"](context)
    stage3 = state["layers"]["analyze"]["stage3"]
    missing_error = (
        "missing_required_outputs:" + ", ".join(missing_output_files)
        if missing_output_files
        else ""
    )
    output_complete = (
        returncode == 0
        and not missing_output_files
        and deps["output_files_present"](context)
    )
    if output_complete:
        state["layers"]["analyze"]["stage2"]["retryCount"] = 0
    if stage3.get("status") == "failed" and output_complete:
        state["currentLayer"] = "analyze"
        state["currentStage"] = 3
        state["completedAt"] = None
        state["layers"]["analyze"]["stage2"]["status"] = "completed"
        state["layers"]["analyze"]["stage2"]["errorMessage"] = ""
        state["layers"]["output"]["status"] = "failed"
        state["layers"]["output"]["errorMessage"] = (
            str(stage3.get("errorMessage", "") or "")
            or str(state["layers"]["output"].get("errorMessage", "") or "")
            or "Stage 3 failed."
        )
    elif output_complete:
        state["currentLayer"] = "output"
        state["currentStage"] = 4
        state["completedAt"] = deps["now_iso"]()
        state["layers"]["analyze"]["stage2"]["status"] = "completed"
        state["layers"]["output"]["status"] = "completed"
        state["summaries"]["stage2"] = "Required .ai-context outputs detected."
        state["layers"]["output"]["errorMessage"] = ""
        state["layers"]["analyze"]["stage2"]["errorMessage"] = ""
    else:
        state["currentLayer"] = "analyze"
        state["currentStage"] = 2
        state["completedAt"] = None
        existing_stage2_error = str(state["layers"]["analyze"]["stage2"].get("errorMessage", "") or "")
        existing_output_error = str(state["layers"]["output"].get("errorMessage", "") or "")
        provider_error = error_message if returncode != 0 else ""
        resolved_error = provider_error or missing_error or existing_stage2_error or "Provider run completed without required outputs."
        state["layers"]["analyze"]["stage2"]["status"] = "failed"
        state["layers"]["analyze"]["stage2"]["errorMessage"] = resolved_error
        state["layers"]["output"]["status"] = "failed"
        state["layers"]["output"]["errorMessage"] = provider_error or missing_error or existing_output_error or "Required .ai-context outputs not found."
        retry = int(state["layers"]["analyze"]["stage2"].get("retryCount", 0) or 0)
        state["layers"]["analyze"]["stage2"]["retryCount"] = retry + 1
    state["layers"]["analyze"]["stage2"]["provider"] = provider_name
    deps["save_state"](context, state)
    return state
