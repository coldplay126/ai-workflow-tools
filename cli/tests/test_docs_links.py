from __future__ import annotations

import contextlib
import io
import json
import re
import shlex
import subprocess
import textwrap
import urllib.parse
from functools import lru_cache
from pathlib import Path

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from awf.cli import KNOWN_COMMANDS, build_parser


REPO_ROOT = Path(__file__).resolve().parents[2]
MACHINE_SPECIFIC_EXAMPLE_ROOT = "/Users/" + "example"
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
BASH_FENCE_RE = re.compile(
    r"^[ \t]*```bash\s*\n(.*?)\n[ \t]*```", re.DOTALL | re.MULTILINE
)
JSON_FENCE_RE = re.compile(
    r"^[ \t]*```json\s*\n(.*?)\n[ \t]*```", re.DOTALL | re.MULTILINE
)
TOML_FENCE_RE = re.compile(
    r"^[ \t]*```toml\s*\n(.*?)\n[ \t]*```", re.DOTALL | re.MULTILINE
)
PYTHON_FENCE_RE = re.compile(
    r"^[ \t]*```python\s*\n(.*?)\n[ \t]*```", re.DOTALL | re.MULTILINE
)
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


def _bash_logical_lines(block: str, start_line: int) -> list[tuple[int, str]]:
    logical_lines: list[tuple[int, str]] = []
    buffer = ""
    buffer_line = start_line
    for index, raw_line in enumerate(block.splitlines(), start_line):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if not buffer:
            buffer_line = index
        if stripped.endswith("\\"):
            buffer += stripped[:-1].rstrip() + " "
            continue

        buffer += stripped
        logical_lines.append((buffer_line, buffer))
        buffer = ""

    if buffer:
        logical_lines.append((buffer_line, buffer.rstrip()))
    return logical_lines


def _awf_argv_from_bash_line(line: str) -> list[str] | None:
    sanitized = re.sub(r"<([A-Za-z0-9_.-]+)>", r"\1", line)
    try:
        tokens = shlex.split(sanitized, comments=True)
    except ValueError:
        return None
    if "awf" not in tokens:
        return None

    awf_index = tokens.index("awf")
    argv: list[str] = []
    for token in tokens[awf_index + 1 :]:
        if token in {"|", "||", "&&", ";"}:
            break
        argv.append(token)
    if any(
        re.search(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{|\()", token)
        for token in argv
    ):
        return None
    return argv


def _is_natural_language_awf_argv(argv: list[str]) -> bool:
    return bool(argv) and argv[0] not in KNOWN_COMMANDS and not argv[0].startswith("-")


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


def test_markdown_awf_bash_examples_parse() -> None:
    parser = build_parser()
    invalid: list[str] = []
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for match in BASH_FENCE_RE.finditer(text):
            start_line = _line_number(text, match.start()) + 1
            for line, command in _bash_logical_lines(match.group(1), start_line):
                argv = _awf_argv_from_bash_line(command)
                if argv is None or _is_natural_language_awf_argv(argv):
                    continue

                stderr = io.StringIO()
                stdout = io.StringIO()
                try:
                    with (
                        contextlib.redirect_stdout(stdout),
                        contextlib.redirect_stderr(stderr),
                    ):
                        parser.parse_args(argv)
                except SystemExit as exc:
                    if exc.code == 0:
                        continue
                    invalid.append(
                        f"{path.relative_to(REPO_ROOT)}:{line} "
                        f"awf {' '.join(argv)} (exit={exc.code})"
                    )

    assert invalid == []


def test_markdown_awf_bash_examples_do_not_use_stale_no_editable() -> None:
    stale: list[str] = []
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for match in BASH_FENCE_RE.finditer(text):
            start_line = _line_number(text, match.start()) + 1
            for line, command in _bash_logical_lines(match.group(1), start_line):
                if (
                    "uv run --project cli --no-editable" in command
                    and " awf" in command
                    and "--reinstall-package awf-cli" not in command
                ):
                    stale.append(f"{path.relative_to(REPO_ROOT)}:{line} {command}")

    assert stale == []


def test_markdown_awf_analyze_examples_do_not_use_removed_deep_flag() -> None:
    stale: list[str] = []
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for line, raw_line in enumerate(text.splitlines(), 1):
            if "awf analyze" in raw_line and "--deep" in raw_line:
                stale.append(f"{path.relative_to(REPO_ROOT)}:{line} {raw_line.strip()}")

    assert stale == []


def test_markdown_toml_fences_are_parseable_when_not_placeholders() -> None:
    invalid: list[str] = []
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for match in TOML_FENCE_RE.finditer(text):
            block = match.group(1)
            if "..." in block or "<" in block:
                continue

            line = _line_number(text, match.start())
            try:
                tomllib.loads(block)
            except tomllib.TOMLDecodeError as exc:
                invalid.append(f"{path.relative_to(REPO_ROOT)}:{line} {exc}")

    assert invalid == []


def test_markdown_python_fences_are_parseable_when_not_placeholders() -> None:
    invalid: list[str] = []
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for match in PYTHON_FENCE_RE.finditer(text):
            block = match.group(1)
            if "..." in block or "<" in block:
                continue

            line = _line_number(text, match.start())
            try:
                compile(textwrap.dedent(block), str(path), "exec")
            except SyntaxError as exc:
                invalid.append(f"{path.relative_to(REPO_ROOT)}:{line} {exc.msg}")

    assert invalid == []
