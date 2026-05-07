from __future__ import annotations

import os
import sys
from pathlib import Path

from awf.providers.claude_sdk import ClaudeSdkProvider
from awf.providers.openai import OpenAiProvider


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_SERVER = ROOT / "cli" / "tests" / "fixtures" / "fixture_mcp_server.py"
SERVER_NAME = "fixture_mcp"


def _write_config(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                f'[mcp."{SERVER_NAME}"]',
                'type = "stdio"',
                'command = "python3"',
                f'args = ["-u", "{FIXTURE_SERVER}"]',
                "timeout = 5",
                "",
                "[mcp_defaults]",
                f'default = "{SERVER_NAME}"',
                f'invoke = "{SERVER_NAME}"',
                f'read = "{SERVER_NAME}"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def main() -> int:
    config_path = ROOT / ".awf.toml"
    backup = config_path.read_text(encoding="utf-8") if config_path.exists() else None
    _write_config(config_path)
    try:
        claude = ClaudeSdkProvider()
        openai = OpenAiProvider()

        output, is_error = claude._execute_tool(  # noqa: SLF001
            "mcp_call_tool",
            {"tool": "echo", "arguments": {"text": "hello"}},
            str(ROOT),
        )
        print(f"claude_invoke_error={is_error}")
        print(f"claude_invoke_output={output}")
        if is_error or "echo:hello" not in output:
            return 1

        output, is_error = claude._execute_tool(  # noqa: SLF001
            "mcp_read_resource",
            {"uri": "fixture://resource"},
            str(ROOT),
        )
        print(f"claude_read_error={is_error}")
        print(f"claude_read_output={output}")
        if is_error or "resource:fixture://resource" not in output:
            return 1

        output, is_error = openai._execute_tool(  # noqa: SLF001
            "mcp_call_tool",
            {"tool": "echo", "arguments": {"text": "hello"}},
            str(ROOT),
        )
        print(f"openai_invoke_error={is_error}")
        print(f"openai_invoke_output={output}")
        if is_error or "echo:hello" not in output:
            return 1

        output, is_error = openai._execute_tool(  # noqa: SLF001
            "mcp_read_resource",
            {"uri": "fixture://resource"},
            str(ROOT),
        )
        print(f"openai_read_error={is_error}")
        print(f"openai_read_output={output}")
        if is_error or "resource:fixture://resource" not in output:
            return 1
        return 0
    finally:
        if backup is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_text(backup, encoding="utf-8")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    raise SystemExit(main())
