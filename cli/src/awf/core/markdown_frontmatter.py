from __future__ import annotations

import re
from pathlib import Path
from typing import Any


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def strip_markdown_frontmatter(text: str) -> str:
    """Return markdown body with a leading YAML frontmatter block removed."""
    match = FRONTMATTER_RE.match(text)
    if not match:
        return text
    raw_frontmatter = match.group(1)
    if not any(":" in line for line in raw_frontmatter.splitlines()):
        return text
    return match.group(2)


def read_markdown_body(path: str | Path) -> str:
    """Read markdown and remove a leading frontmatter block if present."""
    return strip_markdown_frontmatter(
        Path(path).read_text(encoding="utf-8", errors="ignore")
    )


def render_markdown_frontmatter(frontmatter: dict[str, Any], body: str) -> str:
    """Render a constrained frontmatter block and markdown body."""
    clean_body = strip_markdown_frontmatter(body).lstrip("\n")
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {_format_value(value)}")
    lines.append("---")
    lines.append(clean_body)
    rendered = "\n".join(lines)
    return rendered if rendered.endswith("\n") else rendered + "\n"


def _format_value(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_format_scalar(item) for item in value) + "]"
    return _format_scalar(value)


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    text = str(value)
    if not text:
        return '""'
    if _needs_quotes(text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _needs_quotes(text: str) -> bool:
    return (
        text[0] in "-?:,[]{}#&*!|>'\"%@`"
        or ": " in text
        or " #" in text
        or "\n" in text
    )
