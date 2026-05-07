from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import sys
from typing import Any, Callable

from awf.core.events import EventType
from awf.core.event_processor import EventProcessor
from awf.core.analysis_outputs import parse_stage2_output
from awf.core.analysis_prompt import (
    build_stage2_fanout_prompts,
    build_writer_prompts,
    build_judge_prompt,
    build_fallback_writer_prompt,
)
from awf.core.analysis_writer import (
    WriterResult,
    JudgeResult,
    parse_writer_output,
    parse_judge_output,
    build_judge_input,
    validate_evidence_integrity,
)
from awf.core.analysis_state import get_required_output_files
from awf.core.config import AnalysisContext

# Scale thresholds per reference.md §1.3:
#   small: <= 10 files, standard: 11-30, large: > 30
# LARGE_DOMAIN_FILECOUNT_THRESHOLD is configurable via analysis.fanout_large_file_threshold
SMALL_DOMAIN_FILECOUNT_THRESHOLD = 10
LARGE_DOMAIN_FILECOUNT_THRESHOLD = 30

# Legacy v1 targets (4 Writers, 1 file each)
WRITER_TARGETS = [
    ("api", "api-spec.json"),
    ("data", "data-model.md"),
    ("domain", "domain-overview.md"),
    ("integration", "external-integration.md"),
]

ALL_OUTPUT_FILES = ["api-spec.json", "data-model.md", "domain-overview.md", "external-integration.md"]


def get_writer_configs(mode: str = "document") -> list[dict]:
    """Return Writer configs for the given analysis mode (A6).

    Loads from external mode contract (spec-as-truth).
    Falls back to empty list if spec file is missing.
    """
    try:
        from awf.core.spec_loader import load_analysis_mode_contract
        contract = load_analysis_mode_contract(mode)
        return list(contract.get("writers", []))
    except (FileNotFoundError, ValueError, KeyError):
        return []


def get_judge_prompt_name(mode: str = "document") -> str:
    """Return Judge prompt name for the given analysis mode.

    Tries to load from external mode contract (spec-as-truth).
    Falls back to "judge" for backward compatibility.
    """
    try:
        from awf.core.spec_loader import load_analysis_mode_contract
        contract = load_analysis_mode_contract(mode)
        return str(contract.get("judge", {}).get("prompt", "judge"))
    except (FileNotFoundError, ValueError, KeyError):
        return "judge"

# Fallback constraints per §2.3
MAX_FALLBACK_FILES = 3
MAX_FALLBACK_TOKENS_PER_FILE = 4000
LOW_CONFIDENCE_THRESHOLD = 3


def analysis_scale(file_count: int, config: Any) -> str:
    analysis_config = config.raw.get("analysis", {})
    if not isinstance(analysis_config, dict):
        analysis_config = {}
    large_threshold = int(analysis_config.get("fanout_large_file_threshold", LARGE_DOMAIN_FILECOUNT_THRESHOLD) or LARGE_DOMAIN_FILECOUNT_THRESHOLD)
    if file_count > large_threshold:
        return "large"
    if file_count <= SMALL_DOMAIN_FILECOUNT_THRESHOLD:
        return "small"
    return "standard"


def should_stage2_fanout(
    *,
    context: AnalysisContext,
    config: Any,
    scale: str,
    bundle_lines: int,
    bundle_tokens: int,
) -> bool:
    """Check if Stage 2 fanout (Writer/Judge) should be activated.

    Phase 2: Always active unless explicitly disabled via pipeline config.
    Legacy conditions (deep + large + tokens) are kept as fallback.
    """
    analysis_config = config.raw.get("analysis", {})
    if not isinstance(analysis_config, dict):
        analysis_config = {}

    # Phase 2: pipeline config override — fanout_enabled=false disables
    layer3 = analysis_config.get("layer3", {})
    if isinstance(layer3, dict):
        fanout_enabled = layer3.get("fanout_enabled")
        if fanout_enabled is not None:
            return bool(fanout_enabled)
        # writers: [] means disable fanout
        writers = layer3.get("writers")
        if isinstance(writers, list) and len(writers) == 0:
            return False

    # Phase 2 default: always enabled (relaxed from deep+large+tokens)
    return True


def compose_stage2_output_from_writer_results(writer_results: dict[str, str]) -> str:
    parts: list[str] = []
    for _, file_name in WRITER_TARGETS:
        raw = writer_results.get(file_name, "").strip()
        if not raw:
            continue
        parsed = parse_stage2_output(raw)
        content = parsed.get(file_name, raw).rstrip()
        parts.append(f"===FILE: {file_name}===")
        parts.append(content)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def compose_output_from_judge_result(judge_result: JudgeResult, output_files: list[str] | None = None) -> str:
    """Compose final ===FILE: markers output from JudgeResult."""
    files = output_files or ALL_OUTPUT_FILES
    parts: list[str] = []
    for file_name in files:
        content = judge_result.merged_output.get(file_name, "").rstrip()
        if not content:
            continue
        parts.append(f"===FILE: {file_name}===")
        parts.append(content)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def build_stage2_consistency_prompt(
    *,
    context: AnalysisContext,
    stage1_memo_text: str,
    combined_output: str,
) -> str:
    from awf.core.spec_loader import load_prompt_optional
    prompt = load_prompt_optional("analysis", "stage2-consistency",
        service=context.service, domain=context.domain,
        stage1_memo=stage1_memo_text, combined_output=combined_output,
    )
    if prompt:
        return prompt
    return (
        f"Review the Stage 2 fan-out outputs for `{context.service}/{context.domain}`.\n"
        f"## Stage 1 Memo\n{stage1_memo_text}\n\n## Combined Stage 2 Output\n{combined_output}\n"
    )


def analyze_stage2_consistency(raw: str, analysis_mode: str = "document") -> dict[str, Any]:
    parsed = parse_stage2_output(raw)
    output_files = get_required_output_files(analysis_mode)
    issues: list[str] = []

    # Generic check: all required output files must be present and non-empty
    for name in output_files:
        if not parsed.get(name, "").strip():
            issues.append(f"missing_or_empty:{name}")

    # Document-mode-specific cross-file consistency checks
    if analysis_mode == "document":
        api_spec_raw = parsed.get("api-spec.json", "").strip()
        if api_spec_raw:
            try:
                api_spec = json.loads(api_spec_raw)
            except json.JSONDecodeError:
                issues.append("invalid_api_spec_json")
            else:
                endpoints = api_spec.get("endpoints", [])
                if not isinstance(endpoints, list):
                    issues.append("invalid_api_endpoints")
                elif endpoints and not parsed.get("domain-overview.md", "").strip():
                    issues.append("missing_domain_overview")
                elif endpoints and not parsed.get("external-integration.md", "").strip():
                    issues.append("missing_external_integration")

    return {
        "passed": not issues,
        "issues": issues,
    }


def run_stage2_fanout(
    *,
    context: AnalysisContext,
    provider: Any,
    provider_factory: Callable[[], Any] | None,
    provider_name: str,
    add_dirs: list[str],
    stage1_memo_text: str,
    domain_bundle_text: str,
    runner: Callable[[Any, str, str, list[str], str], tuple[Any, float]],
    save_additional_result: Callable[[AnalysisContext, str, str, str], Any],
    processor: EventProcessor | None = None,
    task_id: str | None = None,
) -> tuple[object | None, str | None, dict[str, Any]]:
    """Run Stage 2 fanout with Writer/Judge pipeline (Phase 2).

    Flow: [Structure Writer, Behavior Writer] parallel
          → Judge (sequential)
          → (optional) Fallback Writer + Judge re-run
          → Final 4-file output
    """
    # Load mode contract dynamically
    analysis_mode = context.analysis_mode
    writer_configs = get_writer_configs(analysis_mode)
    if not writer_configs:
        raise ValueError(
            f"No writer configs for mode '{analysis_mode}'. "
            f"Mode contract must define at least one writer for Stage 2 fanout."
        )
    output_files = get_required_output_files(analysis_mode)
    judge_prompt_name = get_judge_prompt_name(analysis_mode)

    metadata: dict[str, Any] = {
        "writerCount": len(writer_configs),
        "executionMode": "parallel_v2",
        "analysisMode": analysis_mode,
        "consistencyPassed": False,
        "consistencyIssues": [],
        "consistencyArtifactSuffix": None,
        "judgeVerdict": None,
        "fallbackTriggered": False,
        "fallbackFiles": [],
    }

    def _emit(event_type: EventType, *, data: dict[str, Any]) -> None:
        if processor is None or task_id is None:
            return
        processor.emit(event_type=event_type, task_id=task_id, source="cli", data=data)

    try:
        # --- Phase 1: Build Writer prompts ---
        writer_prompts = build_writer_prompts(
            context=context,
            domain_bundle_text=domain_bundle_text,
            stage1_memo_text=stage1_memo_text,
            writer_configs=writer_configs,
        )

        # --- Phase 2: Run Writers in parallel ---
        def _run_writer(writer_id: str, prompt: str) -> tuple[str, int, str, float]:
            writer_provider = provider_factory() if provider_factory is not None else provider
            print(f"provider_running: {provider_name} writer {writer_id}", file=sys.stderr)
            _emit(
                EventType.WORKER_SPAWNED,
                data={"worker_id": writer_id, "role": f"stage2_writer_v2:{writer_id}"},
            )
            writer_result, writer_elapsed = runner(
                writer_provider,
                prompt,
                str(context.repo_root),
                add_dirs,
                f"{provider_name} writer {writer_id}",
            )
            writer_output = writer_result.stdout if (writer_result.stdout or "").strip() else (writer_result.stderr or "")
            return writer_id, writer_result.returncode, writer_output, writer_elapsed

        writer_results: list[WriterResult] = []
        writer_failures: list[str] = []

        with ThreadPoolExecutor(max_workers=len(writer_configs)) as executor:
            futures = {
                executor.submit(_run_writer, wc["id"], writer_prompts[wc["id"]]): wc
                for wc in writer_configs
            }

            for future in as_completed(futures):
                wc = futures[future]
                writer_id = wc["id"]
                try:
                    result_id, returncode, raw_output, elapsed = future.result()
                except Exception as exc:
                    writer_failures.append(f"writer_exception:{writer_id}:{exc}")
                    _emit(
                        EventType.WORKER_COMPLETED,
                        data={"worker_id": writer_id, "role": f"stage2_writer_v2:{writer_id}", "passed": False},
                    )
                    continue

                print(f"provider_completed: {provider_name} writer {result_id} ({elapsed:.1f}s)", file=sys.stderr)

                if returncode != 0:
                    writer_failures.append(f"writer_failed:{result_id}")
                    _emit(
                        EventType.WORKER_COMPLETED,
                        data={"worker_id": result_id, "role": f"stage2_writer_v2:{result_id}", "passed": False, "duration_sec": elapsed},
                    )
                    continue

                if not raw_output.strip():
                    writer_failures.append(f"writer_empty:{result_id}")
                    _emit(
                        EventType.WORKER_COMPLETED,
                        data={"worker_id": result_id, "role": f"stage2_writer_v2:{result_id}", "passed": False, "duration_sec": elapsed},
                    )
                    continue

                # Parse WriterResult and validate
                wr = parse_writer_output(raw_output, result_id)
                if not wr.claims and not wr.output_sections:
                    writer_failures.append(f"writer_empty_parsed:{result_id}")
                    _emit(
                        EventType.WORKER_COMPLETED,
                        data={"worker_id": result_id, "role": f"stage2_writer_v2:{result_id}", "passed": False, "duration_sec": elapsed},
                    )
                    continue
                writer_results.append(wr)

                # Save raw artifact
                artifact_path = save_additional_result(context, provider_name, raw_output, f"writer-v2-{result_id}")
                _emit(
                    EventType.ARTIFACT_CREATED,
                    data={
                        "path": str(artifact_path),
                        "kind": "analysis_writer_v2_result",
                        "producer": provider_name,
                        "status": "final",
                        "replaces": None,
                    },
                )
                _emit(
                    EventType.WORKER_COMPLETED,
                    data={
                        "worker_id": result_id,
                        "role": f"stage2_writer_v2:{result_id}",
                        "passed": True,
                        "duration_sec": elapsed,
                        "claims_count": len(wr.claims),
                        "low_confidence_count": len(wr.low_confidence_claims()),
                    },
                )

        if writer_failures:
            # Save successful writer results for inspection even on partial failure
            for wr in writer_results:
                save_additional_result(
                    context, provider_name,
                    wr.raw_output,
                    f"writer-v2-{wr.writer}-partial",
                )
            failure_detail = "; ".join(writer_failures)
            metadata["writerFailures"] = writer_failures
            metadata["successfulWriters"] = [wr.writer for wr in writer_results]
            return None, f"writer_partial_failure:{failure_detail}", metadata

        if len(writer_results) != len(writer_configs):
            return None, "writer_outputs_incomplete", metadata

        # --- Phase 3: Run Judge ---
        judge_result = _run_judge(
            context=context,
            provider=provider,
            provider_name=provider_name,
            add_dirs=add_dirs,
            writer_results=writer_results,
            runner=runner,
            save_additional_result=save_additional_result,
            _emit=_emit,
            judge_prompt_name=judge_prompt_name,
            output_files=output_files,
        )
        if judge_result is None:
            return None, "judge_failed", metadata

        metadata["judgeVerdict"] = judge_result.verdict

        # --- Phase 4: Code fallback (if needed, 1x max) ---
        if judge_result.has_fallback():
            metadata["fallbackTriggered"] = True
            metadata["fallbackFiles"] = judge_result.code_fallback_files

            print(f"judge_fallback: {len(judge_result.code_fallback_files)} files requested", file=sys.stderr)
            _emit(
                EventType.WORKER_SPAWNED,
                data={"worker_id": "fallback", "role": "stage2_fallback", "files": judge_result.code_fallback_files},
            )

            # Re-run affected Writers with code content
            fallback_writer_results = _run_fallback_writers(
                context=context,
                provider=provider,
                provider_factory=provider_factory,
                provider_name=provider_name,
                add_dirs=add_dirs,
                writer_prompts=writer_prompts,
                writer_results=writer_results,
                fallback_files=judge_result.code_fallback_files,
                runner=runner,
                save_additional_result=save_additional_result,
                _emit=_emit,
            )

            if fallback_writer_results:
                # Replace original writer results with fallback versions (not merge)
                # This ensures Judge only sees the improved fallback version, not both
                fallback_ids = {wr.writer for wr in fallback_writer_results}
                merged_results = [wr for wr in writer_results if wr.writer not in fallback_ids] + fallback_writer_results

                # Re-run Judge with updated results
                judge_result = _run_judge(
                    context=context,
                    provider=provider,
                    provider_name=provider_name,
                    add_dirs=add_dirs,
                    writer_results=merged_results,
                    runner=runner,
                    save_additional_result=save_additional_result,
                    _emit=_emit,
                    suffix="fallback",
                    judge_prompt_name=judge_prompt_name,
                    output_files=output_files,
                )
                if judge_result is None:
                    return None, "judge_fallback_failed", metadata
                metadata["judgeVerdict"] = judge_result.verdict

        # --- Phase 4b: Validate evidence integrity (A5) ---
        evidence_violations = validate_evidence_integrity(writer_results, judge_result)
        if evidence_violations:
            metadata["evidenceViolations"] = evidence_violations
            print(
                f"warning: judge evidence integrity violations ({len(evidence_violations)}): "
                + "; ".join(evidence_violations[:5]),
                file=sys.stderr,
            )

        # --- Phase 5: Compose final output ---
        combined_output = compose_output_from_judge_result(judge_result, output_files=output_files)
        if not combined_output.strip():
            return None, "judge_output_empty", metadata

        # Validate consistency
        consistency_summary = analyze_stage2_consistency(combined_output, analysis_mode=analysis_mode)
        metadata["consistencyPassed"] = bool(consistency_summary["passed"])
        metadata["consistencyIssues"] = list(consistency_summary["issues"])

        from awf.providers.base import ProviderResult
        return ProviderResult(returncode=0, stdout=combined_output, stderr=""), None, metadata

    except KeyboardInterrupt:
        raise
    except Exception as exc:
        return None, f"fanout_exception:{exc}", metadata


def _run_judge(
    *,
    context: AnalysisContext,
    provider: Any,
    provider_name: str,
    add_dirs: list[str],
    writer_results: list[WriterResult],
    runner: Callable,
    save_additional_result: Callable,
    _emit: Callable,
    suffix: str = "",
    judge_prompt_name: str = "judge",
    output_files: list[str] | None = None,
) -> JudgeResult | None:
    """Run Judge on Writer results and return parsed JudgeResult."""
    writer_input_text = build_judge_input(writer_results)
    judge_prompt = build_judge_prompt(
        context=context,
        writer_input_text=writer_input_text,
        judge_prompt_name=judge_prompt_name,
    )

    label = f"judge{'-' + suffix if suffix else ''}"
    _emit(
        EventType.WORKER_SPAWNED,
        data={"worker_id": label, "role": f"stage2_{label}"},
    )
    print(f"provider_running: {provider_name} stage2 {label}", file=sys.stderr)

    judge_raw_result, judge_elapsed = runner(
        provider,
        judge_prompt,
        str(context.repo_root),
        add_dirs,
        f"{provider_name} stage2 {label}",
    )
    print(f"provider_completed: {provider_name} stage2 {label} ({judge_elapsed:.1f}s)", file=sys.stderr)

    _emit(
        EventType.WORKER_COMPLETED,
        data={
            "worker_id": label,
            "role": f"stage2_{label}",
            "passed": judge_raw_result.returncode == 0,
            "duration_sec": judge_elapsed,
        },
    )

    if judge_raw_result.returncode != 0:
        return None

    raw_output = judge_raw_result.stdout if (judge_raw_result.stdout or "").strip() else (judge_raw_result.stderr or "")
    if not raw_output.strip():
        return None

    artifact_path = save_additional_result(context, provider_name, raw_output, f"{label}-result")
    _emit(
        EventType.ARTIFACT_CREATED,
        data={
            "path": str(artifact_path),
            "kind": f"analysis_{label}_result",
            "producer": provider_name,
            "status": "final",
            "replaces": None,
        },
    )

    return parse_judge_output(raw_output, required_files=output_files)


def _run_fallback_writers(
    *,
    context: AnalysisContext,
    provider: Any,
    provider_factory: Callable[[], Any] | None,
    provider_name: str,
    add_dirs: list[str],
    writer_prompts: dict[str, str],
    writer_results: list[WriterResult],
    fallback_files: list[str],
    runner: Callable,
    save_additional_result: Callable,
    _emit: Callable,
) -> list[WriterResult]:
    """Re-run Writers that had low-confidence claims, with code fallback content."""
    # Determine which Writers need re-run based on their claims' source_files
    writers_needing_fallback: set[str] = set()
    for wr in writer_results:
        for claim in wr.low_confidence_claims():
            if any(sf in fallback_files for sf in claim.source_files):
                writers_needing_fallback.add(wr.writer)

    # If no specific writer matched, re-run all
    if not writers_needing_fallback:
        writers_needing_fallback = {wr.writer for wr in writer_results}

    results: list[WriterResult] = []
    for writer_id in writers_needing_fallback:
        original_prompt = writer_prompts.get(writer_id, "")
        if not original_prompt:
            continue

        fallback_prompt = build_fallback_writer_prompt(
            original_prompt=original_prompt,
            fallback_files=fallback_files,
            context=context,
            max_tokens_per_file=MAX_FALLBACK_TOKENS_PER_FILE,
        )

        writer_provider = provider_factory() if provider_factory is not None else provider
        print(f"provider_running: {provider_name} fallback writer {writer_id}", file=sys.stderr)

        raw_result, elapsed = runner(
            writer_provider,
            fallback_prompt,
            str(context.repo_root),
            add_dirs,
            f"{provider_name} fallback writer {writer_id}",
        )
        print(f"provider_completed: {provider_name} fallback writer {writer_id} ({elapsed:.1f}s)", file=sys.stderr)

        if raw_result.returncode != 0:
            continue

        raw_output = raw_result.stdout if (raw_result.stdout or "").strip() else (raw_result.stderr or "")
        if not raw_output.strip():
            continue

        wr = parse_writer_output(raw_output, writer_id)
        results.append(wr)

        artifact_path = save_additional_result(context, provider_name, raw_output, f"fallback-writer-{writer_id}")
        _emit(
            EventType.ARTIFACT_CREATED,
            data={
                "path": str(artifact_path),
                "kind": "analysis_fallback_writer_result",
                "producer": provider_name,
                "status": "final",
                "replaces": None,
            },
        )
        _emit(
            EventType.WORKER_COMPLETED,
            data={
                "worker_id": f"fallback-{writer_id}",
                "role": f"stage2_fallback_writer:{writer_id}",
                "passed": True,
                "duration_sec": elapsed,
                "claims_count": len(wr.claims),
            },
        )

    return results


# --- Legacy v1 fanout (kept for backward compatibility) ---

def run_stage2_fanout_v1(
    *,
    context: AnalysisContext,
    provider: Any,
    provider_factory: Callable[[], Any] | None,
    provider_name: str,
    add_dirs: list[str],
    stage1_memo_text: str,
    domain_bundle_text: str,
    runner: Callable[[Any, str, str, list[str], str], tuple[Any, float]],
    save_additional_result: Callable[[AnalysisContext, str, str, str], Any],
    processor: EventProcessor | None = None,
    task_id: str | None = None,
) -> tuple[object | None, str | None, dict[str, Any]]:
    """Legacy v1 fanout: synthesizer → 4 parallel writers → consistency check."""
    fanout_prompts = build_stage2_fanout_prompts(
        context=context,
        domain_bundle_text=domain_bundle_text,
        stage1_memo_text=stage1_memo_text,
    )
    metadata: dict[str, Any] = {
        "writerCount": len(WRITER_TARGETS),
        "executionMode": "parallel",
        "consistencyPassed": False,
        "consistencyIssues": [],
        "consistencyArtifactSuffix": None,
    }

    def _emit(event_type: EventType, *, data: dict[str, Any]) -> None:
        if processor is None or task_id is None:
            return
        processor.emit(event_type=event_type, task_id=task_id, source="cli", data=data)

    try:
        synth_prompt = fanout_prompts.get("synthesizer", "")
        if synth_prompt:
            _emit(
                EventType.WORKER_SPAWNED,
                data={"worker_id": "synthesizer", "role": "stage2_synthesizer"},
            )
            print(f"provider_running: {provider_name} stage2 synthesizer", file=sys.stderr)
            synth_result, synth_elapsed = runner(
                provider,
                synth_prompt,
                str(context.repo_root),
                add_dirs,
                f"{provider_name} stage2 synthesizer",
            )
            print(f"provider_completed: {provider_name} stage2 synthesizer ({synth_elapsed:.1f}s)", file=sys.stderr)
            _emit(
                EventType.WORKER_COMPLETED,
                data={
                    "worker_id": "synthesizer",
                    "role": "stage2_synthesizer",
                    "passed": synth_result.returncode == 0,
                    "duration_sec": synth_elapsed,
                },
            )
            if synth_result.returncode != 0:
                return None, "synthesizer_failed", metadata
            synth_path = save_additional_result(
                context,
                provider_name,
                synth_result.stdout if (synth_result.stdout or "").strip() else (synth_result.stderr or ""),
                "fanout-synthesizer",
            )
            _emit(
                EventType.ARTIFACT_CREATED,
                data={
                    "path": str(synth_path),
                    "kind": "analysis_fanout_synthesizer",
                    "producer": provider_name,
                    "status": "final",
                    "replaces": None,
                },
            )

        def _run_writer(key: str, file_name: str, prompt: str) -> tuple[str, str, int, str, float]:
            writer_provider = provider_factory() if provider_factory is not None else provider
            print(f"provider_running: {provider_name} writer {key}", file=sys.stderr)
            writer_result, writer_elapsed = runner(
                writer_provider,
                prompt,
                str(context.repo_root),
                add_dirs,
                f"{provider_name} writer {key}",
            )
            writer_output = writer_result.stdout if (writer_result.stdout or "").strip() else (writer_result.stderr or "")
            return key, file_name, writer_result.returncode, writer_output, writer_elapsed

        writer_outputs: dict[str, str] = {}
        writer_failures: list[str] = []
        future_to_key: dict[Any, tuple[str, str]] = {}
        with ThreadPoolExecutor(max_workers=len(WRITER_TARGETS)) as executor:
            for key, file_name in WRITER_TARGETS:
                prompt = fanout_prompts.get(key, "")
                _emit(
                    EventType.WORKER_SPAWNED,
                    data={"worker_id": key, "role": f"stage2_writer:{file_name}"},
                )
                future = executor.submit(_run_writer, key, file_name, prompt)
                future_to_key[future] = (key, file_name)

            for future in as_completed(future_to_key):
                key, file_name = future_to_key[future]
                try:
                    result_key, result_file_name, writer_returncode, writer_output, writer_elapsed = future.result()
                except Exception as exc:
                    writer_failures.append(f"writer_exception:{key}:{exc}")
                    _emit(
                        EventType.WORKER_COMPLETED,
                        data={"worker_id": key, "role": f"stage2_writer:{file_name}", "passed": False},
                    )
                    continue
                print(f"provider_completed: {provider_name} writer {result_key} ({writer_elapsed:.1f}s)", file=sys.stderr)
                if writer_returncode != 0:
                    writer_failures.append(f"writer_failed:{result_key}")
                    _emit(
                        EventType.WORKER_COMPLETED,
                        data={
                            "worker_id": result_key,
                            "role": f"stage2_writer:{result_file_name}",
                            "passed": False,
                            "duration_sec": writer_elapsed,
                        },
                    )
                    continue
                if not writer_output.strip():
                    writer_failures.append(f"writer_empty:{result_key}")
                    _emit(
                        EventType.WORKER_COMPLETED,
                        data={
                            "worker_id": result_key,
                            "role": f"stage2_writer:{result_file_name}",
                            "passed": False,
                            "duration_sec": writer_elapsed,
                        },
                    )
                    continue
                writer_path = save_additional_result(context, provider_name, writer_output, f"writer-{result_key}")
                writer_outputs[result_file_name] = writer_output
                _emit(
                    EventType.ARTIFACT_CREATED,
                    data={
                        "path": str(writer_path),
                        "kind": "analysis_fanout_writer_result",
                        "producer": provider_name,
                        "status": "final",
                        "replaces": None,
                    },
                )
                _emit(
                    EventType.WORKER_COMPLETED,
                    data={
                        "worker_id": result_key,
                        "role": f"stage2_writer:{result_file_name}",
                        "passed": True,
                        "duration_sec": writer_elapsed,
                    },
                )

        if writer_failures:
            return None, writer_failures[0], metadata

        if len(writer_outputs) != len(WRITER_TARGETS):
            return None, "writer_outputs_incomplete", metadata

        combined_output = compose_stage2_output_from_writer_results(writer_outputs)
        if not combined_output.strip():
            return None, "fanout_combined_output_empty", metadata

        consistency_prompt = build_stage2_consistency_prompt(
            context=context,
            stage1_memo_text=stage1_memo_text,
            combined_output=combined_output,
        )
        _emit(
            EventType.WORKER_SPAWNED,
            data={"worker_id": "consistency", "role": "stage2_consistency"},
        )
        print(f"provider_running: {provider_name} stage2 consistency", file=sys.stderr)
        consistency_result, consistency_elapsed = runner(
            provider,
            consistency_prompt,
            str(context.repo_root),
            add_dirs,
            f"{provider_name} stage2 consistency",
        )
        print(f"provider_completed: {provider_name} stage2 consistency ({consistency_elapsed:.1f}s)", file=sys.stderr)
        _emit(
            EventType.WORKER_COMPLETED,
            data={
                "worker_id": "consistency",
                "role": "stage2_consistency",
                "passed": consistency_result.returncode == 0,
                "duration_sec": consistency_elapsed,
            },
        )
        if consistency_result.returncode != 0:
            return None, "consistency_failed", metadata
        consistency_output = consistency_result.stdout if (consistency_result.stdout or "").strip() else (consistency_result.stderr or "")
        consistency_path = save_additional_result(context, provider_name, consistency_output, "fanout-consistency")
        _emit(
            EventType.ARTIFACT_CREATED,
            data={
                "path": str(consistency_path),
                "kind": "analysis_fanout_consistency",
                "producer": provider_name,
                "status": "final",
                "replaces": None,
            },
        )
        consistency_summary = analyze_stage2_consistency(combined_output)
        metadata["consistencyPassed"] = bool(consistency_summary["passed"])
        metadata["consistencyIssues"] = list(consistency_summary["issues"])
        metadata["consistencyArtifactSuffix"] = "fanout-consistency"
        if not consistency_summary["passed"]:
            return None, "consistency_check_failed:" + ",".join(consistency_summary["issues"]), metadata

        from awf.providers.base import ProviderResult

        return ProviderResult(returncode=0, stdout=combined_output, stderr=""), None, metadata
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        return None, f"fanout_exception:{exc}", metadata
