"""Behavioral contracts for secure local Supervisor credentials."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

from awf.supervisor.client import HttpResponse
from awf.supervisor.credentials import (
    AccessTokenBroker,
    CredentialStoreError,
    DarwinSecurityKeychainApi,
    FileCredentialStore,
    KeychainItemNotFound,
    KeychainOperationError,
    MacOSKeychainCredentialStore,
    MemoryCredentialStore,
    RefreshTokenNotFound,
    RefreshTokenRevoked,
    TokenExchangeError,
)


NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
AGENT_ID = "local-mac-01"


class RecordingKeychainApi:
    def __init__(self) -> None:
        self.saved: bytes = b""
        self.borrowed_secret: Optional[memoryview] = None
        self.items: Dict[tuple[bytes, bytes], bytes] = {}
        self.operations: List[str] = []

    def upsert(self, service: bytes, account: bytes, secret: memoryview) -> None:
        self.operations.append("upsert")
        assert (service, account) == (b"com.awf.supervisor-agent", AGENT_ID.encode("ascii"))
        self.saved = bytes(secret)
        self.borrowed_secret = secret
        self.items[(service, account)] = self.saved

    def read(self, service: bytes, account: bytes) -> bytes:
        self.operations.append("read")
        try:
            return self.items[(service, account)]
        except KeyError as error:
            raise KeychainItemNotFound() from error

    def delete(self, service: bytes, account: bytes) -> None:
        self.operations.append("delete")
        try:
            del self.items[(service, account)]
        except KeyError as error:
            raise KeychainItemNotFound() from error


class FailingKeychainApi:
    def upsert(self, service: bytes, account: bytes, secret: memoryview) -> None:
        raise KeychainOperationError("upsert", -25293)

    def read(self, service: bytes, account: bytes) -> bytes:
        raise KeychainOperationError("read", -25293)

    def delete(self, service: bytes, account: bytes) -> None:
        raise KeychainOperationError("delete", -25293)


class FixtureTransport:
    def __init__(self, responses: Sequence[HttpResponse]) -> None:
        self._responses = list(responses)
        self.calls: List[Mapping[str, Any]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> HttpResponse:
        self.calls.append(
            {"method": method, "path": path, "payload": payload, "headers": headers}
        )
        if not self._responses:
            raise AssertionError("unexpected token exchange")
        return self._responses.pop(0)


def token_response(token: str, expires_at: datetime) -> HttpResponse:
    return HttpResponse(
        status=200,
        headers={},
        body=json.dumps(
            {"access_token": token, "expires_at": expires_at.isoformat().replace("+00:00", "Z")}
        ).encode("utf-8"),
    )


def test_keychain_store_uses_buffer_api_and_zeroes_temporary_secret() -> None:
    api = RecordingKeychainApi()

    MacOSKeychainCredentialStore(api=api).save_refresh_token(AGENT_ID, "refresh-secret")

    assert api.saved == b"refresh-secret"
    assert api.borrowed_secret is not None
    assert bytes(api.borrowed_secret) == b"\0" * len(b"refresh-secret")


def test_keychain_store_reads_updates_and_deletes_in_place() -> None:
    api = RecordingKeychainApi()
    store = MacOSKeychainCredentialStore(api=api)

    store.save_refresh_token(AGENT_ID, "first-refresh-token")
    assert store.load_refresh_token(AGENT_ID) == "first-refresh-token"
    store.save_refresh_token(AGENT_ID, "rotated-refresh-token")
    assert store.load_refresh_token(AGENT_ID) == "rotated-refresh-token"
    store.delete_refresh_token(AGENT_ID)

    with pytest.raises(RefreshTokenNotFound):
        store.load_refresh_token(AGENT_ID)
    assert api.operations == ["upsert", "read", "upsert", "read", "delete", "read"]


def test_keychain_store_reports_missing_item_without_secret() -> None:
    store = MacOSKeychainCredentialStore(api=RecordingKeychainApi())

    with pytest.raises(RefreshTokenNotFound) as caught:
        store.load_refresh_token(AGENT_ID)

    assert "refresh-secret" not in str(caught.value)


def test_keychain_osstatus_errors_are_redacted() -> None:
    secret = "refresh-secret"
    store = MacOSKeychainCredentialStore(api=FailingKeychainApi())

    with pytest.raises(CredentialStoreError) as caught:
        store.save_refresh_token(AGENT_ID, secret)

    assert "OSStatus -25293" in str(caught.value)
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


def test_access_token_is_kept_in_memory_only(tmp_path: Path) -> None:
    refresh = MemoryCredentialStore("refresh-secret")
    http = FixtureTransport([token_response("access-secret", NOW + timedelta(seconds=900))])
    broker = AccessTokenBroker(refresh, http)

    assert broker.current(AGENT_ID, now=NOW).value == "access-secret"

    assert http.calls == [
        {
            "method": "POST",
            "path": "/v1/local-agent/token",
            "payload": {"agent_id": AGENT_ID, "refresh_token": "refresh-secret"},
            "headers": None,
        }
    ]
    assert "access-secret" not in repr(refresh)
    assert "access-secret" not in repr(broker)
    assert list(tmp_path.iterdir()) == []


def test_broker_refreshes_sixty_seconds_before_aware_expiry() -> None:
    refresh = MemoryCredentialStore("refresh-secret")
    http = FixtureTransport(
        [
            token_response("access-one", NOW + timedelta(minutes=15)),
            token_response("access-two", NOW + timedelta(minutes=30)),
        ]
    )
    broker = AccessTokenBroker(refresh, http)

    assert broker.current(AGENT_ID, now=NOW).value == "access-one"
    assert broker.current(AGENT_ID, now=NOW + timedelta(minutes=13, seconds=59)).value == "access-one"
    assert broker.current(AGENT_ID, now=NOW + timedelta(minutes=14)).value == "access-two"
    assert len(http.calls) == 2


def test_broker_invalidation_forces_a_fresh_exchange() -> None:
    refresh = MemoryCredentialStore("refresh-secret")
    http = FixtureTransport(
        [
            token_response("access-one", NOW + timedelta(minutes=15)),
            token_response("access-two", NOW + timedelta(minutes=15)),
        ]
    )
    broker = AccessTokenBroker(refresh, http)

    assert broker.current(AGENT_ID, now=NOW).value == "access-one"
    broker.invalidate()

    assert broker.current(AGENT_ID, now=NOW).value == "access-two"
    assert len(http.calls) == 2


def test_broker_deletes_a_revoked_refresh_token() -> None:
    refresh = MemoryCredentialStore("refresh-secret")
    http = FixtureTransport([HttpResponse(status=401, headers={}, body=b"{}")])
    broker = AccessTokenBroker(refresh, http)

    with pytest.raises(RefreshTokenRevoked):
        broker.current(AGENT_ID, now=NOW)
    with pytest.raises(RefreshTokenNotFound):
        refresh.load_refresh_token(AGENT_ID)


def test_broker_rejects_malformed_exchange_response() -> None:
    refresh = MemoryCredentialStore("refresh-secret")
    http = FixtureTransport(
        [
            HttpResponse(
                status=200,
                headers={},
                body=b'{"access_token":"access-secret","expires_at":"2026-07-30T12:15:00"}',
            )
        ]
    )
    broker = AccessTokenBroker(refresh, http)

    with pytest.raises(TokenExchangeError) as caught:
        broker.current(AGENT_ID, now=NOW)

    assert "refresh-secret" not in str(caught.value)
    assert "access-secret" not in str(caught.value)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only file permission contract")
def test_linux_file_store_requires_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "refresh-tokens.json"
    store = FileCredentialStore(path)

    store.save_refresh_token(AGENT_ID, "refresh-secret")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.load_refresh_token(AGENT_ID) == "refresh-secret"
    os.chmod(path, 0o644)
    with pytest.raises(CredentialStoreError, match="0600"):
        store.load_refresh_token(AGENT_ID)


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Keychain Services")
def test_real_keychain_lifecycle_never_spawns_security_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    service = b"com.awf.supervisor-agent.test"
    agent_id = "pytest-" + uuid.uuid4().hex
    store = MacOSKeychainCredentialStore(api=DarwinSecurityKeychainApi(), service=service)

    def unexpected_security_process(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Keychain operations must not spawn a security subprocess")

    monkeypatch.setattr(subprocess, "run", unexpected_security_process)
    try:
        try:
            store.delete_refresh_token(agent_id)
        except RefreshTokenNotFound:
            pass
        store.save_refresh_token(agent_id, "first-refresh-token")
        assert store.load_refresh_token(agent_id) == "first-refresh-token"
        store.save_refresh_token(agent_id, "rotated-refresh-token")
        assert store.load_refresh_token(agent_id) == "rotated-refresh-token"
        store.delete_refresh_token(agent_id)
        with pytest.raises(RefreshTokenNotFound):
            store.load_refresh_token(agent_id)
    finally:
        try:
            store.delete_refresh_token(agent_id)
        except RefreshTokenNotFound:
            pass
