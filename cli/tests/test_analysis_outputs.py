from __future__ import annotations

import json
from pathlib import Path

from awf.core.analysis_outputs import (
    generate_analysis_report,
    write_stage2_outputs,
)
from awf.core.analysis_prompt import _read_existing_docs
from awf.core.knowledge import _extract_unit_summary
from awf.core.markdown_frontmatter import strip_markdown_frontmatter


class _Context:
    def __init__(self, tmp_path: Path):
        self.repo_root = tmp_path
        self.docs_root = tmp_path / "docs"
        self.github_root = tmp_path / "github"
        self.service = "sample-api"
        self.domain = "quest-challenge"
        self.mode = "standard"
        self.analysis_mode = "document"
        self.ai_context_dir = self.docs_root / self.service / self.domain / ".ai-context"
        self.domain_directories = ["src/quest"]
        self.existing_docs: list[str] = []
        self.related_domains: list[str] = []
        self.analysis_pipeline_path = self.docs_root / "_templates" / "analysis-pipeline.json"
        self.ai_context_dir.mkdir(parents=True, exist_ok=True)


def test_write_stage2_outputs_stamps_markdown_only(tmp_path: Path):
    context = _Context(tmp_path)
    raw = """===FILE: api-spec.json===
{"endpoints": []}

===FILE: data-model.md===
# Data Model

Tables.

===FILE: domain-overview.md===
# Domain Overview

Overview.

===FILE: external-integration.md===
# External Integration

None.
"""

    written = write_stage2_outputs(context, raw)

    assert {path.name for path in written} == {
        "api-spec.json",
        "data-model.md",
        "domain-overview.md",
        "external-integration.md",
    }
    assert (context.ai_context_dir / "api-spec.json").read_text(encoding="utf-8").startswith("{")

    overview = (context.ai_context_dir / "domain-overview.md").read_text(encoding="utf-8")
    assert overview.startswith("---\n")
    assert "title: Domain overview\n" in overview
    assert "schema: ai_context_markdown_v1\n" in overview
    assert "service: sample-api\n" in overview
    assert "domain: quest-challenge\n" in overview
    assert "source_state: .analysis-state.json\n" in overview
    assert strip_markdown_frontmatter(overview).startswith("# Domain Overview")


def test_strip_markdown_frontmatter_keeps_plain_horizontal_rule():
    text = "---\nnot metadata\n---\n# Body\n"

    assert strip_markdown_frontmatter(text) == text


def test_generate_analysis_report_strips_source_frontmatter(tmp_path: Path):
    context = _Context(tmp_path)
    state = {
        "scale": "small",
        "layers": {
            "bundle": {"fileCount": 2},
            "analyze": {
                "stage1": {"provider": "codex"},
                "stage2": {"provider": "fixture"},
            },
        },
    }
    (context.ai_context_dir / "api-spec.json").write_text(
        json.dumps({"endpoints": [{"path": "/quest"}]}),
        encoding="utf-8",
    )
    (context.ai_context_dir / "domain-overview.md").write_text(
        "---\ntitle: Domain overview\nschema: ai_context_markdown_v1\n---\n"
        "# Domain Overview\n\n## 알려진 이슈\n\n- HIGH issue\n",
        encoding="utf-8",
    )
    (context.ai_context_dir / "external-integration.md").write_text(
        "---\ntitle: External integration\nschema: ai_context_markdown_v1\n---\n"
        "# External Integration\n\n## 환경변수\n\n- API_KEY\n",
        encoding="utf-8",
    )

    report_path = generate_analysis_report(context, state)
    assert report_path is not None
    report = report_path.read_text(encoding="utf-8")
    body = strip_markdown_frontmatter(report)

    assert report.startswith("---\ntitle: Analysis report\n")
    assert "schema: ai_context_markdown_v1\n" in report
    assert "- HIGH issue" in body
    assert "- API_KEY" in body
    assert "title: Domain overview" not in body
    assert "title: External integration" not in body


def test_existing_ai_context_prompt_strips_markdown_frontmatter(tmp_path: Path):
    context = _Context(tmp_path)
    context.existing_docs = ["guide.md"]
    guide = context.docs_root / "guide.md"
    guide.parent.mkdir(parents=True, exist_ok=True)
    guide.write_text(
        "---\ntitle: Guide\nschema: docs_v1\n---\n# Guide\n\nBody.\n",
        encoding="utf-8",
    )
    (context.ai_context_dir / "domain-overview.md").write_text(
        "---\ntitle: Domain overview\nschema: ai_context_markdown_v1\n---\n"
        "# Domain Overview\n\nCurrent body.\n",
        encoding="utf-8",
    )

    rendered = _read_existing_docs(context)

    assert "### guide.md\n# Guide" in rendered
    assert "### generated:domain-overview.md\n# Domain Overview" in rendered
    assert "schema: docs_v1" not in rendered
    assert "schema: ai_context_markdown_v1" not in rendered


def test_knowledge_summary_strips_domain_overview_frontmatter(tmp_path: Path):
    context = _Context(tmp_path)
    (context.ai_context_dir / "domain-overview.md").write_text(
        "---\ntitle: Domain overview\nschema: ai_context_markdown_v1\n---\n"
        "# Domain Overview\n\n## 목적\n\nQuest challenge unit summary.\n",
        encoding="utf-8",
    )
    (context.ai_context_dir / "api-spec.json").write_text(
        json.dumps({"endpoints": [{"path": "/quest"}]}),
        encoding="utf-8",
    )

    summary = _extract_unit_summary(context)

    assert summary is not None
    assert summary["summary"] == "Quest challenge unit summary."
    assert summary["endpoint_count"] == 1
