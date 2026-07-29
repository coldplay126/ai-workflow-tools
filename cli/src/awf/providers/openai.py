from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from awf.core.permissions import PermissionDeniedError, PermissionRuleset, check_permission, sdk_tool_permission_name
from awf.core.model_defaults import DEFAULT_OPENAI_MODEL
from awf.providers.base import ProviderCapability, ProviderResult, TokenUsage
from awf.tools.file_ops import FileOpsToolset
from awf.tools.git_ops import GitOpsToolset
from awf.tools.mcp_ops import McpOpsToolset


class OpenAiProvider:
    name = "openai"
    capabilities = {
        ProviderCapability.COMPLETE,
        ProviderCapability.TOOL_LOOP,
        ProviderCapability.EVENT_STREAM,
    }

    def __init__(
        self,
        api_key_env: Optional[str] = None,
        model: Optional[str] = None,
        max_output_tokens: Optional[int] = None,
        permission_ruleset: Optional[PermissionRuleset] = None,
    ) -> None:
        self.api_key_env = api_key_env or "OPENAI_API_KEY"
        self.model = model or os.environ.get("AWF_OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
        self.max_output_tokens = max_output_tokens or int(os.environ.get("AWF_OPENAI_MAX_OUTPUT_TOKENS", "8192"))
        self.max_tool_rounds = int(os.environ.get("AWF_OPENAI_MAX_TOOL_ROUNDS", "8"))
        self.permission_ruleset = permission_ruleset

    def _tool_definitions(self) -> list[dict]:
        return [
            {
                "type": "function",
                "name": "read_file",
                "description": "Read a UTF-8 text file relative to the provided working root.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            {
                "type": "function",
                "name": "glob_files",
                "description": "Glob for files relative to the working root.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            {
                "type": "function",
                "name": "grep_files",
                "description": "Search for a plain text pattern across files under the working root.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            {
                "type": "function",
                "name": "git_diff",
                "description": "Run git diff in the working repository and return the diff text.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "additionalProperties": False,
                },
                "strict": True,
            },
            {
                "type": "function",
                "name": "git_log",
                "description": "Run git log in the working repository and return the log text.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "additionalProperties": False,
                },
                "strict": True,
            },
            {
                "type": "function",
                "name": "mcp_call_tool",
                "description": "Invoke a configured MCP tool. `server` is optional when a default MCP server mapping is configured.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "server": {"type": "string"},
                        "tool": {"type": "string"},
                        "arguments": {"type": "object"},
                    },
                    "required": ["tool"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            {
                "type": "function",
                "name": "mcp_read_resource",
                "description": "Read a resource from a configured MCP server. `server` is optional when a default MCP server mapping is configured.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "server": {"type": "string"},
                        "uri": {"type": "string"},
                    },
                    "required": ["uri"],
                    "additionalProperties": False,
                },
                "strict": True,
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
                check_permission(self.permission_ruleset, sdk_tool_permission_name(tool_name), f"openai:{tool_name}")
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

    def complete(
        self,
        prompt: str,
        *,
        cwd: str | None = None,
        add_dirs: list[str] | None = None,
        timeout_sec: int | None = None,
    ) -> ProviderResult:
        # TODO(contract v0.1): wire timeout_sec through OpenAI client timeout
        _ = timeout_sec  # accepted for contract parity; not wired to SDK client yet
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            return ProviderResult(
                returncode=2,
                stdout="",
                stderr=f"Missing API key env for openai provider: {self.api_key_env}",
            )

        try:
            from openai import OpenAI
        except ModuleNotFoundError:
            return ProviderResult(
                returncode=2,
                stdout="",
                stderr="openai package is not installed. Install awf-cli with the `sdk` extra.",
            )

        try:
            client = OpenAI(api_key=api_key)
            tools = self._tool_definitions()
            response = client.responses.create(
                model=self.model,
                input=self._guided_prompt(prompt),
                max_output_tokens=self.max_output_tokens,
                tools=tools,
            )
            for _ in range(self.max_tool_rounds):
                calls = [item for item in getattr(response, "output", []) if getattr(item, "type", None) == "function_call"]
                if not calls:
                    break
                tool_outputs: list[dict] = []
                for call in calls:
                    try:
                        tool_input = json.loads(getattr(call, "arguments", "") or "{}")
                    except json.JSONDecodeError:
                        tool_input = {}
                    output, is_error = self._execute_tool(getattr(call, "name", ""), tool_input, cwd)
                    tool_outputs.append(
                        {
                            "type": "function_call_output",
                            "call_id": getattr(call, "call_id", ""),
                            "output": output if not is_error else f"ERROR: {output}",
                        }
                    )
                if not tool_outputs:
                    break
                response = client.responses.create(
                    model=self.model,
                    previous_response_id=getattr(response, "id", None),
                    input=tool_outputs,
                    max_output_tokens=self.max_output_tokens,
                    tools=tools,
                )
            usage = getattr(response, "usage", None)
            return ProviderResult(
                returncode=0,
                stdout=getattr(response, "output_text", "") or "",
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
                stderr=f"openai provider failed: {exc}",
            )
