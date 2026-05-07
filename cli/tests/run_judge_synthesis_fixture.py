from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from fixture_support import (
    ROOT,
    initialize_workflow_fixture,
    prepare_workflow_repo,
)

sys.path.insert(0, str(ROOT / "cli" / "src"))

from awf.core.analysis_state import load_analysis_state, record_cross_synthesis
from awf.core.config import resolve_analysis_context
from awf.core.judge import synthesize_cross_stage2, synthesize_workflow_multi_provider_results
from awf.core.state import load_workflow_state, record_workflow_synthesis


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _review_payload(*, conclusion: str, coverage: int, findings: list[dict]) -> dict:
    return {
        "conclusion": conclusion,
        "findings": findings,
        "coverage": {
            "total_requirements": 8,
            "mapped_requirements": 8,
            "percentage": coverage,
            "gaps": [],
        },
        "evidence": [],
        "risks": [],
        "action_items": [],
    }


def _verify_payload(
    *,
    conclusion: str,
    compliance_percentage: int,
    scope_violations: int = 0,
    compliance_fail: int = 0,
    quality_critical: int = 0,
) -> dict:
    return {
        "conclusion": conclusion,
        "scope": {
            "violations": scope_violations,
        },
        "compliance": {
            "pass": max(0, 10 - compliance_fail),
            "fail": compliance_fail,
            "percentage": compliance_percentage,
        },
        "quality": {
            "critical": quality_critical,
            "major": 0,
            "minor": 0,
        },
        "evidence": [],
        "risks": [],
        "action_items": [],
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        workflow_repo = tmp_dir / "repo"
        prepare_workflow_repo(workflow_repo)
        initialized = initialize_workflow_fixture(
            workflow_repo,
            "Fixture judge synthesis concept covering workflow state recording",
        )
        if initialized.returncode != 0:
            print(initialized.stdout, end="")
            return initialized.returncode
        primary_path = tmp_dir / "primary-review.json"
        secondary_path = tmp_dir / "secondary-review.json"
        _write_json(
            primary_path,
            _review_payload(
                conclusion="FAIL - review conflict",
                coverage=60,
                findings=[
                    {
                        "id": "F1",
                        "category": "logic",
                        "severity": "CRITICAL",
                        "locations": ["a.py:1"],
                        "summary": "critical issue",
                        "recommendation": "fix it",
                    }
                ],
            ),
        )
        _write_json(
            secondary_path,
            _review_payload(
                conclusion="PASS - review clean",
                coverage=100,
                findings=[],
            ),
        )

        workflow = synthesize_workflow_multi_provider_results(
            str(workflow_repo),
            "review",
            str(primary_path),
            str(secondary_path),
            primary_provider="fixture-primary",
            secondary_provider="fixture-secondary",
        )
        print(f"workflow_selected_provider={workflow['selected_provider']}")
        print(f"workflow_final_passed={workflow['final_passed']}")
        print("workflow_synthesis_reasons=" + ",".join(workflow["synthesis_reasons"]))
        print(f"workflow_selection_summary={workflow.get('selection_summary')}")
        if workflow["selected_provider"] != "fixture-secondary" or not workflow["final_passed"]:
            return 1
        record_workflow_synthesis(
            str(workflow_repo),
            "review",
            selected_provider=str(workflow["selected_provider"]),
            selected_result_path=str(workflow["selected_result_path"]),
            judge_passed=bool(workflow["judge_passed"]),
            judge_reasons=list(workflow["judge_reasons"]),
            synthesis_passed=bool(workflow["final_passed"]),
            synthesis_reasons=list(workflow["synthesis_reasons"]),
            selection_summary=str(workflow.get("selection_summary", "") or ""),
            secondary_provider="fixture-secondary",
        )
        workflow_state = load_workflow_state(str(workflow_repo))
        review_synthesis = workflow_state["phases"]["review"].get("synthesis", {})
        print(f"workflow_state_selected_provider={review_synthesis.get('selectedProvider')}")
        print(f"workflow_state_selection_summary={review_synthesis.get('selectionSummary')}")
        if review_synthesis.get("selectedProvider") != "fixture-secondary":
            return 1

        higher_coverage_primary_path = tmp_dir / "primary-review-pass.json"
        higher_coverage_secondary_path = tmp_dir / "secondary-review-pass.json"
        _write_json(
            higher_coverage_primary_path,
            _review_payload(
                conclusion="PASS - review okay",
                coverage=80,
                findings=[],
            ),
        )
        _write_json(
            higher_coverage_secondary_path,
            _review_payload(
                conclusion="PASS - review stronger",
                coverage=100,
                findings=[],
            ),
        )
        workflow_higher_coverage = synthesize_workflow_multi_provider_results(
            str(workflow_repo),
            "review",
            str(higher_coverage_primary_path),
            str(higher_coverage_secondary_path),
            primary_provider="fixture-primary",
            secondary_provider="fixture-secondary",
        )
        print(f"workflow_higher_coverage_selected_provider={workflow_higher_coverage['selected_provider']}")
        print("workflow_higher_coverage_reasons=" + ",".join(workflow_higher_coverage["synthesis_reasons"]))
        print(f"workflow_higher_coverage_summary={workflow_higher_coverage.get('selection_summary')}")
        if workflow_higher_coverage["selected_provider"] != "fixture-secondary":
            return 1

        verify_primary_path = tmp_dir / "primary-verify-pass.json"
        verify_secondary_path = tmp_dir / "secondary-verify-pass.json"
        _write_json(
            verify_primary_path,
            _verify_payload(
                conclusion="PASS - verify okay",
                compliance_percentage=90,
            ),
        )
        _write_json(
            verify_secondary_path,
            _verify_payload(
                conclusion="PASS - verify stronger",
                compliance_percentage=100,
            ),
        )
        workflow_higher_compliance = synthesize_workflow_multi_provider_results(
            str(workflow_repo),
            "verify",
            str(verify_primary_path),
            str(verify_secondary_path),
            primary_provider="fixture-primary",
            secondary_provider="fixture-secondary",
        )
        print(f"workflow_higher_compliance_selected_provider={workflow_higher_compliance['selected_provider']}")
        print("workflow_higher_compliance_reasons=" + ",".join(workflow_higher_compliance["synthesis_reasons"]))
        print(f"workflow_higher_compliance_summary={workflow_higher_compliance.get('selection_summary')}")
        if workflow_higher_compliance["selected_provider"] != "fixture-secondary":
            return 1

        primary_output = "\n".join(
            [
                "===FILE: api-spec.json===",
                '{"endpoints":[]}',
            ]
        )
        secondary_output = "\n".join(
            [
                "===FILE: api-spec.json===",
                '{"endpoints":[]}',
                "===FILE: data-model.md===",
                "# Data Model",
                "===FILE: domain-overview.md===",
                "# Domain Overview",
                "===FILE: external-integration.md===",
                "# External Integration",
            ]
        )
        cross = synthesize_cross_stage2(
            primary_output,
            secondary_output,
            primary_provider="fixture-primary",
            secondary_provider="fixture-secondary",
        )
        print(f"cross_selected_provider={cross['selected_provider']}")
        print(f"cross_final_passed={cross['final_passed']}")
        print("cross_synthesis_reasons=" + ",".join(cross["synthesis_reasons"]))
        print(f"cross_selection_summary={cross.get('selection_summary')}")
        if cross["selected_provider"] != "fixture-secondary" or not cross["final_passed"]:
            return 1

        primary_output_with_extra = "\n".join(
            [
                "===FILE: api-spec.json===",
                '{"endpoints":[]}',
                "===FILE: data-model.md===",
                "# Data Model",
                "===FILE: domain-overview.md===",
                "# Domain Overview",
                "===FILE: external-integration.md===",
                "# External Integration",
                "===FILE: extra-notes.md===",
                "# Extra Notes",
            ]
        )
        cleaner_cross = synthesize_cross_stage2(
            primary_output_with_extra,
            secondary_output,
            primary_provider="fixture-primary",
            secondary_provider="fixture-secondary",
        )
        print(f"cross_cleaner_selected_provider={cleaner_cross['selected_provider']}")
        print("cross_cleaner_reasons=" + ",".join(cleaner_cross["synthesis_reasons"]))
        print(f"cross_cleaner_summary={cleaner_cross.get('selection_summary')}")
        if cleaner_cross["selected_provider"] != "fixture-secondary" or not cleaner_cross["final_passed"]:
            return 1

        docs_root = tmp_dir / "docs"
        templates_dir = docs_root / "_templates"
        templates_dir.mkdir(parents=True, exist_ok=True)
        (templates_dir / "analysis-pipeline.json").write_text("{}\n", encoding="utf-8")
        (templates_dir / "analysis-config.json").write_text(
            json.dumps(
                {
                    "service_map": {"svc": str(ROOT)},
                    "domain_definitions": {
                        "health": {
                            "directories": {"svc": []},
                            "related_domains": [],
                            "existing_docs": [],
                        }
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        context = resolve_analysis_context(
            service="svc",
            domain="health",
            deep=False,
            repo_root=str(ROOT),
            docs_root=str(docs_root),
            github_root=str(tmp_dir),
        )
        analysis_state_path = context.ai_context_dir / ".analysis-state.json"
        analysis_state_backup = analysis_state_path.read_text(encoding="utf-8") if analysis_state_path.exists() else None
        try:
            (context.ai_context_dir / ".tmp").mkdir(parents=True, exist_ok=True)
            record_cross_synthesis(
                context,
                selected_provider=str(cross["selected_provider"]),
                secondary_provider="fixture-secondary",
                judge_passed=bool(cross["judge_passed"]),
                judge_reasons=list(cross["judge_reasons"]),
                synthesis_passed=bool(cross["final_passed"]),
                synthesis_reasons=list(cross["synthesis_reasons"]),
                selection_summary=str(cross.get("selection_summary", "") or ""),
            )
            analysis_state = load_analysis_state(context)
            cross_state = analysis_state["layers"]["analyze"]["stage2"].get("crossSynthesis", {})
            print(f"analysis_state_selected_provider={cross_state.get('selectedProvider')}")
            print(f"analysis_state_selection_summary={cross_state.get('selectionSummary')}")
            if cross_state.get("selectedProvider") != "fixture-secondary":
                return 1
        finally:
            if analysis_state_backup is None:
                analysis_state_path.unlink(missing_ok=True)
            else:
                analysis_state_path.write_text(analysis_state_backup, encoding="utf-8")

        return 0


if __name__ == "__main__":
    raise SystemExit(main())
