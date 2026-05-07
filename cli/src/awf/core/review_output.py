"""Auto-save multi-agent review results to docs/working/codex-reviews/.

Converts AgentResult JSON findings into readable markdown and saves
with date-prefixed filename. Called after run_multi_agent() completes.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any


def save_review_output(
    result,  # MultiAgentResult
    *,
    topic: str,
    cwd: str,
) -> str | None:
    """Save multi-agent result to docs/working/codex-reviews/{date}-{topic}.md.

    Returns the saved file path, or None if nothing to save.
    """
    if not result.agents:
        return None

    try:
        from awf.core.paths import find_repo_root
        repo_root = find_repo_root(None)
    except FileNotFoundError:
        repo_root = Path(cwd)

    reviews_dir = repo_root / "docs" / "working" / "codex-reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)

    today = date.today().strftime("%Y-%m-%d")
    slug = _slugify(topic)
    filename = f"{today}-{slug}.md"
    filepath = reviews_dir / filename

    # Avoid overwriting — append suffix if exists
    if filepath.exists():
        for i in range(2, 10):
            candidate = reviews_dir / f"{today}-{slug}-{i}.md"
            if not candidate.exists():
                filepath = candidate
                break

    content = _render_markdown(result, topic)
    filepath.write_text(content, encoding="utf-8")
    print(f"review saved: {filepath.relative_to(repo_root)}", file=sys.stderr)
    return str(filepath)


def _slugify(text: str) -> str:
    """Convert topic to filesystem-safe slug."""
    import re
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug[:60] or "review"


def _render_markdown(result, topic: str) -> str:
    """Render MultiAgentResult as markdown."""
    lines: list[str] = []
    lines.append(f"# Review: {topic}")
    lines.append("")
    lines.append(f"- **Mode**: {result.mode}")
    lines.append(f"- **Verdict**: {result.judge_verdict}")
    lines.append(f"- **Reason**: {result.judge_reason}")
    lines.append(f"- **Date**: {date.today().isoformat()}")
    lines.append("")

    for agent in result.agents:
        lines.append(f"## {agent.provider_name} / {agent.role}")
        lines.append("")
        lines.append(f"- Elapsed: {agent.elapsed_sec:.1f}s")
        lines.append(f"- Status: {'OK' if agent.ok else 'FAIL'}")
        if agent.conclusion:
            lines.append(f"- Conclusion: {agent.conclusion}")
        lines.append("")

        findings = agent.findings
        if findings:
            lines.append(f"### Findings ({len(findings)})")
            lines.append("")
            for i, f in enumerate(findings, 1):
                sev = f.get("severity", "?")
                cat = f.get("category", "?")
                loc = f.get("location", "?")
                desc = f.get("description", "")
                sug = f.get("suggestion", "")
                lines.append(f"**{i}. [{sev}] {cat}** — `{loc}`")
                lines.append(f"  {desc}")
                if sug:
                    lines.append(f"  > {sug}")
                lines.append("")
        else:
            lines.append("No findings.")
            lines.append("")

    # Raw JSON appendix for traceability
    lines.append("---")
    lines.append("")
    lines.append("<details><summary>Raw agent output</summary>")
    lines.append("")
    for agent in result.agents:
        lines.append(f"### {agent.provider_name}/{agent.role}")
        lines.append("```")
        # Truncate very long output
        stdout = agent.stdout
        if len(stdout) > 5000:
            stdout = stdout[:5000] + "\n... (truncated)"
        lines.append(stdout)
        lines.append("```")
        lines.append("")
    lines.append("</details>")

    return "\n".join(lines) + "\n"
