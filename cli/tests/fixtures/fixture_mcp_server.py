from __future__ import annotations

import json
import sys
from typing import Any


def read_message() -> dict[str, Any] | None:
    header = bytearray()
    while b"\r\n\r\n" not in header:
        chunk = sys.stdin.buffer.read(1)
        if not chunk:
            return None
        header.extend(chunk)
    raw_header, _ = bytes(header).split(b"\r\n\r\n", 1)
    content_length = 0
    for line in raw_header.decode("ascii", errors="ignore").split("\r\n"):
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip().lower() == "content-length":
            content_length = int(value.strip())
            break
    if content_length <= 0:
        return None
    body = sys.stdin.buffer.read(content_length)
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def write_message(payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()


def main() -> int:
    while True:
        message = read_message()
        if message is None:
            return 0
        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {
                            "name": "fixture-mcp",
                            "version": "0.1.0",
                        },
                        "capabilities": {
                            "tools": {},
                            "resources": {},
                        },
                    },
                }
            )
            continue

        if method == "notifications/initialized":
            continue

        if method == "tools/list":
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "Echo fixture text",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "text": {"type": "string"},
                                    },
                                },
                            }
                        ]
                    },
                }
            )
            continue

        if method == "resources/list":
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "resources": [
                            {
                                "name": "fixture-resource",
                                "uri": "fixture://resource",
                            }
                        ]
                    },
                }
            )
            continue

        if method == "resources/read":
            params = message.get("params", {})
            uri = str(params.get("uri", "")) if isinstance(params, dict) else ""
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "contents": [
                            {
                                "uri": uri,
                                "mimeType": "text/plain",
                                "text": f"resource:{uri}",
                            }
                        ]
                    },
                }
            )
            continue

        if method == "tools/call":
            params = message.get("params", {})
            arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
            text = str(arguments.get("text", ""))
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": f"echo:{text}",
                            }
                        ]
                    },
                }
            )
            continue

        write_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"method not found: {method}",
                },
            }
        )


if __name__ == "__main__":
    raise SystemExit(main())
