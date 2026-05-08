from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.analysis_stage1 import run_stage1_file_analyses, save_observation_cache
from awf.providers.base import ProviderResult


class CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self,
        prompt: str,
        *,
        cwd: str | None = None,
        add_dirs=None,
        timeout_sec=None,
    ) -> ProviderResult:
        self.calls += 1
        return ProviderResult(
            returncode=0,
            stdout=(
                "### Fresh observation\n\n"
                "```json\n"
                '{"path":"src/a.ts","role":"service","language":"typescript","lines":1,'
                '"imports":[],"business_logic":[],"signals":[]}\n'
                "```"
            ),
            stderr="",
        )


def _make_context(repo_root: Path) -> SimpleNamespace:
    ai_dir = repo_root / ".ai-context"
    ai_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        github_root=repo_root,
        repo_root=repo_root,
        ai_context_dir=ai_dir,
    )


def test_stage1_observation_cache_can_be_bypassed_for_graph_invalidated_paths(tmp_path: Path):
    source_path = tmp_path / "src" / "a.ts"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("export const a = 1;\n", encoding="utf-8")
    content_hash = hashlib.sha256(
        source_path.read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()

    ctx = _make_context(tmp_path)
    save_observation_cache(
        ctx,
        "src/a.ts",
        content_hash,
        {
            "path": "src/a.ts",
            "role": "cached",
            "observation": {
                "markdown": "### Cached observation",
                "json": {
                    "path": "src/a.ts",
                    "role": "cached",
                    "language": "typescript",
                    "lines": 1,
                    "imports": [],
                    "business_logic": [],
                    "signals": [],
                },
            },
        },
    )

    entries = [{"path": "src/a.ts", "sha256": content_hash}]
    cached_provider = CountingProvider()
    cached_results = run_stage1_file_analyses(
        ctx,
        entries,
        cached_provider,
        use_observation=True,
    )
    assert cached_provider.calls == 0
    assert cached_results[0]["role"] == "cached"

    bypass_provider = CountingProvider()
    fresh_results = run_stage1_file_analyses(
        ctx,
        entries,
        bypass_provider,
        use_observation=True,
        bypass_cache_paths={"src/a.ts"},
    )
    assert bypass_provider.calls == 1
    assert fresh_results[0]["role"] == "service"
