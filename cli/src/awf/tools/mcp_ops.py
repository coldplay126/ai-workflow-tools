from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from awf.core.config import load_awf_config
from awf.core.mcp import (
    invoke_mcp_tool,
    read_mcp_resource,
    resolve_mcp_server_for_operation,
)
from awf.tools.base import ToolResult


class McpOpsToolset:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def invoke(self, server_name: str | None, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            config = load_awf_config(str(self.root))
            server = resolve_mcp_server_for_operation(config, server_name, operation="invoke")
            result = invoke_mcp_tool(server, tool_name, arguments)
            return ToolResult(ok=True, output=json.dumps(result.result, ensure_ascii=False))
        except Exception as exc:
            return ToolResult(ok=False, output="", error=str(exc))

    def read(self, server_name: str | None, uri: str) -> ToolResult:
        try:
            config = load_awf_config(str(self.root))
            server = resolve_mcp_server_for_operation(config, server_name, operation="read")
            result = read_mcp_resource(server, uri)
            return ToolResult(ok=True, output=json.dumps(result.result, ensure_ascii=False))
        except Exception as exc:
            return ToolResult(ok=False, output="", error=str(exc))
