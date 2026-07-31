"""Refresh-token persistence and short-lived local access-token exchange.

Refresh credentials are persisted only through one of the stores in this module.
Access credentials deliberately have no persistence implementation: an
:class:`AccessTokenBroker` keeps them in process memory until expiry or explicit
invalidation.
"""

from __future__ import annotations

import ctypes
import json
import os
import stat
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol

from awf.supervisor.client import HttpResponse, Transport


_KEYCHAIN_ITEM_NOT_FOUND = -25300
_REFRESH_MARGIN = timedelta(seconds=60)
_MAX_FILE_BYTES = 1024 * 1024


class CredentialStoreError(RuntimeError):
    """A refresh-token store could not complete a safe operation."""


class RefreshTokenNotFound(CredentialStoreError):
    """No refresh token is available for the requested agent."""

    def __init__(self) -> None:
        super().__init__("refresh token is not available")


class KeychainItemNotFound(RefreshTokenNotFound):
    """The requested Keychain generic-password item does not exist."""


class KeychainOperationError(CredentialStoreError):
    """A redacted Keychain Services OSStatus failure."""

    def __init__(self, operation: str, status: int) -> None:
        self.operation = operation
        self.status = status
        super().__init__("Keychain {} failed (OSStatus {})".format(operation, status))


class TokenExchangeError(RuntimeError):
    """The local control plane did not return a usable access credential."""


class RefreshTokenRevoked(TokenExchangeError):
    """The control plane rejected and locally revoked the refresh token."""


class RefreshTokenStore(Protocol):
    """Durable storage boundary for revocable refresh credentials."""

    def load_refresh_token(self, agent_id: str) -> str:
        ...

    def save_refresh_token(self, agent_id: str, value: str) -> None:
        ...

    def delete_refresh_token(self, agent_id: str) -> None:
        ...


class DarwinKeychainApi(Protocol):
    """The narrowly scoped subset of Keychain Services used by the store."""

    def upsert(self, service: bytes, account: bytes, secret: memoryview) -> None:
        ...

    def read(self, service: bytes, account: bytes) -> bytes:
        ...

    def delete(self, service: bytes, account: bytes) -> None:
        ...


class MemoryCredentialStore:
    """Test-only refresh-token store that never exposes values in its repr."""

    def __init__(self, value: Optional[str] = None) -> None:
        self._default_value = value
        self._values: Dict[str, str] = {}

    def load_refresh_token(self, agent_id: str) -> str:
        try:
            return self._values[agent_id]
        except KeyError:
            if self._default_value is not None:
                return self._default_value
            raise RefreshTokenNotFound() from None

    def save_refresh_token(self, agent_id: str, value: str) -> None:
        self._values[agent_id] = value

    def delete_refresh_token(self, agent_id: str) -> None:
        removed = self._values.pop(agent_id, None)
        if self._default_value is not None:
            self._default_value = None
            return
        if removed is None:
            raise RefreshTokenNotFound()

    def __repr__(self) -> str:
        return "MemoryCredentialStore(<redacted>)"


class FileCredentialStore:
    """A permission-checked refresh-token file store for non-Keychain hosts."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def load_refresh_token(self, agent_id: str) -> str:
        values = self._read_values()
        try:
            return values[agent_id]
        except KeyError:
            raise RefreshTokenNotFound() from None

    def save_refresh_token(self, agent_id: str, value: str) -> None:
        values = self._read_values_or_empty()
        values[agent_id] = value
        self._write_values(values)

    def delete_refresh_token(self, agent_id: str) -> None:
        values = self._read_values()
        try:
            del values[agent_id]
        except KeyError:
            raise RefreshTokenNotFound() from None
        self._write_values(values)

    def __repr__(self) -> str:
        return "FileCredentialStore(path={!r}, values=<redacted>)".format(str(self._path))

    def _read_values_or_empty(self) -> Dict[str, str]:
        try:
            return self._read_values()
        except RefreshTokenNotFound:
            return {}

    def _read_values(self) -> Dict[str, str]:
        try:
            descriptor = os.open(
                str(self._path),
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            raise RefreshTokenNotFound() from None
        except OSError as error:
            raise CredentialStoreError("refresh-token file cannot be opened safely") from error

        try:
            file_stat = os.fstat(descriptor)
            self._require_secure_regular_file(file_stat)
            if file_stat.st_size > _MAX_FILE_BYTES:
                raise CredentialStoreError("refresh-token file is too large")
            chunks = []
            remaining = file_stat.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(descriptor)

        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CredentialStoreError("refresh-token file is malformed") from None
        if not isinstance(decoded, dict) or not all(
            isinstance(agent_id, str) and isinstance(value, str)
            for agent_id, value in decoded.items()
        ):
            raise CredentialStoreError("refresh-token file has an invalid schema")
        return dict(decoded)

    def _write_values(self, values: Mapping[str, str]) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            encoded = json.dumps(
                dict(values), separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        except (TypeError, UnicodeEncodeError):
            raise CredentialStoreError("refresh token cannot be serialized") from None

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".{}-".format(self._path.name), dir=str(self._path.parent)
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(encoded)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(str(temporary_path), str(self._path))
            self._fsync_parent()
        except OSError as error:
            raise CredentialStoreError("refresh-token file cannot be written safely") from error
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _require_secure_regular_file(file_stat: os.stat_result) -> None:
        if not stat.S_ISREG(file_stat.st_mode):
            raise CredentialStoreError("refresh-token path must be a regular file")
        if stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise CredentialStoreError("refresh-token file must have mode 0600")

    def _fsync_parent(self) -> None:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(str(self._path.parent), directory_flags)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class DarwinSecurityKeychainApi:
    """ctypes Keychain Services adapter with bounded password buffers only."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise CredentialStoreError("macOS Keychain Services are unavailable on this platform")
        self._security = ctypes.CDLL(
            "/System/Library/Frameworks/Security.framework/Security"
        )
        self._core_foundation = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        self._configure_functions()

    def upsert(self, service: bytes, account: bytes, secret: memoryview) -> None:
        password_length = ctypes.c_uint32()
        password_data = ctypes.c_void_p()
        existing_item = ctypes.c_void_p()
        added_item = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            None,
            len(service),
            service,
            len(account),
            account,
            ctypes.byref(password_length),
            ctypes.byref(password_data),
            ctypes.byref(existing_item),
        )
        secret_buffer = self._bounded_buffer(secret)
        secret_pointer = (
            ctypes.cast(secret_buffer, ctypes.c_void_p)
            if secret_buffer is not None
            else None
        )
        try:
            if status == 0:
                self._check(
                    self._security.SecKeychainItemModifyAttributesAndData(
                        existing_item,
                        None,
                        secret.nbytes,
                        secret_pointer,
                    ),
                    "update",
                )
            elif status == _KEYCHAIN_ITEM_NOT_FOUND:
                self._check(
                    self._security.SecKeychainAddGenericPassword(
                        None,
                        len(service),
                        service,
                        len(account),
                        account,
                        secret.nbytes,
                        secret_pointer,
                        ctypes.byref(added_item),
                    ),
                    "add",
                )
            else:
                self._check(status, "find")
        finally:
            self._free_content_and_release(password_data, existing_item, added_item)

    def read(self, service: bytes, account: bytes) -> bytes:
        password_length = ctypes.c_uint32()
        password_data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            None,
            len(service),
            service,
            len(account),
            account,
            ctypes.byref(password_length),
            ctypes.byref(password_data),
            ctypes.byref(item),
        )
        try:
            if status == _KEYCHAIN_ITEM_NOT_FOUND:
                raise KeychainItemNotFound()
            self._check(status, "read")
            return ctypes.string_at(password_data, password_length.value)
        finally:
            self._free_content_and_release(password_data, item, None)

    def delete(self, service: bytes, account: bytes) -> None:
        password_length = ctypes.c_uint32()
        password_data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            None,
            len(service),
            service,
            len(account),
            account,
            ctypes.byref(password_length),
            ctypes.byref(password_data),
            ctypes.byref(item),
        )
        try:
            if status == _KEYCHAIN_ITEM_NOT_FOUND:
                raise KeychainItemNotFound()
            self._check(status, "find")
            self._check(self._security.SecKeychainItemDelete(item), "delete")
        finally:
            self._free_content_and_release(password_data, item, None)

    def _configure_functions(self) -> None:
        self._security.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainItemModifyAttributesAndData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self._security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self._security.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
        self._security.SecKeychainItemDelete.restype = ctypes.c_int32
        self._security.SecKeychainItemFreeContent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self._core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
        self._core_foundation.CFRelease.restype = None

    @staticmethod
    def _bounded_buffer(secret: memoryview) -> Optional[ctypes.Array[Any]]:
        if secret.nbytes == 0:
            return None
        if secret.readonly or not secret.contiguous:
            raise CredentialStoreError("Keychain secret buffer must be writable and contiguous")
        try:
            return (ctypes.c_ubyte * secret.nbytes).from_buffer(secret)
        except (TypeError, BufferError) as error:
            raise CredentialStoreError("Keychain secret buffer is invalid") from error

    @staticmethod
    def _check(status: int, operation: str) -> None:
        if status == _KEYCHAIN_ITEM_NOT_FOUND:
            raise KeychainItemNotFound()
        if status != 0:
            raise KeychainOperationError(operation, status)

    def _free_content_and_release(
        self,
        password_data: ctypes.c_void_p,
        item: ctypes.c_void_p,
        added_item: Optional[ctypes.c_void_p],
    ) -> None:
        cleanup_error: Optional[KeychainOperationError] = None
        if password_data.value:
            status = self._security.SecKeychainItemFreeContent(None, password_data)
            if status != 0:
                cleanup_error = KeychainOperationError("free content", status)
        if item.value:
            self._core_foundation.CFRelease(item)
        if added_item is not None and added_item.value:
            self._core_foundation.CFRelease(added_item)
        if cleanup_error is not None and sys.exc_info()[0] is None:
            raise cleanup_error


class MacOSKeychainCredentialStore:
    """Refresh-token store backed by generic-password Keychain items."""

    SERVICE = b"com.awf.supervisor-agent"

    def __init__(
        self,
        api: Optional[DarwinKeychainApi] = None,
        service: bytes = SERVICE,
    ) -> None:
        self._api = api if api is not None else DarwinSecurityKeychainApi()
        self._service = service

    def load_refresh_token(self, agent_id: str) -> str:
        try:
            value = self._api.read(self._service, self._account(agent_id))
        except KeychainItemNotFound as error:
            raise RefreshTokenNotFound() from error
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise CredentialStoreError("stored refresh token is not valid UTF-8") from None

    def save_refresh_token(self, agent_id: str, value: str) -> None:
        try:
            secret = bytearray(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise CredentialStoreError("refresh token cannot be encoded") from None
        try:
            self._api.upsert(self._service, self._account(agent_id), memoryview(secret))
        finally:
            secret[:] = b"\0" * len(secret)

    def delete_refresh_token(self, agent_id: str) -> None:
        try:
            self._api.delete(self._service, self._account(agent_id))
        except KeychainItemNotFound as error:
            raise RefreshTokenNotFound() from error

    def __repr__(self) -> str:
        return "MacOSKeychainCredentialStore(service={!r}, values=<redacted>)".format(
            self._service
        )

    @staticmethod
    def _account(agent_id: str) -> bytes:
        try:
            account = agent_id.encode("ascii")
        except UnicodeEncodeError as error:
            raise CredentialStoreError("agent ID must be ASCII") from error
        if not account or b"\0" in account:
            raise CredentialStoreError("agent ID is invalid")
        return account


@dataclass(frozen=True)
class AccessToken:
    """An opaque local access token with an aware expiry timestamp."""

    value: str
    expires_at: datetime

    def __repr__(self) -> str:
        return "AccessToken(value=<redacted>, expires_at={!r})".format(self.expires_at)


class AccessTokenBroker:
    """Exchanges a persisted refresh token for an in-memory access token."""

    def __init__(self, refresh_tokens: RefreshTokenStore, transport: Transport) -> None:
        self._refresh_tokens = refresh_tokens
        self._transport = transport
        self._cached_agent_id: Optional[str] = None
        self._cached: Optional[AccessToken] = None
        self._lock = threading.RLock()

    def current(self, agent_id: str, now: Optional[datetime] = None) -> AccessToken:
        current_time = self._require_aware_now(now)
        with self._lock:
            if self._is_current(agent_id, current_time):
                assert self._cached is not None
                return self._cached
            refresh_token = self._refresh_tokens.load_refresh_token(agent_id)
            response = self._transport.request(
                "POST",
                "/v1/local-agent/token",
                payload={"agent_id": agent_id, "refresh_token": refresh_token},
            )
            if response.status in (401, 403):
                try:
                    self._refresh_tokens.delete_refresh_token(agent_id)
                except RefreshTokenNotFound:
                    pass
                self._clear_cache()
                raise RefreshTokenRevoked("refresh token was rejected")
            if response.status < 200 or response.status >= 300:
                raise TokenExchangeError(
                    "token exchange failed with HTTP status {}".format(response.status)
                )
            token = self._parse_response(response, current_time)
            self._cached_agent_id = agent_id
            self._cached = token
            return token

    def invalidate(self) -> None:
        """Forget the cached access credential after a bearer-token rejection."""
        with self._lock:
            self._clear_cache()

    def __repr__(self) -> str:
        return "AccessTokenBroker(refresh_tokens=<redacted>, access_token=<redacted>)"

    def _is_current(self, agent_id: str, now: datetime) -> bool:
        return (
            self._cached_agent_id == agent_id
            and self._cached is not None
            and now < self._cached.expires_at - _REFRESH_MARGIN
        )

    def _clear_cache(self) -> None:
        self._cached_agent_id = None
        self._cached = None

    @staticmethod
    def _require_aware_now(now: Optional[datetime]) -> datetime:
        current_time = now if now is not None else datetime.now(timezone.utc)
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("now must be an aware timestamp")
        return current_time

    @staticmethod
    def _parse_response(response: HttpResponse, now: datetime) -> AccessToken:
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TokenExchangeError("token exchange returned malformed JSON") from None
        if not isinstance(payload, dict):
            raise TokenExchangeError("token exchange returned an invalid response")
        value = payload.get("access_token")
        expires_at = payload.get("expires_at")
        if not isinstance(value, str) or not value or not isinstance(expires_at, str):
            raise TokenExchangeError("token exchange returned an invalid response")
        try:
            parsed_expiry = datetime.fromisoformat(
                expires_at[:-1] + "+00:00" if expires_at.endswith("Z") else expires_at
            )
        except ValueError as error:
            raise TokenExchangeError("token exchange returned an invalid expiry") from error
        if parsed_expiry.tzinfo is None or parsed_expiry.utcoffset() is None:
            raise TokenExchangeError("token exchange returned a naive expiry")
        if parsed_expiry <= now:
            raise TokenExchangeError("token exchange returned an expired credential")
        return AccessToken(value=value, expires_at=parsed_expiry)
