from __future__ import annotations

from dataclasses import dataclass
import os
import json
import selectors
import subprocess
import time
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from awf.core.config import AwfConfig


@dataclass
class McpServerInfo:
    name: str
    transport: str
    config: dict[str, Any]


@dataclass
class McpInvokeResult:
    server: str
    transport: str
    tool: str
    result: Any
    protocol_version: str
    server_info: dict[str, Any]


@dataclass
class McpReadResult:
    server: str
    transport: str
    uri: str
    result: Any
    protocol_version: str
    server_info: dict[str, Any]


def discover_mcp_servers(config: AwfConfig) -> list[McpServerInfo]:
    raw = config.raw.get("mcp", {})
    servers: list[McpServerInfo] = []
    for name, server_config in sorted(raw.items()):
        if not isinstance(server_config, dict):
            continue
        transport = str(server_config.get("type", "unknown"))
        servers.append(
            McpServerInfo(
                name=name,
                transport=transport,
                config=dict(server_config),
            )
        )
    return servers


def summarize_mcp_server(server: McpServerInfo) -> str:
    if server.transport == "stdio":
        command = server.config.get("command", "")
        return f"{server.name} [{server.transport}] command={command}"
    if server.transport in {"http", "sse"}:
        url = server.config.get("url", "")
        return f"{server.name} [{server.transport}] url={url}"
    return f"{server.name} [{server.transport}]"


def resolve_mcp_server(config: AwfConfig, name: str) -> McpServerInfo:
    for server in discover_mcp_servers(config):
        if server.name == name:
            return server
    raise KeyError(f"Unknown MCP server `{name}`")


def resolve_mcp_server_for_operation(
    config: AwfConfig,
    requested_name: str | None,
    *,
    operation: str,
) -> McpServerInfo:
    explicit_name = str(requested_name or "").strip()
    if explicit_name:
        return resolve_mcp_server(config, explicit_name)

    defaults = config.raw.get("mcp_defaults", {})
    if not isinstance(defaults, dict):
        defaults = {}

    for key in (operation, "default"):
        candidate = str(defaults.get(key, "") or "").strip()
        if candidate:
            return resolve_mcp_server(config, candidate)

    servers = discover_mcp_servers(config)
    if len(servers) == 1:
        return servers[0]
    if not servers:
        raise KeyError("No MCP servers configured")
    raise KeyError(
        f"Missing MCP server for `{operation}`. "
        f"Set tool input `server` or configure [mcp_defaults] {operation} = \"<server>\"."
    )


def expand_env_vars(value: str) -> str:
    return os.path.expandvars(value)


def _encode_mcp_message(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def _read_exact(stream, size: int, deadline: float) -> bytes:
    data = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(stream, selectors.EVENT_READ)
    try:
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("mcp_read_timeout")
            events = selector.select(remaining)
            if not events:
                raise TimeoutError("mcp_read_timeout")
            chunk = os.read(stream.fileno(), size - len(data))
            if not chunk:
                raise EOFError("mcp_stream_closed")
            data.extend(chunk)
    finally:
        selector.close()
    return bytes(data)


def _read_mcp_message(stream, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    header = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(stream, selectors.EVENT_READ)
    try:
        while b"\r\n\r\n" not in header:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("mcp_header_timeout")
            events = selector.select(remaining)
            if not events:
                raise TimeoutError("mcp_header_timeout")
            chunk = os.read(stream.fileno(), 1)
            if not chunk:
                raise EOFError("mcp_stream_closed")
            header.extend(chunk)
    finally:
        selector.close()

    raw_header, _ = bytes(header).split(b"\r\n\r\n", 1)
    content_length = None
    for line in raw_header.decode("ascii", errors="ignore").split("\r\n"):
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip().lower() == "content-length":
            content_length = int(value.strip())
            break
    if content_length is None:
        raise ValueError("missing_content_length")

    body = _read_exact(stream, content_length, deadline)
    return json.loads(body.decode("utf-8"))


def _terminate_process(process: subprocess.Popen[bytes], timeout: int) -> None:
    try:
        process.terminate()
        process.wait(timeout=min(timeout, 2))
    except Exception:
        try:
            process.kill()
            process.wait(timeout=1)
        except Exception:
            pass


def _send_mcp_message(process: subprocess.Popen[bytes], payload: dict[str, Any]) -> None:
    if process.stdin is None:
        raise RuntimeError("spawn_error:missing_stdin")
    process.stdin.write(_encode_mcp_message(payload))
    process.stdin.flush()


def _start_stdio_session(
    server: McpServerInfo,
) -> tuple[subprocess.Popen[bytes], Any, int, dict[str, Any]]:
    command = str(server.config.get("command", "") or "").strip()
    args = server.config.get("args", [])
    timeout = int(server.config.get("timeout", 10) or 10)
    if not command:
        raise ValueError("missing_command")
    if not isinstance(args, list):
        raise ValueError("invalid_args")

    resolved_args = [expand_env_vars(str(item)) for item in args]
    process = subprocess.Popen(
        [expand_env_vars(command), *resolved_args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdin is None or process.stdout is None:
        raise RuntimeError("spawn_error:missing_pipes")

    initialize_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "awf-cli",
                "version": "0.1.0",
            },
        },
    }
    _send_mcp_message(process, initialize_request)
    response = _read_mcp_message(process.stdout, timeout)
    if response.get("id") != 1 or "result" not in response:
        raise ValueError("invalid_initialize_response")

    initialized_notification = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }
    _send_mcp_message(process, initialized_notification)
    return process, process.stdout, timeout, response.get("result", {})


def _request_optional_list(
    process: subprocess.Popen[bytes],
    stream,
    *,
    request_id: int,
    method: str,
    result_key: str,
    timeout: int,
) -> tuple[bool, list[str]]:
    request = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {},
    }
    _send_mcp_message(process, request)
    response = _read_mcp_message(stream, timeout)
    if response.get("id") != request_id:
        raise ValueError(f"invalid_{method.replace('/', '_')}_response")
    if "error" in response:
        return False, []
    result = response.get("result", {})
    entries = result.get(result_key, []) if isinstance(result, dict) else []
    if not isinstance(entries, list):
        return True, []
    names: list[str] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("uri") or "").strip()
        if name:
            names.append(name)
    return True, names


def _http_request_jsonrpc(
    server: McpServerInfo,
    *,
    request_id: int,
    method: str,
    params: dict[str, Any],
) -> tuple[dict[str, Any], Any]:
    url = str(server.config.get("url", "") or "").strip()
    timeout = int(server.config.get("timeout", 30) or 30)
    headers = server.config.get("headers", {})
    if not url:
        raise ValueError("missing_url")
    if not isinstance(headers, dict):
        raise ValueError("invalid_headers")

    expanded_headers = {str(key): expand_env_vars(str(value)) for key, value in headers.items()}
    expanded_headers.setdefault("Content-Type", "application/json")
    expanded_headers.setdefault("Accept", "application/json, application/problem+json")
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
    ).encode("utf-8")
    request = urllib_request.Request(url, data=payload, headers=expanded_headers, method="POST")
    with urllib_request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="ignore")
        parsed = json.loads(body) if body.strip() else {}
        if not isinstance(parsed, dict):
            raise ValueError("invalid_jsonrpc_response")
        if parsed.get("id") != request_id:
            raise ValueError(f"invalid_{method.replace('/', '_')}_response")
        if "error" in parsed:
            error_payload = parsed.get("error", {})
            raise RuntimeError(f"{method.replace('/', '_')}_error:{json.dumps(error_payload, ensure_ascii=False)}")
        result = parsed.get("result")
        return parsed, result


def check_stdio_server(server: McpServerInfo) -> tuple[bool, str]:
    if server.transport != "stdio":
        return False, f"unsupported_transport:{server.transport}"
    process: subprocess.Popen[bytes] | None = None
    timeout = int(server.config.get("timeout", 10) or 10)
    try:
        process, stdout, timeout, result = _start_stdio_session(server)
        server_info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
        server_name = server_info.get("name", "unknown")
        protocol = result.get("protocolVersion", "unknown") if isinstance(result, dict) else "unknown"
        capabilities = result.get("capabilities", {}) if isinstance(result, dict) else {}
        capability_keys = ",".join(sorted(capabilities.keys())) if isinstance(capabilities, dict) and capabilities else "none"
        tools_supported, tool_names = _request_optional_list(
            process,
            stdout,
            request_id=2,
            method="tools/list",
            result_key="tools",
            timeout=timeout,
        )
        resources_supported, resource_names = _request_optional_list(
            process,
            stdout,
            request_id=3,
            method="resources/list",
            result_key="resources",
            timeout=timeout,
        )
        tool_preview = ",".join(tool_names[:5]) if tool_names else "none"
        resource_preview = ",".join(resource_names[:5]) if resource_names else "none"
        return (
            True,
            "initialize_ok:"
            f"name={server_name},"
            f"protocol={protocol},"
            f"capabilities={capability_keys},"
            f"tools_supported={tools_supported},"
            f"tool_count={len(tool_names)},"
            f"tool_preview={tool_preview},"
            f"resources_supported={resources_supported},"
            f"resource_count={len(resource_names)},"
            f"resource_preview={resource_preview}",
        )
    except FileNotFoundError:
        return False, "command_not_found"
    except TimeoutError as exc:
        return False, f"timeout:{timeout}s:{exc}"
    except Exception as exc:
        stderr_detail = ""
        if process is not None and process.stderr is not None:
            try:
                stderr_detail = process.stderr.read(4096).decode("utf-8", errors="ignore").strip()
            except Exception:
                stderr_detail = ""
        if stderr_detail:
            return False, f"{exc} | stderr={stderr_detail}"
        return False, f"spawn_error:{exc}"
    finally:
        if process is not None:
            _terminate_process(process, timeout)


def check_http_server(server: McpServerInfo) -> tuple[bool, str]:
    if server.transport != "http":
        return False, f"unsupported_transport:{server.transport}"
    try:
        parsed, result = _http_request_jsonrpc(
            server,
            request_id=1,
            method="initialize",
            params={
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "awf-cli", "version": "0.1.0"},
            },
        )
        server_info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
        server_name = server_info.get("name", "unknown")
        protocol = result.get("protocolVersion", "unknown") if isinstance(result, dict) else "unknown"
        return True, f"http_initialize_ok:name={server_name},protocol={protocol},jsonrpc={parsed.get('jsonrpc', 'unknown')}"
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore").strip()
        return False, f"http_error:{exc.code}:{detail or exc.reason}"
    except urllib_error.URLError as exc:
        return False, f"connection_error:{exc.reason}"
    except json.JSONDecodeError as exc:
        return False, f"invalid_json:{exc}"
    except Exception as exc:
        return False, f"spawn_error:{exc}"


def check_sse_server(server: McpServerInfo) -> tuple[bool, str]:
    if server.transport != "sse":
        return False, f"unsupported_transport:{server.transport}"
    url = str(server.config.get("url", "") or "").strip()
    timeout = int(server.config.get("timeout", 30) or 30)
    headers = server.config.get("headers", {})
    if not url:
        return False, "missing_url"
    if not isinstance(headers, dict):
        return False, "invalid_headers"

    expanded_headers = {str(key): expand_env_vars(str(value)) for key, value in headers.items()}
    expanded_headers.setdefault("Accept", "text/event-stream")
    request = urllib_request.Request(url, headers=expanded_headers, method="GET")
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            preview = response.read(256).decode("utf-8", errors="ignore")
            if "text/event-stream" not in content_type:
                return False, f"unexpected_content_type:{content_type}"
            return True, f"sse_connect_ok:status={response.status},content_type={content_type},preview={preview.strip() or 'empty'}"
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore").strip()
        return False, f"http_error:{exc.code}:{detail or exc.reason}"
    except urllib_error.URLError as exc:
        return False, f"connection_error:{exc.reason}"
    except Exception as exc:
        return False, f"spawn_error:{exc}"


def check_mcp_server(server: McpServerInfo) -> tuple[bool, str]:
    if server.transport == "stdio":
        return check_stdio_server(server)
    if server.transport == "http":
        return check_http_server(server)
    if server.transport == "sse":
        return check_sse_server(server)
    return False, f"unsupported_transport:{server.transport}"


def invoke_mcp_tool(server: McpServerInfo, tool_name: str, arguments: dict[str, Any]) -> McpInvokeResult:
    if not tool_name.strip():
        raise ValueError("missing_tool_name")
    if server.transport == "http":
        _, result = _http_request_jsonrpc(
            server,
            request_id=10,
            method="tools/call",
            params={
                "name": tool_name,
                "arguments": arguments,
            },
        )
        init_result = result if isinstance(result, dict) else {}
        return McpInvokeResult(
            server=server.name,
            transport=server.transport,
            tool=tool_name,
            result=result,
            protocol_version=str(init_result.get("protocolVersion", "unknown")),
            server_info=init_result.get("serverInfo", {}) if isinstance(init_result.get("serverInfo", {}), dict) else {},
        )
    if server.transport != "stdio":
        raise ValueError(f"unsupported_transport_for_invoke:{server.transport}")
    process: subprocess.Popen[bytes] | None = None
    timeout = int(server.config.get("timeout", 10) or 10)
    try:
        process, stdout, timeout, result = _start_stdio_session(server)
        request = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }
        _send_mcp_message(process, request)
        response = _read_mcp_message(stdout, timeout)
        if response.get("id") != 10:
            raise ValueError("invalid_tools_call_response")
        if "error" in response:
            error_payload = response.get("error", {})
            raise RuntimeError(f"tool_call_error:{json.dumps(error_payload, ensure_ascii=False)}")
        payload = response.get("result")
        server_info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
        protocol = result.get("protocolVersion", "unknown") if isinstance(result, dict) else "unknown"
        return McpInvokeResult(
            server=server.name,
            transport=server.transport,
            tool=tool_name,
            result=payload,
            protocol_version=str(protocol),
            server_info=server_info if isinstance(server_info, dict) else {},
        )
    finally:
        if process is not None:
            _terminate_process(process, timeout)


def read_mcp_resource(server: McpServerInfo, uri: str) -> McpReadResult:
    if not uri.strip():
        raise ValueError("missing_resource_uri")
    if server.transport == "http":
        _, result = _http_request_jsonrpc(
            server,
            request_id=11,
            method="resources/read",
            params={
                "uri": uri,
            },
        )
        init_result = result if isinstance(result, dict) else {}
        return McpReadResult(
            server=server.name,
            transport=server.transport,
            uri=uri,
            result=result,
            protocol_version=str(init_result.get("protocolVersion", "unknown")),
            server_info=init_result.get("serverInfo", {}) if isinstance(init_result.get("serverInfo", {}), dict) else {},
        )
    if server.transport != "stdio":
        raise ValueError(f"unsupported_transport_for_read:{server.transport}")
    process: subprocess.Popen[bytes] | None = None
    timeout = int(server.config.get("timeout", 10) or 10)
    try:
        process, stdout, timeout, result = _start_stdio_session(server)
        request = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "resources/read",
            "params": {
                "uri": uri,
            },
        }
        _send_mcp_message(process, request)
        response = _read_mcp_message(stdout, timeout)
        if response.get("id") != 11:
            raise ValueError("invalid_resources_read_response")
        if "error" in response:
            error_payload = response.get("error", {})
            raise RuntimeError(f"resource_read_error:{json.dumps(error_payload, ensure_ascii=False)}")
        payload = response.get("result")
        server_info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
        protocol = result.get("protocolVersion", "unknown") if isinstance(result, dict) else "unknown"
        return McpReadResult(
            server=server.name,
            transport=server.transport,
            uri=uri,
            result=payload,
            protocol_version=str(protocol),
            server_info=server_info if isinstance(server_info, dict) else {},
        )
    finally:
        if process is not None:
            _terminate_process(process, timeout)
