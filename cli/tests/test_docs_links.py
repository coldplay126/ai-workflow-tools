from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from functools import lru_cache
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MACHINE_SPECIFIC_EXAMPLE_ROOT = "/Users/" + "example"
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
JSON_FENCE_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


@lru_cache(maxsize=1)
def _markdown_files() -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(REPO_ROOT / line for line in result.stdout.splitlines())


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _link_target(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        return value[1 : value.index(">")]
    return value.split()[0] if value else ""


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key: {key}")
        seen.add(key)
        result[key] = value
    return result


def _strip_json_line_comments(block: str) -> str:
    return "\n".join(
        line for line in block.splitlines() if not line.lstrip().startswith("//")
    )


def test_docs_do_not_use_machine_specific_example_paths() -> None:
    offenders: list[str] = []
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        if MACHINE_SPECIFIC_EXAMPLE_ROOT in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))

    assert offenders == []


def test_docs_markdown_links_resolve_inside_repo() -> None:
    missing: list[str] = []
    for path in _markdown_files():
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


def test_markdown_json_fences_are_parseable_when_not_placeholders() -> None:
    invalid: list[str] = []
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for match in JSON_FENCE_RE.finditer(text):
            raw_block = match.group(1)
            if "..." in raw_block or "<" in raw_block:
                continue

            line = _line_number(text, match.start())
            block = _strip_json_line_comments(raw_block).strip()
            if not block:
                continue
            try:
                json.loads(block, object_pairs_hook=_reject_duplicate_json_keys)
            except (json.JSONDecodeError, ValueError) as exc:
                invalid.append(f"{path.relative_to(REPO_ROOT)}:{line} {exc}")

    assert invalid == []
