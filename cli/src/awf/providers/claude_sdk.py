from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from awf.core.permissions import PermissionDeniedError, PermissionRuleset, check_permission, sdk_tool_permission_name
from awf.providers.base import ProviderCapability, ProviderResult, TokenUsage
from awf.tools.file_ops import FileOpsToolset
from awf.tools.git_ops import GitOpsToolset
from awf.tools.mcp_ops import McpOpsToolset


class ClaudeSdkProvider:
    name = "claude-sdk"
    capabilities = {
        ProviderCapability.COMPLETE,
        ProviderCapability.TOOL_LOOP,
        ProviderCapability.EVENT_STREAM,
    }

    def __init__(
        self,
        api_key_env: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        permission_ruleset: Optional[PermissionRuleset] = None,
    ) -> None:
        self.api_key_env = api_key_env or "ANTHROPIC_API_KEY"
        self.model = model or os.environ.get("AWF_CLAUDE_SDK_MODEL", "claude-sonnet-4-6")
        self.max_tokens = max_tokens or int(os.environ.get("AWF_CLAUDE_SDK_MAX_TOKENS", "8192"))
        self.max_tool_rounds = int(os.environ.get("AWF_CLAUDE_SDK_MAX_TOOL_ROUNDS", "8"))
        self.permission_ruleset = permission_ruleset

    def _tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "read_file",
                "description": "Read a UTF-8 text file relative to the provided working root.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "glob_files",
                "description": "Glob for files relative to the working root.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                    },
                    "required": ["pattern"],
                },
            },
            {
                "name": "grep_files",
                "description": "Search for a plain text pattern across files under the working root.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["pattern"],
                },
            },
            {
                "name": "git_diff",
                "description": "Run git diff in the working repository and return the diff text.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            {
                "name": "git_log",
                "description": "Run git log in the working repository and return the log text.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            {
                "name": "mcp_call_tool",
                "description": "Invoke a configured MCP tool. `server` is optional when a default MCP server mapping is configured.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "server": {"type": "string"},
                        "tool": {"type": "string"},
                        "arguments": {"type": "object"},
                    },
                    "required": ["tool"],
                },
            },
            {
                "name": "mcp_read_resource",
                "description": "Read a resource from a configured MCP server. `server` is optional when a default MCP server mapping is configured.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "server": {"type": "string"},
                        "uri": {"type": "string"},
                    },
                    "required": ["uri"],
                },
            },
        ]

    @staticmethod
    def _guided_prompt(prompt: str) -> str:
        guidance = (
            "Tool usage guidance:\n"
            "- Prefer embedded prompt/context first for information already provided.\n"
            "- Use file and git tools for repository-local evidence.\n"
            "- Use MCP tools only when you need external or provider-configured data that is not already present in the prompt or repository.\n"
            "- Prefer `mcp_read_resource` for stable reference material and `mcp_call_tool` for active lookups or computations.\n"
            "- If a default MCP server is configured, you may omit `server`; otherwise provide it explicitly.\n"
            "- Do not call MCP tools speculatively; call them only when they materially improve correctness.\n\n"
        )
        return guidance + prompt

    def _execute_tool(self, tool_name: str, tool_input: dict, cwd: Optional[str]) -> tuple[str, bool]:
        if self.permission_ruleset is not None:
            try:
                check_permission(self.permission_ruleset, sdk_tool_permission_name(tool_name), f"claude-sdk:{tool_name}")
            except PermissionDeniedError as exc:
                return str(exc), True
        root = Path(cwd).resolve() if cwd else Path.cwd().resolve()
        file_tools = FileOpsToolset(root)
        git_tools = GitOpsToolset(root)
        mcp_tools = McpOpsToolset(root)

        if tool_name == "read_file":
            result = file_tools.read(str(tool_input.get("path", "")))
        elif tool_name == "glob_files":
            result = file_tools.glob(str(tool_input.get("pattern", "")))
        elif tool_name == "grep_files":
            raw_paths = tool_input.get("paths")
            paths = [str(item) for item in raw_paths] if isinstance(raw_paths, list) else None
            result = file_tools.grep(str(tool_input.get("pattern", "")), paths=paths)
        elif tool_name == "git_diff":
            args = tool_input.get("args")
            result = git_tools.diff(*(str(item) for item in args)) if isinstance(args, list) else git_tools.diff()
        elif tool_name == "git_log":
            args = tool_input.get("args")
            result = git_tools.log(*(str(item) for item in args)) if isinstance(args, list) else git_tools.log()
        elif tool_name == "mcp_call_tool":
            raw_arguments = tool_input.get("arguments")
            arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
            result = mcp_tools.invoke(
                str(tool_input.get("server", "") or "") or None,
                str(tool_input.get("tool", "")),
                arguments,
            )
        elif tool_name == "mcp_read_resource":
            result = mcp_tools.read(
                str(tool_input.get("server", "") or "") or None,
                str(tool_input.get("uri", "")),
            )
        else:
            return f"Unsupported tool: {tool_name}", True

        if result.ok:
            return result.output or "", False
        return result.error or "tool execution failed", True

    @staticmethod
    def _content_blocks_to_dicts(blocks: list) -> list[dict]:
        serialized: list[dict] = []
        for block in blocks:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                serialized.append({"type": "text", "text": getattr(block, "text", "")})
            elif block_type == "tool_use":
                serialized.append(
                    {
                        "type": "tool_use",
                        "id": getattr(block, "id", ""),
                        "name": getattr(block, "name", ""),
                        "input": getattr(block, "input", {}),
                    }
                )
        return serialized

    def complete(
        self,
        prompt: str,
        *,
        cwd: str | None = None,
        add_dirs: list[str] | None = None,
        timeout_sec: int | None = None,
    ) -> ProviderResult:
        # TODO(contract v0.1): wire timeout_sec through Anthropic client timeout
        _ = timeout_sec  # accepted for contract parity; not wired to SDK client yet
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            return ProviderResult(
                returncode=2,
                stdout="",
                stderr=f"Missing API key env for claude-sdk provider: {self.api_key_env}",
            )

        try:
            from anthropic import Anthropic
        except ModuleNotFoundError:
            return ProviderResult(
                returncode=2,
                stdout="",
                stderr="anthropic package is not installed. Install awf-cli with the `sdk` extra.",
            )

        try:
            client = Anthropic(api_key=api_key)
            messages: list[dict] = [{"role": "user", "content": self._guided_prompt(prompt)}]
            tools = self._tool_definitions()
            response = None

            for _ in range(self.max_tool_rounds):
                response = client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    messages=messages,
                    tools=tools,
                )
                if getattr(response, "stop_reason", None) != "tool_use":
                    break

                assistant_content = self._content_blocks_to_dicts(list(getattr(response, "content", [])))
                messages.append({"role": "assistant", "content": assistant_content})

                tool_results: list[dict] = []
                for block in getattr(response, "content", []):
                    if getattr(block, "type", None) != "tool_use":
                        continue
                    output, is_error = self._execute_tool(
                        getattr(block, "name", ""),
                        getattr(block, "input", {}) or {},
                        cwd,
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": getattr(block, "id", ""),
                            "content": output,
                            "is_error": is_error,
                        }
                    )
                if not tool_results:
                    break
                messages.append({"role": "user", "content": tool_results})

            if response is None:
                return ProviderResult(returncode=2, stdout="", stderr="claude-sdk provider produced no response")

            text_parts: list[str] = []
            for block in getattr(response, "content", []):
                if getattr(block, "type", None) == "text":
                    text_parts.append(getattr(block, "text", ""))
            usage = getattr(response, "usage", None)
            return ProviderResult(
                returncode=0,
                stdout="".join(text_parts),
                stderr="",
                usage=TokenUsage(
                    input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                    output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                )
                if usage is not None
                else None,
            )
        except Exception as exc:
            return ProviderResult(
                returncode=2,
                stdout="",
                stderr=f"claude-sdk provider failed: {exc}",
            )
