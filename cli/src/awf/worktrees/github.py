from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_GH_VIEW_FIELDS = (
    "number,state,baseRefName,baseRefOid,headRefName,headRefOid,mergeCommit,"
    "reviewDecision,statusCheckRollup,files,url"
)
_ALLOWED_CHECK_CONCLUSIONS = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
_MAX_CHANGED_PATHS = 256
_MAX_CHANGED_PATH_BYTES = 1024
_MAX_FIELD_BYTES = 2048
_MAX_STDERR_BYTES = 512
_HTTP_URL_USERINFO = re.compile(r"(?P<scheme>https?://)(?P<userinfo>[^/@\s]*@)", re.IGNORECASE)
_SECRET = re.compile(
    r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|bearer\s+\S+|token\s+\S+)"
)
_PULL_REQUEST_URL = re.compile(r"/pull/(\d+)(?:[/?#].*)?$")


class ExternalServiceError(RuntimeError):
    """Raised when an external GitHub operation cannot complete safely."""


@dataclass(frozen=True)
class PullRequest:
    number: int
    state: str
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    merge_commit_sha: str | None
    review_decision: str
    checks_passed: bool
    changed_paths: tuple[str, ...]
    url: str


class GhClient:
    def __init__(
        self,
        cwd: Path,
        *,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        timeout: float = 30.0,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.cwd = cwd.resolve()
        self.command_runner = command_runner or subprocess.run
        self.timeout = timeout

    def view_pr(self, number: int) -> PullRequest:
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise ValueError("pull request number must be a positive integer")
        completed = self._run("pr", "view", str(number), "--json", _GH_VIEW_FIELDS)
        return _pull_request_from_json(completed.stdout)

    def find_open_pr(self, *, head: str, base: str) -> PullRequest | None:
        if not isinstance(head, str) or not head or not isinstance(base, str) or not base:
            raise ValueError("pull request head and base must be non-empty strings")
        completed = self._run(
            "pr",
            "list",
            "--state",
            "open",
            "--base",
            base,
            "--head",
            head,
            "--json",
            _GH_VIEW_FIELDS,
        )
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as error:
            raise ExternalServiceError("gh pr list returned malformed JSON") from error
        if not isinstance(payload, list):
            raise ExternalServiceError("gh pr list returned an invalid pull request list")
        matches = tuple(
            pull_request
            for item in payload
            if isinstance(item, dict)
            and (pull_request := _pull_request_from_json(json.dumps(item))).state == "OPEN"
            and pull_request.head_ref == head
            and pull_request.base_ref == base
        )
        if len(matches) > 1:
            raise ExternalServiceError(
                "gh pr list returned multiple open pull requests for the branch"
            )
        return matches[0] if matches else None

    def create_pr(
        self, *, base: str, head: str, title: str, body: str
    ) -> PullRequest:
        completed = self._run(
            "pr", "create", "--base", base, "--head", head, "--title", title, "--body", body
        )
        output = completed.stdout if isinstance(completed.stdout, str) else ""
        match = _PULL_REQUEST_URL.search(output.strip())
        if match is None:
            raise ExternalServiceError("gh pr create returned an invalid pull request URL")
        return self.view_pr(int(match.group(1)))

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        command = ["gh", *args]
        operation = " ".join(args[:2]) or "command"
        try:
            completed = self.command_runner(
                command,
                cwd=str(self.cwd),
                check=False,
                shell=False,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ExternalServiceError(f"gh {operation} failed to launch") from error
        if completed.returncode != 0:
            detail = _bounded_stderr(completed.stderr)
            suffix = f": {detail}" if detail else ""
            raise ExternalServiceError(
                f"gh {operation} failed ({completed.returncode}){suffix}"
            )
        return completed


def _pull_request_from_json(value: str) -> PullRequest:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise ExternalServiceError("gh pr view returned malformed JSON") from error
    if not isinstance(payload, Mapping):
        raise ExternalServiceError("gh pr view returned an invalid pull request")
    try:
        number = _positive_int(payload, "number")
        state = _required_string(payload, "state").upper()
        base_ref = _required_string(payload, "baseRefName")
        base_sha = _required_string(payload, "baseRefOid")
        head_ref = _required_string(payload, "headRefName")
        head_sha = _required_string(payload, "headRefOid")
        review_decision = _optional_string(payload.get("reviewDecision")).upper()
        merge_commit_sha = _merge_commit_sha(payload.get("mergeCommit"))
        checks_passed = _checks_passed(payload.get("statusCheckRollup"))
        changed_paths = _changed_paths(payload.get("files"))
        url = _required_string(payload, "url")
    except (TypeError, ValueError) as error:
        raise ExternalServiceError("gh pr view returned an invalid pull request") from error
    return PullRequest(
        number=number,
        state=state,
        base_ref=base_ref,
        base_sha=base_sha,
        head_ref=head_ref,
        head_sha=head_sha,
        merge_commit_sha=merge_commit_sha,
        review_decision=review_decision,
        checks_passed=checks_passed,
        changed_paths=changed_paths,
        url=url,
    )


def _positive_int(payload: Mapping[str, Any], field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(field)
    return value


def _required_string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > _MAX_FIELD_BYTES
    ):
        raise ValueError(field)
    return value


def _optional_string(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_FIELD_BYTES:
        raise ValueError("string")
    return value


def _merge_commit_sha(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("mergeCommit")
    sha = value.get("oid")
    if sha is None:
        return None
    if (
        not isinstance(sha, str)
        or not sha
        or len(sha.encode("utf-8")) > _MAX_FIELD_BYTES
    ):
        raise ValueError("mergeCommit.oid")
    return sha


def _checks_passed(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return False
    for check in value:
        if not isinstance(check, Mapping):
            return False
        if check.get("status") != "COMPLETED":
            return False
        if check.get("conclusion") not in _ALLOWED_CHECK_CONCLUSIONS:
            return False
    return True


def _changed_paths(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("files")
    paths: list[str] = []
    for file in value:
        if not isinstance(file, Mapping):
            raise ValueError("files")
        path = file.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("files.path")
        paths.append(_truncate_utf8(path, _MAX_CHANGED_PATH_BYTES))
        if len(paths) == _MAX_CHANGED_PATHS:
            break
    return tuple(paths)


def _bounded_stderr(value: str | None) -> str:
    if not value:
        return ""
    redacted = _HTTP_URL_USERINFO.sub(r"\g<scheme><redacted>@", value)
    redacted = _SECRET.sub("<redacted>", redacted)
    return _truncate_utf8(redacted.strip(), _MAX_STDERR_BYTES)


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    return value.encode("utf-8")[:maximum_bytes].decode("utf-8", errors="ignore")
