#!/usr/bin/env python3
"""Loopback-only HTTPS proxy fixture for local Supervisor agent process tests.

The production local configuration accepts only execute-api HTTPS origins.  This
fixture deliberately keeps that production path intact: urllib CONNECTs to the
loopback proxy, then speaks TLS to a test-trusted certificate for the accepted
execute-api host.  No request is ever forwarded.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import socket
import socketserver
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit

from awf.supervisor.contracts import validate_contract


API_HOST = "fixture.execute-api.ap-northeast-2.amazonaws.com"
_API_AUTHORITY = "{}:443".format(API_HOST)
_ARTIFACT_KINDS = {
    "checkpoint": "checkpoints",
    "provenance": "provenance",
    "redacted-result": "redacted-results",
}


class FixtureProtocolError(AssertionError):
    """The tested client sent a request outside the local fixture protocol."""


class _LoopbackProxy(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, fixture: "FakeSupervisorServer") -> None:
        self.fixture = fixture
        super().__init__(("127.0.0.1", 0), _ProxyConnection)


class _ProxyConnection(socketserver.BaseRequestHandler):
    """One CONNECT tunnel and one protected HTTPS request per client socket."""

    def handle(self) -> None:
        raw = self._read_connect_request()
        if raw is None:
            return
        method, authority, _version = raw
        if method != "CONNECT" or authority != _API_AUTHORITY:
            self.server.fixture.record_protocol_error(  # type: ignore[attr-defined]
                "unexpected proxy target {} {}".format(method, authority)
            )
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        fixture: FakeSupervisorServer = self.server.fixture  # type: ignore[attr-defined]
        try:
            secure = fixture.tls_context.wrap_socket(self.request, server_side=True)
        except ssl.SSLError as error:
            fixture.record_protocol_error("TLS setup failed: {}".format(error))
            return
        with secure:
            self._serve_https_request(secure, fixture)

    def _read_connect_request(self) -> Optional[tuple[str, str, str]]:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            received = self.request.recv(4096)
            if not received:
                return None
            data.extend(received)
            if len(data) > 16 * 1024:
                raise FixtureProtocolError("proxy CONNECT header exceeds 16 KiB")
        head, _separator, _rest = bytes(data).partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        if not lines:
            return None
        parts = lines[0].split(" ", 2)
        if len(parts) != 3:
            raise FixtureProtocolError("malformed proxy CONNECT request")
        return parts[0], parts[1], parts[2]

    @staticmethod
    def _serve_https_request(secure: ssl.SSLSocket, fixture: "FakeSupervisorServer") -> None:
        reader = secure.makefile("rb")
        try:
            request_line = reader.readline(16 * 1024)
            if not request_line:
                return
            try:
                method, target, version = request_line.decode("iso-8859-1").rstrip("\r\n").split(" ", 2)
            except ValueError as error:
                raise FixtureProtocolError("malformed tunneled request line") from error
            if version != "HTTP/1.1":
                raise FixtureProtocolError("unexpected tunneled HTTP version")
            headers: dict[str, str] = {}
            while True:
                line = reader.readline(16 * 1024)
                if line in (b"", b"\r\n"):
                    break
                try:
                    name, value = line.decode("iso-8859-1").rstrip("\r\n").split(":", 1)
                except ValueError as error:
                    raise FixtureProtocolError("malformed tunneled header") from error
                headers[name.lower()] = value.strip()
            content_length = int(headers.get("content-length", "0"))
            if content_length < 0 or content_length > 1024 * 1024:
                raise FixtureProtocolError("invalid tunneled content length")
            body = reader.read(content_length)
            status, response = fixture.dispatch(method, target, headers, body)
            payload = b"" if response is None else json.dumps(
                response, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            reason = {200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 409: "Conflict", 503: "Service Unavailable"}.get(status, "Fixture")
            secure.sendall(
                "HTTP/1.1 {} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n".format(
                    status, reason, len(payload)
                ).encode("ascii")
                + payload
            )
        finally:
            reader.close()


class FakeSupervisorServer:
    """A deterministic protected local-agent API, available only via loopback.

    Constructor contract:
    - ``command`` and ``job`` are version-one Supervisor mappings.
    - ``prompt`` is returned with its canonical SHA-256.
    - ``accept_append_events`` controls every durable ``append_event`` response;
      ``append_outcomes`` instead supplies a finite scripted sequence.
    - ``desired_states`` supplies response values for successive job reads.

    The fixture records every protected bearer header, validated request body,
    durable-outbox visibility observed before every event response, artifacts,
    and all append attempts.  It never implements a stop endpoint.
    """

    def __init__(
        self,
        *,
        command: Mapping[str, Any],
        job: Mapping[str, Any],
        prompt: str,
        access_token: str,
        accept_append_events: bool = True,
        append_outcomes: Optional[Iterable[bool]] = None,
        desired_states: Optional[Iterable[str]] = None,
        idle_poll_wait_sec: float = 0.1,
    ) -> None:
        validate_contract("command", command)
        validate_contract("job", job)
        if not isinstance(prompt, str):
            raise TypeError("prompt must be text")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("access_token must be non-empty text")
        if idle_poll_wait_sec <= 0:
            raise ValueError("idle_poll_wait_sec must be positive")
        self._command = copy.deepcopy(dict(command))
        self._job = copy.deepcopy(dict(job))
        self._prompt = prompt
        self._access_token = access_token
        self._accept_append_events = accept_append_events
        self._append_outcomes = list(append_outcomes) if append_outcomes is not None else None
        self._desired_states = list(desired_states or ())
        self._idle_poll_wait_sec = idle_poll_wait_sec
        self._command_delivered = False
        self._authenticated_agent_id: Optional[str] = None
        self._outbox_path: Optional[Path] = None
        self._temporary: Optional[tempfile.TemporaryDirectory[str]] = None
        self._server: Optional[_LoopbackProxy] = None
        self._thread: Optional[threading.Thread] = None
        self._tls_context: Optional[ssl.SSLContext] = None
        self._lock = threading.RLock()

        self.requests: list[dict[str, Any]] = []
        self.bearer_headers: list[Optional[str]] = []
        self.heartbeat_requests: list[dict[str, Any]] = []
        self.claim_attempts: list[str] = []
        self.preclaim_headers: list[dict[str, str]] = []
        self.command_poll_count = 0
        self.command_delivery_count = 0
        self.prompt_checksums: list[str] = []
        self.event_attempts: list[dict[str, Any]] = []
        self.outbox_observed_before_acceptance: list[bool] = []
        self.event_validation_errors: list[str] = []
        self.artifacts: list[dict[str, Any]] = []
        self.protocol_errors: list[str] = []

    def __enter__(self) -> "FakeSupervisorServer":
        self.start()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    @property
    def proxy_url(self) -> str:
        if self._server is None:
            raise RuntimeError("fixture server is not running")
        host, port = self._server.server_address
        return "http://{}:{}".format(host, port)

    @property
    def api_url(self) -> str:
        return "https://{}".format(API_HOST)

    @property
    def certificate_path(self) -> Path:
        if self._temporary is None:
            raise RuntimeError("fixture server is not running")
        return Path(self._temporary.name) / "certificate.pem"

    @property
    def tls_context(self) -> ssl.SSLContext:
        if self._tls_context is None:
            raise RuntimeError("fixture server is not running")
        return self._tls_context

    @property
    def outbox_path(self) -> Path:
        if self._outbox_path is None:
            raise RuntimeError("fixture outbox has not been observed")
        return self._outbox_path

    @property
    def job_state(self) -> str:
        with self._lock:
            return str(self._job["state"])

    @property
    def active_lease_marker_exists(self) -> bool:
        return self.outbox_path.with_name("active-lease.json").exists()

    def observe_outbox(self, path: Path) -> None:
        self._outbox_path = Path(path)

    def set_append_outcomes(self, outcomes: Iterable[bool]) -> None:
        with self._lock:
            self._append_outcomes = list(outcomes)

    def artifacts_for_kind(self, kind: str) -> list[dict[str, Any]]:
        return [artifact for artifact in self.artifacts if artifact["kind"] == kind]

    def accepted_events_by_sequence(self) -> list[dict[str, Any]]:
        accepted: list[dict[str, Any]] = []
        seen: set[tuple[str, int, int]] = set()
        for event in self.event_attempts:
            if not event["accepted"]:
                continue
            key = (event["job_id"], event["generation"], event["sequence"])
            if key not in seen:
                seen.add(key)
                accepted.append(copy.deepcopy(event))
        return accepted

    def record_protocol_error(self, message: str) -> None:
        with self._lock:
            self.protocol_errors.append(message)

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("fixture server is already running")
        self._temporary = tempfile.TemporaryDirectory(prefix="awf-supervisor-e2e-")
        directory = Path(self._temporary.name)
        certificate = directory / "certificate.pem"
        private_key = directory / "private-key.pem"
        config = directory / "openssl.cnf"
        config.write_text(
            """[req]\ndistinguished_name = dn\nprompt = no\nx509_extensions = extensions\n[dn]\nCN = fixture.execute-api.ap-northeast-2.amazonaws.com\n[extensions]\nsubjectAltName = DNS:fixture.execute-api.ap-northeast-2.amazonaws.com\nbasicConstraints = critical,CA:TRUE\nkeyUsage = critical,digitalSignature,keyEncipherment,keyCertSign\n""",
            encoding="utf-8",
        )
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-config",
                str(config),
                "-keyout",
                str(private_key),
                "-out",
                str(certificate),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(certificate), keyfile=str(private_key))
        self._tls_context = context
        self._server = _LoopbackProxy(self)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="fake-supervisor-loopback",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        self._tls_context = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def dispatch(
        self, method: str, target: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, Optional[dict[str, Any]]]:
        parsed = urlsplit(target)
        path = parsed.path
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            self.record_protocol_error("unexpected tunneled request target {!r}".format(target))
            return 400, {"error": "invalid target"}
        try:
            payload = self._decode_body(body)
        except FixtureProtocolError as error:
            self.record_protocol_error(str(error))
            return 400, {"error": "invalid json"}
        with self._lock:
            self.requests.append(
                {
                    "method": method,
                    "path": path,
                    "headers": dict(headers),
                    "body": copy.deepcopy(payload),
                }
            )
            if path != "/v1/local-agent/token":
                authorization = headers.get("authorization")
                self.bearer_headers.append(authorization)
                if authorization != "Bearer {}".format(self._access_token):
                    return 401, {"error": "missing fixture bearer"}
            return self._dispatch_protected(method, path, payload, headers)

    @staticmethod
    def _decode_body(body: bytes) -> Optional[dict[str, Any]]:
        if not body:
            return None
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FixtureProtocolError("request body is not JSON") from error
        if not isinstance(value, dict):
            raise FixtureProtocolError("request body is not a JSON object")
        return value

    def _dispatch_protected(
        self,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]],
        headers: Mapping[str, str],
    ) -> tuple[int, Optional[dict[str, Any]]]:
        if method == "POST" and path == "/v1/local-agent/token":
            if payload is None or payload.get("refresh_token") != "fixture-refresh-token":
                return 401, {"error": "bad refresh token"}
            if not isinstance(payload.get("agent_id"), str) or not payload["agent_id"]:
                return 400, {"error": "invalid agent ID"}
            self._authenticated_agent_id = payload["agent_id"]
            return 200, {
                "access_token": self._access_token,
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            }
        if method == "GET" and path == "/v1/local-agent/commands":
            self.command_poll_count += 1
            if not self._command_delivered:
                self._command_delivered = True
                self.command_delivery_count += 1
                return 200, copy.deepcopy(self._command)
            time.sleep(self._idle_poll_wait_sec)
            return 204, None
        if method == "POST" and path == "/v1/local-agent/heartbeat":
            if payload is None:
                return 400, {"error": "missing heartbeat"}
            self.heartbeat_requests.append(copy.deepcopy(payload))
            return 200, {}
        if method == "POST" and path == "/v1/local-agent/jobs/{}/claim".format(self._job["job_id"]):
            if payload is None or payload.get("command_id") != self._command["command_id"]:
                return 409, {"error": "claim does not match command"}
            self.claim_attempts.append(str(payload["command_id"]))
            if self._job["state"] not in {"QUEUED", "PAUSED"}:
                return 409, {"error": "already claimed"}
            self._job.update(
                state="CLAIMED",
                owner_agent_id=self._agent_id_from_bearer(),
                lease_expires_at="2099-07-31T12:05:00Z",
                updated_at="2026-07-31T12:00:01Z",
            )
            return 200, self._copy_job()
        if method == "POST" and path == "/v1/local-agent/jobs/{}/renew".format(self._job["job_id"]):
            self._job["lease_expires_at"] = "2099-07-31T12:06:00Z"
            return 200, self._copy_job()
        if method == "GET" and path == "/v1/local-agent/jobs/{}".format(self._job["job_id"]):
            command_id = headers.get("x-awf-command-id")
            generation = headers.get("x-awf-generation")
            if command_id is not None or generation is not None:
                if not isinstance(command_id, str) or not isinstance(generation, str):
                    return 400, {"error": "incomplete preclaim headers"}
                self.preclaim_headers.append(
                    {
                        "x-awf-command-id": command_id,
                        "x-awf-generation": generation,
                    }
                )
                if (
                    command_id != self._command["command_id"]
                    or generation != str(self._command["generation"])
                    or self._job["generation"] != self._command["generation"]
                    or self._job["state"] not in {"QUEUED", "PAUSED"}
                ):
                    return 409, {"error": "preclaim does not match command"}
                return 200, self._copy_job()
            if self._job["owner_agent_id"] != self._agent_id_from_bearer():
                return 409, {"error": "job is not owner fenced"}
            if self._desired_states:
                self._job["desired_state"] = self._desired_states.pop(0)
            return 200, self._copy_job()
        if method == "GET" and path == "/v1/local-agent/jobs/{}/prompt".format(self._job["job_id"]):
            digest = hashlib.sha256(self._prompt.encode("utf-8")).hexdigest()
            self.prompt_checksums.append(digest)
            return 200, {"prompt": self._prompt, "sha256": digest}
        if method == "GET" and path == "/v1/local-agent/jobs/{}/decision".format(self._job["job_id"]):
            return 200, {
                "schema_version": 1,
                "generation": self._job["generation"],
                "decision": "APPROVE",
                "requested_action": "CONTINUE",
            }
        if method == "POST" and path == "/v1/local-agent/jobs/{}/transition".format(self._job["job_id"]):
            return self._transition(payload)
        if method == "POST" and path == "/v1/local-agent/jobs/{}/artifacts".format(self._job["job_id"]):
            return self._upload_artifact(payload)
        if method == "POST" and path == "/v1/local-agent/jobs/{}/events".format(self._job["job_id"]):
            return self._append_event(payload)
        return 404, {"error": "unknown fixture route"}

    def _transition(self, payload: Optional[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        if payload is None:
            return 400, {"error": "missing transition"}
        if payload.get("generation") != self._job["generation"] or payload.get("from_state") != self._job["state"]:
            return 409, {"error": "invalid transition source"}
        if (payload.get("from_state"), payload.get("to_state")) not in {
            ("CLAIMED", "PREPARING"),
            ("PREPARING", "RUNNING"),
            ("WAITING_APPROVAL", "RUNNING"),
        }:
            return 409, {"error": "invalid transition"}
        self._job["state"] = payload["to_state"]
        self._job["updated_at"] = "2026-07-31T12:00:02Z"
        return 200, self._copy_job()

    def _upload_artifact(self, payload: Optional[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        if payload is None:
            return 400, {"error": "missing artifact"}
        kind = payload.get("kind")
        if kind not in _ARTIFACT_KINDS or payload.get("generation") != self._job["generation"]:
            return 400, {"error": "invalid artifact identity"}
        encoded = payload.get("artifact_base64")
        claimed_digest = payload.get("sha256")
        if not isinstance(encoded, str) or not isinstance(claimed_digest, str):
            return 400, {"error": "invalid artifact payload"}
        try:
            raw = base64.b64decode(encoded.encode("ascii"), validate=True)
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            return 400, {"error": "invalid artifact encoding: {}".format(type(error).__name__)}
        digest = hashlib.sha256(raw).hexdigest()
        if digest != claimed_digest or not isinstance(decoded, dict):
            return 400, {"error": "artifact checksum mismatch"}
        uri = "s3://fixture-bucket/artifacts/{}/{}/{}/{}.json".format(
            _ARTIFACT_KINDS[kind], self._job["job_id"], self._job["generation"], digest
        )
        self.artifacts.append(
            {
                "kind": kind,
                "sha256": digest,
                "body": raw,
                "json": decoded,
                "uri": uri,
            }
        )
        return 200, {"artifact_uri": uri, "sha256": digest}

    def _append_event(self, payload: Optional[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        if payload is None:
            return 400, {"error": "missing event"}
        try:
            validate_contract("event", payload)
        except Exception as error:
            self.event_validation_errors.append(str(error))
            return 400, {"error": "invalid event"}
        event = copy.deepcopy(payload)
        observed = self._outbox_contains(event)
        accepted = self._next_append_outcome()
        event["accepted"] = accepted
        self.event_attempts.append(event)
        self.outbox_observed_before_acceptance.append(observed)
        if not accepted:
            return 503, {"error": "scripted event withholding"}
        data = event["data"]
        terminal = data.get("terminal_status")
        if terminal in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            self._job["state"] = terminal
        elif data.get("status_code") == "WAITING_APPROVAL":
            self._job["state"] = "WAITING_APPROVAL"
        elif data.get("status_code") == "PAUSED":
            self._job["state"] = "PAUSED"
        self._job["updated_at"] = "2026-07-31T12:00:03Z"
        return 200, self._copy_job()

    def _next_append_outcome(self) -> bool:
        if self._append_outcomes is not None:
            if self._append_outcomes:
                return bool(self._append_outcomes.pop(0))
            return self._accept_append_events
        return self._accept_append_events

    def _outbox_contains(self, event: Mapping[str, Any]) -> bool:
        path = self._outbox_path
        if path is None or not path.exists():
            return False
        connection = sqlite3.connect(path, timeout=1)
        try:
            row = connection.execute(
                "SELECT 1 FROM event_outbox WHERE job_id = ? AND generation = ? AND sequence = ?",
                (event["job_id"], event["generation"], event["sequence"]),
            ).fetchone()
            return row is not None
        finally:
            connection.close()

    def _copy_job(self) -> dict[str, Any]:
        validate_contract("job", self._job)
        return copy.deepcopy(self._job)

    def _agent_id_from_bearer(self) -> str:
        if self._authenticated_agent_id is None:
            raise FixtureProtocolError("claim arrived before token exchange")
        return self._authenticated_agent_id
