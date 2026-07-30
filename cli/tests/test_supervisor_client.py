"""Behavioral contracts for the AWF Supervisor admin client and SigV4 transport."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Type

import pytest
from botocore.exceptions import CredentialRetrievalError, ProfileNotFound

import awf.supervisor.client as client_module
from awf.supervisor.client import (
    HttpResponse,
    RepoRef,
    SigV4Transport,
    SupervisorAuthRequired,
    SupervisorClient,
    SupervisorConflict,
    SupervisorRemoteError,
)
from awf.supervisor.config import SupervisorConfig
from awf.supervisor.contracts import RequestedTarget, SupervisorAgent, SupervisorJob


NOW = "2026-07-30T12:00:00Z"
SUPPLIED_KEY = "5a2c1e31-cf45-4fe4-ae47-f40d3eb90837"
GENERATED_KEY = "2b0f54d4-a2cc-4ca8-b229-e84606ed80a6"
MEBIBYTE = 1024 * 1024


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    payload: Optional[Mapping[str, Any]]
    headers: Optional[Mapping[str, str]]


class RecordingTransport:
    """A deterministic transport double that retains every client request."""

    def __init__(self, responses: Sequence[HttpResponse]) -> None:
        self._responses = list(responses)
        self.calls: List[RecordedRequest] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> HttpResponse:
        self.calls.append(
            RecordedRequest(
                method=method,
                path=path,
                payload=payload,
                headers=headers,
            )
        )
        if not self._responses:
            raise AssertionError("unexpected transport request")
        return self._responses.pop(0)


def job_fixture(**updates: Any) -> Dict[str, Any]:
    payload = {
        "schema_version": 1,
        "job_id": "job-1",
        "workflow_id": "2026-07-30-login-contract",
        "state": "QUEUED",
        "desired_state": "RUNNING",
        "approval_required": True,
        "requested_target": "auto",
        "owner_agent_id": None,
        "lease_expires_at": None,
        "generation": 3,
        "attempt": 0,
        "repo_refs": [{"repo": "blip-server", "base": "main"}],
        "required_capabilities": ["git", "omp", "github"],
        "checkpoint": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    payload.update(updates)
    return payload


def agent_fixture(**updates: Any) -> Dict[str, Any]:
    payload = {
        "schema_version": 1,
        "agent_id": "local-agent-1",
        "environment": "local",
        "status": "ONLINE",
        "last_heartbeat_at": NOW,
        "max_concurrency": 2,
        "active_jobs": 1,
        "capabilities": ["git", "omp"],
        "repos": ["blip-server"],
        "version": {"awf": "1.0.0", "omp": "0.1.0"},
    }
    payload.update(updates)
    return payload


def response(
    status: int,
    payload: Any,
    *,
    request_id: str = "request-123",
) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers={"x-request-id": request_id},
        body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    )


def supervisor_config() -> SupervisorConfig:
    return SupervisorConfig(
        api_url="https://abc123.execute-api.ap-northeast-2.amazonaws.com",
        region="ap-northeast-2",
        profile="team-profile",
        poll_interval_seconds=2,
        request_timeout_seconds=30,
    )


def assert_canonical_uuid4(value: str) -> None:
    parsed = uuid.UUID(value)
    assert parsed.version == 4
    assert str(parsed) == value


def test_submit_uses_only_the_six_admin_request_fields_and_supplied_key() -> None:
    transport = RecordingTransport([response(201, job_fixture())])

    job = SupervisorClient(transport).submit_job(
        workflow_id="2026-07-30-login-contract",
        requested_target=RequestedTarget.AUTO,
        repo_refs=[RepoRef(repo="blip-server", base="main")],
        required_capabilities=["git", "omp", "github"],
        prompt="Fix the login contract.\n",
        idempotency_key=SUPPLIED_KEY,
    )

    assert isinstance(job, SupervisorJob)
    assert job.job_id == "job-1"
    assert transport.calls == [
        RecordedRequest(
            method="POST",
            path="/v1/admin/jobs",
            payload={
                "schema_version": 1,
                "workflow_id": "2026-07-30-login-contract",
                "requested_target": "auto",
                "repo_refs": [{"repo": "blip-server", "base": "main"}],
                "required_capabilities": ["git", "omp", "github"],
                "prompt": "Fix the login contract.\n",
            },
            headers={"Idempotency-Key": SUPPLIED_KEY},
        )
    ]


def test_submit_generates_a_canonical_uuid4_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module.uuid, "uuid4", lambda: uuid.UUID(GENERATED_KEY))
    transport = RecordingTransport([response(201, job_fixture())])

    SupervisorClient(transport).submit_job(
        workflow_id="2026-07-30-login-contract",
        requested_target=RequestedTarget.AUTO,
        repo_refs=[RepoRef(repo="blip-server", base="main")],
        required_capabilities=["git", "omp"],
        prompt="Fix the login contract.\n",
    )

    assert transport.calls[0].headers == {"Idempotency-Key": GENERATED_KEY}
    assert_canonical_uuid4(GENERATED_KEY)


@pytest.mark.parametrize(
    "idempotency_key",
    ["not-a-uuid", str(uuid.uuid1()), SUPPLIED_KEY.upper()],
)
def test_submit_rejects_malformed_non_v4_or_noncanonical_supplied_keys(
    idempotency_key: str,
) -> None:
    transport = RecordingTransport([response(201, job_fixture())])

    with pytest.raises(ValueError, match="canonical UUID4"):
        SupervisorClient(transport).submit_job(
            workflow_id="2026-07-30-login-contract",
            requested_target=RequestedTarget.AUTO,
            repo_refs=[RepoRef(repo="blip-server", base="main")],
            required_capabilities=["git", "omp"],
            prompt="Fix the login contract.\n",
            idempotency_key=idempotency_key,
        )

    assert transport.calls == []


def test_client_uses_exact_job_and_agent_routes_and_decision_bodies() -> None:
    transport = RecordingTransport(
        [
            response(200, job_fixture()),
            response(200, job_fixture(desired_state="CANCELLED")),
            response(200, job_fixture(state="RUNNING")),
            response(200, job_fixture(desired_state="CANCELLED")),
            response(200, {"schema_version": 1, "agents": [agent_fixture()]}),
        ]
    )
    client = SupervisorClient(transport)

    fetched = client.get_job("job-1")
    cancelled = client.cancel_job("job-1", generation=3)
    approved = client.approve_job("job-1", generation=3)
    rejected = client.reject_job("job-1", generation=3)
    agents = client.list_agents()

    assert isinstance(fetched, SupervisorJob)
    assert cancelled.desired_state == "CANCELLED"
    assert approved.state.value == "RUNNING"
    assert rejected.desired_state == "CANCELLED"
    assert agents == [SupervisorAgent.from_dict(agent_fixture())]
    assert [
        (call.method, call.path, call.payload) for call in transport.calls
    ] == [
        ("GET", "/v1/admin/jobs/job-1", None),
        ("POST", "/v1/admin/jobs/job-1/cancel", {"generation": 3}),
        (
            "POST",
            "/v1/admin/jobs/job-1/decisions",
            {"generation": 3, "decision": "APPROVE", "requested_action": "CONTINUE"},
        ),
        (
            "POST",
            "/v1/admin/jobs/job-1/decisions",
            {"generation": 3, "decision": "REJECT", "requested_action": "CANCEL"},
        ),
        ("GET", "/v1/admin/agents", None),
    ]
    for call in transport.calls[1:4]:
        assert call.headers is not None
        key = call.headers["Idempotency-Key"]
        assert_canonical_uuid4(key)


def test_client_rejects_invalid_successful_job_and_agent_envelopes() -> None:
    invalid_job = job_fixture()
    del invalid_job["job_id"]
    invalid_agent = agent_fixture()
    del invalid_agent["agent_id"]
    transport = RecordingTransport(
        [
            response(200, invalid_job),
            response(200, {"schema_version": 1, "agents": [invalid_agent]}),
        ]
    )
    client = SupervisorClient(transport)

    with pytest.raises(ValueError):
        client.get_job("job-1")
    with pytest.raises(ValueError):
        client.list_agents()


@pytest.mark.parametrize("body", [b"", b"not-json", b"[]"])
def test_client_rejects_empty_malformed_or_non_object_success_bodies(body: bytes) -> None:
    transport = RecordingTransport(
        [HttpResponse(status=200, headers={"x-request-id": "request-parse"}, body=body)]
    )

    with pytest.raises(ValueError):
        SupervisorClient(transport).get_job("job-1")


def test_client_rejects_response_bodies_larger_than_one_mebibyte() -> None:
    transport = RecordingTransport(
        [
            HttpResponse(
                status=200,
                headers={"x-request-id": "request-large"},
                body=b"x" * (MEBIBYTE + 1),
            )
        ]
    )

    with pytest.raises(ValueError, match="1 MiB"):
        SupervisorClient(transport).get_job("job-1")


@pytest.mark.parametrize(
    ("status", "code", "message", "error_type"),
    [
        (401, "AUTH_REQUIRED", "AWS SSO login is required", SupervisorAuthRequired),
        (403, "POLICY_DENIED", "operator is not permitted", SupervisorRemoteError),
        (404, "NOT_FOUND", "job-1 does not exist", SupervisorRemoteError),
        (409, "GENERATION_CONFLICT", "stale generation", SupervisorConflict),
        (429, "RATE_LIMITED", "try again later", SupervisorRemoteError),
        (503, "SERVICE_UNAVAILABLE", "control plane unavailable", SupervisorRemoteError),
    ],
)
def test_client_maps_error_statuses_and_preserves_canonical_request_context(
    status: int,
    code: str,
    message: str,
    error_type: Type[Exception],
) -> None:
    request_id = "request-{}".format(status)
    transport = RecordingTransport(
        [response(status, {"code": code, "message": message}, request_id=request_id)]
    )

    with pytest.raises(error_type) as raised:
        SupervisorClient(transport).get_job("job-1")

    error = raised.value
    assert getattr(error, "request_id") == request_id
    assert str(error) == "{} (request_id={})".format(message, request_id)
    if isinstance(error, SupervisorRemoteError):
        assert error.status == status
        assert error.code == code


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"not-json", id="malformed"),
        pytest.param(b"[]", id="non-object"),
        pytest.param(b"x" * (MEBIBYTE + 1), id="oversized"),
    ],
)
def test_client_maps_invalid_non_success_bodies_to_stable_remote_error(
    body: bytes,
) -> None:
    status = 502
    request_id = "request-invalid-error"
    transport = RecordingTransport(
        [HttpResponse(status=status, headers={"x-request-id": request_id}, body=body)]
    )

    with pytest.raises(SupervisorRemoteError) as raised:
        SupervisorClient(transport).get_job("job-1")

    error = raised.value
    assert type(error) is SupervisorRemoteError
    assert error.status == status
    assert error.request_id == request_id
    assert error.code == "INVALID_ERROR_RESPONSE"
    assert error.message == "Supervisor returned an invalid error response"
    assert str(error) == (
        "Supervisor returned an invalid error response "
        "(request_id=request-invalid-error)"
    )

def test_client_maps_recursive_non_success_json_decode_to_stable_remote_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = 502
    request_id = "request-recursive-error"
    nesting = 2_048
    body = b'{"error":' + (b"[" * nesting) + b"null" + (b"]" * nesting) + b"}"
    assert len(body) < MEBIBYTE
    decode_error = RecursionError("JSON nesting exhausted the decoder stack")

    def recursive_json_loads(value: str) -> Any:
        assert value == body.decode("utf-8")
        raise decode_error

    monkeypatch.setattr(client_module.json, "loads", recursive_json_loads)
    transport = RecordingTransport(
        [HttpResponse(status=status, headers={"x-request-id": request_id}, body=body)]
    )

    with pytest.raises(SupervisorRemoteError) as raised:
        SupervisorClient(transport).get_job("job-1")

    error = raised.value
    assert type(error) is SupervisorRemoteError
    assert error.code == "INVALID_ERROR_RESPONSE"
    assert error.message == "Supervisor returned an invalid error response"
    assert error.status == status
    assert error.request_id == request_id

def test_post_failure_is_not_retried_and_keeps_its_idempotency_key() -> None:
    transport = RecordingTransport(
        [
            response(
                503,
                {"code": "SERVICE_UNAVAILABLE", "message": "control plane unavailable"},
            )
        ]
    )

    with pytest.raises(SupervisorRemoteError):
        SupervisorClient(transport).submit_job(
            workflow_id="2026-07-30-login-contract",
            requested_target=RequestedTarget.AUTO,
            repo_refs=[RepoRef(repo="blip-server", base="main")],
            required_capabilities=["git", "omp"],
            prompt="Fix the login contract.\n",
            idempotency_key=SUPPLIED_KEY,
        )

    assert len(transport.calls) == 1
    assert transport.calls[0].headers == {"Idempotency-Key": SUPPLIED_KEY}


def test_sigv4_transport_signs_tls_requests_with_compact_json_and_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen_credentials = object()
    session_profiles: List[Optional[str]] = []
    signer_arguments: List[Tuple[object, str, str]] = []
    signed_requests: List[Any] = []
    prepared_requests: List[Tuple[Any, int]] = []

    class Credentials:
        def get_frozen_credentials(self) -> object:
            return frozen_credentials

    class Session:
        def __init__(self, profile: Optional[str] = None) -> None:
            session_profiles.append(profile)

        def get_credentials(self) -> Credentials:
            return Credentials()

    class RecordingSigV4Auth:
        def __init__(self, credentials: object, service: str, region: str) -> None:
            signer_arguments.append((credentials, service, region))

        def add_auth(self, request: Any) -> None:
            signed_requests.append(request)
            request.headers["Authorization"] = "AWS4-HMAC-SHA256 test-signature"

    def fake_send_prepared(prepared: Any, *, timeout: int) -> HttpResponse:
        prepared_requests.append((prepared, timeout))
        return response(200, {"ok": True})

    monkeypatch.setattr(client_module.botocore.session, "Session", Session)
    monkeypatch.setattr(client_module, "SigV4Auth", RecordingSigV4Auth)
    monkeypatch.setattr(client_module, "_send_prepared", fake_send_prepared)

    transport = SigV4Transport(supervisor_config())
    result = transport.request(
        "POST",
        "/v1/admin/jobs",
        payload={"schema_version": 1, "prompt": "hello"},
        headers={"Idempotency-Key": SUPPLIED_KEY},
    )

    request = signed_requests[0]
    prepared, timeout = prepared_requests[0]
    assert result == response(200, {"ok": True})
    assert session_profiles == ["team-profile"]
    assert signer_arguments == [(frozen_credentials, "execute-api", "ap-northeast-2")]
    assert (
        request.url
        == "https://abc123.execute-api.ap-northeast-2.amazonaws.com/v1/admin/jobs"
    )
    assert request.data == b'{"schema_version":1,"prompt":"hello"}'
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Idempotency-Key"] == SUPPLIED_KEY
    assert (
        prepared.url
        == "https://abc123.execute-api.ap-northeast-2.amazonaws.com/v1/admin/jobs"
    )
    assert prepared.body == b'{"schema_version":1,"prompt":"hello"}'
    assert prepared.headers["Authorization"] == "AWS4-HMAC-SHA256 test-signature"
    assert timeout == 30


def test_sigv4_transport_requires_available_aws_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_profiles: List[Optional[str]] = []

    class MissingCredentialsSession:
        def __init__(self, profile: Optional[str] = None) -> None:
            session_profiles.append(profile)

        def get_credentials(self) -> None:
            return None

    monkeypatch.setattr(
        client_module.botocore.session,
        "Session",
        MissingCredentialsSession,
    )

    with pytest.raises(SupervisorAuthRequired, match="AWS SSO credentials are unavailable"):
        SigV4Transport(supervisor_config())

    assert session_profiles == ["team-profile"]

def test_sigv4_transport_maps_session_construction_errors_to_auth_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_profiles: List[Optional[str]] = []
    session_error = ProfileNotFound(profile="team-profile")

    class FailingSession:
        def __init__(self, profile: Optional[str] = None) -> None:
            session_profiles.append(profile)
            raise session_error

    monkeypatch.setattr(client_module.botocore.session, "Session", FailingSession)

    with pytest.raises(SupervisorAuthRequired, match="AWS SSO credentials are unavailable") as raised:
        SigV4Transport(supervisor_config())

    assert raised.value.__cause__ is session_error
    assert session_profiles == ["team-profile"]


def test_sigv4_transport_maps_initial_credential_resolution_errors_to_auth_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_profiles: List[Optional[str]] = []
    credential_error = ProfileNotFound(profile="team-profile")

    class FailingCredentialSession:
        def __init__(self, profile: Optional[str] = None) -> None:
            session_profiles.append(profile)

        def get_credentials(self) -> None:
            raise credential_error

    monkeypatch.setattr(
        client_module.botocore.session,
        "Session",
        FailingCredentialSession,
    )

    with pytest.raises(SupervisorAuthRequired, match="AWS SSO credentials are unavailable") as raised:
        SigV4Transport(supervisor_config())

    assert raised.value.__cause__ is credential_error
    assert session_profiles == ["team-profile"]


def test_sigv4_transport_maps_frozen_credential_errors_to_auth_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_profiles: List[Optional[str]] = []
    credential_error = CredentialRetrievalError(
        provider="sso",
        error_msg="the cached session expired",
    )

    class FailingCredentials:
        def get_frozen_credentials(self) -> object:
            raise credential_error

    class Session:
        def __init__(self, profile: Optional[str] = None) -> None:
            session_profiles.append(profile)

        def get_credentials(self) -> FailingCredentials:
            return FailingCredentials()

    monkeypatch.setattr(client_module.botocore.session, "Session", Session)

    transport = SigV4Transport(supervisor_config())

    with pytest.raises(SupervisorAuthRequired) as raised:
        transport.request("GET", "/v1/admin/jobs")

    assert raised.value.__cause__ is credential_error
    assert session_profiles == ["team-profile"]


@pytest.mark.parametrize(
    ("response_kind", "failure_type"),
    [
        pytest.param("response", TimeoutError, id="response-timeout"),
        pytest.param("http-error", ConnectionResetError, id="http-error-reset"),
    ],
)
def test_send_prepared_maps_body_read_failures_to_single_network_error(
    monkeypatch: pytest.MonkeyPatch,
    response_kind: str,
    failure_type: Type[OSError],
) -> None:
    attempts: List[Tuple[str, int]] = []
    read_amounts: List[int] = []
    read_failure = failure_type("body read failed")

    class ReadFailureResponse:
        headers = {"x-request-id": "response-read-failure"}

        def __enter__(self) -> "ReadFailureResponse":
            return self

        def __exit__(
            self,
            exception_type: Any,
            exception: Any,
            traceback: Any,
        ) -> None:
            return None

        def getcode(self) -> int:
            return 503

        def read(self, amount: int) -> bytes:
            read_amounts.append(amount)
            raise read_failure

    class ReadFailureHTTPError(client_module.urllib_error.HTTPError):
        def __init__(self) -> None:
            super().__init__(
                "https://abc123.execute-api.ap-northeast-2.amazonaws.com/v1/admin/jobs",
                503,
                "Service Unavailable",
                {"x-request-id": "http-error-read-failure"},
                None,
            )

        def read(self, amount: int) -> bytes:
            read_amounts.append(amount)
            raise read_failure

    class ReadFailureOpener:
        def open(self, request: Any, timeout: int) -> ReadFailureResponse:
            attempts.append((request.full_url, timeout))
            if response_kind == "http-error":
                raise ReadFailureHTTPError()
            return ReadFailureResponse()

    monkeypatch.setattr(
        client_module.urllib_request,
        "build_opener",
        lambda *handlers: ReadFailureOpener(),
    )
    prepared = client_module.AWSRequest(
        method="GET",
        url="https://abc123.execute-api.ap-northeast-2.amazonaws.com/v1/admin/jobs",
    ).prepare()

    with pytest.raises(SupervisorRemoteError) as raised:
        client_module._send_prepared(prepared, timeout=30)

    error = raised.value
    assert error.status == 0
    assert error.code == "NETWORK_ERROR"
    assert error.message == "Supervisor API request failed"
    assert len(attempts) == 1
    assert read_amounts == [MEBIBYTE + 1]


def test_send_prepared_returns_redirect_without_replaying_signed_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = "https://api.example/v1/admin/jobs"
    redirect_target = "https://redirected.example/collect"
    prompt_body = b'{"prompt":"do not replay this signed body"}'
    authorization = "AWS4-HMAC-SHA256 Credential=test-access-key/test-signature"
    prepared = client_module.AWSRequest(
        method="POST",
        url=origin,
        data=prompt_body,
        headers={
            "Authorization": authorization,
            "Idempotency-Key": SUPPLIED_KEY,
            "Content-Type": "application/json",
        },
    ).prepare()
    dispatches: List[Tuple[str, str, Mapping[str, str], bytes]] = []
    opener_builds: List[Tuple[Any, ...]] = []

    class FakeResponse:
        def __init__(
            self,
            status: int,
            headers: Mapping[str, str],
            body: bytes,
        ) -> None:
            self._status = status
            self.headers = headers
            self._body = body

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(
            self,
            exception_type: Any,
            exception: Any,
            traceback: Any,
        ) -> None:
            return None

        def getcode(self) -> int:
            return self._status

        def read(self, amount: int) -> bytes:
            return self._body

    def record_dispatch(request: Any) -> None:
        dispatches.append(
            (
                request.get_method(),
                request.full_url,
                {
                    name.lower(): value
                    for name, value in request.header_items()
                },
                request.data,
            )
        )

    def initial_redirect() -> FakeResponse:
        return FakeResponse(
            302,
            {"Location": redirect_target, "x-request-id": "redirect-123"},
            b'{"code":"REDIRECT","message":"do not follow"}',
        )

    def follow_if_allowed(request: Any, redirect_handler: Any) -> FakeResponse:
        redirect = initial_redirect()
        redirected_request = redirect_handler.redirect_request(
            request,
            redirect,
            302,
            "Found",
            redirect.headers,
            redirect_target,
        )
        if redirected_request is None:
            return redirect
        record_dispatch(redirected_request)
        return FakeResponse(200, {"x-request-id": "target-123"}, b'{"ok":true}')

    class RedirectAwareOpener:
        def __init__(self, redirect_handler: Any) -> None:
            self._redirect_handler = redirect_handler

        def open(self, request: Any, timeout: int) -> FakeResponse:
            assert timeout == 30
            record_dispatch(request)
            return follow_if_allowed(request, self._redirect_handler)

    def redirect_handler_from(handlers: Tuple[Any, ...]) -> Any:
        for handler in handlers:
            if isinstance(handler, client_module.urllib_request.HTTPRedirectHandler):
                return handler
            if (
                isinstance(handler, type)
                and issubclass(
                    handler,
                    client_module.urllib_request.HTTPRedirectHandler,
                )
            ):
                return handler()
        return client_module.urllib_request.HTTPRedirectHandler()

    def build_no_redirect_opener(*handlers: Any) -> RedirectAwareOpener:
        opener_builds.append(handlers)
        return RedirectAwareOpener(redirect_handler_from(handlers))

    def following_urlopen(request: Any, *, timeout: int) -> FakeResponse:
        assert timeout == 30
        record_dispatch(request)
        return follow_if_allowed(
            request,
            client_module.urllib_request.HTTPRedirectHandler(),
        )

    monkeypatch.setattr(
        client_module.urllib_request,
        "build_opener",
        build_no_redirect_opener,
    )
    monkeypatch.setattr(client_module.urllib_request, "urlopen", following_urlopen)

    result = client_module._send_prepared(prepared, timeout=30)

    assert result == HttpResponse(
        status=302,
        headers={"Location": redirect_target, "x-request-id": "redirect-123"},
        body=b'{"code":"REDIRECT","message":"do not follow"}',
    )
    assert len(opener_builds) == 1
    assert len(dispatches) == 1
    method, dispatched_url, dispatched_headers, dispatched_body = dispatches[0]
    assert method == "POST"
    assert dispatched_url == origin
    assert dispatched_headers["authorization"] == authorization
    assert dispatched_headers["idempotency-key"] == SUPPLIED_KEY
    assert dispatched_body == prompt_body
    assert not any(
        dispatch[1] == redirect_target
        and (
            dispatch[2].get("authorization") == authorization
            or dispatch[2].get("idempotency-key") == SUPPLIED_KEY
            or dispatch[3] == prompt_body
        )
        for dispatch in dispatches
    )
