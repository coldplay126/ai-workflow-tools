from __future__ import annotations

import re
import urllib.parse
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_ROOT = REPO_ROOT / "docs"
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _link_target(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        return value[1 : value.index(">")]
    return value.split()[0] if value else ""


def test_docs_do_not_use_machine_specific_example_paths() -> None:
    offenders: list[str] = []
    for path in sorted(DOCS_ROOT.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        if "/Users/example" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))

    assert offenders == []


def test_docs_markdown_links_resolve_inside_repo() -> None:
    missing: list[str] = []
    for path in sorted(DOCS_ROOT.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK_RE.finditer(text):
            raw_target = _link_target(match.group(1))
            target = raw_target.split("#", 1)[0]
            if not target or URL_SCHEME_RE.match(target):
                continue

            line = _line_number(text, match.start())
            if target.startswith("/"):
                missing.append(f"{path.relative_to(REPO_ROOT)}:{line} {raw_target}")
                continue

            resolved = (path.parent / urllib.parse.unquote(target)).resolve()
            try:
                resolved.relative_to(REPO_ROOT)
            except ValueError:
                missing.append(f"{path.relative_to(REPO_ROOT)}:{line} {raw_target}")
                continue
            if not resolved.exists():
                missing.append(f"{path.relative_to(REPO_ROOT)}:{line} {raw_target}")

    assert missing == []
