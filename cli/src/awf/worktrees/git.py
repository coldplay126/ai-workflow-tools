from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import hashlib
import os
import signal
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """Raised when a checked Git command cannot complete successfully."""



class GitRemoteError(GitError):
    """Raised when a Git transport operation against origin cannot complete."""


_REMOTE_SAFETY_REJECTION_MARKERS = (
    "force-with-lease",
    "stale info",
)


def _is_remote_safety_rejection(error: GitError) -> bool:
    detail = str(error).lower()
    return any(marker in detail for marker in _REMOTE_SAFETY_REJECTION_MARKERS)

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
        try:
            self._run("fetch", "origin", ref)
        except GitError as error:
            raise GitRemoteError(str(error)) from error
        return self._text(self._run("rev-parse", "FETCH_HEAD").stdout)

    def remote_branch_sha(self, branch: str) -> str | None:
        """Return the exact origin branch SHA, or None if it is absent.

        Raise GitRemoteError when the origin lookup fails or returns malformed data.
        """
        ref = f"refs/heads/{branch}"
        try:
            completed = self._run("ls-remote", "--heads", "origin", ref)
        except GitError as error:
            raise GitRemoteError(str(error)) from error
        if not completed.stdout:
            return None
        try:
            rows = completed.stdout.decode("ascii", errors="strict").splitlines()
        except UnicodeDecodeError as error:
            raise GitRemoteError("git ls-remote returned an invalid branch record") from error
        if len(rows) != 1:
            raise GitRemoteError("git ls-remote returned multiple branch records")
        oid, separator, returned_ref = rows[0].partition("\t")
        if (
            not separator
            or returned_ref != ref
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", oid) is None
        ):
            raise GitRemoteError("git ls-remote returned an invalid branch record")
        return oid


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


    def delete_branch_if_at(self, branch: str, expected_sha: str) -> None:
        self._run("update-ref", "-d", f"refs/heads/{branch}", expected_sha)


    def delete_remote_branch_if_at(self, branch: str, expected_sha: str) -> None:
        ref = f"refs/heads/{branch}"
        try:
            self._run(
                "push",
                f"--force-with-lease={ref}:{expected_sha}",
                "origin",
                f":{ref}",
            )
        except GitError as error:
            if _is_remote_safety_rejection(error):
                raise
            raise GitRemoteError(str(error)) from error

    @contextmanager
    def hold_worktree_branch_if_at(
        self, worktree_path: Path, branch: str, expected_sha: str
    ) -> Iterator[None]:
        ref = f"refs/heads/{branch}"
        branch_transaction = self._start_ref_transaction(self.cwd)
        head_transaction: subprocess.Popen[str] | None = None
        try:
            self._ref_transaction_command(branch_transaction, "start")
            self._ref_transaction_command(
                branch_transaction, f"verify {ref} {expected_sha}", response=False
            )

            # Git rejects duplicate branch/HEAD verification in one transaction
            # because HEAD resolves through the branch, so hold both refs separately.
            head_transaction = self._start_ref_transaction(worktree_path)
            self._ref_transaction_command(head_transaction, "start")
            self._ref_transaction_command(
                head_transaction, "option no-deref", response=False
            )
            self._ref_transaction_command(
                head_transaction, f"symref-verify HEAD {ref}", response=False
            )

            self._ref_transaction_command(branch_transaction, "prepare")
            self._ref_transaction_command(head_transaction, "prepare")
            yield
        finally:
            if head_transaction is not None:
                self._abort_ref_transaction(head_transaction)
            self._abort_ref_transaction(branch_transaction)

    def _start_ref_transaction(self, cwd: Path) -> subprocess.Popen[str]:
        try:
            return subprocess.Popen(
                ["git", "update-ref", "--stdin"],
                cwd=str(cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as error:
            raise GitError(f"git update-ref failed to launch: {error}") from error

    def _abort_ref_transaction(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            try:
                self._ref_transaction_command(process, "abort")
            except GitError:
                _stop_process_group(process)
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        try:
            process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            _stop_process_group(process)

    def _ref_transaction_command(
        self,
        process: subprocess.Popen[str],
        command: str,
        *,
        response: bool = True,
    ) -> None:
        if process.stdin is None or process.stdout is None:
            raise GitError("git update-ref did not expose transaction pipes")
        try:
            process.stdin.write(f"{command}\n")
            process.stdin.flush()
            if not response:
                return
            result = process.stdout.readline().strip()
        except (OSError, ValueError) as error:
            raise GitError(f"git update-ref transaction failed: {error}") from error
        if result == f"{command.split(' ', 1)[0]}: ok":
            return
        stderr = process.stderr.read() if process.stderr is not None else ""
        detail = _bounded_stderr(stderr.encode("utf-8", errors="replace"))
        suffix = f": {detail}" if detail else ""
        raise GitError(f"git update-ref transaction rejected {command!r}{suffix}")

    def merge_base(self, left: str, right: str) -> str:
        return self._text(self._run("merge-base", left, right).stdout)

    def commit_parents(self, ref: str) -> tuple[str, ...]:
        completed = self._run("show", "--no-patch", "--format=%P", ref)
        return tuple(
            parent
            for parent in completed.stdout.decode("ascii", errors="strict").split()
            if parent
        )

    def commit_message(self, cwd: Path, ref: str = "HEAD") -> str:
        completed = self._run(
            "show", "--no-patch", "--format=%B", ref, cwd=cwd
        )
        return completed.stdout.decode("utf-8", errors="replace").rstrip("\n")


    def path_blob(self, ref: str, path: str) -> str | None:
        completed = self._run("ls-tree", "-z", ref, "--", path)
        if not completed.stdout:
            return None
        record = completed.stdout.split(b"\0", 1)[0]
        metadata, separator, _ = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise GitError("git ls-tree returned an invalid path record")
        return fields[2].decode("ascii", errors="strict")

    def binary_diff(
        self,
        base: str,
        head: str,
        *,
        paths: Sequence[str] | None = None,
    ) -> bytes:
        args = [
            "diff",
            "--binary",
            "--full-index",
            "--find-renames",
            f"{base}..{head}",
        ]
        if paths is not None:
            args.extend(("--", *paths))
        return self._run(*args).stdout

    def apply_indexed_patch(self, cwd: Path, patch: bytes) -> None:
        self._run("apply", "--3way", "--index", "-", cwd=cwd, input_bytes=patch)

    def reset_hard(self, cwd: Path, ref: str) -> None:
        """Set a managed checkout to ref and discard tracked staged/unstaged changes."""
        self._run("reset", "--hard", "-q", "--end-of-options", ref, cwd=cwd)

    def changed_paths(
        self,
        cwd: Path,
        base: str,
        head: str = "HEAD",
        *,
        find_renames: bool = False,
    ) -> tuple[str, ...]:
        arguments = ["diff", "--name-only", "-z"]
        if find_renames:
            arguments.append("--find-renames")
        arguments.append(f"{base}..{head}")
        completed = self._run(*arguments, cwd=cwd)
        return _nul_records(completed.stdout)

    def commit(self, cwd: Path, message: str, *, allow_empty: bool = False) -> str:
        arguments = ["commit"]
        if allow_empty:
            arguments.append("--allow-empty")
        arguments.extend(("-m", message))
        self._run(*arguments, cwd=cwd)
        return self.head_sha(cwd)

    def push_branch(self, cwd: Path, branch: str) -> None:
        try:
            self._run("push", "-u", "origin", f"HEAD:refs/heads/{branch}", cwd=cwd)
        except GitError as error:
            raise GitRemoteError(str(error)) from error

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
        path=Path(raw_path),
        head_sha=_optional_string(fields.get("HEAD")),
        branch=_optional_string(branch),
        bare=fields.get("bare") is True,
        detached=fields.get("detached") is True,
        locked=_optional_string(fields.get("locked")),
        prunable=_optional_string(fields.get("prunable")),
    )


def _optional_string(value: str | bool | None) -> str | None:
    return value if isinstance(value, str) else None
