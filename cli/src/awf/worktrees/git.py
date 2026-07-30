from __future__ import annotations

import hashlib
import os
import signal
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """Raised when a checked Git command cannot complete successfully."""


@dataclass(frozen=True)
class GitCompleted:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class GitWorktree:
    path: Path
    head_sha: str | None
    branch: str | None
    bare: bool = False
    detached: bool = False
    locked: str | None = None
    prunable: str | None = None


class GitClient:
    def __init__(self, cwd: Path, *, timeout: float = 30.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.cwd = cwd
        self.timeout = timeout

    def repository_root(self) -> Path:
        output = self._run("rev-parse", "--show-toplevel").stdout
        return Path(_path_from_line(output)).resolve()

    def repository_name(self) -> str:
        return self.repository_root().name

    def repository_id(self) -> str:
        normalized_remote = _normalize_remote_url(self.remote_url())
        payload = normalized_remote.encode("utf-8") + b"\0" + os.fsencode(
            self.repository_root()
        )
        return hashlib.sha256(payload).hexdigest()

    def remote_url(self) -> str:
        return self._text(self._run("remote", "get-url", "origin").stdout)

    def head_sha(self, cwd: Path | None = None) -> str:
        return self._text(self._run("rev-parse", "HEAD", cwd=cwd).stdout)

    def status_porcelain(self, cwd: Path | None = None) -> tuple[str, ...]:
        completed = self._run("status", "--porcelain=v1", "-z", cwd=cwd)
        return _nul_records(completed.stdout)

    def list_worktrees(self) -> tuple[GitWorktree, ...]:
        completed = self._run("worktree", "list", "--porcelain", "-z")
        return _parse_worktrees(completed.stdout)

    def fetch_ref(self, ref: str) -> str:
        self._run("fetch", "origin", ref)
        return self._text(self._run("rev-parse", "FETCH_HEAD").stdout)

    def resolve_ref(self, ref: str) -> str:
        return self._text(self._run("rev-parse", "--verify", ref).stdout)

    def default_remote_branch(self) -> str:
        ref = self._text(
            self._run("symbolic-ref", "refs/remotes/origin/HEAD").stdout
        )
        prefix = "refs/remotes/origin/"
        if not ref.startswith(prefix) or ref == prefix:
            raise GitError(f"git symbolic-ref returned an invalid origin HEAD: {ref}")
        return ref[len(prefix) :]

    def add_worktree(self, path: Path, branch: str, start_sha: str) -> None:
        self._run("worktree", "add", "-b", branch, str(path), start_sha)

    def remove_worktree(self, path: Path) -> None:
        self._run("worktree", "remove", str(path))

    def delete_local_branch(self, branch: str) -> None:
        self._run("branch", "-d", branch)

    def delete_branch_if_at(self, branch: str, expected_sha: str) -> None:
        self._run("update-ref", "-d", f"refs/heads/{branch}", expected_sha)

    def delete_remote_branch(self, branch: str) -> None:
        self._run("push", "origin", "--delete", branch)

    def merge_base(self, left: str, right: str) -> str:
        return self._text(self._run("merge-base", left, right).stdout)

    def binary_diff(self, base: str, head: str) -> bytes:
        return self._run(
            "diff", "--binary", "--full-index", "--find-renames", f"{base}..{head}"
        ).stdout

    def apply_indexed_patch(self, cwd: Path, patch: bytes) -> None:
        self._run("apply", "--3way", "--index", "-", cwd=cwd, input_bytes=patch)

    def changed_paths(
        self, cwd: Path, base: str, head: str = "HEAD"
    ) -> tuple[str, ...]:
        completed = self._run("diff", "--name-only", "-z", f"{base}..{head}", cwd=cwd)
        return _nul_records(completed.stdout)

    def commit(self, cwd: Path, message: str) -> str:
        self._run("commit", "-m", message, cwd=cwd)
        return self.head_sha(cwd)

    def push_branch(self, cwd: Path, branch: str) -> None:
        self._run("push", "-u", "origin", f"HEAD:refs/heads/{branch}", cwd=cwd)

    def _run(
        self,
        *args: str,
        cwd: Path | None = None,
        input_bytes: bytes | None = None,
    ) -> GitCompleted:
        command = args[0] if args else "git"
        try:
            process = subprocess.Popen(
                ["git", *args],
                cwd=str((cwd or self.cwd).resolve()),
                stdin=subprocess.PIPE if input_bytes is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = process.communicate(
                input=input_bytes, timeout=self.timeout
            )
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = _stop_process_group(process)
            detail = _bounded_stderr(stderr or exc.stderr)
            suffix = f": {detail}" if detail else ""
            raise GitError(
                f"git {command} timed out after {self.timeout:g} seconds{suffix}"
            ) from exc
        except (OSError, ValueError) as exc:
            raise GitError(f"git {command} failed to launch: {exc}") from exc
        if process.returncode != 0:
            detail = _bounded_stderr(stderr)
            if "not a git repository" in detail.lower():
                detail = f"not a Git repository: {detail}"
            raise GitError(f"git {command} failed ({process.returncode}): {detail}")
        return GitCompleted(process.returncode, stdout, stderr)

    @staticmethod
    def _text(value: bytes) -> str:
        return value.decode("utf-8", errors="replace").strip()


_HTTP_URL_USERINFO = re.compile(
    r"(?P<scheme>https?://)(?P<userinfo>[^/@\s]*@)", re.IGNORECASE
)
_PROCESS_TERMINATION_GRACE_SECONDS = 0.2


def _path_from_line(value: bytes) -> str:
    if value.endswith(b"\n"):
        value = value[:-1]
    return os.fsdecode(value)


def _normalize_remote_url(url: str) -> str:
    normalized = url.strip()
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    if re.match(r"^[^/@:\s]+@[^/:\s]+:.+$", normalized):
        user_and_host, path = normalized.split(":", 1)
        normalized = f"ssh://{user_and_host}/{path}"
    return _HTTP_URL_USERINFO.sub(r"\g<scheme>", normalized)


def _bounded_stderr(value: bytes | None) -> str:
    if not value:
        return ""
    redacted = _redact_url_userinfo(value.decode("utf-8", errors="replace"))
    return _truncate_utf8(redacted.strip(), 512)


def _redact_url_userinfo(value: str) -> str:
    return _HTTP_URL_USERINFO.sub(r"\g<scheme><redacted>@", value)


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    return value.encode("utf-8")[:maximum_bytes].decode("utf-8", errors="ignore")


def _stop_process_group(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=_PROCESS_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return process.communicate()


def _nul_records(value: bytes) -> tuple[str, ...]:
    return tuple(os.fsdecode(record) for record in value.split(b"\0") if record)


def _parse_worktrees(value: bytes) -> tuple[GitWorktree, ...]:
    worktrees: list[GitWorktree] = []
    fields: dict[str, str | bool] = {}
    for raw_field in value.split(b"\0"):
        if not raw_field:
            if fields:
                worktrees.append(_worktree_from_fields(fields))
                fields = {}
            continue
        key, separator, raw_value = raw_field.partition(b" ")
        field = key.decode("ascii", errors="replace")
        if field == "worktree" and fields:
            worktrees.append(_worktree_from_fields(fields))
            fields = {}
        if separator:
            fields[field] = (
                os.fsdecode(raw_value)
                if field == "worktree"
                else raw_value.decode("utf-8", errors="replace")
            )
        else:
            fields[field] = "" if field in {"locked", "prunable"} else True
    if fields:
        worktrees.append(_worktree_from_fields(fields))
    return tuple(worktrees)


def _worktree_from_fields(fields: dict[str, str | bool]) -> GitWorktree:
    raw_path = fields.get("worktree")
    if not isinstance(raw_path, str):
        raise GitError("git worktree list returned an entry without a worktree path")
    branch = fields.get("branch")
    if isinstance(branch, str) and branch.startswith("refs/heads/"):
        branch = branch[len("refs/heads/") :]
    return GitWorktree(
        path=Path(raw_path).resolve(),
        head_sha=_optional_string(fields.get("HEAD")),
        branch=_optional_string(branch),
        bare=fields.get("bare") is True,
        detached=fields.get("detached") is True,
        locked=_optional_string(fields.get("locked")),
        prunable=_optional_string(fields.get("prunable")),
    )


def _optional_string(value: str | bool | None) -> str | None:
    return value if isinstance(value, str) else None
