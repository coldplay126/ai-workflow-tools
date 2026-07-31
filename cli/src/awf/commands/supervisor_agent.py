"""CLI composition for the durable local and AWS Supervisor agent runtime."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

import botocore.session

from awf.supervisor.agent import SupervisorAgentRuntime
from awf.supervisor.client import HttpResponse, SigV4Transport
from awf.supervisor.config import SupervisorConfig, load_supervisor_config
from awf.supervisor.contracts import AgentEnvironment
from awf.supervisor.credentials import AccessTokenBroker, MacOSKeychainCredentialStore
from awf.supervisor.executor import SupervisorJobExecutor
from awf.supervisor.idle_status import inspect_idle_status
from awf.supervisor.install import LaunchdInstallError, install_launchd, uninstall_launchd
from awf.supervisor.runtime_paths import RuntimePathError, RuntimePaths, resolve_runtime_paths
from awf.supervisor.store import SupervisorStore
from awf.supervisor.transport import (
    AwsSqsCommandSource,
    BrokerBearerTransport,
    HttpLeaseApi,
    LocalHttpCommandSource,
)
from awf.supervisor.workspace import AgentctlWorkspaceAdapter, LocalGitWorkspaceAdapter


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BUSY = 3
EXIT_UNKNOWN = 4
EXIT_REMOTE = 5
_SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class _UrlHttpTransport:
    """Minimal unsigned HTTP port used before local bearer attachment."""

    def __init__(self, *, config: SupervisorConfig) -> None:
        if not config.api_url:
            raise ValueError("Supervisor API URL is required")
        self._base_url = config.api_url.rstrip("/")
        self._timeout = config.request_timeout_seconds

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> HttpResponse:
        if not isinstance(method, str) or not method:
            raise ValueError("HTTP method is required")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("HTTP path must be absolute")
        body = None
        request_headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        request = urllib_request.Request(
            self._base_url + path, data=body, headers=request_headers, method=method
        )
        try:
            with urllib_request.urlopen(request, timeout=self._timeout) as response:
                return HttpResponse(
                    status=response.getcode(),
                    headers={key: value for key, value in response.headers.items()},
                    body=response.read(),
                )
        except urllib_error.HTTPError as error:
            return HttpResponse(
                status=error.code,
                headers={key: value for key, value in error.headers.items()},
                body=error.read(),
            )


def run_supervisor_agent_enroll(args: argparse.Namespace) -> int:
    """Enroll a local agent through the IAM admin route and retain only its secret."""

    try:
        agent_id = _agent_id(args.agent_id)
    except ValueError as error:
        return _agent_error(str(error), EXIT_USAGE, as_json=bool(args.json))
    try:
        response = SigV4Transport(load_supervisor_config()).request(
            "POST", "/v1/admin/agents/enroll", payload={"agent_id": agent_id}
        )
        if response.status != 201:
            raise ValueError("agent enrollment was rejected")
        payload = _response_object(response)
        refresh_token = payload.get("refresh_token")
        if payload.get("agent_id") != agent_id or not isinstance(refresh_token, str) or not refresh_token:
            raise ValueError("agent enrollment returned an invalid response")
        MacOSKeychainCredentialStore().save_refresh_token(agent_id, refresh_token)
    except Exception:
        return _agent_error("enrollment failed", EXIT_REMOTE, as_json=bool(args.json))

    result = {"schema_version": _SCHEMA_VERSION, "agent_id": agent_id, "status": "enrolled"}
    if args.json:
        _print_result(result, as_json=True)
    else:
        print("agent_id={} status=enrolled".format(agent_id))
    return EXIT_OK


def run_supervisor_agent_run(args: argparse.Namespace) -> int:
    """Compose and start the real runtime; no agent command is a no-op."""

    try:
        runtime = _build_runtime(args)
        return runtime.run()
    except RuntimePathError:
        return _agent_error("runtime paths are unavailable", EXIT_UNKNOWN, as_json=False)
    except ValueError as error:
        return _agent_error(str(error), EXIT_USAGE, as_json=False)
    except Exception:
        return _agent_error("agent runtime could not start", EXIT_REMOTE, as_json=False)


def run_supervisor_agent_doctor(args: argparse.Namespace) -> int:
    """Verify only non-secret local prerequisites and report a stable health object."""

    try:
        agent_id = _agent_id(args.agent_id)
        environment = _environment(args.environment)
    except ValueError:
        return _doctor_result(args, status="unknown", exit_code=EXIT_USAGE)
    try:
        paths = _resolve_paths(args, environment)
        checks = _doctor_checks(
            agent_id=agent_id, environment=environment, paths=paths
        )
    except (RuntimePathError, OSError, ValueError):
        return _doctor_result(args, status="unknown", exit_code=EXIT_UNKNOWN)
    except Exception:
        return _doctor_result(args, status="unknown", exit_code=EXIT_UNKNOWN)

    result = {
        "schema_version": _SCHEMA_VERSION,
        "agent_id": agent_id,
        "environment": environment.value,
        "status": "ok",
        "checks": checks,
    }
    _print_result(result, as_json=bool(args.json))
    return EXIT_OK


def run_supervisor_agent_idle_status(args: argparse.Namespace) -> int:
    """Report only safe, busy, or unknown from the shared state and marker paths."""

    environment_value = getattr(args, "environment", None)
    try:
        environment = _environment(environment_value)
    except ValueError:
        status = "unknown"
        exit_code = EXIT_USAGE
    else:
        try:
            paths = _resolve_paths(args, environment)
            inspected = inspect_idle_status(paths)
            status = inspected.state.value
            exit_code = inspected.exit_code
        except Exception:
            status = "unknown"
            exit_code = EXIT_UNKNOWN

    result = {
        "schema_version": _SCHEMA_VERSION,
        "environment": environment_value if environment_value in ("local", "aws") else "unknown",
        "status": status,
    }
    _print_result(result, as_json=bool(args.json))
    return exit_code


def run_supervisor_agent_install_launchd(args: argparse.Namespace) -> int:
    """Install the local agent only through the validated atomic launchd installer."""

    try:
        agent_id = _agent_id(args.agent_id)
    except ValueError as error:
        return _agent_error(str(error), EXIT_USAGE, as_json=False)
    try:
        paths = _resolve_paths(args, AgentEnvironment.LOCAL)
        plist_path = install_launchd(agent_id=agent_id, repo_root=paths.repo_root)
    except LaunchdInstallError as error:
        return _agent_error(str(error), EXIT_REMOTE, as_json=False)
    except (RuntimePathError, ValueError):
        return _agent_error("launchd installation failed", EXIT_REMOTE, as_json=False)
    print("installed: {}".format(plist_path))
    return EXIT_OK


def run_supervisor_agent_uninstall_launchd(args: argparse.Namespace) -> int:
    """Remove only this user's validated Supervisor launchd service."""

    try:
        _agent_id(args.agent_id)
    except ValueError as error:
        return _agent_error(str(error), EXIT_USAGE, as_json=False)
    try:
        uninstall_launchd()
    except LaunchdInstallError as error:
        return _agent_error(str(error), EXIT_REMOTE, as_json=False)
    except ValueError:
        return _agent_error("launchd uninstall failed", EXIT_REMOTE, as_json=False)
    print("uninstalled: com.awf.supervisor-agent")
    return EXIT_OK


def _build_runtime(args: argparse.Namespace) -> SupervisorAgentRuntime:
    agent_id = _agent_id(args.agent_id)
    environment = _environment(args.environment)
    transport = _transport(args.transport)
    _validate_transport(environment, transport)
    paths = _resolve_paths(args, environment)
    store = SupervisorStore(paths.store_path)
    config = load_supervisor_config()

    if environment is AgentEnvironment.LOCAL:
        workspace = LocalGitWorkspaceAdapter(
            github_root=paths.repo_root, state_root=paths.state_root
        )
        http = _UrlHttpTransport(config=config)
        broker = AccessTokenBroker(MacOSKeychainCredentialStore(), http)
        authenticated = BrokerBearerTransport(
            http=http, token_broker=broker, agent_id=agent_id
        )
        source = LocalHttpCommandSource(transport=authenticated)
        lease_api = HttpLeaseApi(transport=authenticated, environment=environment)
    else:
        workspace = AgentctlWorkspaceAdapter(
            repo_root=paths.repo_root, workspace_root=paths.repo_root.parent
        )
        authenticated = SigV4Transport(config)
        source = _aws_command_source(config)
        lease_api = HttpLeaseApi(transport=authenticated, environment=environment)

    executor = SupervisorJobExecutor(
        lease_api=lease_api,
        store=store,
        workspace=workspace,
        environment=environment.value,
    )
    return SupervisorAgentRuntime(
        paths=paths,
        store=store,
        workspace=workspace,
        executor=executor,
        source=source,
        lease_api=lease_api,
        agent_id=agent_id,
        environment=environment,
        version={"schema_version": str(_SCHEMA_VERSION), "runtime": "awf-supervisor-agent"},
    )


def _aws_command_source(config: SupervisorConfig) -> AwsSqsCommandSource:
    queue_url = os.environ.get("AWF_SUPERVISOR_SQS_QUEUE_URL")
    if not queue_url:
        raise ValueError("AWF_SUPERVISOR_SQS_QUEUE_URL is required for aws agents")
    raw_visibility = os.environ.get("AWF_SUPERVISOR_SQS_VISIBILITY_TIMEOUT_SECONDS", "60")
    try:
        visibility = int(raw_visibility)
    except ValueError as error:
        raise ValueError("AWS SQS visibility timeout is invalid") from error
    session = botocore.session.get_session()
    if config.profile:
        session.set_config_variable("profile", config.profile)
    return AwsSqsCommandSource(
        sqs_client=session.create_client("sqs", region_name=config.region),
        queue_url=queue_url,
        visibility_timeout_seconds=visibility,
    )


def _doctor_checks(
    *, agent_id: str, environment: AgentEnvironment, paths: RuntimePaths
) -> Dict[str, str]:
    repo_root = paths.repo_root.resolve(strict=True)
    if not repo_root.is_dir() or not os.access(str(repo_root), os.R_OK | os.X_OK):
        raise OSError("repository root is not readable")
    # Force a filesystem read so a directory with deceptive mode bits fails doctor.
    next(repo_root.iterdir(), None)
    config = load_supervisor_config()
    if environment is AgentEnvironment.LOCAL:
        _UrlHttpTransport(config=config)
        MacOSKeychainCredentialStore().load_refresh_token(agent_id)
    else:
        _aws_command_source(config)
    return {"runtime": "ready"}


def _resolve_paths(args: argparse.Namespace, environment: AgentEnvironment) -> RuntimePaths:
    return resolve_runtime_paths(
        environment=environment.value,
        state_dir=getattr(args, "state_dir", None),
        active_lease_path=getattr(args, "active_lease_path", None),
        repo_root=getattr(args, "repo_root", None),
    )


def _agent_id(value: Any) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("invalid agent ID")
    return value


def _environment(value: Any) -> AgentEnvironment:
    try:
        return AgentEnvironment(value)
    except ValueError as error:
        raise ValueError("environment must be local or aws") from error


def _transport(value: Any) -> str:
    if value not in ("http", "sqs"):
        raise ValueError("transport must be http or sqs")
    return value


def _validate_transport(environment: AgentEnvironment, transport: str) -> None:
    expected = "http" if environment is AgentEnvironment.LOCAL else "sqs"
    if transport != expected:
        raise ValueError("{} agents require {} transport".format(environment.value, expected))


def _response_object(response: HttpResponse) -> Dict[str, Any]:
    try:
        value = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("server returned invalid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("server returned invalid JSON")
    return value


def _doctor_result(args: argparse.Namespace, *, status: str, exit_code: int) -> int:
    agent_id = getattr(args, "agent_id", "unknown")
    environment = getattr(args, "environment", "unknown")
    result = {
        "schema_version": _SCHEMA_VERSION,
        "agent_id": agent_id if isinstance(agent_id, str) else "unknown",
        "environment": environment if environment in ("local", "aws") else "unknown",
        "status": status,
        "checks": {},
    }
    _print_result(result, as_json=bool(getattr(args, "json", False)))
    return exit_code


def _print_result(result: Dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return
    print(" ".join("{}={}".format(key, value) for key, value in result.items()))


def _agent_error(message: str, exit_code: int, *, as_json: bool) -> int:
    if as_json:
        print(
            json.dumps(
                {"schema_version": _SCHEMA_VERSION, "status": "unknown", "error": message},
                separators=(",", ":"),
            )
        )
    else:
        print("error: {}".format(message), file=sys.stderr)
    return exit_code


__all__ = [
    "run_supervisor_agent_doctor",
    "run_supervisor_agent_enroll",
    "run_supervisor_agent_idle_status",
    "run_supervisor_agent_install_launchd",
    "run_supervisor_agent_run",
    "run_supervisor_agent_uninstall_launchd",
]
