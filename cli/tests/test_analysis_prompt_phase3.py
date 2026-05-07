from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.cli import build_parser
from awf.core.analysis_prompt import DEFAULT_REFERENCE_POLICY, _load_reference_policy, build_project_bundle, build_prompt
from awf.core.config import AnalysisContext


def _make_context(
    tmp_path: Path,
    *,
    related_domains: list[str],
    mode: str = "deep",
    pipeline_config: dict | None = None,
) -> AnalysisContext:
    docs_root = tmp_path / "docs"
    github_root = tmp_path / "github"
    repo_root = tmp_path / "repo"
    service = "sample-api"
    domain = "notification"

    docs_root.mkdir(parents=True, exist_ok=True)
    github_root.mkdir(parents=True, exist_ok=True)
    repo_root.mkdir(parents=True, exist_ok=True)
    templates_dir = docs_root / "_templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    (templates_dir / "analysis-pipeline.json").write_text(
        json.dumps(pipeline_config or {}, ensure_ascii=False),
        encoding="utf-8",
    )

    return AnalysisContext(
        repo_root=repo_root,
        docs_root=docs_root,
        github_root=github_root,
        analysis_config_path=templates_dir / "analysis-config.json",
        analysis_pipeline_path=templates_dir / "analysis-pipeline.json",
        service=service,
        domain=domain,
        mode=mode,
        ai_context_dir=docs_root / service / domain / ".ai-context",
        domain_directories=[],
        all_directories={service: []},
        related_domains=related_domains,
        existing_docs=[],
    )


def _capture_cli(argv: list[str]) -> tuple[int, str]:
    parser = build_parser()
    args = parser.parse_args(argv)
    stderr = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = stderr
    try:
        rc = args.handler(args)
    finally:
        sys.stderr = old_stderr
    return rc, stderr.getvalue()


def test_build_project_bundle_uses_related_domain_refs_and_reason_log(tmp_path: Path) -> None:
    context = _make_context(tmp_path, related_domains=["common"])
    related_dir = context.docs_root / context.service / "common" / ".ai-context"
    related_dir.mkdir(parents=True, exist_ok=True)
    (related_dir / "domain-overview.md").write_text("# Common flow\nshared flow\n", encoding="utf-8")
    (context.docs_root / context.service / "project-context.md").write_text(
        "# Project Context\nshared knowledge\n",
        encoding="utf-8",
    )

    bundle, log = build_project_bundle(context)

    assert bundle is not None
    assert "common/.ai-context/domain-overview.md" in bundle
    assert "project-context.md" in bundle
    assert log["total_reference_tokens"] > 0
    assert [item["level"] for item in log["references_used"]] == [2, 3]
    assert "related_domains `common`" in str(log["references_used"][0]["reason"])


def test_build_project_bundle_drops_lower_priority_docs_first(tmp_path: Path) -> None:
    context = _make_context(
        tmp_path,
        related_domains=["common"],
        pipeline_config={"reference_policy": {"max_documents": 1}},
    )
    related_dir = context.docs_root / context.service / "common" / ".ai-context"
    related_dir.mkdir(parents=True, exist_ok=True)
    (related_dir / "domain-overview.md").write_text("# Common flow\nshared flow\n", encoding="utf-8")
    (context.docs_root / context.service / "project-context.md").write_text(
        "# Project Context\nshared knowledge\n",
        encoding="utf-8",
    )

    bundle, log = build_project_bundle(context)

    assert bundle is not None
    assert len(log["references_used"]) == 1
    assert log["references_used"][0]["level"] == 2
    assert len(log["references_dropped"]) == 1
    assert log["references_dropped"][0]["level"] == 3


def test_build_project_bundle_keeps_v2_behavior_when_related_domains_empty(tmp_path: Path) -> None:
    context = _make_context(tmp_path, related_domains=[], mode="deep")
    (context.docs_root / context.service).mkdir(parents=True, exist_ok=True)
    (context.docs_root / context.service / "project-context.md").write_text(
        "# Project Context\nshould not trigger expansion alone\n",
        encoding="utf-8",
    )

    bundle, log = build_project_bundle(context)

    assert bundle is None
    assert log["references_used"] == []
    assert log["references_dropped"] == []
    assert log["total_reference_tokens"] == 0


def test_build_prompt_skips_legacy_project_context_when_phase3_active(tmp_path: Path) -> None:
    context = _make_context(tmp_path, related_domains=["common"], mode="deep")
    context.ai_context_dir.mkdir(parents=True, exist_ok=True)
    (context.docs_root / context.service).mkdir(parents=True, exist_ok=True)
    (context.docs_root / context.service / "project-context.md").write_text(
        "# Project Context\nphase3 should own this reference\n",
        encoding="utf-8",
    )
    (context.ai_context_dir / "domain-overview.md").write_text(
        "# Existing Analysis\nkeep this section\n",
        encoding="utf-8",
    )

    prompt = build_prompt(context)

    assert "## 기존 .ai-context (이전 분석 결과)" in prompt
    assert "keep this section" in prompt
    assert "## 서비스 도메인 지식 (project-context)" not in prompt
    assert "phase3 should own this reference" not in prompt


def test_analyze_logs_dropped_references_even_without_project_bundle(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    docs_root = tmp_path / "docs"
    github_root = tmp_path / "github"
    service = "sample-api"
    domain = "notification"

    templates_dir = docs_root / "_templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    (templates_dir / "analysis-config.json").write_text(
        json.dumps(
            {
                "service_map": {service: str(github_root / service)},
                "domain_definitions": {
                    domain: {
                        "directories": {service: []},
                        "related_domains": ["common"],
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
    (templates_dir / "analysis-pipeline.json").write_text(
        json.dumps({"reference_policy": {"max_tokens": 0}}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    (github_root / service).mkdir(parents=True, exist_ok=True)
    related_dir = docs_root / service / "common" / ".ai-context"
    related_dir.mkdir(parents=True, exist_ok=True)
    (related_dir / "domain-overview.md").write_text("# Common flow\nshared flow\n", encoding="utf-8")

    rc, stderr = _capture_cli(
        [
            "analyze",
            service,
            domain,
            "--repo-root",
            str(repo_root),
            "--docs-root",
            str(docs_root),
            "--github-root",
            str(github_root),
            "--provider",
            "fixture",
            "--yolo",
        ]
    )

    assert rc == 1
    assert "reference_dropped: level=2" in stderr
    assert "common/.ai-context/domain-overview.md" in stderr
    assert "reference_tokens_total: 0" in stderr


def test_load_reference_policy_falls_back_on_invalid_values(tmp_path: Path) -> None:
    context = _make_context(
        tmp_path,
        related_domains=["common"],
        pipeline_config={
            "reference_policy": {
                "max_documents": "invalid",
                "max_tokens": {"oops": 1},
                "require_reason": "YES",
            }
        },
    )

    policy = _load_reference_policy(context)

    assert policy["max_documents"] == DEFAULT_REFERENCE_POLICY["max_documents"]
    assert policy["max_tokens"] == DEFAULT_REFERENCE_POLICY["max_tokens"]
    assert policy["require_reason"] is True


def test_analyze_parser_rejects_deep_flag() -> None:
    parser = build_parser()
    try:
        parser.parse_args(["analyze", "svc", "domain", "--deep"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("--deep should be rejected by the analyze parser")
