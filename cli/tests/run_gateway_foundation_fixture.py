from __future__ import annotations

import sys
from pathlib import Path

CLI_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CLI_ROOT.parent
GITHUB_ROOT = REPO_ROOT.parent
DOCS_ROOT = GITHUB_ROOT / "analysis-docs"
sys.path.insert(0, str(CLI_ROOT / "src"))

from awf.core.config import load_awf_config
from awf.core.task import TaskConstraints, TaskContext, TaskDefinition, TaskType, resolve_execution_mode
from awf.providers.base import ProviderCapability
from awf.providers.registry import ProviderRegistry


def main() -> int:
    config = load_awf_config(str(REPO_ROOT))
    registry = ProviderRegistry(config)

    claude_caps = registry.capabilities("claude-code")
    assert ProviderCapability.COMPLETE in claude_caps
    assert ProviderCapability.ANALYZE_NATIVE in claude_caps
    assert ProviderCapability.WF_NATIVE in claude_caps
    assert ProviderCapability.EVENT_STREAM in claude_caps

    codex_caps = registry.capabilities("codex")
    assert codex_caps == {ProviderCapability.COMPLETE, ProviderCapability.ADD_DIR}

    task = TaskDefinition(
        task_id="task-1",
        parent_task_id=None,
        correlation_id="corr-1",
        idempotency_key="corr-1:analyze:stage2:0",
        type=TaskType.ANALYZE,
        params={"service": "sample-api", "domain": "health"},
        constraints=TaskConstraints(),
        context=TaskContext(
            cwd=str(REPO_ROOT),
            repo_root=str(REPO_ROOT),
            docs_root=str(DOCS_ROOT),
            github_root=str(GITHUB_ROOT),
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
