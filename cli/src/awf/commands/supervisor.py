"""User-facing commands for the AWF Supervisor API."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Iterable, List, Optional

from awf.supervisor.client import (
    RepoRef,
    SigV4Transport,
    SupervisorAuthRequired,
    SupervisorClient,
    SupervisorConflict,
    SupervisorRemoteError,
)
from awf.supervisor.config import load_supervisor_config
from awf.supervisor.contracts import (
    JobState,
    RequestedTarget,
    SupervisorAgent,
    SupervisorJob,
)


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_CONFLICT = 4
EXIT_REMOTE = 5

_PROMPT_LIMIT_BYTES = 64 * 1024
_CAPABILITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ACTIVE_WATCH_STATES = frozenset(
    {
        JobState.QUEUED,
        JobState.CLAIMED,
        JobState.PREPARING,
        JobState.RUNNING,
    }
)


def parse_positive_generation(value: str) -> int:
    """Return an argparse-validated, strictly positive generation."""
    try:
        generation = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("generation must be a positive integer") from exc
    if generation <= 0:
        raise argparse.ArgumentTypeError("generation must be a positive integer")
    return generation


def parse_watch_interval(value: str) -> int:
    """Return an argparse-validated polling interval in seconds."""
    try:
        interval = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("interval must be an integer from 1 to 60") from exc
    if not 1 <= interval <= 60:
        raise argparse.ArgumentTypeError("interval must be an integer from 1 to 60")
    return interval


def parse_harness_idempotency_key(value: str) -> str:
    """Validate the deterministic replay key reserved for the E2E harness."""
    if os.environ.get("AWF_SUPERVISOR_E2E_HARNESS") != "1":
        raise argparse.ArgumentTypeError(
            "--idempotency-key requires AWF_SUPERVISOR_E2E_HARNESS=1"
        )
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "--idempotency-key must be a canonical lowercase UUID4"
        ) from exc
    if parsed.version != 4 or str(parsed) != value:
        raise argparse.ArgumentTypeError(
            "--idempotency-key must be a canonical lowercase UUID4"
        )
    return value


def run_supervisor_submit(args: argparse.Namespace) -> int:
    try:
        _validate_identifier(args.workflow_id, "workflow_id")
        prompt = _load_prompt(args)
        repo_refs = _parse_repo_refs(args.repo)
        required_capabilities = _required_capabilities(args.require_capability)
        job = _client(args).submit_job(
            workflow_id=args.workflow_id,
            requested_target=RequestedTarget(args.target),
            repo_refs=repo_refs,
            required_capabilities=required_capabilities,
            prompt=prompt,
            idempotency_key=args.idempotency_key,
        )
    except SupervisorAuthRequired as exc:
        return _print_error(exc, EXIT_AUTH)
    except SupervisorConflict as exc:
        return _print_error(exc, EXIT_CONFLICT)
    except (OSError, ValueError, SupervisorRemoteError) as exc:
        return _print_error(exc, EXIT_REMOTE)
    _print_job(job, as_json=bool(args.json))
    return EXIT_OK


def run_supervisor_status(args: argparse.Namespace) -> int:
    try:
        _validate_identifier(args.job_id, "job_id")
        job = _client(args).get_job(args.job_id)
    except SupervisorAuthRequired as exc:
        return _print_error(exc, EXIT_AUTH)
    except SupervisorConflict as exc:
        return _print_error(exc, EXIT_CONFLICT)
    except (OSError, ValueError, SupervisorRemoteError) as exc:
        return _print_error(exc, EXIT_REMOTE)
    _print_job(job, as_json=bool(args.json))
    return EXIT_OK


def run_supervisor_watch(args: argparse.Namespace) -> int:
    try:
        _validate_identifier(args.job_id, "job_id")
        interval = _resolve_watch_interval(args)
        client = _client(args)
        previous = None
        while True:
            job = client.get_job(args.job_id)
            current = (job.state, job.generation, job.updated_at)
            if current != previous:
                _print_job(job, as_json=bool(args.json))
                previous = current
            if job.state not in _ACTIVE_WATCH_STATES:
                return EXIT_OK
            time.sleep(interval)
    except KeyboardInterrupt:
        return 130
    except SupervisorAuthRequired as exc:
        return _print_error(exc, EXIT_AUTH)
    except SupervisorConflict as exc:
        return _print_error(exc, EXIT_CONFLICT)
    except (OSError, ValueError, SupervisorRemoteError) as exc:
        return _print_error(exc, EXIT_REMOTE)


def run_supervisor_cancel(args: argparse.Namespace) -> int:
    try:
        _validate_identifier(args.job_id, "job_id")
        job = _client(args).cancel_job(args.job_id, generation=args.generation)
    except SupervisorAuthRequired as exc:
        return _print_error(exc, EXIT_AUTH)
    except SupervisorConflict as exc:
        return _print_error(exc, EXIT_CONFLICT)
    except (OSError, ValueError, SupervisorRemoteError) as exc:
        return _print_error(exc, EXIT_REMOTE)
    _print_job(job, as_json=bool(args.json))
    return EXIT_OK


def run_supervisor_approve(args: argparse.Namespace) -> int:
    try:
        _validate_identifier(args.job_id, "job_id")
        job = _client(args).approve_job(args.job_id, generation=args.generation)
    except SupervisorAuthRequired as exc:
        return _print_error(exc, EXIT_AUTH)
    except SupervisorConflict as exc:
        return _print_error(exc, EXIT_CONFLICT)
    except (OSError, ValueError, SupervisorRemoteError) as exc:
        return _print_error(exc, EXIT_REMOTE)
    _print_job(job, as_json=bool(args.json))
    return EXIT_OK


def run_supervisor_reject(args: argparse.Namespace) -> int:
    try:
        _validate_identifier(args.job_id, "job_id")
        job = _client(args).reject_job(args.job_id, generation=args.generation)
    except SupervisorAuthRequired as exc:
        return _print_error(exc, EXIT_AUTH)
    except SupervisorConflict as exc:
        return _print_error(exc, EXIT_CONFLICT)
    except (OSError, ValueError, SupervisorRemoteError) as exc:
        return _print_error(exc, EXIT_REMOTE)
    _print_job(job, as_json=bool(args.json))
    return EXIT_OK


def run_supervisor_agents(args: argparse.Namespace) -> int:
    try:
        agents = _client(args).list_agents()
    except SupervisorAuthRequired as exc:
        return _print_error(exc, EXIT_AUTH)
    except SupervisorConflict as exc:
        return _print_error(exc, EXIT_CONFLICT)
    except (OSError, ValueError, SupervisorRemoteError) as exc:
        return _print_error(exc, EXIT_REMOTE)
    for agent in agents:
        _print_agent(agent, as_json=bool(args.json))
    return EXIT_OK


def _client(args: argparse.Namespace) -> SupervisorClient:
    del args
    config = load_supervisor_config()
    return SupervisorClient(SigV4Transport(config))


def _validate_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid {}".format(field))


def _resolve_watch_interval(args: argparse.Namespace) -> int:
    if args.interval is not None:
        return args.interval
    return load_supervisor_config().poll_interval_seconds


def _load_prompt(args: argparse.Namespace) -> str:
    prompt = args.prompt
    if prompt is not None:
        if not isinstance(prompt, str):
            raise ValueError("prompt must be a string")
        if len(prompt.encode("utf-8")) > _PROMPT_LIMIT_BYTES:
            raise ValueError("prompt must not exceed 64 KiB")
    else:
        prompt_path = Path(args.prompt_file)
        if not prompt_path.is_file():
            raise ValueError("prompt file must be a regular file")
        with prompt_path.open("rb") as prompt_file:
            contents = prompt_file.read(_PROMPT_LIMIT_BYTES + 1)
        if not isinstance(contents, bytes):
            raise ValueError("prompt file must contain bytes")
        if len(contents) > _PROMPT_LIMIT_BYTES:
            raise ValueError("prompt must not exceed 64 KiB")
        prompt = contents.decode("utf-8")
    if not prompt:
        raise ValueError("prompt must not be empty")
    return prompt


def _parse_repo_ref(value: str) -> RepoRef:
    repo, separator, base = value.partition(":")
    if not separator or not repo or not base:
        raise ValueError("repo must use REPO:BASE format")
    return RepoRef(repo=repo, base=base)


def _parse_repo_refs(values: Iterable[str]) -> List[RepoRef]:
    repo_refs = []
    repo_names = set()
    for value in values:
        repo_ref = _parse_repo_ref(value)
        if repo_ref.repo in repo_names:
            raise ValueError("repo_refs contains duplicate repo names")
        repo_names.add(repo_ref.repo)
        repo_refs.append(repo_ref)
    return repo_refs


def _required_capabilities(values: Iterable[str]) -> List[str]:
    capabilities = ["git", "omp"]
    for value in values:
        if _CAPABILITY_PATTERN.fullmatch(value) is None:
            raise ValueError("invalid required capability")
        if value in capabilities:
            raise ValueError("duplicate required capability")
        capabilities.append(value)
    return capabilities


def _print_job(job: SupervisorJob, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(job.to_dict(), ensure_ascii=False, separators=(",", ":")))
        return
    print(
        "job_id={} workflow_id={} state={} desired_state={} generation={} "
        "requested_target={}".format(
            job.job_id,
            job.workflow_id,
            job.state.value,
            job.desired_state,
            job.generation,
            job.requested_target.value,
        )
    )


def _print_agent(agent: SupervisorAgent, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(agent.to_dict(), ensure_ascii=False, separators=(",", ":")))
        return
    print(
        "agent_id={} environment={} status={} active_jobs={} max_concurrency={} "
        "capabilities={} repos={}".format(
            agent.agent_id,
            agent.environment.value,
            agent.status.value,
            agent.active_jobs,
            agent.max_concurrency,
            ",".join(agent.capabilities),
            ",".join(agent.repos),
        )
    )


def _print_error(exc: BaseException, exit_code: int) -> int:
    print("error: {}".format(exc), file=sys.stderr)
    return exit_code
