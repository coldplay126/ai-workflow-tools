from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import DeploymentAdapter


PROTOCOL = "awf.deployment-evidence/v1"
_MAX_REQUEST_BYTES = 16 * 1024
_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_STDOUT_BYTES = 64 * 1024
_MAX_STDERR_BYTES = 8 * 1024
_TERMINATE_GRACE_SECONDS = 1.0
_GIT_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{32,128}")
_RFC3339_UTC = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)"
)
_SAFE_EVIDENCE_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_SAFE_DIAGNOSTIC_CODE = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_DEFAULT_ENVIRONMENT_NAMES = ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG")


class EvidenceProtocolError(ValueError):
    """Raised when an adapter does not satisfy the strict evidence protocol."""


@dataclass(frozen=True)
class DeploymentEvidenceRequest:
    protocol: str
    request_id: str
    repository_id: str
    pull_request_number: int
    source_head_sha: str
    subject_revision: str
    requested_at: str

    @classmethod
    def create(
        cls,
        *,
        repository_id: str,
        pull_request_number: int,
        source_head_sha: str,
        subject_revision: str,
        now: datetime | None = None,
    ) -> DeploymentEvidenceRequest:
        issued_at = now or datetime.now(timezone.utc)
        request = cls(
            protocol=PROTOCOL,
            request_id=secrets.token_urlsafe(32),
            repository_id=repository_id,
            pull_request_number=pull_request_number,
            source_head_sha=source_head_sha,
            subject_revision=subject_revision,
            requested_at=_format_utc(issued_at),
        )
        request.validate()
        return request

    def validate(self) -> None:
        if self.protocol != PROTOCOL:
            raise EvidenceProtocolError("unsupported evidence request protocol")
        if not isinstance(self.request_id, str) or _REQUEST_ID.fullmatch(self.request_id) is None:
            raise EvidenceProtocolError("evidence request_id is invalid")
        _validate_identifier(self.repository_id, "repository_id")
        if (
            not isinstance(self.pull_request_number, int)
            or isinstance(self.pull_request_number, bool)
            or self.pull_request_number <= 0
        ):
            raise EvidenceProtocolError("pull_request_number must be a positive integer")
        _validate_oid(self.source_head_sha, "source_head_sha")
        _validate_oid(self.subject_revision, "subject_revision")
        _parse_utc_timestamp(self.requested_at, "requested_at")

    def to_bytes(self) -> bytes:
        self.validate()
        payload = json.dumps(
            {
                "protocol": self.protocol,
                "request_id": self.request_id,
                "repository_id": self.repository_id,
                "pull_request_number": self.pull_request_number,
                "source_head_sha": self.source_head_sha,
                "subject_revision": self.subject_revision,
                "requested_at": self.requested_at,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(payload) > _MAX_REQUEST_BYTES:
            raise EvidenceProtocolError("evidence request exceeds the maximum size")
        return payload


@dataclass(frozen=True)
class DeploymentEvidenceResponse:
    protocol: str
    request_id: str
    repository_id: str
    subject_revision: str
    status: str
    observed_at: str
    production_image_git_sha: str | None = None
    evidence_id: str | None = None
    diagnostic_code: str | None = None

    @classmethod
    def parse(
        cls,
        raw: bytes,
        request: DeploymentEvidenceRequest,
        *,
        max_age_seconds: int,
        now: datetime | None = None,
    ) -> DeploymentEvidenceResponse:
        payload = _strict_json_object(raw)
        required = {
            "protocol",
            "request_id",
            "repository_id",
            "subject_revision",
            "status",
            "observed_at",
        }
        optional = {
            "evidence_id",
            "diagnostic_code",
            "production_image_git_sha",
        }
        unknown = set(payload) - required - optional
        missing = required - set(payload)
        if unknown:
            raise EvidenceProtocolError(f"evidence response has an unknown field: {sorted(unknown)[0]}")
        if missing:
            raise EvidenceProtocolError(f"evidence response is missing {sorted(missing)[0]}")
        if payload["protocol"] != PROTOCOL:
            raise EvidenceProtocolError("evidence response protocol does not match")
        if payload["request_id"] != request.request_id:
            raise EvidenceProtocolError("evidence response request_id does not match")
        if payload["repository_id"] != request.repository_id:
            raise EvidenceProtocolError("evidence response repository_id does not match")
        if payload["subject_revision"] != request.subject_revision:
            raise EvidenceProtocolError("evidence response subject_revision does not match")
        _validate_identifier(payload["repository_id"], "repository_id")
        _validate_oid(payload["subject_revision"], "subject_revision")
        status = payload["status"]
        if not isinstance(status, str) or status not in {
            "healthy",
            "superseded_healthy",
            "pending",
            "failed",
            "unknown",
        }:
            raise EvidenceProtocolError("evidence response status is invalid")
        production_image_git_sha = payload.get("production_image_git_sha")
        if status == "superseded_healthy":
            if "production_image_git_sha" not in payload:
                raise EvidenceProtocolError(
                    "superseded_healthy evidence requires production_image_git_sha"
                )
            _validate_oid(production_image_git_sha, "production_image_git_sha")
            if production_image_git_sha == payload["subject_revision"]:
                raise EvidenceProtocolError(
                    "production_image_git_sha must differ from subject_revision"
                )
        elif "production_image_git_sha" in payload:
            raise EvidenceProtocolError(
                "production_image_git_sha is only valid for superseded_healthy evidence"
            )
        observed_at = payload["observed_at"]
        observed = _parse_utc_timestamp(observed_at, "observed_at")
        received_at = now or datetime.now(timezone.utc)
        if observed > received_at:
            raise EvidenceProtocolError("evidence response observed_at is in the future")
        if received_at - observed > timedelta(seconds=max_age_seconds):
            raise EvidenceProtocolError("evidence response is stale")
        evidence_id = payload.get("evidence_id")
        if evidence_id is not None and (
            not isinstance(evidence_id, str)
            or _SAFE_EVIDENCE_ID.fullmatch(evidence_id) is None
        ):
            raise EvidenceProtocolError("evidence response evidence_id is invalid")
        diagnostic_code = payload.get("diagnostic_code")
        if diagnostic_code is not None and (
            not isinstance(diagnostic_code, str)
            or _SAFE_DIAGNOSTIC_CODE.fullmatch(diagnostic_code) is None
        ):
            raise EvidenceProtocolError("evidence response diagnostic_code is invalid")
        return cls(
            protocol=PROTOCOL,
            request_id=request.request_id,
            repository_id=request.repository_id,
            subject_revision=request.subject_revision,
            status=status,
            observed_at=observed_at,
            production_image_git_sha=production_image_git_sha,
            evidence_id=evidence_id,
            diagnostic_code=diagnostic_code,
        )

    def validate(self) -> None:
        if self.protocol != PROTOCOL:
            raise EvidenceProtocolError("unsupported evidence response protocol")
        if not isinstance(self.request_id, str) or _REQUEST_ID.fullmatch(self.request_id) is None:
            raise EvidenceProtocolError("evidence response request_id is invalid")
        _validate_identifier(self.repository_id, "repository_id")
        _validate_oid(self.subject_revision, "subject_revision")
        if self.status not in {
            "healthy",
            "superseded_healthy",
            "pending",
            "failed",
            "unknown",
        }:
            raise EvidenceProtocolError("evidence response status is invalid")
        _parse_utc_timestamp(self.observed_at, "observed_at")
        if self.status == "superseded_healthy":
            _validate_oid(self.production_image_git_sha, "production_image_git_sha")
            if self.production_image_git_sha == self.subject_revision:
                raise EvidenceProtocolError(
                    "production_image_git_sha must differ from subject_revision"
                )
        elif self.production_image_git_sha is not None:
            raise EvidenceProtocolError(
                "production_image_git_sha is only valid for superseded_healthy evidence"
            )
        if self.evidence_id is not None and (
            not isinstance(self.evidence_id, str)
            or _SAFE_EVIDENCE_ID.fullmatch(self.evidence_id) is None
        ):
            raise EvidenceProtocolError("evidence response evidence_id is invalid")
        if self.diagnostic_code is not None and (
            not isinstance(self.diagnostic_code, str)
            or _SAFE_DIAGNOSTIC_CODE.fullmatch(self.diagnostic_code) is None
        ):
            raise EvidenceProtocolError("evidence response diagnostic_code is invalid")

    def registry_evidence(self, *, adapter_digest: str, received_at: datetime) -> dict[str, str]:
        self.validate()
        payload = {
            "protocol": self.protocol,
            "subject_revision": self.subject_revision,
            "status": self.status,
            "observed_at": self.observed_at,
            "received_at": _format_utc(received_at),
            "adapter_digest": adapter_digest,
            "response_digest": hashlib.sha256(self.to_bytes()).hexdigest(),
        }
        if self.production_image_git_sha is not None:
            payload["production_image_git_sha"] = self.production_image_git_sha
        if self.evidence_id is not None:
            payload["evidence_id"] = self.evidence_id
        if self.diagnostic_code is not None:
            payload["diagnostic_code"] = self.diagnostic_code
        return payload

    def to_bytes(self) -> bytes:
        self.validate()
        payload: dict[str, str] = {
            "protocol": self.protocol,
            "request_id": self.request_id,
            "repository_id": self.repository_id,
            "subject_revision": self.subject_revision,
            "status": self.status,
            "observed_at": self.observed_at,
        }
        if self.production_image_git_sha is not None:
            payload["production_image_git_sha"] = self.production_image_git_sha
        if self.evidence_id is not None:
            payload["evidence_id"] = self.evidence_id
        if self.diagnostic_code is not None:
            payload["diagnostic_code"] = self.diagnostic_code
        return json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")


@dataclass(frozen=True)
class EvidenceProbeResult:
    response: DeploymentEvidenceResponse | None
    received_at: datetime
    failure_code: str | None = None
    superseded_verified: bool = False

    @property
    def status(self) -> str:
        return self.response.status if self.response is not None else "unknown"

    @property
    def is_cleanup_healthy(self) -> bool:
        return self.response is not None and (
            self.response.status == "healthy"
            or (
                self.response.status == "superseded_healthy"
                and self.superseded_verified
            )
        )


class DeploymentEvidenceExecutor:
    """Run a trusted adapter with bounded, non-shell process supervision."""

    def __init__(
        self,
        *,
        neutral_cwd: Path,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        stdout_limit: int = _MAX_STDOUT_BYTES,
        stderr_limit: int = _MAX_STDERR_BYTES,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if timeout_seconds <= 0 or stdout_limit <= 0 or stderr_limit <= 0:
            raise ValueError("deployment evidence limits must be positive")
        self.neutral_cwd = neutral_cwd.resolve()
        self.timeout_seconds = timeout_seconds
        self.stdout_limit = stdout_limit
        self.stderr_limit = stderr_limit
        self.now = now or (lambda: datetime.now(timezone.utc))

    def execute(
        self, adapter: DeploymentAdapter, request: DeploymentEvidenceRequest
    ) -> EvidenceProbeResult:
        received_at = self.now()
        try:
            adapter.validate_executable()
            request_bytes = request.to_bytes()
            if not self.neutral_cwd.is_dir():
                raise EvidenceProtocolError("neutral adapter working directory is unavailable")
        except (OSError, ValueError, EvidenceProtocolError):
            return EvidenceProbeResult(None, received_at, "deployment_adapter_invalid")

        try:
            process = subprocess.Popen(
                list(adapter.command),
                cwd=str(self.neutral_cwd),
                env=_adapter_environment(adapter),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                text=False,
                start_new_session=True,
            )
        except OSError:
            return EvidenceProbeResult(None, received_at, "deployment_adapter_spawn_failed")

        deadline = time.monotonic() + self.timeout_seconds
        stdout, stdout_overflow = _BoundedReader(process.stdout, self.stdout_limit).start()
        stderr, stderr_overflow = _BoundedReader(process.stderr, self.stderr_limit).start()
        try:
            assert process.stdin is not None
            process.stdin.write(request_bytes)
            process.stdin.close()
        except OSError:
            self._terminate(process)
            self._join_readers(stdout, stderr, time.monotonic() + _TERMINATE_GRACE_SECONDS)
            return EvidenceProbeResult(
                None, received_at, "deployment_adapter_io_failed"
            )

        failure_code: str | None = None
        while process.poll() is None:
            if stdout_overflow.is_set() or stderr_overflow.is_set():
                failure_code = "deployment_adapter_output_overflow"
                break
            if time.monotonic() >= deadline:
                failure_code = "deployment_adapter_timeout"
                break
            time.sleep(0.01)
        if failure_code is None and not self._join_readers(stdout, stderr, deadline):
            failure_code = "deployment_adapter_timeout"
        if failure_code is not None:
            self._terminate(process)
            self._join_readers(
                stdout, stderr, time.monotonic() + _TERMINATE_GRACE_SECONDS
            )
            return EvidenceProbeResult(None, received_at, failure_code)
        if stdout_overflow.is_set() or stderr_overflow.is_set():
            return EvidenceProbeResult(None, received_at, "deployment_adapter_output_overflow")
        if process.returncode != 0:
            return EvidenceProbeResult(None, received_at, "deployment_adapter_nonzero")
        received_at = self.now()
        try:
            response = DeploymentEvidenceResponse.parse(
                stdout.value(),
                request,
                max_age_seconds=adapter.max_age_seconds,
                now=received_at,
            )
        except EvidenceProtocolError:
            return EvidenceProbeResult(
                None, received_at, "deployment_evidence_invalid"
            )
        return EvidenceProbeResult(response, received_at)

    @staticmethod
    def _join_readers(
        stdout: _BoundedReader, stderr: _BoundedReader, deadline: float
    ) -> bool:
        stdout_finished = stdout.join_until(deadline)
        stderr_finished = stderr.join_until(deadline)
        return stdout_finished and stderr_finished

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        DeploymentEvidenceExecutor._signal_process_group(process, signal.SIGTERM)
        deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is None:
                try:
                    process.wait(timeout=min(0.05, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    continue
            else:
                time.sleep(max(0.0, min(0.05, deadline - time.monotonic())))
        DeploymentEvidenceExecutor._signal_process_group(process, signal.SIGKILL)
        if process.poll() is None:
            try:
                process.wait(timeout=_TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:  # pragma: no cover - kernel-owned wedge
                pass

    @staticmethod
    def _signal_process_group(
        process: subprocess.Popen[bytes], signal_number: int
    ) -> None:
        try:
            os.killpg(process.pid, signal_number)
        except (AttributeError, OSError, ProcessLookupError):
            if process.poll() is None:
                try:
                    if signal_number == signal.SIGTERM:
                        process.terminate()
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass


class _BoundedReader:
    def __init__(self, stream: Any, limit: int) -> None:
        self.stream = stream
        self.limit = limit
        self.buffer = bytearray()
        self.overflow = threading.Event()
        self.thread = threading.Thread(target=self._read, daemon=True)

    def start(self) -> tuple[_BoundedReader, threading.Event]:
        self.thread.start()
        return self, self.overflow

    def join_until(self, deadline: float) -> bool:
        self.thread.join(max(0.0, deadline - time.monotonic()))
        return not self.thread.is_alive()

    def close(self) -> None:
        try:
            self.stream.close()
        except OSError:
            pass

    def value(self) -> bytes:
        return bytes(self.buffer)

    def _read(self) -> None:
        try:
            while True:
                chunk = self.stream.read(8192)
                if not chunk:
                    return
                if len(self.buffer) + len(chunk) > self.limit:
                    self.overflow.set()
                    return
                self.buffer.extend(chunk)
        finally:
            self.close()


def _adapter_environment(adapter: DeploymentAdapter) -> dict[str, str]:
    environment = {
        name: value
        for name in _DEFAULT_ENVIRONMENT_NAMES
        if (value := os.environ.get(name)) is not None
    }
    for name in adapter.environment:
        value = os.environ.get(name)
        if value is not None:
            environment[name] = value
    return environment


def _strict_json_object(raw: bytes) -> Mapping[str, Any]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise EvidenceProtocolError("evidence response is not valid UTF-8") from exc
    decoder = json.JSONDecoder(object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    try:
        payload, index = decoder.raw_decode(text)
    except (json.JSONDecodeError, EvidenceProtocolError) as exc:
        raise EvidenceProtocolError("evidence response is not valid JSON") from exc
    if text[index:].strip():
        raise EvidenceProtocolError("evidence response contains trailing JSON")
    if not isinstance(payload, dict):
        raise EvidenceProtocolError("evidence response must be a JSON object")
    return payload


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise EvidenceProtocolError(f"evidence response repeats {key!r}")
        output[key] = value
    return output


def _reject_constant(value: str) -> None:
    raise EvidenceProtocolError(f"evidence response contains non-JSON constant {value}")


def _validate_identifier(value: object, field: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 512
        or "\0" in value
    ):
        raise EvidenceProtocolError(f"{field} is invalid")


def _validate_oid(value: object, field: str) -> None:
    if not isinstance(value, str) or _GIT_OBJECT_ID.fullmatch(value) is None:
        raise EvidenceProtocolError(f"{field} is not a Git object ID")


def _parse_utc_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or _RFC3339_UTC.fullmatch(value) is None:
        raise EvidenceProtocolError(f"{field} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceProtocolError(f"{field} must be an RFC3339 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise EvidenceProtocolError(f"{field} must be UTC")
    return parsed


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise EvidenceProtocolError("timestamps must be UTC")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
