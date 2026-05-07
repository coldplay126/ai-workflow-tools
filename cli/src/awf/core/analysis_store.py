from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional


def _state_deps():
    from awf.core.analysis_state import (
        REQUIRED_OUTPUT_FILES,
        get_required_output_files,
        _now_iso,
        ensure_ai_context_dirs,
        load_analysis_state,
        save_analysis_state,
    )

    return {
        "REQUIRED_OUTPUT_FILES": REQUIRED_OUTPUT_FILES,
        "get_required_output_files": get_required_output_files,
        "now_iso": _now_iso,
        "ensure_dirs": ensure_ai_context_dirs,
        "load_state": load_analysis_state,
        "save_state": save_analysis_state,
    }


def compute_bundle_config_hash(context) -> str:
    payload = {
        "service": context.service,
        "domain": context.domain,
        "mode": context.mode,
        "domain_directories": context.domain_directories,
        "all_directories": context.all_directories,
        "related_domains": context.related_domains,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def mark_analysis_started(context, provider_name: str) -> dict:
    deps = _state_deps()
    state = deps["load_state"](context)
    state["mode"] = context.mode
    state["currentLayer"] = "analyze"
    state["currentStage"] = 2
    state["startedAt"] = state.get("startedAt") or deps["now_iso"]()
    state["layers"]["input"]["status"] = "completed"
    if not state["artifacts"].get("domain_bundle"):
        state["layers"]["bundle"]["status"] = "pending"
    state["layers"]["analyze"]["stage2"]["status"] = "in_progress"
    state["layers"]["analyze"]["stage2"]["provider"] = provider_name
    state["layers"]["analyze"]["stage2"]["errorMessage"] = ""
    state["layers"]["output"]["status"] = "in_progress"
    state["layers"]["output"]["errorMessage"] = ""
    deps["save_state"](context, state)
    return state


def save_analysis_prompt(context, provider_name: str, prompt: str) -> Path:
    deps = _state_deps()
    _, tmp_dir = deps["ensure_dirs"](context)
    safe_provider = provider_name.replace(":", "_")
    path = tmp_dir / f"prompt-stage2-{safe_provider}.txt"
    path.write_text(prompt, encoding="utf-8")
    state = deps["load_state"](context)
    state["artifacts"]["prompt_file"] = str(path.relative_to(context.ai_context_dir))
    deps["save_state"](context, state)
    return path


def save_analysis_result(context, provider_name: str, content: str) -> Path:
    deps = _state_deps()
    _, tmp_dir = deps["ensure_dirs"](context)
    safe_provider = provider_name.replace(":", "_")
    path = tmp_dir / f"result-stage2-{safe_provider}.txt"
    path.write_text(content, encoding="utf-8")
    state = deps["load_state"](context)
    state["artifacts"]["result_file"] = str(path.relative_to(context.ai_context_dir))
    deps["save_state"](context, state)
    return path


def save_additional_analysis_result(context, provider_name: str, content: str, suffix: str) -> Path:
    deps = _state_deps()
    _, tmp_dir = deps["ensure_dirs"](context)
    safe_provider = provider_name.replace(":", "_")
    safe_suffix = suffix.replace(":", "_")
    path = tmp_dir / f"result-stage2-{safe_provider}-{safe_suffix}.txt"
    path.write_text(content, encoding="utf-8")
    return path


def save_stage2_draft(context, content: str) -> Path:
    deps = _state_deps()
    _, tmp_dir = deps["ensure_dirs"](context)
    path = tmp_dir / "stage2-draft.md"
    path.write_text(content, encoding="utf-8")
    state = deps["load_state"](context)
    state["artifacts"]["stage2_draft"] = str(path.relative_to(context.ai_context_dir))
    deps["save_state"](context, state)
    return path


def save_stage1_memo(context, content: str, provider_name: str = "awf-cli") -> Path:
    deps = _state_deps()
    _, tmp_dir = deps["ensure_dirs"](context)
    path = tmp_dir / "stage1-analysis.md"
    path.write_text(content, encoding="utf-8")
    state = deps["load_state"](context)
    state["currentLayer"] = "analyze"
    state["currentStage"] = 1
    state["layers"]["analyze"]["stage1"]["status"] = "completed"
    state["layers"]["analyze"]["stage1"]["provider"] = provider_name
    state["layers"]["analyze"]["stage1"]["errorMessage"] = ""
    state["summaries"]["stage1"] = content.splitlines()[0].strip() if content.strip() else "Stage 1 analysis memo created."
    state["artifacts"]["stage1_memo"] = str(path.relative_to(context.ai_context_dir))
    deps["save_state"](context, state)
    return path


def save_stage3_final(
    context,
    content: str,
    provider_name: str = "awf-cli",
    reason: str = "",
    status: str = "completed",
) -> Path:
    deps = _state_deps()
    _, tmp_dir = deps["ensure_dirs"](context)
    path = tmp_dir / "stage3-final.md"
    path.write_text(content, encoding="utf-8")
    state = deps["load_state"](context)
    state["currentLayer"] = "analyze"
    state["currentStage"] = 3
    state["layers"]["analyze"]["stage3"]["status"] = status
    state["layers"]["analyze"]["stage3"]["provider"] = provider_name
    state["layers"]["analyze"]["stage3"]["reason"] = reason
    state["layers"]["analyze"]["stage3"]["errorMessage"] = ""
    if content.strip():
        state["summaries"]["stage3"] = content.splitlines()[0].strip()
    elif status == "scaffold":
        state["summaries"]["stage3"] = "Stage 3 scaffold recorded for future cross-service validation."
    else:
        state["summaries"]["stage3"] = "Stage 3 cross-service validation completed."
    state["artifacts"]["stage3_final"] = str(path.relative_to(context.ai_context_dir))
    deps["save_state"](context, state)
    return path


def mark_stage3_started(context, provider_name: str, reason: str = "") -> dict:
    deps = _state_deps()
    state = deps["load_state"](context)
    state["currentLayer"] = "analyze"
    state["currentStage"] = 3
    state["layers"]["analyze"]["stage3"]["status"] = "in_progress"
    state["layers"]["analyze"]["stage3"]["provider"] = provider_name
    state["layers"]["analyze"]["stage3"]["reason"] = reason
    state["layers"]["analyze"]["stage3"]["errorMessage"] = ""
    deps["save_state"](context, state)
    return state


def mark_stage3_failed(context, provider_name: str, error_message: str, reason: str = "") -> dict:
    deps = _state_deps()
    state = deps["load_state"](context)
    state["currentLayer"] = "analyze"
    state["currentStage"] = 3
    state["completedAt"] = None
    state["layers"]["analyze"]["stage3"]["status"] = "failed"
    state["layers"]["analyze"]["stage3"]["provider"] = provider_name
    state["layers"]["analyze"]["stage3"]["reason"] = reason
    state["layers"]["analyze"]["stage3"]["errorMessage"] = error_message
    state["layers"]["output"]["status"] = "failed"
    state["layers"]["output"]["errorMessage"] = error_message
    retry = int(state["layers"]["analyze"]["stage3"].get("retryCount", 0) or 0)
    state["layers"]["analyze"]["stage3"]["retryCount"] = retry + 1
    deps["save_state"](context, state)
    return state


def mark_stage3_skipped(context, reason: str) -> dict:
    deps = _state_deps()
    state = deps["load_state"](context)
    state["layers"]["analyze"]["stage3"]["status"] = "skipped"
    state["layers"]["analyze"]["stage3"]["reason"] = reason
    state["summaries"]["stage3"] = reason
    state["artifacts"]["stage3_final"] = None
    deps["save_state"](context, state)
    return state


def save_domain_bundle(
    context,
    content: str,
    file_count: int,
    *,
    line_count: int = 0,
    token_estimate: int = 0,
    scale: str | None = None,
) -> Path:
    deps = _state_deps()
    _, tmp_dir = deps["ensure_dirs"](context)
    path = tmp_dir / "domain-bundle.xml"
    path.write_text(content, encoding="utf-8")
    state = deps["load_state"](context)
    state["currentLayer"] = "bundle"
    state["currentStage"] = 1
    if scale:
        state["scale"] = scale
    state["layers"]["bundle"]["status"] = "completed"
    state["layers"]["bundle"]["fileCount"] = file_count
    state["layers"]["bundle"]["lineCount"] = line_count
    state["layers"]["bundle"]["tokenEstimate"] = token_estimate
    state["layers"]["bundle"]["configHash"] = compute_bundle_config_hash(context)
    state["artifacts"]["domain_bundle"] = str(path.relative_to(context.ai_context_dir))
    deps["save_state"](context, state)
    return path


def save_project_bundle(context, content: str) -> Optional[Path]:
    deps = _state_deps()
    if context.mode != "deep":
        return None
    _, tmp_dir = deps["ensure_dirs"](context)
    path = tmp_dir / "project-bundle.xml"
    path.write_text(content, encoding="utf-8")
    state = deps["load_state"](context)
    state["artifacts"]["project_bundle"] = str(path.relative_to(context.ai_context_dir))
    state["layers"]["bundle"]["configHash"] = compute_bundle_config_hash(context)
    deps["save_state"](context, state)
    return path


def output_files_present(context) -> bool:
    deps = _state_deps()
    analysis_mode = getattr(context, "analysis_mode", "document")
    required = deps["get_required_output_files"](analysis_mode)
    if not required:
        return False
    return all((context.ai_context_dir / name).exists() for name in required)


def read_analysis_artifact(context, relative_path: Optional[str]) -> str:
    if not relative_path:
        return ""
    path = context.ai_context_dir / relative_path
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def record_cross_synthesis(
    context,
    *,
    selected_provider: str,
    secondary_provider: str | None,
    judge_passed: bool,
    judge_reasons: list[str],
    synthesis_passed: bool,
    synthesis_reasons: list[str],
    selection_summary: str = "",
) -> dict:
    deps = _state_deps()
    state = deps["load_state"](context)
    stage2 = state["layers"]["analyze"]["stage2"]
    stage2["crossSynthesis"] = {
        "selectedProvider": selected_provider,
        "secondaryProvider": secondary_provider,
        "judgePassed": judge_passed,
        "judgeReasons": list(judge_reasons),
        "synthesisPassed": synthesis_passed,
        "synthesisReasons": list(synthesis_reasons),
        "selectionSummary": selection_summary,
        "recordedAt": deps["now_iso"](),
    }
    deps["save_state"](context, state)
    return state


def save_stage2_fanout_scaffold(
    context,
    *,
    selected: bool,
    scale: str,
    bundle_lines: int,
    bundle_tokens: int,
    prompts: dict[str, str],
) -> dict:
    deps = _state_deps()
    _, tmp_dir = deps["ensure_dirs"](context)
    state = deps["load_state"](context)
    stage2 = state["layers"]["analyze"]["stage2"]
    writer_prompts: dict[str, str] = {}

    synthesizer_prompt = prompts.get("synthesizer", "")
    synthesizer_path = None
    if synthesizer_prompt:
        path = tmp_dir / "prompt-stage2-synthesizer.txt"
        path.write_text(synthesizer_prompt, encoding="utf-8")
        synthesizer_path = str(path.relative_to(context.ai_context_dir))

    for key, prompt in prompts.items():
        if key == "synthesizer":
            continue
        path = tmp_dir / f"prompt-stage2-writer-{key}.txt"
        path.write_text(prompt, encoding="utf-8")
        writer_prompts[key] = str(path.relative_to(context.ai_context_dir))

    stage2["fanout"] = {
        "selected": selected,
        "scale": scale,
        "bundleLines": bundle_lines,
        "bundleTokens": bundle_tokens,
        "recordedAt": deps["now_iso"](),
    }
    state["artifacts"]["fanout_synthesizer_prompt"] = synthesizer_path
    state["artifacts"]["fanout_writer_prompts"] = writer_prompts
    deps["save_state"](context, state)
    return state


def record_stage2_fanout_execution(
    context,
    *,
    used: bool,
    status: str,
    provider_name: str,
    writer_count: int,
    execution_mode: str | None = None,
    fallback_to_single: bool = False,
    consistency_passed: bool | None = None,
    consistency_issues: list[str] | None = None,
    consistency_artifact_suffix: str | None = None,
) -> dict:
    deps = _state_deps()
    state = deps["load_state"](context)
    fanout = state["layers"]["analyze"]["stage2"].setdefault("fanout", {})
    fanout["used"] = used
    fanout["status"] = status
    fanout["provider"] = provider_name
    fanout["writerCount"] = writer_count
    if execution_mode is not None:
        fanout["executionMode"] = execution_mode
    fanout["fallbackToSingle"] = fallback_to_single
    if consistency_passed is not None:
        fanout["consistencyPassed"] = consistency_passed
    if consistency_issues is not None:
        fanout["consistencyIssues"] = list(consistency_issues)
    if consistency_artifact_suffix is not None:
        fanout["consistencyArtifactSuffix"] = consistency_artifact_suffix
    fanout["executedAt"] = deps["now_iso"]()
    deps["save_state"](context, state)
    return state
