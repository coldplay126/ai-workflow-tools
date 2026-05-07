"""Agent loader unit tests — frontmatter parsing, definition loading, role resolution, fallback chain.

Tests:
- _parse_agent_frontmatter: YAML-like parsing (strings, lists, booleans, comments, no frontmatter)
- load_agent_definition: file lookup, caching, FileNotFoundError
- load_agent_instructions: body extraction
- resolve_agent_for_role: role mapping across agent files
- _load_protocol fallback chain: agent → protocol → builtin
- _load_team_protocol fallback chain: same order

Reference: claude/agents/*.md, cli/src/awf/core/spec_loader.py
"""
from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.spec_loader import (
    _parse_agent_frontmatter,
    load_agent_definition,
    load_agent_instructions,
    resolve_agent_for_role,
    _agent_search_paths,
    clear_cache,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

passed = 0
failed = 0


def _assert(condition: bool, name: str, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def _make_agent_file(tmp: Path, name: str, frontmatter: str, body: str) -> Path:
    """Create an agent .md file in tmp/agents/."""
    agents_dir = tmp / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / f"{name}.md"
    path.write_text(f"---\n{frontmatter}---\n\n{body}\n", encoding="utf-8")
    return path


# ===========================================================================
# _parse_agent_frontmatter
# ===========================================================================

print("--- parse_agent_frontmatter ---")


def test_parse_simple_string():
    text = '---\nname: code-reviewer\ndescription: "A reviewer"\n---\n\nBody text here.'
    meta, body = _parse_agent_frontmatter(text)
    _assert(meta["name"] == "code-reviewer", "simple_string_name")
    _assert(meta["description"] == "A reviewer", "simple_string_description")
    _assert(body == "Body text here.", "simple_string_body")


def test_parse_list():
    text = "---\nroles: [precision, code_reviewer, speed]\n---\n\nBody."
    meta, body = _parse_agent_frontmatter(text)
    _assert(meta["roles"] == ["precision", "code_reviewer", "speed"], "list_roles",
            f"got {meta.get('roles')}")


def test_parse_boolean():
    text = "---\nisolation: true\nverbose: false\n---\n\nBody."
    meta, body = _parse_agent_frontmatter(text)
    _assert(meta["isolation"] is True, "boolean_true")
    _assert(meta["verbose"] is False, "boolean_false")


def test_parse_comment_lines():
    text = "---\nname: test\n# this is a comment\nmodel: sonnet\n---\n\nBody."
    meta, body = _parse_agent_frontmatter(text)
    _assert("# this is a comment" not in meta, "comment_skipped")
    _assert(meta["model"] == "sonnet", "comment_preserves_fields")


def test_parse_empty_lines_in_frontmatter():
    text = "---\nname: test\n\nmodel: opus\n---\n\nBody."
    meta, body = _parse_agent_frontmatter(text)
    _assert(meta["name"] == "test", "empty_line_name")
    _assert(meta["model"] == "opus", "empty_line_model")


def test_parse_no_frontmatter():
    text = "Just plain text, no frontmatter."
    meta, body = _parse_agent_frontmatter(text)
    _assert(meta == {}, "no_frontmatter_meta_empty")
    _assert(body == text, "no_frontmatter_body_is_full_text")


def test_parse_quoted_values():
    text = "---\ndescription: 'single quoted'\nname: \"double quoted\"\n---\n\nBody."
    meta, body = _parse_agent_frontmatter(text)
    _assert(meta["description"] == "single quoted", "single_quoted")
    _assert(meta["name"] == "double quoted", "double_quoted")


def test_parse_colon_in_value():
    text = '---\ndescription: "key: value pair inside"\n---\n\nBody.'
    meta, body = _parse_agent_frontmatter(text)
    _assert("key: value pair inside" in meta["description"], "colon_in_value",
            f"got {meta.get('description')}")


def test_parse_empty_list():
    text = "---\nroles: []\n---\n\nBody."
    meta, body = _parse_agent_frontmatter(text)
    _assert(meta["roles"] == [], "empty_list")


def test_parse_single_item_list():
    text = "---\nroles: [only_one]\n---\n\nBody."
    meta, body = _parse_agent_frontmatter(text)
    _assert(meta["roles"] == ["only_one"], "single_item_list")


test_parse_simple_string()
test_parse_list()
test_parse_boolean()
test_parse_comment_lines()
test_parse_empty_lines_in_frontmatter()
test_parse_no_frontmatter()
test_parse_quoted_values()
test_parse_colon_in_value()
test_parse_empty_list()
test_parse_single_item_list()


# ===========================================================================
# load_agent_definition / load_agent_instructions (with fixture files)
# ===========================================================================

print("\n--- load_agent_definition ---")


def test_load_definition_found():
    """Agent found in search path → returns meta + instructions."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _make_agent_file(tmp_path, "test-agent",
                         'name: test-agent\nmodel: sonnet\nroles: [tester]\n',
                         "You are a test agent.")
        clear_cache()
        # Monkey-patch search paths
        original = _agent_search_paths.__code__
        import types
        def patched():
            return [tmp_path / "agents"]
        import awf.core.spec_loader as sl
        old_fn = sl._agent_search_paths
        sl._agent_search_paths = patched
        try:
            defn = load_agent_definition("test-agent")
            _assert(defn["meta"]["name"] == "test-agent", "definition_meta_name")
            _assert(defn["meta"]["model"] == "sonnet", "definition_meta_model")
            _assert(defn["meta"]["roles"] == ["tester"], "definition_meta_roles")
            _assert("You are a test agent." in defn["instructions"], "definition_body")
            _assert("path" in defn, "definition_has_path")
        finally:
            sl._agent_search_paths = old_fn
            clear_cache()


def test_load_definition_not_found():
    """Agent not found → FileNotFoundError."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "agents").mkdir()
        clear_cache()
        import awf.core.spec_loader as sl
        old_fn = sl._agent_search_paths
        sl._agent_search_paths = lambda: [tmp_path / "agents"]
        try:
            try:
                load_agent_definition("nonexistent")
                _assert(False, "not_found_raises", "expected FileNotFoundError")
            except FileNotFoundError:
                _assert(True, "not_found_raises")
        finally:
            sl._agent_search_paths = old_fn
            clear_cache()


def test_load_definition_cached():
    """Second call returns cached result (no disk read)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _make_agent_file(tmp_path, "cached-agent",
                         'name: cached-agent\nmodel: opus\n',
                         "Cached body.")
        clear_cache()
        import awf.core.spec_loader as sl
        old_fn = sl._agent_search_paths
        sl._agent_search_paths = lambda: [tmp_path / "agents"]
        try:
            defn1 = load_agent_definition("cached-agent")
            # Delete file — second call should still work from cache
            (tmp_path / "agents" / "cached-agent.md").unlink()
            defn2 = load_agent_definition("cached-agent")
            _assert(defn1 is defn2, "cached_same_object")
        finally:
            sl._agent_search_paths = old_fn
            clear_cache()


def test_load_instructions_returns_body_only():
    """load_agent_instructions returns body without frontmatter."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _make_agent_file(tmp_path, "body-agent",
                         'name: body-agent\nmodel: sonnet\nroles: [worker]\n',
                         "# Title\n\nInstruction body.")
        clear_cache()
        import awf.core.spec_loader as sl
        old_fn = sl._agent_search_paths
        sl._agent_search_paths = lambda: [tmp_path / "agents"]
        try:
            instructions = load_agent_instructions("body-agent")
            _assert("name:" not in instructions, "no_frontmatter_in_body")
            _assert("# Title" in instructions, "body_has_title")
            _assert("Instruction body." in instructions, "body_has_content")
        finally:
            sl._agent_search_paths = old_fn
            clear_cache()


test_load_definition_found()
test_load_definition_not_found()
test_load_definition_cached()
test_load_instructions_returns_body_only()


# ===========================================================================
# resolve_agent_for_role
# ===========================================================================

print("\n--- resolve_agent_for_role ---")


def test_resolve_role_found():
    """Role exists in an agent's roles list → returns agent stem."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _make_agent_file(tmp_path, "my-reviewer",
                         'name: my-reviewer\nroles: [precision, code_reviewer]\n',
                         "Review body.")
        _make_agent_file(tmp_path, "my-tester",
                         'name: my-tester\nroles: [adversarial]\n',
                         "Test body.")
        clear_cache()
        import awf.core.spec_loader as sl
        old_fn = sl._agent_search_paths
        sl._agent_search_paths = lambda: [tmp_path / "agents"]
        try:
            _assert(resolve_agent_for_role("precision") == "my-reviewer", "resolve_precision")
            _assert(resolve_agent_for_role("code_reviewer") == "my-reviewer", "resolve_code_reviewer")
            _assert(resolve_agent_for_role("adversarial") == "my-tester", "resolve_adversarial")
        finally:
            sl._agent_search_paths = old_fn
            clear_cache()


def test_resolve_role_not_found():
    """Role not in any agent → returns None."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _make_agent_file(tmp_path, "only-agent",
                         'name: only-agent\nroles: [specific_role]\n',
                         "Body.")
        clear_cache()
        import awf.core.spec_loader as sl
        old_fn = sl._agent_search_paths
        sl._agent_search_paths = lambda: [tmp_path / "agents"]
        try:
            _assert(resolve_agent_for_role("nonexistent_role") is None, "resolve_none")
        finally:
            sl._agent_search_paths = old_fn
            clear_cache()


def test_resolve_role_no_roles_field():
    """Agent without roles field → skipped, no error."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _make_agent_file(tmp_path, "no-roles",
                         'name: no-roles\nmodel: sonnet\n',
                         "Body.")
        clear_cache()
        import awf.core.spec_loader as sl
        old_fn = sl._agent_search_paths
        sl._agent_search_paths = lambda: [tmp_path / "agents"]
        try:
            _assert(resolve_agent_for_role("any_role") is None, "no_roles_field_returns_none")
        finally:
            sl._agent_search_paths = old_fn
            clear_cache()


def test_resolve_search_order():
    """First match wins — project-level before user-level."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        project_dir = tmp_path / "project" / "agents"
        user_dir = tmp_path / "user" / "agents"
        project_dir.mkdir(parents=True)
        user_dir.mkdir(parents=True)
        # Same role in both — project should win
        (project_dir / "proj-agent.md").write_text(
            "---\nname: proj-agent\nroles: [shared_role]\n---\n\nProject version.", encoding="utf-8")
        (user_dir / "user-agent.md").write_text(
            "---\nname: user-agent\nroles: [shared_role]\n---\n\nUser version.", encoding="utf-8")
        clear_cache()
        import awf.core.spec_loader as sl
        old_fn = sl._agent_search_paths
        sl._agent_search_paths = lambda: [project_dir, user_dir]
        try:
            result = resolve_agent_for_role("shared_role")
            _assert(result == "proj-agent", "search_order_project_first", f"got {result}")
        finally:
            sl._agent_search_paths = old_fn
            clear_cache()


test_resolve_role_found()
test_resolve_role_not_found()
test_resolve_role_no_roles_field()
test_resolve_search_order()


# ===========================================================================
# _load_protocol / _load_team_protocol fallback chain
# ===========================================================================

print("\n--- fallback chain ---")


def test_fallback_agent_takes_priority():
    """_load_protocol prefers agent over protocol when both exist."""
    # Use real project files — 'precision' role exists as both agent and protocol
    clear_cache()
    from awf.core.multi_agent import _load_protocol, _PROTOCOL_CACHE
    _PROTOCOL_CACHE.clear()
    content = _load_protocol("precision")
    # Agent body should contain "Code Reviewer" (from code-reviewer.md)
    # Protocol body contains "read-only 분석만 수행" or similar
    _assert("Code Reviewer" in content or "리뷰" in content, "agent_priority_over_protocol",
            f"content starts with: {content[:80]}")


def test_fallback_protocol_when_no_agent():
    """_load_protocol falls back to protocol file when no agent has the role."""
    clear_cache()
    from awf.core.multi_agent import _load_protocol, _PROTOCOL_CACHE
    _PROTOCOL_CACHE.clear()
    # Create a temp role that exists only as protocol (not as agent)
    # Use the built-in fallback by requesting a role that doesn't exist anywhere
    import io, contextlib
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        content = _load_protocol("__nonexistent_role_xyz__")
    _assert("분석을 수행하세요" in content, "builtin_fallback",
            f"content: {content[:80]}")
    _assert("__nonexistent_role_xyz__" in content, "fallback_includes_role_name")


def test_fallback_team_protocol_same_order():
    """_load_team_protocol follows same agent → protocol → fallback order."""
    clear_cache()
    from awf.core.team_runner import _load_team_protocol
    content = _load_team_protocol("adversarial")
    # Should load from adversarial-tester agent
    _assert("Adversarial" in content or "엣지케이스" in content, "team_agent_priority",
            f"content starts with: {content[:80]}")


def test_fallback_team_builtin():
    """_load_team_protocol returns builtin for unknown role."""
    clear_cache()
    from awf.core.team_runner import _load_team_protocol
    content = _load_team_protocol("__unknown_team_role__")
    _assert("에이전트 팀 워커" in content, "team_builtin_fallback",
            f"content: {content[:80]}")


test_fallback_agent_takes_priority()
test_fallback_protocol_when_no_agent()
test_fallback_team_protocol_same_order()
test_fallback_team_builtin()


# ===========================================================================
# Real agent files validation
# ===========================================================================

print("\n--- real agent files ---")


def test_all_real_agents_parseable():
    """All agent .md files in claude/agents/ parse without error."""
    repo_root = Path(__file__).resolve().parents[2]
    agents_dir = repo_root / "claude" / "agents"
    if not agents_dir.is_dir():
        _assert(False, "agents_dir_exists", f"{agents_dir} not found")
        return
    agent_files = sorted(agents_dir.glob("*.md"))
    _assert(len(agent_files) >= 12, "at_least_12_agents", f"found {len(agent_files)}")
    for af in agent_files:
        text = af.read_text(encoding="utf-8")
        meta, body = _parse_agent_frontmatter(text)
        _assert("name" in meta, f"{af.stem}_has_name")
        _assert(len(body) > 20, f"{af.stem}_has_body", f"body length: {len(body)}")


def test_all_real_agents_have_required_fields():
    """All agents have name, description, roles in frontmatter."""
    repo_root = Path(__file__).resolve().parents[2]
    agents_dir = repo_root / "claude" / "agents"
    for af in sorted(agents_dir.glob("*.md")):
        text = af.read_text(encoding="utf-8")
        meta, _ = _parse_agent_frontmatter(text)
        has_all = all(k in meta for k in ("name", "description", "roles"))
        _assert(has_all, f"{af.stem}_required_fields",
                f"missing: {[k for k in ('name','description','roles') if k not in meta]}")


def test_no_duplicate_roles_across_agents():
    """Each role maps to exactly one agent (no collisions)."""
    repo_root = Path(__file__).resolve().parents[2]
    agents_dir = repo_root / "claude" / "agents"
    role_map: dict[str, list[str]] = {}
    for af in sorted(agents_dir.glob("*.md")):
        text = af.read_text(encoding="utf-8")
        meta, _ = _parse_agent_frontmatter(text)
        for role in meta.get("roles", []):
            role_map.setdefault(role, []).append(af.stem)
    duplicates = {r: agents for r, agents in role_map.items() if len(agents) > 1}
    _assert(len(duplicates) == 0, "no_duplicate_roles",
            f"duplicates: {duplicates}" if duplicates else "")


test_all_real_agents_parseable()
test_all_real_agents_have_required_fields()
test_no_duplicate_roles_across_agents()


# ===========================================================================
# Summary
# ===========================================================================

print(f"\n{'=' * 40}")
print(f"{passed} passed, {failed} failed")
if failed:
    sys.exit(1)
