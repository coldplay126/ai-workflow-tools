from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from awf.core.config import load_awf_config
from awf.core.task import TaskConstraints, TaskContext, TaskDefinition, TaskType, resolve_execution_mode
from awf.providers.base import ProviderCapability
from awf.providers.registry import ProviderRegistry


def main() -> int:
    config = load_awf_config("/Users/example/Documents/GitHub/ai-workflow-tools")
    registry = ProviderRegistry(config)

    claude_caps = registry.capabilities("claude-code")
    assert ProviderCapability.COMPLETE in claude_caps
    assert ProviderCapability.ANALYZE_NATIVE in claude_caps
    assert ProviderCapability.WF_NATIVE in claude_caps
    assert ProviderCapability.EVENT_STREAM in claude_caps

    codex_caps = registry.capabilities("codex")
    assert codex_caps == {ProviderCapability.COMPLETE}

    task = TaskDefinition(
        task_id="task-1",
        parent_task_id=None,
        correlation_id="corr-1",
        idempotency_key="corr-1:analyze:stage2:0",
        type=TaskType.ANALYZE,
        params={"service": "sample-api", "domain": "health"},
        constraints=TaskConstraints(),
        context=TaskContext(
            cwd="/Users/example/Documents/GitHub/ai-workflow-tools",
            repo_root="/Users/example/Documents/GitHub/ai-workflow-tools",
            docs_root="/Users/example/Documents/GitHub/analysis-docs",
            github_root="/Users/example/Documents/GitHub",
            config=config,
            provider_name="claude-code",
        ),
    )
    assert resolve_execution_mode(registry.get("claude-code"), task) == "native"
    assert resolve_execution_mode(registry.get("codex"), task) == "fallback"

    print("gateway_foundation_ok=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
