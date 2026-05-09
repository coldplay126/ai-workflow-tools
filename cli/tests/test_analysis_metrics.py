from __future__ import annotations

from pathlib import Path

from awf.commands.analyze import _record_analysis_complete_safe
from awf.core.analysis_state import load_analysis_state, save_analysis_state
from awf.core.config import AnalysisContext
from awf.core.event_processor import EventProcessor
from awf.core.events import EventType
from awf.core.operational_metrics import iter_events
from awf.core.state_updater import AnalysisStateUpdater


def _context(tmp_path: Path) -> AnalysisContext:
    ai_context_dir = tmp_path / "docs" / "sample-api" / "orders" / ".ai-context"
    ai_context_dir.mkdir(parents=True)
    return AnalysisContext(
        repo_root=tmp_path,
        docs_root=tmp_path / "docs",
        github_root=tmp_path,
        analysis_config_path=tmp_path / "docs" / "analysis-config.json",
        analysis_pipeline_path=tmp_path / "docs" / "_templates" / "analysis-pipeline.json",
        service="sample-api",
        domain="orders",
        mode="standard",
        ai_context_dir=ai_context_dir,
        domain_directories=["src/orders"],
        all_directories={},
        related_domains=[],
        existing_docs=[],
        analysis_mode="document",
    )


def test_analysis_state_updater_records_analysis_bundle_metrics(tmp_path: Path) -> None:
    context = _context(tmp_path)
    processor = EventProcessor(handlers=[AnalysisStateUpdater(context).handle])

    processor.emit(
        event_type=EventType.ARTIFACT_CREATED,
        task_id="analyze-sample-api-orders",
        source="cli",
        data={
            "kind": "analysis_bundle",
            "path": str(context.ai_context_dir / ".tmp" / "domain-bundle.xml"),
            "source_file_count": 4,
            "bundle_line_count": 88,
            "bundle_token_estimate": 640,
        },
    )

    state = load_analysis_state(context)
    bundle = state["eventSync"]["analysisBundle"]
    assert bundle["sourceFileCount"] == 4
    assert bundle["lineCount"] == 88
    assert bundle["tokenEstimate"] == 640
    assert state["eventSync"]["artifacts"]["byKind"]["analysis_bundle"] == 1


def test_record_analysis_complete_uses_state_metrics_and_counts_outputs(tmp_path: Path) -> None:
    context = _context(tmp_path)
    state = load_analysis_state(context)
    state["layers"]["bundle"]["fileCount"] = 3
    state["layers"]["bundle"]["lineCount"] = 40
    state["layers"]["bundle"]["tokenEstimate"] = 250
    state["eventSync"] = {
        "analysisBundle": {
            "sourceFileCount": 5,
            "lineCount": 70,
            "tokenEstimate": 500,
        }
    }
    save_analysis_state(context, state)

    for name in ["api-spec.json", "data-model.md", "ANALYSIS_REPORT.md"]:
        (context.ai_context_dir / name).write_text("ok\n", encoding="utf-8")

    _record_analysis_complete_safe(context, started_at=None, mode="resume")

    [event] = list(iter_events(tmp_path))
    assert event["type"] == "analysis_complete"
    payload = event["payload"]
    assert payload["source_file_count"] == 5
    assert payload["bundle_line_count"] == 70
    assert payload["bundle_token_estimate"] == 500
    assert payload["output_file_count"] == 3

    log_path = tmp_path / ".awf-operations" / "log.md"
    assert "analysis_complete" in log_path.read_text(encoding="utf-8")
