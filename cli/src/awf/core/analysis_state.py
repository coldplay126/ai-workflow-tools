from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from awf.core.config import AnalysisContext
from awf.core.analysis_resume import (
    cleanup_failed_stage2_artifacts,
    cleanup_orphan_tmp_files,
    finalize_analysis_run,
    resolve_analysis_resume,
)
from awf.core.analysis_store import (
    compute_bundle_config_hash,
    mark_analysis_started,
    mark_stage3_failed,
    mark_stage3_skipped,
    mark_stage3_started,
    output_files_present,
    read_analysis_artifact,
    record_cross_synthesis,
    record_stage2_fanout_execution,
    save_additional_analysis_result,
    save_analysis_prompt,
    save_analysis_result,
    save_domain_bundle,
    save_project_bundle,
    save_stage1_memo,
    save_stage2_draft,
    save_stage2_fanout_scaffold,
    save_stage3_final,
)
from awf.core.analysis_status import summarize_analysis_state


# Default output files (used only when spec-as-truth loading fails AND mode is unknown)
_DEFAULT_OUTPUT_FILES = [
    "api-spec.json",
    "data-model.md",
    "domain-overview.md",
    "external-integration.md",
]

# Kept for backward compatibility imports
REQUIRED_OUTPUT_FILES = _DEFAULT_OUTPUT_FILES


def get_required_output_files(mode: str = "document") -> list[str]:
    """Return required output files for the given analysis mode (A6).

    Loads from external mode contract (spec-as-truth).
    Falls back to default document output files only for 'document' mode.
    Returns empty list for other modes if spec is missing.
    """
    try:
        from awf.core.spec_loader import load_analysis_mode_contract
        contract = load_analysis_mode_contract(mode)
        return list(contract["required_output_files"])
    except (FileNotFoundError, ValueError, KeyError):
        if mode == "document":
            return list(_DEFAULT_OUTPUT_FILES)
        return []


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_ai_context_dirs(context: AnalysisContext) -> tuple[Path, Path]:
    ai_context_dir = context.ai_context_dir
    tmp_dir = ai_context_dir / ".tmp"
    ai_context_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return ai_context_dir, tmp_dir


def analysis_state_path(context: AnalysisContext) -> Path:
    return context.ai_context_dir / ".analysis-state.json"


def load_analysis_state(context: AnalysisContext) -> dict:
    state_path = analysis_state_path(context)
    if state_path.exists():
        raw = state_path.read_text(encoding="utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Corrupted state file — try one retry (transient write race)
            import sys
            try:
                raw_retry = state_path.read_text(encoding="utf-8")
                return json.loads(raw_retry)
            except (json.JSONDecodeError, OSError):
                print(
                    f"warning: corrupted analysis state at {state_path}, resetting to fresh state",
                    file=sys.stderr,
                )
                # Rename corrupted file for forensics, return fresh state
                backup = state_path.with_suffix(".json.corrupted")
                try:
                    state_path.rename(backup)
                except OSError:
                    pass
    return {
        "id": f"analysis-{context.service}-{context.domain}-{datetime.now().strftime('%Y%m%d')}",
        "service": context.service,
        "domain": context.domain,
        "mode": context.mode,
        "scale": "standard",
        "startedAt": _now_iso(),
        "completedAt": None,
        "currentLayer": "input",
        "currentStage": 1,
        "layers": {
            "input": {"status": "pending"},
            "bundle": {"status": "pending", "fileCount": 0, "lineCount": 0, "tokenEstimate": 0},
            "analyze": {
                "stage1": {
                    "status": "pending", "provider": "codex", "errorMessage": "", "retryCount": 0,
                    "observation": {"total_files": 0, "cached": 0, "analyzed": 0, "cache_hit_rate": 0.0},
                },
                "stage2": {"status": "pending", "provider": "", "errorMessage": "", "retryCount": 0},
                "stage3": {"status": "pending", "provider": "opus", "reason": "", "errorMessage": "", "retryCount": 0},
            },
            "output": {"status": "pending", "errorMessage": ""},
        },
        "summaries": {"stage1": "", "stage2": "", "stage3": ""},
        "artifacts": {
            "domain_bundle": None,
            "project_bundle": None,
            "stage1_memo": ".tmp/stage1-analysis.md",
            "stage2_draft": ".tmp/stage2-draft.md",
            "stage3_final": ".tmp/stage3-final.md",
            "prompt_file": None,
            "result_file": None,
            "fanout_synthesizer_prompt": None,
            "fanout_writer_prompts": {},
        },
    }


def save_analysis_state(context: AnalysisContext, state: dict) -> Path:
    """Atomic save: write to temp file then rename to prevent corruption."""
    import os
    import threading

    path = analysis_state_path(context)
    tmp_path = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)
    return path


def should_run_stage3(
    *,
    mode: str,
    stage3_force: bool,
    stage_routing_stage3: str | None,
    related_domains_count: int,
    stage3_retry_blocked: bool = False,
) -> tuple[bool, str]:
    """Evaluate Stage 3 activation decision (canonical rule).

    Priority (§2 02-stages.md):
    1. mode must be "deep" AND not retry-blocked
    2. stage3_force overrides routing skip
    3. related_domains >= 3 overrides routing skip
    4. Otherwise: follow stage_routing config

    Returns (should_run, reason).
    """
    if mode != "deep":
        return False, "not_deep_mode"

    if stage3_retry_blocked:
        return False, "retry_blocked"

    stage3_skip = stage_routing_stage3 == "skip"

    if stage3_force and stage3_skip:
        stage3_skip = False
        reason_detail = "force_enabled"
    elif not stage3_force and related_domains_count >= 3 and stage3_skip:
        stage3_skip = False
        reason_detail = f"auto_enabled:related_domains={related_domains_count}"
    else:
        reason_detail = "routing_default"

    if stage3_skip:
        return False, f"skipped:routing={stage_routing_stage3}"

    return True, reason_detail


def update_observation_stats(state: dict, *, total_files: int, cached: int, analyzed: int) -> None:
    """Update observation statistics in analysis state (Layer 2 tracking)."""
    stage1 = state.get("layers", {}).get("analyze", {}).get("stage1", {})
    stage1["observation"] = {
        "total_files": total_files,
        "cached": cached,
        "analyzed": analyzed,
        "cache_hit_rate": round(cached / total_files, 2) if total_files > 0 else 0.0,
    }
