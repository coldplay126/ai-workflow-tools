from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from awf.core.analysis_prompt import (
    build_stage2_fanout_prompts,
    build_domain_bundle,
    build_project_bundle,
    build_prompt,
    build_provider_prompt,
    build_stage1_memo,
    build_stage3_note,
    build_stage3_prompt,
    estimate_bundle_tokens,
)
from awf.core.analysis_fanout import analysis_scale, run_stage2_fanout, should_stage2_fanout
from awf.core.analysis_files import collect_domain_files, hashes_changed, load_hashes_file, save_hashes_file
from awf.core.judge import cross_secondary_candidates, explain_judge_reasons, synthesize_cross_stage2
from awf.core.analysis_state import (
    ensure_ai_context_dirs,
    finalize_analysis_run,
    get_required_output_files,
    load_analysis_state,
    mark_stage3_skipped,
    mark_stage3_started,
    mark_stage3_failed,
    mark_analysis_started,
    read_analysis_artifact,
    resolve_analysis_resume,
    record_cross_synthesis,
    save_domain_bundle,
    save_stage1_memo,
    save_stage3_final,
    save_analysis_prompt,
    save_project_bundle,
    save_analysis_result,
    save_additional_analysis_result,
    save_stage2_draft,
    save_stage2_fanout_scaffold,
    save_analysis_state,
    record_stage2_fanout_execution,
    summarize_analysis_state,
)
from awf.core.analysis_outputs import analyze_stage2_output, generate_analysis_report, write_stage2_outputs
from awf.core.config import AnalysisContext, load_awf_config, resolve_analysis_context
from awf.core.events import EventType
from awf.core.artifact_manager import ArtifactManager
from awf.core.event_processor import EventProcessor, run_complete_with_events
from awf.core.event_sync_summary import summarize_event_sync
from awf.core.gateway_runner import run_native_provider_task
from awf.core.permissions import PermissionDeniedError, build_permission_ruleset, check_permission, provider_permission_name
from awf.core.progress import ProgressDisplay
from awf.core.readiness import maybe_doctor_hint
from awf.core.state_updater import AnalysisStateUpdater
from awf.core.task import TaskConstraints, TaskContext, TaskDefinition, TaskType, resolve_execution_mode
from awf.commands.ready_gate import enforce_ready_gate
from awf.providers.base import ProviderCapability
from awf.providers.registry import ProviderRegistry, UnknownProviderError


# Warning threshold: domains larger than this trigger a recommendation to use Claude Code /analysis
SMALL_DOMAIN_FILECOUNT_THRESHOLD = 30  # matches reference.md §1.3 large domain threshold


def _accumulate_knowledge_safe(context) -> None:
    """K6: Accumulate domain knowledge after successful analysis."""
    try:
        from awf.core.knowledge import accumulate_knowledge
        ctx_path = accumulate_knowledge(context)
        if ctx_path:
            print(f"project_context: {ctx_path}")
    except Exception as exc:
        print(f"warning: knowledge accumulation failed: {exc}", file=sys.stderr)


def _int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _count_analysis_output_files(context) -> int:
    names = list(get_required_output_files(getattr(context, "analysis_mode", "document")))
    if "ANALYSIS_REPORT.md" not in names:
        names.append("ANALYSIS_REPORT.md")
    return sum(1 for name in names if (context.ai_context_dir / name).is_file())


def _analysis_complete_metrics(context) -> dict[str, int]:
    state = load_analysis_state(context)
    bundle = state.get("layers", {}).get("bundle", {}) or {}
    sync_bundle = (
        (state.get("eventSync", {}) or {}).get("analysisBundle", {}) or {}
    )
    metrics: dict[str, int] = {}
    for key, primary, fallback in [
        ("source_file_count", sync_bundle.get("sourceFileCount"), bundle.get("fileCount")),
        ("bundle_line_count", sync_bundle.get("lineCount"), bundle.get("lineCount")),
        ("bundle_token_estimate", sync_bundle.get("tokenEstimate"), bundle.get("tokenEstimate")),
    ]:
        value = _int_or_none(primary)
        if value is None:
            value = _int_or_none(fallback)
        if value is not None:
            metrics[key] = value
    metrics["output_file_count"] = _count_analysis_output_files(context)
    return metrics


def _record_analysis_complete_safe(
    context, *, started_at: float | None, mode: str | None = None
) -> None:
    """Persist an analysis_complete summary to .awf-operations/.

    Failures are swallowed so telemetry can never block completion.
    """
    try:
        import time as _time
        from awf.core.operational_metrics import record_analysis_complete
        from awf.core.wiki import log_event

        total_seconds = None
        if started_at is not None:
            total_seconds = _time.monotonic() - started_at
        metrics = _analysis_complete_metrics(context)
        payload: dict = {
            "service": getattr(context, "service", None),
            "domain": getattr(context, "domain", None),
            "mode": mode or getattr(context, "mode", None),
            **metrics,
        }
        if total_seconds is not None:
            payload["total_seconds"] = round(total_seconds, 2)
        record_analysis_complete(
            context.repo_root,
            service=payload["service"],
            domain=payload["domain"],
            mode=payload["mode"],
            total_seconds=total_seconds,
            source_file_count=metrics.get("source_file_count"),
            bundle_line_count=metrics.get("bundle_line_count"),
            bundle_token_estimate=metrics.get("bundle_token_estimate"),
            output_file_count=metrics.get("output_file_count"),
        )
        elapsed_part = (
            f" elapsed={payload['total_seconds']}s"
            if "total_seconds" in payload
            else ""
        )
        log_event(
            context.repo_root,
            "analysis_complete",
            f"{payload['service']}/{payload['domain']} mode={payload['mode']}"
            f"{elapsed_part}",
        )
    except Exception as exc:
        print(f"warning: analysis_complete record failed: {exc}", file=sys.stderr)


def _record_stage1_invalidation_safe(
    context, invalidation, *, transitive_enabled: bool
) -> None:
    """Persist a stage1 invalidation summary to .awf-operations/.

    Failures are swallowed: operational telemetry must never block analysis.
    """
    try:
        from awf.core.operational_metrics import record_stage1_invalidation
        from awf.core.wiki import log_event

        service = getattr(getattr(context, "service_config", None), "name", None) or getattr(
            context, "service", None
        )
        record_stage1_invalidation(
            context.repo_root,
            invalidation,
            service=service,
            transitive_enabled=transitive_enabled,
        )
        summary = (
            f"direct={len(invalidation.target_files) - len(invalidation.indirect_paths)} "
            f"indirect={len(invalidation.indirect_paths)} "
            f"unchanged={len(invalidation.unchanged_files)}"
        )
        if service:
            summary = f"{service}: {summary}"
        log_event(context.repo_root, "stage1_invalidation", summary)
    except Exception as exc:
        print(f"warning: operations metrics record failed: {exc}", file=sys.stderr)

_STAGE_PROVIDER_ALIASES: dict[str, str] = {
    "sonnet": "claude:sonnet",
    "opus": "claude-code",
    "haiku": "claude:sonnet",
    "codex": "codex",
}


def _load_pipeline_config(context: AnalysisContext) -> dict:
    path = context.analysis_pipeline_path
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _resolve_stage_provider(
    explicit: str | None,
    pipeline_config: dict,
    stage: str,
    scale: str,
    config,
) -> str:
    """Resolve provider for an analysis stage.

    Priority: CLI --provider > stage_routing[scale][stage] > global default.
    ``stage`` is one of "stage1", "stage2", "stage3".
    """
    if explicit:
        return explicit
    stage_routing = pipeline_config.get("stage_routing", {})
    scale_routing = stage_routing.get(scale, {})
    routed = scale_routing.get(stage)
    if routed and routed != "skip":
        return _STAGE_PROVIDER_ALIASES.get(str(routed), str(routed))
    return config.provider_name()


def _resolve_analysis_mode(args: argparse.Namespace) -> str | None:
    return getattr(args, "mode", None)


def _is_non_interactive(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "non_interactive", False))


def _run_provider_with_heartbeat(
    provider,
    prompt: str,
    cwd: str,
    add_dirs: list[str],
    label: str,
    *,
    processor: EventProcessor | None = None,
    task_id: str | None = None,
    display_task_events: bool = True,
):
    processor = processor or EventProcessor(handlers=[ProgressDisplay().handle])
    return run_complete_with_events(
        provider=provider,
        prompt=prompt,
        cwd=cwd,
        add_dirs=add_dirs,
        label=label,
        task_type="analyze",
        task_id=task_id or str(uuid.uuid4()),
        source=getattr(provider, "name", "provider"),
        processor=processor,
        display_task_events=display_task_events,
    )


def _supports_live_stage3(provider_name: str) -> bool:
    return provider_name in {"claude-code", "claude-sdk"}


def _apply_provider_permission_mode(provider, *, yolo: bool) -> None:
    if yolo and hasattr(provider, "set_permission_mode"):
        provider.set_permission_mode("bypassPermissions")


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _provider_add_dirs(context: AnalysisContext, mode: str) -> tuple[list[str], list[str]]:
    if mode == "off":
        return [], []

    warnings: list[str] = []
    repo_root = context.repo_root.resolve()
    candidates: set[str] = set()

    def add_external(path: Path) -> None:
        resolved = path.resolve()
        if resolved.exists() and not _is_under(resolved, repo_root):
            candidates.add(str(resolved))

    if mode == "minimal":
        add_external(context.docs_root)
        for directories in context.all_directories.values():
            for directory in directories:
                path = Path(directory).resolve()
                root = path
                for parent in path.parents:
                    if parent.parent == context.github_root:
                        root = parent
                        break
                add_external(root)
    else:
        add_external(context.docs_root)
        add_external(context.ai_context_dir)
        for directories in context.all_directories.values():
            for directory in directories:
                path = Path(directory).resolve()
                add_external(path)
                for parent in path.parents:
                    if parent.parent == context.github_root:
                        add_external(parent)
                        break

    add_dirs = sorted(candidates)
    max_dirs = int(os.environ.get("AWF_PROVIDER_ADD_DIRS_MAX", "4"))
    if mode == "minimal" and len(add_dirs) > max_dirs:
        warnings.append(
            f"warning: provider_add_dirs minimal found {len(add_dirs)} external roots; "
            f"passing first {max_dirs}. Set --provider-add-dirs full or AWF_PROVIDER_ADD_DIRS_MAX to override."
        )
        add_dirs = add_dirs[:max_dirs]
    return add_dirs, warnings


def _resume_mode_label(resume: dict) -> str:
    if resume["skip_provider"]:
        return "skip_completed"
    if resume["blocked_by_retry_limit"]:
        return "blocked_retry_limit"
    if resume["reused_result"]:
        return "reuse_saved_stage2"
    return "fresh_or_retry"


def run_analyze(args: argparse.Namespace) -> int:
    import time as _time
    _analysis_started_at = _time.monotonic()

    # --check mode: drift detection across all analyzed units
    if getattr(args, "check", False):
        return _run_drift_check(args)

    # --catalog mode: analysis status overview
    if getattr(args, "catalog", False):
        return _run_catalog(args)

    # --cycles mode: import-cycle diagnostic over saved graphs
    if getattr(args, "cycles", False):
        return _run_cycles(args)

    # --all mode: scan service and run each domain sequentially
    if getattr(args, "all", False):
        if not getattr(args, "dry_run", False):
            gate_rc = enforce_ready_gate(
                args,
                "analysis",
                json_output=getattr(args, "output_format", "text") == "json",
            )
            if gate_rc != 0:
                return gate_rc
        return _run_analyze_all(args)

    if not args.domain:
        print("error: domain is required (or use --all)", file=sys.stderr)
        return 2

    if bool(getattr(args, "status", False)):
        try:
            context = resolve_analysis_context(
                service=args.service,
                domain=args.domain,
                deep=False,
                repo_root=args.repo_root,
                docs_root=args.docs_root,
                github_root=args.github_root,
                use_ai_discovery=False,
            )
            state = load_analysis_state(context)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if getattr(args, "json", False):
            print(json.dumps(state, ensure_ascii=False, indent=2))
        else:
            print(summarize_analysis_state(state))
        return 0

    if not getattr(args, "dry_run", False):
        gate_rc = enforce_ready_gate(
            args,
            "analysis",
            json_output=getattr(args, "output_format", "text") == "json",
        )
        if gate_rc != 0:
            return gate_rc

    execution_mode = _resolve_analysis_mode(args)
    non_interactive = _is_non_interactive(args)

    # JSON mode: redirect all progress output to stderr, reserve stdout for final JSON
    output_format = getattr(args, "output_format", "text")
    _original_stdout = None
    if output_format == "json":
        _original_stdout = sys.stdout
        sys.stdout = sys.stderr

    try:
        config = load_awf_config(args.repo_root)
        context = resolve_analysis_context(
            service=args.service,
            domain=args.domain,
            deep=False,
            repo_root=args.repo_root,
            docs_root=args.docs_root,
            github_root=args.github_root,
            use_ai_discovery=not bool(getattr(args, "dry_run", False)),
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    pipeline_config = _load_pipeline_config(context)
    if context.related_domains or pipeline_config.get("stage3_force", False):
        context.mode = "deep"

    _default_provider = args.provider or config.provider_name()
    prompt = build_prompt(context, execution_mode=execution_mode, native=(_default_provider == "claude-code"))

    if args.print_prompt or args.dry_run:
        if args.dry_run and output_format == "json" and _original_stdout:
            sys.stdout = _original_stdout
            print(json.dumps({
                "command": "analyze",
                "service": context.service,
                "domain": context.domain,
                "repo_root": str(context.repo_root),
                "docs_root": str(context.docs_root),
                "github_root": str(context.github_root),
                "ai_context_dir": str(context.ai_context_dir),
                "mode": context.mode,
                "execution_mode": execution_mode or "default",
                "domain_directories": context.domain_directories,
                "all_directories": context.all_directories,
                "prompt": prompt,
            }, ensure_ascii=False, indent=2))
            return 0
        print("=== awf analyze context ===")
        print(f"repo_root: {context.repo_root}")
        print(f"docs_root: {context.docs_root}")
        print(f"github_root: {context.github_root}")
        print(f"ai_context_dir: {context.ai_context_dir}")
        print(f"mode: {context.mode}")
        print(f"execution_mode: {execution_mode or 'default'}")
        print(f"domain_directories: {json.dumps(context.domain_directories, ensure_ascii=False)}")
        print(f"all_directories: {json.dumps(context.all_directories, ensure_ascii=False)}")
        print()
        print(prompt)
        if args.dry_run:
            return 0

    ensure_ai_context_dirs(context)
    domain_files, discovery = collect_domain_files(context)
    if not discovery.get("existing_directories"):
        candidates = discovery.get("candidate_directories", [])
        print(
            f"warning: no source directories found for {context.service}/{context.domain}. "
            f"Tried: {', '.join(str(c) for c in candidates[:5])}. "
            f"Configure domain_definitions in analysis-config.json or check the service directory structure.",
            file=sys.stderr,
        )
    elif not domain_files:
        existing = discovery.get("existing_directories", [])
        print(
            f"warning: directories exist but no source files matched. "
            f"Directories: {', '.join(str(e) for e in existing[:3])}. "
            f"Glob patterns: {', '.join(str(p) for p in discovery.get('glob_patterns', [])[:5])}",
            file=sys.stderr,
        )
    resume = resolve_analysis_resume(context, current_file_entries=domain_files)
    print(f"resume_mode: {_resume_mode_label(resume)}")
    for message in resume["messages"]:
        print(message)
    if resume["skip_provider"]:
        state = load_analysis_state(context)
        print(f"state_file: {context.ai_context_dir / '.analysis-state.json'}")
        print(f"output_status: {state.get('layers', {}).get('output', {}).get('status')}")
        return 0
    if resume["blocked_by_retry_limit"]:
        state = load_analysis_state(context)
        print(f"state_file: {context.ai_context_dir / '.analysis-state.json'}")
        print(f"output_status: {state.get('layers', {}).get('output', {}).get('status')}")
        print("error: stage2 retry limit reached; inspect .analysis-state.json and .ai-context/.tmp artifacts.", file=sys.stderr)
        if not non_interactive:
            print("next_step: inspect the saved result/prompt files or switch to Claude Code /analysis for manual recovery", file=sys.stderr)
        return 1

    if resume["reused_result"]:
        state = load_analysis_state(context)
        raw_result = read_analysis_artifact(context, state.get("artifacts", {}).get("result_file"))
        if raw_result:
            save_stage2_draft(context, raw_result)
            output_summary = analyze_stage2_output(raw_result, context.analysis_mode)
            write_stage2_outputs(context, raw_result)
            if output_summary["missing_files"]:
                print(
                    "warning: saved stage2 result is missing required outputs: "
                    + ", ".join(output_summary["missing_files"]),
                    file=sys.stderr,
                )
            finalized = finalize_analysis_run(context, state.get("layers", {}).get("analyze", {}).get("stage2", {}).get("provider", "resume"), 0)
            print(f"state_file: {context.ai_context_dir / '.analysis-state.json'}")
            print(f"output_status: {finalized.get('layers', {}).get('output', {}).get('status')}")
            if finalized.get("layers", {}).get("output", {}).get("status") == "completed":
                _accumulate_knowledge_safe(context)
                _record_analysis_complete_safe(
                    context, started_at=_analysis_started_at, mode="resume"
                )
                return 0

    artifact_manager = ArtifactManager()
    state_updater = AnalysisStateUpdater(context)
    processor = EventProcessor(handlers=[ProgressDisplay().handle, artifact_manager.handle, state_updater.handle])
    analyze_task_id = f"analyze-{context.service}-{context.domain}"
    processor.emit(
        event_type=EventType.STAGE_STARTED,
        task_id=analyze_task_id,
        source="cli",
        data={"stage": "stage1", "description": "build analysis context and prompt"},
    )

    previous_hashes = load_hashes_file(context)
    hashes_have_changed = hashes_changed(context, domain_files)
    hashes_path = save_hashes_file(context, domain_files)
    print(f"hashes_file: {hashes_path}")
    if hashes_have_changed:
        print("hashes_changed: true")
    file_count = len(domain_files)
    scale = analysis_scale(file_count, config)
    if file_count > SMALL_DOMAIN_FILECOUNT_THRESHOLD:
        print(
            "warning: this domain exceeds the recommended awf analyze small-domain threshold "
            f"({file_count} files > {SMALL_DOMAIN_FILECOUNT_THRESHOLD}). "
            "Prefer Claude Code /analysis or use --dry-run to prepare prompt/bundle artifacts.",
            file=sys.stderr,
        )
    elif context.mode == "deep":
        print(
            "warning: automatic reference expansion is better suited to Claude Code /analysis for larger cross-service analysis.",
            file=sys.stderr,
        )
    if execution_mode == "cross":
        print(
            "info: execution_mode=cross enabled; a secondary Stage 2 judge pass will run when the primary output is complete.",
            file=sys.stderr,
        )
    elif execution_mode == "precise":
        print(
            "info: execution_mode=precise enabled; prompts now prefer conservative evidence-heavy analysis.",
            file=sys.stderr,
        )
    print(f"analysis_scale: {scale}")

    # Stage 1: per-file analysis BEFORE domain bundle (results enrich the bundle)
    file_analyses_text = None
    file_analyses_list: list[dict] | None = None
    registry = ProviderRegistry(config)
    stage1_provider_name = _resolve_stage_provider(None, pipeline_config, "stage1", scale, config)
    # Skip Stage 1 if the routed provider is not available or if running in fixture mode
    stage2_provider_name = args.provider or config.provider_name()
    if stage2_provider_name == "fixture":
        stage1_provider_name = None
    if file_count > 0 and stage1_provider_name:
        try:
            stage1_provider = registry.get(stage1_provider_name)
            _apply_provider_permission_mode(stage1_provider, yolo=bool(getattr(args, "yolo", False)))
            stage1_max_concurrent = pipeline_config.get("stage_routing", {}).get(scale, {}).get("max_concurrent", 5)

            from awf.core.analysis_stage1 import (
                format_file_analyses_for_memo,
                load_stage1_file_analyses,
                merge_stage1_analyses,
                run_stage1_file_analyses,
                save_stage1_file_analyses,
            )
            from awf.core.analysis_files import compute_changed_files, compute_deleted_files
            from awf.core.graph_builder import (
                expand_stage1_targets_with_import_graph,
                transitive_invalidation_status,
            )

            # K1: Incremental — only analyze changed files when previous results exist
            stage1_target_files = domain_files
            previous_analyses: list[dict] = []
            unchanged_paths: set[str] = set()
            bypass_cache_paths: set[str] = set()

            ti_enabled, ti_reason = transitive_invalidation_status(pipeline_config)
            if not ti_enabled:
                print(
                    f"stage1_invalidation: transitive disabled ({ti_reason})",
                    file=sys.stderr,
                )

            cached_stage1_analyses = load_stage1_file_analyses(context)
            if resume.get("hashes_changed") and cached_stage1_analyses:
                previous_analyses = cached_stage1_analyses
                changed_files, _unchanged_files = compute_changed_files(
                    context,
                    domain_files,
                    saved_hashes=previous_hashes,
                )
                deleted_files = compute_deleted_files(
                    context,
                    domain_files,
                    saved_hashes=previous_hashes,
                )
                if ti_enabled:
                    graph_invalidation = expand_stage1_targets_with_import_graph(
                        context,
                        domain_files,
                        changed_files,
                        deleted_files,
                    )
                    unchanged_paths = {e["path"] for e in graph_invalidation.unchanged_files}
                    bypass_cache_paths = set(graph_invalidation.indirect_paths)
                    _record_stage1_invalidation_safe(
                        context, graph_invalidation, transitive_enabled=True
                    )
                    if graph_invalidation.target_files:
                        stage1_target_files = graph_invalidation.target_files
                        print(
                            f"stage1_incremental: {len(changed_files)} direct + "
                            f"{len(graph_invalidation.indirect_paths)} graph-dependent / "
                            f"{len(graph_invalidation.unchanged_files)} unchanged "
                            f"(deleted={len(graph_invalidation.deleted_paths)}, "
                            f"saving {len(graph_invalidation.unchanged_files)} provider calls)",
                            file=sys.stderr,
                        )
                        if graph_invalidation.indirect_paths:
                            print(
                                "stage1_graph_invalidation: "
                                f"{len(graph_invalidation.invalidating_paths)} invalidating path(s), "
                                f"{len(graph_invalidation.indirect_paths)} reverse dependent(s)",
                                file=sys.stderr,
                            )
                    else:
                        stage1_target_files = []
                else:
                    # Transitive invalidation disabled — fall back to direct-only
                    # incremental: re-analyze changed files; reuse previous results
                    # for everything else even if their imports moved.
                    changed_paths = {e["path"] for e in changed_files}
                    unchanged_paths = {
                        e["path"] for e in domain_files if e["path"] not in changed_paths
                    }
                    bypass_cache_paths = set()
                    stage1_target_files = changed_files
                    print(
                        f"stage1_incremental: {len(changed_files)} direct / "
                        f"{len(unchanged_paths)} unchanged "
                        f"(deleted={len(deleted_files)}, transitive=off)",
                        file=sys.stderr,
                    )

            print(f"stage1_provider: {stage1_provider_name} (files={len(stage1_target_files)}, parallel={stage1_max_concurrent})", file=sys.stderr)

            def _stage1_progress(done: int, total: int) -> None:
                print(f"stage1_progress: {done}/{total}", file=sys.stderr)

            # v3 observation mode: enabled via pipeline_config layer2 or default for non-fixture
            use_observation = pipeline_config.get("layer2", {}).get("mode", "legacy") == "observation"

            new_analyses = run_stage1_file_analyses(
                context,
                stage1_target_files,
                stage1_provider,
                max_concurrent=int(stage1_max_concurrent),
                on_progress=_stage1_progress,
                use_observation=use_observation,
                bypass_cache_paths=bypass_cache_paths,
            )

            # Merge new + previous for unchanged files
            if previous_analyses and unchanged_paths:
                file_analyses = merge_stage1_analyses(new_analyses, previous_analyses, unchanged_paths)
            else:
                file_analyses = new_analyses

            analyses_path = save_stage1_file_analyses(context, file_analyses)

            # Rebuild the import graph for the next incremental run.
            # The current run consumes the previous graph before this point.
            try:
                from awf.core.graph_builder import build_and_save_graph

                graph_path = build_and_save_graph(context, domain_files)
                if graph_path:
                    print(f"import_graph: {graph_path}", file=sys.stderr)
            except Exception as graph_exc:  # noqa: BLE001
                print(f"warning: import-graph step skipped: {graph_exc}", file=sys.stderr)

            file_analyses_text = format_file_analyses_for_memo(file_analyses)
            file_analyses_list = file_analyses
            parse_errors = sum(1 for a in file_analyses if a.get("parse_error") or a.get("provider_error"))
            print(f"stage1_file_analyses: {analyses_path} (ok={len(file_analyses) - parse_errors}, errors={parse_errors})")
            processor.emit(
                event_type=EventType.ARTIFACT_CREATED,
                task_id=analyze_task_id,
                source="cli",
                data={"path": str(analyses_path), "kind": "analysis_stage1_file_analyses", "producer": stage1_provider_name, "status": "final", "replaces": None},
            )
        except (UnknownProviderError, Exception) as exc:
            print(f"warning: stage1 file analysis skipped: {exc}", file=sys.stderr)

    # Domain bundle — built AFTER Stage 1 so file analyses and import context are included
    # Extract precomputed context from Stage 1 to avoid redundant import tracking
    precomputed_context: dict[str, str] | None = None
    if file_analyses_list:
        precomputed_context = {}
        for a in file_analyses_list:
            for rel, sig in (a.get("_context_files") or {}).items():
                if rel not in precomputed_context:
                    precomputed_context[rel] = sig
    # Pass observations to domain bundle for v3 scale-based content policy
    observations_list = None
    if file_analyses_list and any(a.get("observation") for a in file_analyses_list):
        observations_list = file_analyses_list

    domain_bundle_text = build_domain_bundle(
        context, domain_files,
        file_analyses=file_analyses_list,
        precomputed_context=precomputed_context,
        observations=observations_list,
    )
    bundle_line_count = len(domain_bundle_text.splitlines())
    bundle_token_estimate = estimate_bundle_tokens(domain_bundle_text)
    domain_bundle_path = save_domain_bundle(
        context,
        domain_bundle_text,
        file_count,
        line_count=bundle_line_count,
        token_estimate=bundle_token_estimate,
        scale=scale,
    )
    print(f"domain_bundle: {domain_bundle_path}")
    print(f"bundle_line_count: {bundle_line_count}")
    print(f"bundle_token_estimate: {bundle_token_estimate}")
    processor.emit(
        event_type=EventType.ARTIFACT_CREATED,
        task_id=analyze_task_id,
        source="cli",
        data={
            "path": str(domain_bundle_path),
            "kind": "analysis_bundle",
            "producer": "cli",
            "status": "final",
            "replaces": None,
            "source_file_count": file_count,
            "bundle_line_count": bundle_line_count,
            "bundle_token_estimate": bundle_token_estimate,
        },
    )
    project_bundle, project_bundle_log = build_project_bundle(context)
    if project_bundle is not None:
        project_bundle_path = save_project_bundle(context, project_bundle)
        if project_bundle_path is not None:
            print(f"project_bundle: {project_bundle_path}")
            processor.emit(
                event_type=EventType.ARTIFACT_CREATED,
                task_id=analyze_task_id,
                source="cli",
                data={"path": str(project_bundle_path), "kind": "analysis_project_bundle", "producer": "cli", "status": "final", "replaces": None},
            )
    for item in project_bundle_log.get("references_used", []):
        print(
            "reference_used: "
            f"level={item['level']} tokens={item['tokens']} document={item['document']} reason={item['reason']}",
            file=sys.stderr,
        )
    for item in project_bundle_log.get("references_dropped", []):
        print(
            "reference_dropped: "
            f"level={item['level']} document={item['document']} reason={item['reason']}",
            file=sys.stderr,
        )
    print(
        f"reference_tokens_total: {project_bundle_log.get('total_reference_tokens', 0)}",
        file=sys.stderr,
    )

    # Skip File Analyses in memo when Stage 1 results are already in domain-bundle summary attrs
    stage1_memo_text = build_stage1_memo(
        context, discovery=discovery, file_analyses_text=file_analyses_text,
        skip_file_analyses=bool(file_analyses_list),
    )
    stage1_memo_path = save_stage1_memo(context, stage1_memo_text)
    print(f"stage1_memo: {stage1_memo_path}")
    processor.emit(
        event_type=EventType.ARTIFACT_CREATED,
        task_id=analyze_task_id,
        source="cli",
        data={"path": str(stage1_memo_path), "kind": "analysis_stage1_memo", "producer": "cli", "status": "final", "replaces": None},
    )
    if file_count == 0:
        processor.emit(
            event_type=EventType.STAGE_COMPLETED,
            task_id=analyze_task_id,
            source="cli",
            data={"stage": "stage1"},
        )
        state = load_analysis_state(context)
        state["currentLayer"] = "bundle"
        state["currentStage"] = 1
        state["layers"]["bundle"]["status"] = "failed"
        state["layers"]["bundle"]["errorMessage"] = "source_discovery_empty"
        state["layers"]["output"]["status"] = "failed"
        state["layers"]["output"]["errorMessage"] = "source_discovery_empty"
        state["summaries"]["stage2"] = ""
        state["summaries"]["stage3"] = ""
        save_analysis_state(context, state)
        print("input_quality_failed: no target source files collected", file=sys.stderr)
        print("analysis_failed_reason: source_discovery_empty", file=sys.stderr)
        print(f"discovery_candidate_directories: {discovery.get('candidate_directories', [])}", file=sys.stderr)
        print(f"discovery_existing_directories: {discovery.get('existing_directories', [])}", file=sys.stderr)
        print(f"discovery_glob_patterns: {discovery.get('glob_patterns', [])}", file=sys.stderr)
        print(f"state_file: {context.ai_context_dir / '.analysis-state.json'}")
        print("output_status: failed")
        print("next_step: verify domain mapping or add analysis-config override before retrying", file=sys.stderr)
        return 1
    fanout_selected = should_stage2_fanout(
        context=context,
        config=config,
        scale=scale,
        bundle_lines=bundle_line_count,
        bundle_tokens=bundle_token_estimate,
    )
    save_stage2_fanout_scaffold(
        context,
        selected=fanout_selected,
        scale=scale,
        bundle_lines=bundle_line_count,
        bundle_tokens=bundle_token_estimate,
        prompts=build_stage2_fanout_prompts(
            context=context,
            domain_bundle_text=domain_bundle_text,
            stage1_memo_text=stage1_memo_text,
        ),
    )
    print(f"fanout_selected: {str(fanout_selected).lower()}")
    if fanout_selected:
        print(
            "info: Stage 2 fan-out selected; synthesizer runs first, then 4 writers run in parallel, "
            "then a consistency pass validates the combined output.",
            file=sys.stderr,
        )

    provider_name = _resolve_stage_provider(args.provider, pipeline_config, "stage2", scale, config)
    try:
        provider = registry.get(provider_name)
        _apply_provider_permission_mode(provider, yolo=bool(getattr(args, "yolo", False)))
    except UnknownProviderError:
        print(f"error: unsupported provider `{provider_name}` for stage2.", file=sys.stderr)
        return 2
    try:
        ruleset = build_permission_ruleset(config.raw, yolo=getattr(args, "yolo", False))
        check_permission(ruleset, provider_permission_name(provider_name, config.raw.get("provider", {}).get("aliases")), "analyze")
    except PermissionDeniedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    prompt, prompt_warnings = build_provider_prompt(
        context=context,
        provider_name=provider_name,
        base_prompt=prompt,
        domain_bundle_text=domain_bundle_text,
        project_bundle_text=project_bundle,
        stage1_memo_text=stage1_memo_text,
    )
    for warning in prompt_warnings:
        print(warning, file=sys.stderr)
    mark_analysis_started(context, provider_name)
    prompt_path = save_analysis_prompt(context, provider_name, prompt)
    print(f"prompt_file: {prompt_path}")
    processor.emit(
        event_type=EventType.ARTIFACT_CREATED,
        task_id=analyze_task_id,
        source="cli",
        data={"path": str(prompt_path), "kind": "analysis_prompt", "producer": "cli", "status": "final", "replaces": None},
    )
    processor.emit(
        event_type=EventType.STAGE_COMPLETED,
        task_id=analyze_task_id,
        source="cli",
        data={"stage": "stage1"},
    )
    add_dir_mode = str(getattr(args, "provider_add_dirs", "off") or "off")
    add_dirs, add_dir_warnings = _provider_add_dirs(context, add_dir_mode)
    for warning in add_dir_warnings:
        print(warning, file=sys.stderr)
    print(
        "provider_context: "
        f"cwd={context.repo_root} add_dirs={len(add_dirs)} add_dir_mode={add_dir_mode} prompt_chars={len(prompt)} "
        f"prompt_lines={len(prompt.splitlines())}",
        file=sys.stderr,
    )
    fanout_used = False
    if provider_name == "claude-code" and not non_interactive:
        print("info: claude-code stage2 can take a while on larger domains; press Ctrl-C to stop.", file=sys.stderr)
    timeout_sec = getattr(provider, "timeout_sec", None)
    try:
        execution_mode_name = None
        processor.emit(
            event_type=EventType.STAGE_STARTED,
            task_id=analyze_task_id,
            source="cli",
            data={"stage": "stage2", "description": "provider analysis execution"},
        )
        if provider_name == "claude-code":
            native_task = TaskDefinition(
                task_id=str(uuid.uuid4()),
                parent_task_id=None,
                correlation_id=f"{context.service}:{context.domain}",
                idempotency_key=f"{context.service}:{context.domain}:analyze:stage2",
                type=TaskType.ANALYZE,
                params={
                    "service": context.service,
                    "domain": context.domain,
                    "prompt": prompt,
                    "mode": context.mode,
                    "execution_mode": execution_mode or "default",
                    "add_dirs": add_dirs,
                },
                constraints=TaskConstraints(
                    timeout_sec=timeout_sec,
                    max_retries=2,
                    mode=execution_mode,
                    deep=(context.mode == "deep"),
                    non_interactive=non_interactive,
                    dry_run=bool(args.dry_run),
                ),
                context=TaskContext(
                    cwd=str(context.repo_root),
                    repo_root=str(context.repo_root),
                    docs_root=str(context.docs_root),
                    github_root=str(context.github_root),
                    config=config,
                    provider_name=provider_name,
                ),
            )
            execution_mode_name = resolve_execution_mode(provider, native_task)
        if fanout_selected:
            fanout_result, fanout_error, fanout_metadata = run_stage2_fanout(
                context=context,
                provider=provider,
                provider_factory=lambda: registry.get(provider_name),
                provider_name=provider_name,
                add_dirs=add_dirs,
                stage1_memo_text=stage1_memo_text,
                domain_bundle_text=domain_bundle_text,
                runner=lambda provider, prompt, cwd, add_dirs, label: _run_provider_with_heartbeat(
                    provider,
                    prompt,
                    cwd,
                    add_dirs,
                    label,
                    processor=processor,
                    task_id=analyze_task_id,
                    display_task_events=False,
                ),
                save_additional_result=save_additional_analysis_result,
                processor=processor,
                task_id=analyze_task_id,
            )
            if fanout_result is not None:
                result = fanout_result
                stage2_elapsed = 0.0
                fanout_used = True
                record_stage2_fanout_execution(
                    context,
                    used=True,
                    status="completed",
                    provider_name=provider_name,
                    writer_count=int(fanout_metadata.get("writerCount", 4) or 4),
                    execution_mode=str(fanout_metadata.get("executionMode", "parallel") or "parallel"),
                    consistency_passed=bool(fanout_metadata.get("consistencyPassed", False)),
                    consistency_issues=list(fanout_metadata.get("consistencyIssues", [])),
                    consistency_artifact_suffix=fanout_metadata.get("consistencyArtifactSuffix"),
                )
            else:
                print(f"warning: Stage 2 fan-out fell back to single-agent path ({fanout_error})", file=sys.stderr)
                record_stage2_fanout_execution(
                    context,
                    used=False,
                    status="fallback",
                    provider_name=provider_name,
                    writer_count=int(fanout_metadata.get("writerCount", 4) or 4),
                    execution_mode=str(fanout_metadata.get("executionMode", "parallel") or "parallel"),
                    fallback_to_single=True,
                    consistency_passed=bool(fanout_metadata.get("consistencyPassed", False)),
                    consistency_issues=list(fanout_metadata.get("consistencyIssues", [])),
                    consistency_artifact_suffix=fanout_metadata.get("consistencyArtifactSuffix"),
                )
                if timeout_sec is not None:
                    print(f"provider_running: {provider_name} stage2 (timeout: {timeout_sec}s)", file=sys.stderr)
                else:
                    print(f"provider_running: {provider_name} stage2", file=sys.stderr)
                if execution_mode_name == "native":
                    print(f"provider_execution_mode: {provider_name} native", file=sys.stderr)
                    result, stage2_elapsed = run_native_provider_task(provider=provider, task=native_task, processor=processor)
                else:
                    result, stage2_elapsed = _run_provider_with_heartbeat(
                        provider,
                        prompt,
                        str(context.repo_root),
                        add_dirs,
                        f"{provider_name} stage2",
                        processor=processor,
                        task_id=analyze_task_id,
                    )
        else:
            if timeout_sec is not None:
                print(f"provider_running: {provider_name} stage2 (timeout: {timeout_sec}s)", file=sys.stderr)
            else:
                print(f"provider_running: {provider_name} stage2", file=sys.stderr)
            if execution_mode_name == "native":
                print(f"provider_execution_mode: {provider_name} native", file=sys.stderr)
                result, stage2_elapsed = run_native_provider_task(provider=provider, task=native_task, processor=processor)
            else:
                result, stage2_elapsed = _run_provider_with_heartbeat(
                    provider,
                    prompt,
                    str(context.repo_root),
                    add_dirs,
                    f"{provider_name} stage2",
                    processor=processor,
                    task_id=analyze_task_id,
                )
    except KeyboardInterrupt:
        state = load_analysis_state(context)
        state["layers"]["analyze"]["stage2"]["status"] = "failed"
        state["layers"]["analyze"]["stage2"]["errorMessage"] = "interrupted_by_user"
        save_analysis_state(context, state)
        print("error: analysis interrupted by user during stage2 provider execution.", file=sys.stderr)
        return 130
    if not fanout_used:
        print(f"provider_completed: {provider_name} stage2 ({stage2_elapsed:.1f}s)", file=sys.stderr)
    captured_output = result.stdout if (result.stdout or "").strip() else (result.stderr or "")
    result_path = save_analysis_result(
        context,
        provider_name,
        captured_output,
    )
    print(f"result_file: {result_path}")
    processor.emit(
        event_type=EventType.ARTIFACT_CREATED,
        task_id=analyze_task_id,
        source="cli",
        data={"path": str(result_path), "kind": "analysis_result", "producer": provider_name, "status": "final", "replaces": None},
    )
    if result.stdout:
        save_stage2_draft(context, result.stdout)
        output_summary = analyze_stage2_output(result.stdout, context.analysis_mode)
        write_stage2_outputs(context, result.stdout)
        processor.emit(
            event_type=EventType.STAGE_COMPLETED,
            task_id=analyze_task_id,
            source="cli",
            data={"stage": "stage2", "duration_sec": stage2_elapsed},
        )
        if output_summary["extra_files"]:
            print("stage2_extra_files: " + ", ".join(output_summary["extra_files"]))
        if output_summary["missing_files"]:
            missing_text = ", ".join(output_summary["missing_files"])
            print(f"warning: stage2 output missing required files: {missing_text}", file=sys.stderr)
            state = load_analysis_state(context)
            state["layers"]["analyze"]["stage2"]["errorMessage"] = f"missing_required_outputs:{missing_text}"
            save_analysis_state(context, state)
        # Multi-agent cross-validation of Stage 2 results
        exec_mode = getattr(args, "mode", None)
        if exec_mode and exec_mode not in {"solo", None} and result.returncode == 0 and not output_summary["missing_files"]:
            if provider_name == "fixture":
                print("multi_agent_validation: skipped for fixture provider", file=sys.stderr)
            else:
                from awf.core.multi_agent import run_multi_agent
                from awf.core.spec_loader import load_prompt_optional
                validation_prompt = load_prompt_optional(
                    "analysis", "cross-validate",
                    service=context.service, domain=context.domain,
                    stage2_result=result.stdout[:20000], stage1_memo=stage1_memo_text[:5000],
                )
                if not validation_prompt:
                    validation_prompt = (
                        f"다음은 `{context.service}/{context.domain}` 도메인의 Stage 2 분석 결과입니다.\n"
                        f"이 결과의 정확성을 검증하세요.\n\n"
                        f"## Stage 2 결과\n\n{result.stdout[:20000]}\n\n"
                        f"## Stage 1 메모\n\n{stage1_memo_text[:5000]}\n"
                    )
                multi_result = run_multi_agent(
                    mode=exec_mode,
                    prompt=validation_prompt,
                    primary_provider=provider,
                    registry=registry,
                    config={},
                    cwd=str(context.repo_root),
                )
                for agent in multi_result.agents:
                    if agent.role != "primary" and agent.stdout.strip():
                        val_path = context.ai_context_dir / ".tmp" / f"validation-{agent.provider_name}-{agent.role}.txt"
                        val_path.write_text(agent.stdout, encoding="utf-8")
                        print(f"multi_agent_{agent.role}: {agent.provider_name} ({agent.elapsed_sec:.1f}s)", file=sys.stderr)
                print(f"multi_agent_judge: {multi_result.judge_verdict} ({multi_result.judge_reason})", file=sys.stderr)

        if context.mode == "deep" and not resume.get("stage3_retry_blocked"):
            processor.emit(
                event_type=EventType.STAGE_STARTED,
                task_id=analyze_task_id,
                source="cli",
                data={"stage": "stage3", "description": "cross-service validation and finalization"},
            )
            stage3_provider_name = _resolve_stage_provider(args.provider, pipeline_config, "stage3", scale, config)
            stage3_routing = pipeline_config.get("stage_routing", {}).get(scale, {}).get("stage3")
            stage3_force = pipeline_config.get("stage3_force", False)
            related_count = len(context.related_domains) if hasattr(context, "related_domains") else 0

            from awf.core.analysis_state import should_run_stage3 as _should_run_stage3
            _stage3_run, _stage3_reason = _should_run_stage3(
                mode=context.mode,
                stage3_force=stage3_force,
                stage_routing_stage3=stage3_routing,
                related_domains_count=related_count,
            )
            stage3_skip = not _stage3_run
            if _stage3_reason == "force_enabled":
                print("info: stage3 force-enabled by stage3_force flag", file=sys.stderr)
            elif "auto_enabled" in _stage3_reason:
                print(f"info: stage3 auto-enabled: related_domains={related_count} >= 3 threshold", file=sys.stderr)
            if stage3_skip:
                mark_stage3_skipped(context, reason="stage_routing skip for scale=" + scale)
                print(f"stage3_skipped: scale={scale} routing=skip")
            elif project_bundle and _supports_live_stage3(stage3_provider_name) and not output_summary["missing_files"]:
                if stage3_provider_name != provider_name:
                    try:
                        stage3_provider = registry.get(stage3_provider_name)
                        _apply_provider_permission_mode(stage3_provider, yolo=bool(getattr(args, "yolo", False)))
                    except UnknownProviderError:
                        stage3_provider = provider
                        stage3_provider_name = provider_name
                else:
                    stage3_provider = provider
                mark_stage3_started(context, stage3_provider_name, "reference expansion live stage3 validation")
                stage3_prompt, stage3_warnings = build_stage3_prompt(
                    context,
                    provider_name=stage3_provider_name,
                    project_bundle_text=project_bundle,
                    stage1_memo_text=stage1_memo_text,
                )
                for warning in stage3_warnings:
                    print(warning, file=sys.stderr)
                if stage3_provider_name == "claude-code" and not non_interactive:
                    print("info: claude-code stage3 validation can take a while when reference expansion is active.", file=sys.stderr)
                if timeout_sec is not None:
                    print(f"provider_running: {stage3_provider_name} stage3 (timeout: {timeout_sec}s)", file=sys.stderr)
                else:
                    print(f"provider_running: {stage3_provider_name} stage3", file=sys.stderr)
                try:
                    stage3_result, stage3_elapsed = _run_provider_with_heartbeat(
                        stage3_provider,
                        stage3_prompt,
                        str(context.repo_root),
                        add_dirs,
                        f"{stage3_provider_name} stage3",
                    )
                except KeyboardInterrupt:
                    mark_stage3_failed(
                        context,
                        stage3_provider_name,
                        error_message="interrupted_by_user",
                        reason="reference expansion live stage3 validation interrupted",
                    )
                    print("error: analysis interrupted by user during stage3 provider execution.", file=sys.stderr)
                    return 130
                print(f"provider_completed: {stage3_provider_name} stage3 ({stage3_elapsed:.1f}s)", file=sys.stderr)
                if stage3_result.returncode == 0 and (stage3_result.stdout or "").strip():
                    stage3_path = save_stage3_final(
                        context,
                        stage3_result.stdout,
                        provider_name=stage3_provider_name,
                        reason="reference expansion live stage3 validation",
                        status="completed",
                    )
                    print(f"stage3_final: {stage3_path}")
                    processor.emit(
                        event_type=EventType.ARTIFACT_CREATED,
                        task_id=analyze_task_id,
                        source="cli",
                        data={"path": str(stage3_path), "kind": "analysis_stage3_final", "producer": stage3_provider_name, "status": "final", "replaces": None},
                    )
                else:
                    stage3_error = stage3_result.stderr or "Stage 3 provider run failed."
                    mark_stage3_failed(
                        context,
                        provider_name=stage3_provider_name,
                        error_message=stage3_error,
                        reason="reference expansion live stage3 validation failed",
                    )
                    print(f"warning: stage3 failed: {stage3_error}", file=sys.stderr)
                    # Stage 3 failure is recorded by mark_stage3_failed() above.
                    # Do NOT overwrite `result` — Stage 2 output is still valid.
            else:
                stage3_path = save_stage3_final(
                    context,
                    build_stage3_note(context),
                    provider_name="awf-cli",
                    reason="reference expansion placeholder stage3 scaffold",
                    status="scaffold",
                )
                print(f"stage3_final: {stage3_path}")
                processor.emit(
                    event_type=EventType.ARTIFACT_CREATED,
                    task_id=analyze_task_id,
                    source="cli",
                    data={"path": str(stage3_path), "kind": "analysis_stage3_final", "producer": "cli", "status": "scaffold", "replaces": None},
                )
            processor.emit(
                event_type=EventType.STAGE_COMPLETED,
                task_id=analyze_task_id,
                source="cli",
                data={"stage": "stage3"},
            )
        elif resume.get("stage3_retry_blocked"):
            print("stage3_blocked: retry limit reached; keeping failed state", file=sys.stderr)
        else:
            mark_stage3_skipped(context, "stage3 skipped: no reference expansion targets")

    if execution_mode == "cross" and result.returncode == 0 and (result.stdout or "").strip():
        secondary_candidates = cross_secondary_candidates(provider_name)
        final_provider_name = provider_name
        for candidate in secondary_candidates:
            if not registry.supports(candidate):
                continue
            try:
                secondary_provider = registry.get(candidate)
                _apply_provider_permission_mode(secondary_provider, yolo=bool(getattr(args, "yolo", False)))
            except UnknownProviderError:
                continue
            try:
                check_permission(ruleset, provider_permission_name(candidate, config.raw.get("provider", {}).get("aliases")), "analyze:cross-secondary")
            except PermissionDeniedError as exc:
                print(f"permission_skip: {candidate} ({exc})", file=sys.stderr)
                continue
            secondary_timeout_sec = getattr(secondary_provider, "timeout_sec", None)
            if secondary_timeout_sec is not None:
                print(f"provider_running: {candidate} stage2 secondary (timeout: {secondary_timeout_sec}s)", file=sys.stderr)
            else:
                print(f"provider_running: {candidate} stage2 secondary", file=sys.stderr)
            try:
                secondary_result, secondary_elapsed = _run_provider_with_heartbeat(
                    secondary_provider,
                    prompt,
                    str(context.repo_root),
                    add_dirs,
                    f"{candidate} stage2 secondary",
                )
            except KeyboardInterrupt:
                print("error: analysis interrupted by user during cross secondary stage2 execution.", file=sys.stderr)
                return 130
            print(f"provider_completed: {candidate} stage2 secondary ({secondary_elapsed:.1f}s)", file=sys.stderr)
            secondary_output = secondary_result.stdout if (secondary_result.stdout or "").strip() else (secondary_result.stderr or "")
            secondary_result_path = save_additional_analysis_result(context, candidate, secondary_output, "secondary")
            print(f"secondary_result_file: {secondary_result_path}")
            synthesis = synthesize_cross_stage2(
                result.stdout,
                secondary_result.stdout if secondary_result.returncode == 0 else "",
                primary_provider=provider_name,
                secondary_provider=candidate,
                secondary_failed=not (secondary_result.returncode == 0 and (secondary_result.stdout or "").strip()),
            )
            print(f"cross_judge: {'PASS' if synthesis['judge_passed'] else 'FAIL'}")
            if synthesis["judge_reasons"]:
                print("cross_judge_reasons: " + ", ".join(synthesis["judge_reasons"]))
                print("cross_judge_reason_details: " + " | ".join(explain_judge_reasons(synthesis["judge_reasons"])))
            print(f"cross_synthesis_provider: {synthesis['selected_provider']}")
            print(f"cross_synthesis_result: {'PASS' if synthesis['final_passed'] else 'FAIL'}")
            if synthesis["synthesis_reasons"]:
                print("cross_synthesis_reasons: " + ", ".join(synthesis["synthesis_reasons"]))
                print(
                    "cross_synthesis_reason_details: "
                    + " | ".join(explain_judge_reasons(synthesis["synthesis_reasons"]))
                )
            if synthesis.get("selection_summary"):
                print(f"cross_synthesis_selection_basis: {synthesis['selection_summary']}")
            record_cross_synthesis(
                context,
                selected_provider=str(synthesis["selected_provider"]),
                secondary_provider=candidate,
                judge_passed=bool(synthesis["judge_passed"]),
                judge_reasons=list(synthesis["judge_reasons"]),
                synthesis_passed=bool(synthesis["final_passed"]),
                synthesis_reasons=list(synthesis["synthesis_reasons"]),
                selection_summary=str(synthesis.get("selection_summary", "") or ""),
            )
            if synthesis["selected_provider"] == candidate and secondary_result.returncode == 0 and (secondary_result.stdout or "").strip():
                save_analysis_result(context, candidate, secondary_result.stdout)
                save_stage2_draft(context, secondary_result.stdout)
                write_stage2_outputs(context, secondary_result.stdout)
                result = type(result)(
                    returncode=0,
                    stdout=secondary_result.stdout,
                    stderr=result.stderr,
                )
                final_provider_name = candidate
            if not synthesis["final_passed"]:
                state = load_analysis_state(context)
                state["layers"]["analyze"]["stage2"]["errorMessage"] = "cross_mode_conflict:" + ",".join(synthesis["judge_reasons"])
                state["layers"]["output"]["errorMessage"] = "cross_mode_conflict:" + ",".join(synthesis["judge_reasons"])
                save_analysis_state(context, state)
                result = type(result)(
                    returncode=5,
                    stdout=result.stdout,
                    stderr=(result.stderr + "\n" if result.stderr else "") + "cross-mode judge failed",
                )
            provider_name = final_provider_name
            break
    finalized = finalize_analysis_run(
        context,
        provider_name,
        result.returncode,
        result.stderr or "",
    )
    print(f"state_file: {context.ai_context_dir / '.analysis-state.json'}")
    print(f"output_status: {finalized.get('layers', {}).get('output', {}).get('status')}")
    event_summary = summarize_event_sync(finalized.get("eventSync"))
    if event_summary:
        print(event_summary, file=sys.stderr)
    if finalized.get('layers', {}).get('output', {}).get('status') == 'completed':
        report_path = generate_analysis_report(context, finalized)
        if report_path:
            print(f"analysis_report: {report_path}")
        _accumulate_knowledge_safe(context)
        _record_analysis_complete_safe(context, started_at=_analysis_started_at)
        if not non_interactive:
            print("next_step: review the generated .ai-context files; related-domain reference expansion is now applied automatically when configured")
    elif result.returncode == 124:
        if not non_interactive:
            print(
                "next_step: this provider timed out; prefer Claude Code /analysis or run awf analyze --dry-run to prepare artifacts first",
                file=sys.stderr,
            )
    elif result.returncode != 0:
        if not non_interactive:
            print(
                "next_step: inspect .analysis-state.json and .ai-context/.tmp to decide whether to retry or switch to Claude Code /analysis",
                file=sys.stderr,
            )
    # JSON output mode: restore stdout and print clean JSON
    if output_format == "json" and _original_stdout:
        sys.stdout = _original_stdout
        import json as json_mod
        output_files = []
        for fname in ["api-spec.json", "data-model.md", "domain-overview.md", "external-integration.md", "ANALYSIS_REPORT.md"]:
            fpath = context.ai_context_dir / fname
            if fpath.exists():
                output_files.append(fname)
        json_result = {
            "command": "analyze",
            "service": context.service,
            "domain": context.domain,
            "mode": context.mode,
            "status": finalized.get("layers", {}).get("output", {}).get("status", "unknown"),
            "scale": finalized.get("scale", "unknown"),
            "output_files": output_files,
            "ai_context_dir": str(context.ai_context_dir),
            "elapsed_sec": round(stage2_elapsed, 1) if 'stage2_elapsed' in dir() else None,
        }
        print(json_mod.dumps(json_result, ensure_ascii=False, indent=2))
    else:
        if result.stdout:
            print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
        hint = maybe_doctor_hint(provider_name, result.stderr)
        if hint:
            print(hint, file=sys.stderr)
    return result.returncode


def _run_analyze_all(args: argparse.Namespace) -> int:
    """Analyze all domains in a service sequentially with delay between each."""
    import copy
    import time as time_mod
    from pathlib import Path
    from awf.core.scanner import scan_repo
    from awf.core.config import load_awf_config

    github_root = args.github_root or str(Path.home() / "Documents" / "GitHub")
    repo_path = Path(github_root) / args.service
    if not repo_path.is_dir():
        print(f"error: repo not found at {repo_path}", file=sys.stderr)
        return 2

    scan_result = scan_repo(repo_path, use_ai=not bool(getattr(args, "dry_run", False)))
    if not scan_result.units:
        print(f"error: no domains found in {repo_path}", file=sys.stderr)
        return 1

    delay = int(getattr(args, "delay", 10) if getattr(args, "delay", None) is not None else 10)
    total = len(scan_result.units)
    print(f"=== awf analyze --all: {args.service} ({total} domains, delay={delay}s) ===", file=sys.stderr)
    print(f"language: {scan_result.language}/{scan_result.framework}", file=sys.stderr)
    print(f"pattern: {scan_result.unit_pattern}", file=sys.stderr)
    print(file=sys.stderr)

    succeeded = []
    failed = []
    skipped = []

    for i, domain_info in enumerate(scan_result.units):
        domain_name = domain_info.name
        print(f"--- [{i+1}/{total}] {domain_name} ({domain_info.file_count} files) ---", file=sys.stderr)

        # Create a copy of args with the domain set
        domain_args = copy.copy(args)
        domain_args.domain = domain_name
        # Remove --all to prevent recursion
        domain_args.all = False
        domain_args.no_ready_gate = True

        try:
            rc = run_analyze(domain_args)
            if rc == 0:
                succeeded.append(domain_name)
                print(f"  result: completed", file=sys.stderr)
            else:
                failed.append((domain_name, rc))
                print(f"  result: failed (rc={rc})", file=sys.stderr)
        except KeyboardInterrupt:
            print(f"\ninterrupted at {domain_name}. {len(succeeded)} completed, {len(failed)} failed, {total - i - 1} remaining.", file=sys.stderr)
            return 130
        except Exception as exc:
            failed.append((domain_name, str(exc)))
            print(f"  result: error ({exc})", file=sys.stderr)

        # Delay between domains (except last)
        if i < total - 1:
            print(f"  waiting {delay}s before next domain...", file=sys.stderr)
            time_mod.sleep(delay)

    # Summary
    print(f"\n=== Summary: {args.service} ===", file=sys.stderr)
    print(f"  total: {total}", file=sys.stderr)
    print(f"  succeeded: {len(succeeded)}", file=sys.stderr)
    print(f"  failed: {len(failed)}", file=sys.stderr)
    if failed:
        for name, reason in failed:
            print(f"    - {name}: {reason}", file=sys.stderr)

    return 0 if not failed else 1


def _resolve_service_docs_root(args: argparse.Namespace) -> tuple[Path, Path, str]:
    """Resolve docs_root, github_root, and service name for catalog/check commands.

    Uses a dummy domain to resolve paths via resolve_analysis_context, then
    extracts docs_root and github_root from the context.
    """
    from awf.core.config import resolve_analysis_context
    # Use a placeholder domain just to resolve paths
    ctx = resolve_analysis_context(
        service=args.service,
        domain="__placeholder__",
        deep=False,
        repo_root=getattr(args, "repo_root", None),
        docs_root=getattr(args, "docs_root", None),
        github_root=getattr(args, "github_root", None),
        use_ai_discovery=False,
    )
    return ctx.docs_root, ctx.github_root, args.service


def _load_analysis_config(docs_root: Path) -> dict:
    """Load analysis-config.json from docs _templates."""
    config_path = docs_root / "_templates" / "analysis-config.json"
    if config_path.exists():
        return json.loads(config_path.read_text(encoding="utf-8"))
    return {}


def _scan_ai_context_units(docs_root: Path, service: str) -> dict[str, dict]:
    """Scan .ai-context directories for a service and return unit metadata."""
    units: dict[str, dict] = {}
    service_dir = docs_root / service
    if not service_dir.is_dir():
        return units
    for unit_dir in sorted(service_dir.iterdir()):
        ai_context = unit_dir / ".ai-context"
        if not ai_context.is_dir():
            continue
        unit_name = unit_dir.name
        meta: dict = {"name": unit_name, "path": str(ai_context)}

        # Check output files
        output_files = ["api-spec.json", "data-model.md", "domain-overview.md", "external-integration.md"]
        present_files = [f for f in output_files if (ai_context / f).is_file()]
        meta["output_files"] = len(present_files)

        # Check domain-overview line count
        overview_path = ai_context / "domain-overview.md"
        if overview_path.is_file():
            meta["overview_lines"] = len(overview_path.read_text(encoding="utf-8", errors="ignore").splitlines())
        else:
            meta["overview_lines"] = 0

        # Load state
        state_path = ai_context / ".analysis-state.json"
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            meta["completed_at"] = state.get("completedAt")
            meta["output_status"] = state.get("layers", {}).get("output", {}).get("status")
        else:
            meta["completed_at"] = None
            meta["output_status"] = None

        # Load hashes
        hashes_path = ai_context / ".tmp" / "hashes.json"
        if hashes_path.is_file():
            meta["hashes"] = json.loads(hashes_path.read_text(encoding="utf-8"))
        else:
            meta["hashes"] = None

        units[unit_name] = meta
    return units


def _load_unit_import_graph(meta: dict):
    """Load the import graph saved alongside this unit's analysis, if any."""
    from awf.core.import_graph import ImportGraph

    ai_context_path = meta.get("path")
    if not ai_context_path:
        return None
    return ImportGraph.load(Path(ai_context_path) / ".tmp" / "import-graph.json")


def _check_unit_drift(meta: dict, gh_root: Path) -> dict:
    """Compare saved hashes with current files to detect drift."""
    hashes_data = meta.get("hashes")
    if not hashes_data:
        return {"stale": False, "reason": "no_hashes", "changed_files": []}

    saved_files = hashes_data.get("files", [])
    if not saved_files:
        return {"stale": False, "reason": "empty_hashes", "changed_files": []}

    import hashlib
    changed: list[dict] = []
    for entry in saved_files:
        file_path = gh_root / entry["path"]
        if not file_path.is_file():
            changed.append({"path": entry["path"], "status": "deleted"})
            continue
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        current_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if current_hash != entry["sha256"]:
            changed.append({"path": entry["path"], "status": "modified"})

    # Transitive stale candidates: files unchanged on disk but whose imported
    # source moved. Loaded from the import graph saved on the previous run.
    # The exit code keeps using direct changes only, so existing CI integrations
    # do not start failing on transitive findings alone.
    direct_paths = {c["path"] for c in changed}
    transitive_stale_files: list[str] = []
    if direct_paths:
        try:
            from awf.core.graph_builder import compute_transitive_stale_paths

            graph = _load_unit_import_graph(meta)
            transitive_stale_files = sorted(
                compute_transitive_stale_paths(graph, direct_paths)
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"warning: import-graph stale lookup failed for {meta.get('name', '?')}: {exc}",
                file=sys.stderr,
            )

    return {
        "stale": len(changed) > 0,
        "changed_count": len(changed),
        "total_files": len(saved_files),
        "changed_files": changed,
        "transitive_stale_count": len(transitive_stale_files),
        "transitive_stale_files": transitive_stale_files,
    }


def _run_drift_check(args: argparse.Namespace) -> int:
    """Detect stale .ai-context by comparing saved hashes with current source files."""
    try:
        docs_root, gh_root, service = _resolve_service_docs_root(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    analyzed_units = _scan_ai_context_units(docs_root, service)
    if not analyzed_units:
        print(f"No analyzed units found for {service}.")
        return 0

    print(f"=== Drift Detection: {service} ===")
    stale_count = 0
    up_to_date_count = 0
    total_transitive = 0

    for name, meta in sorted(analyzed_units.items()):
        drift = _check_unit_drift(meta, gh_root)
        if drift["stale"]:
            stale_count += 1
            changed = drift["changed_files"]
            transitive = drift.get("transitive_stale_files", [])
            total_transitive += len(transitive)
            date = (meta.get("completed_at") or "")[:10]
            extra = f" + {len(transitive)} transitive" if transitive else ""
            print(
                f"  \u26a0 {name}: {drift['changed_count']} direct{extra} "
                f"since last analysis ({date})"
            )
            for cf in changed[:5]:
                print(f"    {cf['status']}: {cf['path']}")
            if len(changed) > 5:
                print(f"    ... and {len(changed) - 5} more direct")
            for path in transitive[:5]:
                print(f"    transitive: {path}")
            if len(transitive) > 5:
                print(f"    ... and {len(transitive) - 5} more transitive")
        else:
            up_to_date_count += 1
            print(f"  \u2713 {name}: no changes")

    transitive_note = f" (+{total_transitive} transitive)" if total_transitive else ""
    print(
        f"\nstale: {stale_count} units{transitive_note}, "
        f"up-to-date: {up_to_date_count} units"
    )
    # Exit code intentionally uses direct stale only, so existing CI gates
    # that key on "did source change?" keep their semantics. Transitive
    # findings are operational visibility, not a hard fail.
    return 1 if stale_count > 0 else 0


def _run_catalog(args: argparse.Namespace) -> int:
    """Show analysis status for all units in a service."""
    try:
        docs_root, gh_root, service = _resolve_service_docs_root(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    analysis_config = _load_analysis_config(docs_root)
    domain_definitions = analysis_config.get("domain_definitions", {})

    # Auto-discover if config is empty
    if not domain_definitions:
        from awf.core.scanner import scan_repo
        repo_path = gh_root / service
        if repo_path.is_dir():
            scan_result = scan_repo(repo_path, use_ai=False)
            for d in scan_result.units:
                domain_definitions[d.name] = {
                    "directories": {service: [d.directory]},
                }

    # All units from config (denominator)
    config_units = set()
    for def_name, def_value in domain_definitions.items():
        dirs = def_value.get("directories", {})
        if service in dirs:
            config_units.add(def_name)

    # Analyzed units from .ai-context (numerator)
    analyzed_units = _scan_ai_context_units(docs_root, service)

    # Merge: all known units
    all_units = sorted(config_units | set(analyzed_units.keys()))

    print(f"=== {service} ({len(all_units)} units) ===")
    completed = 0
    thin = 0
    stale = 0
    not_analyzed = 0

    for name in all_units:
        meta = analyzed_units.get(name)
        if not meta or meta.get("output_status") != "completed":
            not_analyzed += 1
            print(f"  \u2717 {name:<25} \u2014           not analyzed")
            continue

        date = (meta.get("completed_at") or "")[:10]
        lines = meta.get("overview_lines", 0)
        files = len((meta.get("hashes") or {}).get("files", []))
        drift = _check_unit_drift(meta, gh_root)

        if lines < 15:
            thin += 1
            print(f"  \u2717 {name:<25} {date}  {lines:>4}\uc904  {files}\ud30c\uc77c  thin (< 15\uc904)")
        elif drift["stale"]:
            stale += 1
            transitive = drift.get("transitive_stale_count", 0)
            extra = f"+{transitive}T " if transitive else ""
            print(f"  \u26a0 {name:<25} {date}  {lines:>4}\uc904  {files}\ud30c\uc77c  stale ({drift['changed_count']} {extra}changed)")
        else:
            completed += 1
            print(f"  \u2713 {name:<25} {date}  {lines:>4}\uc904  {files}\ud30c\uc77c")

    print(f"\n  \ubd84\uc11d \uc644\ub8cc: {completed}/{len(all_units)}, "
          f"\ube48\uc57d: {thin}, stale: {stale}, \ubbf8\ubd84\uc11d: {not_analyzed}")
    return 0


def _run_cycles(args: argparse.Namespace) -> int:
    """Report import cycles per unit using each unit's saved import graph.

    Exit code: 1 if any cycle found anywhere in the service, 0 otherwise.
    Units with no saved graph are skipped (and noted), not failed \u2014 a fresh
    repo simply has nothing to analyze yet.
    """
    try:
        docs_root, gh_root, service = _resolve_service_docs_root(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    analyzed_units = _scan_ai_context_units(docs_root, service)
    if not analyzed_units:
        print(f"No analyzed units found for {service}.")
        return 0

    print(f"=== Import Cycles: {service} ===")
    units_with_cycles = 0
    units_clean = 0
    units_no_graph = 0
    total_cycles = 0

    for name, meta in sorted(analyzed_units.items()):
        try:
            graph = _load_unit_import_graph(meta)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {name}: graph load failed ({exc})")
            continue

        if graph is None:
            units_no_graph += 1
            print(f"  \u00b7 {name}: no import graph saved (run analyze first)")
            continue

        cycles = graph.detect_cycles()
        if not cycles:
            units_clean += 1
            print(f"  \u2713 {name}: no cycles ({len(graph.nodes)} files, {graph.edge_count()} edges)")
            continue

        units_with_cycles += 1
        total_cycles += len(cycles)
        print(f"  \u26a0 {name}: {len(cycles)} cycle(s)")
        # Cycles are SCCs; the smallest are usually the most actionable.
        for cycle in sorted(cycles, key=len)[:5]:
            arrow = " \u2192 ".join(cycle + [cycle[0]])
            print(f"    cycle ({len(cycle)} files): {arrow}")
        if len(cycles) > 5:
            print(f"    ... and {len(cycles) - 5} more")

    summary = (
        f"\nunits with cycles: {units_with_cycles} ({total_cycles} total), "
        f"clean: {units_clean}, no graph: {units_no_graph}"
    )
    print(summary)
    return 1 if units_with_cycles > 0 else 0
