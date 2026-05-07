from __future__ import annotations

import json
from typing import Any

from awf.core.mcp import McpServerInfo, check_http_server, invoke_mcp_tool, read_mcp_resource
import awf.core.mcp as mcp_module


class _FakeHeaders:
    def __init__(self, content_type: str = "application/json") -> None:
        self._content_type = content_type

    def get(self, key: str, default: str = "") -> str:
        if key.lower() == "content-type":
            return self._content_type
        return default


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], *, status: int = 200) -> None:
        self._payload = json.dumps(payload).encode("utf-8")
        self.status = status
        self.headers = _FakeHeaders()

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def main() -> int:
    original_urlopen = mcp_module.urllib_request.urlopen

    def fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        body = json.loads(request.data.decode("utf-8"))
        method = body.get("method")
        request_id = body.get("id")
        if method == "initialize":
            return _FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "fixture-http-mcp", "version": "0.1.0"},
                        "capabilities": {"tools": {}, "resources": {}},
                    },
                }
            )
        if method == "tools/call":
            params = body.get("params", {})
            arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
            return _FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": f"http-echo:{arguments.get('text', '')}",
                            }
                        ]
                    },
                }
            )
        if method == "resources/read":
            params = body.get("params", {})
            uri = params.get("uri", "") if isinstance(params, dict) else ""
            return _FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "contents": [
                            {
                                "uri": uri,
                                "mimeType": "text/plain",
                                "text": f"http-resource:{uri}",
                            }
                        ]
                    },
                }
            )
        raise AssertionError(f"unexpected method: {method}")

    server = McpServerInfo(
        name="fixture_http_mcp",
        transport="http",
        config={"url": "https://fixture.invalid/mcp", "timeout": 5},
    )

    try:
        mcp_module.urllib_request.urlopen = fake_urlopen
        checked = check_http_server(server)
        print(checked)
        invoked = invoke_mcp_tool(server, "echo", {"text": "hello"})
        print(json.dumps({"server": invoked.server, "tool": invoked.tool, "result": invoked.result}, ensure_ascii=False))
        read_back = read_mcp_resource(server, "fixture://resource")
        print(json.dumps({"server": read_back.server, "uri": read_back.uri, "result": read_back.result}, ensure_ascii=False))
        return 0
    finally:
        mcp_module.urllib_request.urlopen = original_urlopen


if __name__ == "__main__":
    raise SystemExit(main())
