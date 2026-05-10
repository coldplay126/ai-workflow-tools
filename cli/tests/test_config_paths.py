from __future__ import annotations

import io
import json
from pathlib import Path

from awf.core.config import resolve_analysis_context, resolve_runtime_paths
from awf.core.mcp import McpServerInfo
import awf.core.mcp as mcp_module


def _repo_root(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / ".awf.toml").write_text("", encoding="utf-8")
    return path


def test_runtime_path_overrides_expand_user_and_win_over_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    repo = _repo_root(tmp_path / "repo")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AWF_DOCS_ROOT", str(tmp_path / "env-docs"))
    monkeypatch.setenv("AWF_GITHUB_ROOT", str(tmp_path / "env-github"))
    (repo / ".awf.toml").write_text(
        "\n".join(
            [
                "[paths]",
                'analysis_docs = "~/portable-analysis-docs"',
                'awf_github = "~/portable-github"',
            ]
        ),
        encoding="utf-8",
    )

    paths = resolve_runtime_paths(str(repo))

    assert paths["analysis_docs"] == str((home / "portable-analysis-docs").resolve())
    assert paths["awf_github"] == str((home / "portable-github").resolve())


def test_runtime_path_env_fallbacks_when_overrides_are_absent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo_root(tmp_path / "repo")
    docs_root = tmp_path / "env-docs"
    github_root = tmp_path / "env-github"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AWF_DOCS_ROOT", str(docs_root))
    monkeypatch.setenv("AWF_GITHUB_ROOT", str(github_root))

    paths = resolve_runtime_paths(str(repo))

    assert paths["analysis_docs"] == str(docs_root.resolve())
    assert paths["awf_github"] == str(github_root.resolve())


def test_runtime_github_root_defaults_to_repo_parent_without_env_or_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo_root(tmp_path / "github" / "repo")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AWF_DOCS_ROOT", raising=False)
    monkeypatch.delenv("AWF_GITHUB_ROOT", raising=False)

    paths = resolve_runtime_paths(str(repo))

    assert paths["awf_github"] == str(repo.parent.resolve())


def test_analysis_context_expands_github_root_placeholder_in_service_map(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo_root(tmp_path / "repo")
    docs_root = tmp_path / "analysis-docs"
    github_root = tmp_path / "github"
    templates = docs_root / "_templates"
    templates.mkdir(parents=True)
    (templates / "analysis-pipeline.json").write_text("{}", encoding="utf-8")
    (templates / "analysis-config.json").write_text(
        json.dumps(
            {
                "service_map": {
                    "sample-api": "${AWF_GITHUB_ROOT}/sample-api",
                    "sample-web": "${AWF_GITHUB_ROOT}/sample-web",
                },
                "domain_definitions": {
                    "quest-challenge": {
                        "directories": {
                            "sample-api": ["src/domain/quest"],
                            "sample-web": ["src/routes/quest"],
                        },
                        "related_domains": ["points"],
                        "existing_docs": ["existing.md"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    context = resolve_analysis_context(
        "sample-api",
        "quest-challenge",
        deep=False,
        repo_root=str(repo),
        docs_root=str(docs_root),
        github_root=str(github_root),
    )

    assert context.domain_directories == [
        str((github_root / "sample-api" / "src/domain/quest").resolve())
    ]
    assert context.all_directories == {
        "sample-api": [str((github_root / "sample-api" / "src/domain/quest").resolve())],
        "sample-web": [str((github_root / "sample-web" / "src/routes/quest").resolve())],
    }
    assert context.ai_context_dir == docs_root / "sample-api" / "quest-challenge" / ".ai-context"


def test_mcp_stdio_session_expands_command_and_args_env_vars(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, list[str]] = {}

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()

    def fake_popen(argv, **_kwargs):
        captured["argv"] = list(argv)
        return FakeProcess()

    monkeypatch.setenv("AWF_MCP_COMMAND", "/bin/echo")
    monkeypatch.setenv("AWF_DOCS_ROOT", str(tmp_path / "docs-root"))
    monkeypatch.setattr(mcp_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        mcp_module,
        "_read_mcp_message",
        lambda _stream, _timeout: {"id": 1, "result": {"serverInfo": {"name": "fixture"}}},
    )

    mcp_module._start_stdio_session(
        McpServerInfo(
            name="fixture",
            transport="stdio",
            config={
                "command": "${AWF_MCP_COMMAND}",
                "args": ["--root", "${AWF_DOCS_ROOT}"],
                "timeout": 5,
            },
        )
    )

    assert captured["argv"] == ["/bin/echo", "--root", str(tmp_path / "docs-root")]
