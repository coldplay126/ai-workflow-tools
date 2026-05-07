#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "cli" / "src"))

from awf.core.analysis_state import ensure_ai_context_dirs, load_analysis_state
from awf.core.config import resolve_analysis_context
from awf.core.events import EventType, ExecutionEvent
from awf.core.state import initialize_workflow, load_workflow_state
from awf.core.state_updater import AnalysisStateUpdater, WorkflowStateUpdater


def _event(event_type: EventType, task_id: str, data: dict) -> ExecutionEvent:
    return ExecutionEvent(
        type=event_type,
        timestamp="2026-03-31T18:20:00+09:00",
        run_id="run-1",
        task_id=task_id,
        source="cli",
        sequence=0,
        data=data,
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        repo_root = tmpdir / "repo"
        docs_root = tmpdir / "docs"
        github_root = tmpdir
        repo_root.mkdir()
        (repo_root / "pyproject.toml").write_text("[project]\nname='x'\nversion='0.1.0'\n", encoding="utf-8")
        (repo_root / "docs").mkdir(parents=True)
        (repo_root / "docs" / "awf-cli-architecture.md").write_text("# fixture\n", encoding="utf-8")
        (repo_root / ".workflow" / "artifacts").mkdir(parents=True)
        (repo_root / ".workflow" / "tmp").mkdir(parents=True)
        initialize_workflow(str(repo_root), "fixture concept", force=True)

        (docs_root / "_templates").mkdir(parents=True)
        (docs_root / "_templates" / "analysis-config.json").write_text(
            json.dumps(
                {
                    "service_map": {"sample-api": "${AWF_GITHUB_ROOT}/sample-api"},
                    "domain_definitions": {
                        "health": {
                            "directories": {"sample-api": ["src/domain/health-check"]},
                            "related_domains": [],
                            "existing_docs": [],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        (docs_root / "_templates" / "analysis-pipeline.json").write_text(
            json.dumps({"version": "fixture"}),
            encoding="utf-8",
        )
        target_dir = github_root / "sample-api" / "src" / "domain" / "health-check"
        target_dir.mkdir(parents=True)
        (target_dir / "lightship.controller.ts").write_text("export default 1;\n", encoding="utf-8")
        context = resolve_analysis_context(
            service="sample-api",
            domain="health",
            deep=False,
            repo_root=str(repo_root),
            docs_root=str(docs_root),
            github_root=str(github_root),
        )
        ensure_ai_context_dirs(context)

        analysis_updater = AnalysisStateUpdater(context)
        analysis_updater.handle(_event(EventType.TASK_STARTED, "analyze-1", {"task_type": "analyze", "provider": "fixture"}))
        analysis_updater.handle(_event(EventType.STAGE_STARTED, "analyze-1", {"stage": "stage1"}))
        analysis_updater.handle(_event(EventType.WORKER_SPAWNED, "analyze-1", {"worker_id": "api", "role": "stage2_writer:api-spec.json"}))
        analysis_updater.handle(_event(EventType.ARTIFACT_CREATED, "analyze-1", {"kind": "analysis_prompt", "path": "x"}))
        analysis_updater.handle(_event(EventType.WORKER_COMPLETED, "analyze-1", {"worker_id": "api", "passed": True, "duration_sec": 0.5}))
        analysis_updater.handle(_event(EventType.STAGE_COMPLETED, "analyze-1", {"stage": "stage1", "duration_sec": 1.2}))
        analysis_updater.handle(_event(EventType.TASK_COMPLETED, "analyze-1", {"task_type": "analyze", "provider": "fixture", "returncode": 0, "duration_sec": 1.4}))
        analysis_state = load_analysis_state(context)
        if analysis_state.get("eventSync", {}).get("stages", {}).get("stage1", {}).get("status") != "completed":
            raise SystemExit("analysis stage sync missing")
        if analysis_state.get("eventSync", {}).get("artifacts", {}).get("created") != 1:
            raise SystemExit("analysis artifact sync missing")
        if analysis_state.get("eventSync", {}).get("tasks", {}).get("analyze-1", {}).get("status") != "completed":
            raise SystemExit("analysis task sync missing")
        if analysis_state.get("eventSync", {}).get("workers", {}).get("api", {}).get("passed") is not True:
            raise SystemExit("analysis worker sync missing")

        workflow_updater = WorkflowStateUpdater(str(repo_root))
        workflow_updater.handle(_event(EventType.TASK_STARTED, "wf-1", {"task_type": "wf_phase", "provider": "fixture"}))
        workflow_updater.handle(_event(EventType.STAGE_STARTED, "wf-1", {"stage": "prepare"}))
        workflow_updater.handle(_event(EventType.STAGE_COMPLETED, "wf-1", {"stage": "prepare", "duration_sec": 0.2}))
        workflow_updater.handle(_event(EventType.PHASE_STARTED, "wf-1", {"phase": "review"}))
        workflow_updater.handle(
            _event(
                EventType.ESCAPE_TRIGGERED,
                "wf-1",
                {
                    "phase": "review",
                    "provider": "fixture",
                    "reason": "scope_divergence",
                    "summary": "fixture escape",
                    "recommended_action": "replan",
                },
            )
        )
        workflow_updater.handle(
            _event(
                EventType.ORCHESTRATOR_DECIDED,
                "wf-1",
                {
                    "phase": "review",
                    "decision": "escalate_user",
                    "reason": "scope_divergence requires replan decision",
                    "replan_target": "plan",
                },
            )
        )
        workflow_updater.handle(_event(EventType.GATE_EVALUATED, "wf-1", {"gate": "G2", "passed": True}))
        workflow_updater.handle(_event(EventType.ARTIFACT_CREATED, "wf-1", {"kind": "wf_result", "path": "y"}))
        workflow_updater.handle(_event(EventType.TASK_COMPLETED, "wf-1", {"task_type": "wf_phase", "provider": "fixture", "returncode": 0, "duration_sec": 0.7}))
        wf_state = load_workflow_state(str(repo_root))
        if wf_state.get("eventSync", {}).get("stages", {}).get("prepare", {}).get("status") != "completed":
            raise SystemExit("workflow stage sync missing")
        if wf_state.get("eventSync", {}).get("phases", {}).get("review", {}).get("status") != "started":
            raise SystemExit("workflow phase sync missing")
        if wf_state.get("eventSync", {}).get("gates", {}).get("G2", {}).get("passed") is not True:
            raise SystemExit("workflow gate sync missing")
        if wf_state.get("eventSync", {}).get("artifacts", {}).get("created") != 1:
            raise SystemExit("workflow artifact sync missing")
        if wf_state.get("eventSync", {}).get("tasks", {}).get("wf-1", {}).get("status") != "completed":
            raise SystemExit("workflow task sync missing")
        if wf_state.get("eventSync", {}).get("escapes", {}).get("review", {}).get("reason") != "scope_divergence":
            raise SystemExit("workflow escape sync missing")
        if wf_state.get("eventSync", {}).get("decisions", {}).get("review", {}).get("decision") != "escalate_user":
            raise SystemExit("workflow decision sync missing")

    print("gateway_state_sync_ok=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
