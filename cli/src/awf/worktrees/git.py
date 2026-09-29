from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager

import hashlib
import os
import re
import shutil
import signal
import subprocess
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory, mkstemp
from typing import overload


class GitError(RuntimeError):
    """Raised when a checked Git command cannot complete successfully."""

    def __init__(self, detail: str, *, returncode: int | None = None) -> None:
        super().__init__(detail)
        self.returncode = returncode


class GitBranchDeleteAborted(GitError):
    """The local ref deletion transaction was aborted before commit was attempted."""


class GitPatchConflict(GitError):
    """Raised when an indexed three-way patch application leaves conflicts."""

    def __init__(self, paths: tuple[str, ...], detail: str) -> None:
        super().__init__(detail)
        self.paths = paths



class GitRemoteError(GitError):
    """Raised when a Git transport operation against origin cannot complete."""


_REMOTE_SAFETY_REJECTION_MARKERS = (
    "force-with-lease",
    "stale info",
)
_OBJECT_FORMATS = {"sha1": 40, "sha256": 64}



def _is_remote_safety_rejection(error: GitError) -> bool:
    lines = (
        line for line in str(error).lower().splitlines()
        if not re.search(r"(?:^|\s)remote:", line)
    )
    return any(
        marker in line
        for line in lines
        for marker in _REMOTE_SAFETY_REJECTION_MARKERS
    )

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


@dataclass(frozen=True)
class GitPathUsage:
    allocated_bytes: int
    entry_count: int


@dataclass(frozen=True)
class GitStatusEntry:
    index_status: str
    worktree_status: str
    path: str
    original_path: str | None = None


@dataclass(frozen=True)
class GitIndexBackup:
    index_path: Path
    backup_path: Path
    existed: bool

def _bundle_verification_environment() -> dict[str, str]:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment



class GitClient:
    def __init__(self, cwd: Path, *, timeout: float = 30.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.cwd = cwd
        self.timeout = timeout

    def repository_root(self) -> Path:
        output = self._run("rev-parse", "--show-toplevel").stdout
        return Path(_path_from_line(output)).resolve()

    def common_git_directory(self) -> Path:
        """Identify one Git object/ref store across its linked worktrees, including bare."""
        output = self._run(
            "rev-parse", "--path-format=absolute", "--git-common-dir"
        ).stdout
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

    def validate_branch_name(self, branch: str) -> None:
        """Validate a literal local branch name with Git's ref-format parser."""
        if (
            not branch
            or any(character.isspace() for character in branch)
            or "\0" in branch
            or branch.startswith("refs/")
        ):
            raise GitError("branch name must be a non-empty local branch name")
        normalized = self._text(
            self._run("check-ref-format", "--branch", branch).stdout
        )
        if normalized != branch:
            raise GitError("branch name must not use branch expansion")

    def create_bundle(self, destination: Path, *, cwd: Path) -> None:
        """Create a self-contained bundle containing HEAD and its ancestry."""
        try:
            details = destination.lstat()
        except FileNotFoundError:
            details = None
        except OSError as error:
            raise GitError(f"unable to inspect bundle destination: {error}") from error
        if details is not None:
            raise GitError("bundle destination already exists")
        if not cwd.is_dir():
            raise GitError(f"bundle source directory is unavailable: {cwd}")
        self._run("bundle", "create", str(destination), "HEAD", cwd=cwd)
        try:
            details = destination.lstat()
        except OSError as error:
            raise GitError("git bundle creation did not produce an artifact") from error
        if not stat.S_ISREG(details.st_mode):
            raise GitError("git bundle creation produced a non-regular artifact")

    def create_detached_commit_bundle(
        self, destination: Path, *, commit_sha: str
    ) -> None:
        """Create a self-contained commit bundle without mutating the source repo."""
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit_sha) is None:
            raise GitError("commit bundle head must be an object identifier")
        try:
            details = destination.lstat()
        except FileNotFoundError:
            details = None
        except OSError as error:
            raise GitError(f"unable to inspect bundle destination: {error}") from error
        if details is not None:
            raise GitError("bundle destination already exists")

        source_environment = _bundle_verification_environment()
        source_object_format = self._text(
            self._run(
                "rev-parse",
                "--show-object-format",
                env=source_environment,
            ).stdout
        )
        source_oid_length = _OBJECT_FORMATS.get(source_object_format)
        if source_oid_length is None:
            raise GitError("Git uses an unsupported object format")
        if len(commit_sha) != source_oid_length:
            raise GitError("commit bundle head does not match source object format")

        common_directory = Path(
            self._text(
                self._run(
                    "rev-parse",
                    "--git-common-dir",
                    env=source_environment,
                ).stdout
            )
        )
        if not common_directory.is_absolute():
            common_directory = self.cwd / common_directory
        objects = common_directory.resolve() / "objects"
        if not objects.is_dir():
            raise GitError("bundle source objects directory is unavailable")

        with TemporaryDirectory(prefix="awf-bundle-source-") as temporary:
            temporary_path = Path(temporary)
            isolated_directory = temporary_path / "isolated"
            isolated_directory.mkdir(mode=0o700)
            source = temporary_path / "source.git"
            environment = _bundle_verification_environment()
            environment["GIT_TEMPLATE_DIR"] = str(isolated_directory)
            environment["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(objects)
            command_prefix = ("-c", f"core.hooksPath={isolated_directory}")
            self._run(
                *command_prefix,
                "init",
                "--bare",
                f"--object-format={source_object_format}",
                f"--template={isolated_directory}",
                str(source),
                cwd=temporary_path,
                env=environment,
            )
            self._run(
                *command_prefix,
                "update-ref",
                "HEAD",
                commit_sha,
                cwd=source,
                env=environment,
            )
            self._run(
                *command_prefix,
                "bundle",
                "create",
                str(destination),
                "HEAD",
                cwd=source,
                env=environment,
            )
        try:
            details = destination.lstat()
        except OSError as error:
            raise GitError("git bundle creation did not produce an artifact") from error
        if not stat.S_ISREG(details.st_mode):
            raise GitError("git bundle creation produced a non-regular artifact")

    def verify_bundle(self, bundle: Path, *, expected_head: str) -> None:
        """Recover a bundle into a fresh bare repository and verify its objects."""
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", expected_head) is None:
            raise GitError("expected bundle head must be an object identifier")
        expected_object_format = (
            "sha1"
            if len(expected_head) == _OBJECT_FORMATS["sha1"]
            else "sha256"
        )

        try:
            details = bundle.lstat()
        except OSError as error:
            raise GitError(f"bundle artifact is unavailable: {bundle}") from error
        if not stat.S_ISREG(details.st_mode):
            raise GitError("bundle artifact must be a regular file")

        with TemporaryDirectory(prefix="awf-bundle-verify-") as temporary:
            temporary_path = Path(temporary)
            isolated_directory = temporary_path / "isolated"
            isolated_directory.mkdir(mode=0o700)
            environment = _bundle_verification_environment()
            environment["GIT_TEMPLATE_DIR"] = str(isolated_directory)
            recovered = temporary_path / "recovered.git"
            command_prefix = (
                "-c",
                f"core.hooksPath={isolated_directory}",
                "-c",
                "protocol.file.allow=always",
            )
            self._run(
                *command_prefix,
                "init",
                "--bare",
                f"--object-format={expected_object_format}",
                f"--template={isolated_directory}",
                str(recovered),
                cwd=temporary_path,
                env=environment,
            )
            self._run(
                *command_prefix,
                "bundle",
                "verify",
                str(bundle.resolve()),
                cwd=recovered,
                env=environment,
            )
            self._run(
                *command_prefix,
                "fetch",
                "--no-tags",
                str(bundle.resolve()),
                "HEAD:refs/heads/archive-verify",
                cwd=recovered,
                env=environment,
            )
            recovered_head = self._text(
                self._run(
                    *command_prefix,
                    "rev-parse",
                    "refs/heads/archive-verify",
                    cwd=recovered,
                    env=environment,
                ).stdout
            )
            if recovered_head != expected_head:
                raise GitError(
                    "independent bundle recovery does not match the expected HEAD"
                )
            self._run(
                *command_prefix,
                "fsck",
                "--full",
                "--strict",
                cwd=recovered,
                env=environment,
            )

    def status_porcelain(self, cwd: Path | None = None) -> tuple[str, ...]:
        completed = self._run(
            "-c", "status.renames=false", "status", "--porcelain=v1", "-z", cwd=cwd
        )
        return _nul_records(completed.stdout)

    def status_porcelain_entries(
        self, cwd: Path | None = None
    ) -> tuple[GitStatusEntry, ...]:
        completed = self._run(
            "-c", "status.renames=false", "status", "--porcelain=v1", "-z", cwd=cwd
        )
        return _parse_status_porcelain(_nul_records(completed.stdout))

    def path_is_ignored(self, cwd: Path, path: str) -> bool:
        """Return whether one literal repository-relative path is ignored."""
        self._compact_path_candidate(cwd, path)
        try:
            self._run(
                "check-ignore",
                "--stdin",
                "-z",
                cwd=cwd,
                input_bytes=os.fsencode(path) + b"\0",
            )
        except GitError as error:
            if error.returncode == 1:
                return False
            raise
        return True

    def tracked_paths(self, cwd: Path, path: str) -> tuple[str, ...]:
        """Return tracked descendants using conservative case-insensitive matching."""
        self._compact_path_candidate(cwd, path)
        target_parts = tuple(part.casefold() for part in PurePosixPath(path).parts)
        tracked = _nul_records(self._run("ls-files", "-z", cwd=cwd).stdout)
        return tuple(
            sorted(
                candidate
                for candidate in tracked
                if len(PurePosixPath(candidate).parts) >= len(target_parts)
                and tuple(
                    part.casefold()
                    for part in PurePosixPath(candidate).parts[: len(target_parts)]
                )
                == target_parts
            )
        )

    def compact_path_usage(self, cwd: Path, path: str) -> GitPathUsage:
        """Measure allocated disk blocks and entries without following symlinks."""
        candidate = self._compact_path_candidate(cwd, path)
        return _allocated_tree_usage(candidate)

    def remove_ignored_path(self, cwd: Path, path: str) -> None:
        """Remove a revalidated ignored file or tree without touching Git state."""
        candidate = self._compact_path_candidate(cwd, path)
        if not self.path_is_ignored(cwd, path):
            raise GitError(f"compact path {path!r} is no longer ignored")
        if self.tracked_paths(cwd, path):
            raise GitError(f"compact path {path!r} contains tracked descendants")
        try:
            if stat.S_ISDIR(candidate.lstat().st_mode):
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
        except OSError as error:
            raise GitError(f"unable to remove compact path {path!r}: {error}") from error

    @staticmethod
    def _compact_path_candidate(cwd: Path, path: str) -> Path:
        if (
            not isinstance(path, str)
            or not path
            or "\0" in path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
        ):
            raise GitError("compact paths must be repository-relative")
        root = cwd.resolve()
        candidate = root / path
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise GitError("compact paths must stay within the worktree") from error
        current = root
        try:
            for part in ("", *Path(path).parts):
                if part:
                    current /= part
                if current.is_symlink():
                    raise GitError(f"compact path {path!r} has a symlinked ancestor")
                current.lstat()
        except FileNotFoundError as error:
            raise GitError(f"compact path {path!r} does not exist") from error
        except OSError as error:
            raise GitError(f"unable to inspect compact path {path!r}: {error}") from error
        try:
            candidate.resolve(strict=True).relative_to(root)
        except ValueError as error:
            raise GitError(f"compact path {path!r} escapes the worktree") from error
        except OSError as error:
            raise GitError(f"unable to resolve compact path {path!r}: {error}") from error
        return candidate

    def untracked_paths(self, cwd: Path) -> tuple[str, ...]:
        return tuple(
            sorted(
                _nul_records(
                    self._run(
                        "ls-files",
                        "--others",
                        "--exclude-standard",
                        "-z",
                        cwd=cwd,
                    ).stdout
                )
            )
        )

    def remove_untracked_paths(self, cwd: Path, paths: Sequence[str]) -> None:
        requested = tuple(sorted(set(paths)))
        if any(
            not path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            for path in requested
        ):
            raise GitError("untracked cleanup paths must be repository-relative")
        current = set(self.untracked_paths(cwd))
        if any(path not in current for path in requested):
            raise GitError("untracked cleanup path changed before removal")
        for path in requested:
            candidate = cwd / path
            try:
                if candidate.is_symlink() or candidate.is_file():
                    candidate.unlink()
                else:
                    raise GitError(
                        f"untracked cleanup path {path!r} is not a file or symlink"
                    )
            except OSError as error:
                raise GitError(
                    f"unable to remove untracked cleanup path {path!r}: {error}"
                ) from error

    def unmerged_paths(self, cwd: Path) -> tuple[str, ...]:
        completed = self._run(
            "diff", "--name-only", "--diff-filter=U", "-z", cwd=cwd
        )
        return tuple(sorted(_nul_records(completed.stdout)))

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

    def add_worktree(
        self,
        path: Path,
        branch: str,
        start_sha: str,
        *,
        reuse_exact_branch: bool = False,
    ) -> None:
        try:
            self._run("worktree", "add", "-b", branch, str(path), start_sha)
            return
        except GitError as creation_error:
            if not reuse_exact_branch:
                raise
            try:
                existing_sha = self._text(
                    self._run(
                        "rev-parse",
                        "--verify",
                        f"refs/heads/{branch}",
                    ).stdout
                ).strip()
            except GitError:
                raise creation_error
            if existing_sha != start_sha:
                raise creation_error
        self._run("worktree", "add", str(path), branch)

    def remove_worktree(
        self,
        path: Path,
        *,
        force: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> None:
        arguments = ("worktree", "remove", "--force", str(path)) if force else (
            "worktree",
            "remove",
            str(path),
        )
        self._run(*arguments, env=env)

    def restore_paths_to_ref(
        self, cwd: Path, ref: str, paths: tuple[str, ...]
    ) -> None:
        if not ref:
            raise GitError("a source ref is required when restoring paths")
        if not paths:
            raise GitError("at least one path is required when restoring paths")
        self._run(
            "--literal-pathspecs",
            "restore",
            f"--source={ref}",
            "--staged",
            "--worktree",
            "--",
            *paths,
            cwd=cwd,
        )

    def local_branch_sha(self, branch: str) -> str | None:
        """Return a direct local branch's exact object ID, or None when absent."""
        self.validate_branch_name(branch)
        ref = f"refs/heads/{branch}"
        completed = self._run(
            "for-each-ref",
            "--format=%(refname)\t%(objectname)\t%(symref)",
            ref,
        )
        try:
            records = completed.stdout.decode("utf-8", errors="strict").splitlines()
        except UnicodeDecodeError as error:
            raise GitError(
                "git for-each-ref returned an invalid local branch record"
            ) from error

        matching_records: list[tuple[str, str, str]] = []
        for record in records:
            fields = record.split("\t")
            if len(fields) != 3:
                raise GitError("git for-each-ref returned an invalid local branch record")
            returned_ref, object_id, symref = fields
            if returned_ref == ref:
                matching_records.append((returned_ref, object_id, symref))
        if not matching_records:
            return None
        if len(matching_records) != 1:
            raise GitError("git for-each-ref returned multiple local branch records")
        _, object_id, symref = matching_records[0]
        if symref:
            raise GitError("local branch must be a direct reference")
        self._require_object_id(
            object_id,
            "git for-each-ref returned an invalid local branch",
        )
        return object_id

    def delete_inactive_branch_if_at(
        self,
        branch: str,
        expected_sha: str,
        *,
        before_commit: Callable[[], None] | None = None,
    ) -> None:
        """Delete an un-checked-out direct branch under prepared ref and HEAD locks."""
        self.validate_branch_name(branch)
        self._require_object_id(expected_sha, "expected branch head")
        current_sha = self.local_branch_sha(branch)
        if current_sha is None:
            raise GitError("local branch is absent")
        if current_sha != expected_sha:
            raise GitError("local branch changed before deletion")

        inventory = self.list_worktrees()
        self._require_branch_inactive(inventory, branch)
        ref = f"refs/heads/{branch}"
        branch_transaction = self._start_ref_transaction(
            self.cwd, isolate_hooks=True
        )
        committed = False
        commit_started = False
        try:
            self._ref_transaction_command(branch_transaction, "start")
            self._ref_transaction_command(
                branch_transaction, "option no-deref", response=False
            )
            self._ref_transaction_command(
                branch_transaction, f"delete {ref} {expected_sha}", response=False
            )
            self._ref_transaction_command(branch_transaction, "prepare")

            with self._hold_live_worktree_heads(inventory):
                self._revalidate_inactive_branch(inventory, branch)
                if before_commit is not None:
                    before_commit()
                self._revalidate_inactive_branch(inventory, branch)
                commit_started = True
                self._ref_transaction_command(branch_transaction, "commit")
                committed = True
        except GitError as error:
            if not commit_started:
                raise GitBranchDeleteAborted(str(error)) from error
            raise
        finally:
            if committed:
                self._close_ref_transaction(branch_transaction)
            else:
                self._abort_ref_transaction(branch_transaction)

    @staticmethod
    def _require_object_id(value: str, description: str) -> None:
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None:
            raise GitError(f"{description} must be an object identifier")

    @staticmethod
    def _require_branch_inactive(
        inventory: tuple[GitWorktree, ...], branch: str
    ) -> None:
        if any(worktree.branch == branch for worktree in inventory):
            raise GitError("local branch is checked out by a registered worktree")

    def _revalidate_inactive_branch(
        self, expected_inventory: tuple[GitWorktree, ...], branch: str
    ) -> None:
        inventory = self.list_worktrees()
        if inventory != expected_inventory:
            raise GitError(
                "registered worktree inventory changed before local branch deletion"
            )
        self._require_branch_inactive(inventory, branch)

    @contextmanager
    def _hold_live_worktree_heads(
        self, inventory: tuple[GitWorktree, ...]
    ) -> Iterator[None]:
        with ExitStack() as cleanup:
            for worktree in inventory:
                if worktree.prunable is not None:
                    continue
                transaction = self._start_ref_transaction(
                    worktree.path, isolate_hooks=True
                )
                cleanup.callback(self._abort_ref_transaction, transaction)
                self._ref_transaction_command(transaction, "start")
                self._ref_transaction_command(
                    transaction, "option no-deref", response=False
                )
                if worktree.branch is not None:
                    self._ref_transaction_command(
                        transaction,
                        f"symref-verify HEAD refs/heads/{worktree.branch}",
                        response=False,
                    )
                elif worktree.detached and worktree.head_sha is not None:
                    self._require_object_id(
                        worktree.head_sha,
                        "registered worktree HEAD",
                    )
                    self._ref_transaction_command(
                        transaction,
                        f"verify HEAD {worktree.head_sha}",
                        response=False,
                    )
                else:
                    raise GitError(
                        "registered worktree HEAD is neither a branch symref nor detached"
                    )
                self._ref_transaction_command(transaction, "prepare")
            yield

    def delete_branch_if_at(self, branch: str, expected_sha: str) -> None:
        self._run("update-ref", "-d", f"refs/heads/{branch}", expected_sha)


    def delete_remote_branch_if_at(
        self, branch: str, expected_sha: str, *, skip_hooks: bool = False
    ) -> None:
        ref = f"refs/heads/{branch}"
        arguments = ["push"]
        if skip_hooks:
            arguments.append("--no-verify")
        arguments.extend(
            (
                f"--force-with-lease={ref}:{expected_sha}",
                "origin",
                f":{ref}",
            )
        )
        try:
            self._run(*arguments)
        except GitError as error:
            if _is_remote_safety_rejection(error):
                raise
            raise GitRemoteError(str(error), returncode=error.returncode) from error

    @contextmanager
    def hold_branch_if_at(self, branch: str, expected_sha: str) -> Iterator[None]:
        """Hold one local branch while a caller owns the worktree HEAD boundary."""
        ref = f"refs/heads/{branch}"
        branch_transaction = self._start_ref_transaction(self.cwd)
        try:
            self._ref_transaction_command(branch_transaction, "start")
            self._ref_transaction_command(
                branch_transaction, f"verify {ref} {expected_sha}", response=False
            )
            self._ref_transaction_command(branch_transaction, "prepare")
            yield
        finally:
            self._abort_ref_transaction(branch_transaction)

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

    def _start_ref_transaction(
        self, cwd: Path, *, isolate_hooks: bool = False
    ) -> subprocess.Popen[str]:
        command = ["git"]
        environment: dict[str, str] | None = None
        if isolate_hooks:
            command.extend(("-c", "core.hooksPath=/dev/null"))
            environment = _bundle_verification_environment()
        command.extend(("update-ref", "--stdin"))
        try:
            return subprocess.Popen(
                command,
                cwd=str(cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
                env=environment,
            )
        except OSError as error:
            raise GitError(f"git update-ref failed to launch: {error}") from error

    def _abort_ref_transaction(self, process: subprocess.Popen[str]) -> None:
        try:
            if process.poll() is None:
                try:
                    self._ref_transaction_command(process, "abort")
                except GitError:
                    _stop_process_group(process)
        finally:
            self._close_ref_transaction(process)

    def _close_ref_transaction(self, process: subprocess.Popen[str]) -> None:
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
        try:
            process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            _stop_process_group(process)
        finally:
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except (OSError, ValueError):
                        pass

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


    def path_entry(self, ref: str, path: str) -> tuple[str, str] | None:
        completed = self._run("ls-tree", "-z", ref, "--", path)
        if not completed.stdout:
            return None
        record = completed.stdout.split(b"\0", 1)[0]
        metadata, separator, _ = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise GitError("git ls-tree returned an invalid path record")
        mode = fields[0].decode("ascii", errors="strict")
        object_id = fields[2].decode("ascii", errors="strict")
        if (
            mode not in {"100644", "100755", "120000", "160000"}
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", object_id) is None
        ):
            raise GitError("git ls-tree returned invalid path metadata")
        return mode, object_id

    def path_blob(self, ref: str, path: str) -> str | None:
        entry = self.path_entry(ref, path)
        return entry[1] if entry is not None else None

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
        if self.unmerged_paths(cwd):
            raise GitError("git apply requires a clean index")
        try:
            self._run("apply", "--3way", "--index", "-", cwd=cwd, input_bytes=patch)
        except GitError as error:
            if error.returncode is None:
                raise
            try:
                paths = self.unmerged_paths(cwd)
            except GitError:
                raise error
            if paths:
                raise GitPatchConflict(paths, str(error)) from error
            raise

    def backup_index(self, cwd: Path) -> GitIndexBackup:
        index_path = Path(
            self._text(
                self._run(
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-path",
                    "index",
                    cwd=cwd,
                ).stdout
            )
        )
        backup_path: Path | None = None
        try:
            descriptor, backup = mkstemp(
                prefix="awf-index-backup-",
                dir=index_path.parent,
            )
            os.close(descriptor)
            backup_path = Path(backup)
            existed = index_path.exists()
            if existed:
                shutil.copy2(index_path, backup_path)
            return GitIndexBackup(
                index_path=index_path,
                backup_path=backup_path,
                existed=existed,
            )
        except OSError as error:
            if backup_path is not None:
                try:
                    backup_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise GitError(f"unable to back up the Git index: {error}") from error

    def restore_index(self, backup: GitIndexBackup) -> None:
        try:
            if backup.existed:
                os.replace(backup.backup_path, backup.index_path)
            else:
                backup.index_path.unlink(missing_ok=True)
                backup.backup_path.unlink(missing_ok=True)
        except OSError as error:
            raise GitError(f"unable to restore the Git index: {error}") from error

    def discard_index_backup(self, backup: GitIndexBackup) -> None:
        try:
            backup.backup_path.unlink(missing_ok=True)
        except OSError as error:
            raise GitError(
                f"unable to remove the Git index backup: {error}"
            ) from error

    def stage_paths(self, cwd: Path, paths: tuple[str, ...]) -> None:
        if not paths:
            raise GitError("at least one path is required for staging")
        self._run("--literal-pathspecs", "add", "--", *paths, cwd=cwd)

    def worktree_changed_paths(self, cwd: Path) -> tuple[str, ...]:
        tracked = _nul_records(
            self._run(
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                "HEAD",
                cwd=cwd,
            ).stdout
        )
        untracked = _nul_records(
            self._run(
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                cwd=cwd,
            ).stdout
        )
        return tuple(sorted(set(tracked).union(untracked)))

    def indexed_changed_paths(self, cwd: Path, base: str) -> tuple[str, ...]:
        completed = self._run(
            "diff",
            "--cached",
            "--name-only",
            "-z",
            "--no-renames",
            base,
            cwd=cwd,
        )
        return _nul_records(completed.stdout)

    def index_entry_snapshot(
        self, cwd: Path, paths: tuple[str, ...]
    ) -> tuple[tuple[str, tuple[str, str] | None], ...]:
        if not paths:
            return ()
        if (
            any(not isinstance(path, str) for path in paths)
            or paths != tuple(sorted(paths))
            or len(paths) != len(set(paths))
        ):
            raise GitError("index entry paths must be sorted and unique")
        completed = self._run(
            "--literal-pathspecs",
            "ls-files",
            "--stage",
            "-z",
            "--",
            *paths,
            cwd=cwd,
        )
        entries: dict[str, tuple[str, str] | None] = {
            path: None for path in paths
        }
        for record in completed.stdout.split(b"\0"):
            if not record:
                continue
            metadata, separator, raw_path = record.partition(b"\t")
            fields = metadata.split()
            if not separator or len(fields) != 3:
                raise GitError("git ls-files returned an invalid index record")
            raw_mode, raw_blob, stage = fields
            if raw_mode not in (b"100644", b"100755", b"120000", b"160000"):
                raise GitError("git ls-files returned an unsupported index mode")
            if stage != b"0":
                continue
            path = os.fsdecode(raw_path)
            if path not in entries:
                raise GitError("git ls-files returned an unexpected index path")
            try:
                mode = raw_mode.decode("ascii", errors="strict")
                blob_oid = raw_blob.decode("ascii", errors="strict")
            except UnicodeDecodeError as error:
                raise GitError("git ls-files returned an invalid index entry") from error
            if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", blob_oid) is None:
                raise GitError("git ls-files returned an invalid index blob")
            entries[path] = (mode, blob_oid)
        return tuple((path, entries[path]) for path in paths)

    def index_tree_sha(self, cwd: Path) -> str:
        return self._text(self._run("write-tree", cwd=cwd).stdout)

    def commit_tree_sha(self, ref: str, cwd: Path) -> str:
        return self._text(
            self._run("show", "--no-patch", "--format=%T", ref, cwd=cwd).stdout
        )

    def unstaged_paths(self, cwd: Path) -> tuple[str, ...]:
        tracked = _nul_records(
            self._run(
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                cwd=cwd,
            ).stdout
        )
        untracked = _nul_records(
            self._run(
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                cwd=cwd,
            ).stdout
        )
        return tuple(sorted(set(tracked).union(untracked)))

    def staged_diff_has_conflict_markers(self, cwd: Path) -> bool:
        return _has_added_conflict_marker(
            self._run("diff", "--cached", "--no-ext-diff", cwd=cwd).stdout
        )

    def index_has_conflict_markers(
        self, cwd: Path, paths: tuple[str, ...]
    ) -> bool:
        for _, entry in self.index_entry_snapshot(cwd, paths):
            if entry is None:
                continue
            mode, object_id = entry
            if mode == "160000":
                continue
            contents = self._run("cat-file", "blob", object_id, cwd=cwd).stdout
            if any(
                line.startswith(_CONFLICT_MARKER_PREFIXES)
                for line in contents.splitlines()
            ):
                return True
        return False

    def tree_has_conflict_markers(
        self, cwd: Path, ref: str, paths: tuple[str, ...]
    ) -> bool:
        for path in paths:
            entry = self.path_entry(ref, path)
            if entry is None:
                continue
            mode, object_id = entry
            if mode == "160000":
                continue
            contents = self._run("cat-file", "blob", object_id, cwd=cwd).stdout
            if any(
                line.startswith(_CONFLICT_MARKER_PREFIXES)
                for line in contents.splitlines()
            ):
                return True
        return False

    def worktree_diff_has_conflict_markers(self, cwd: Path) -> bool:
        if _has_added_conflict_marker(
            self._run("diff", "--no-ext-diff", cwd=cwd).stdout
        ):
            return True
        untracked = _nul_records(
            self._run(
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                cwd=cwd,
            ).stdout
        )
        for path in untracked:
            candidate = cwd / path
            try:
                if candidate.is_symlink():
                    contents = os.fsencode(candidate.readlink())
                elif candidate.is_file():
                    contents = candidate.read_bytes()
                else:
                    continue
            except OSError as error:
                raise GitError(
                    f"unable to inspect untracked path {path!r} for conflict markers: {error}"
                ) from error
            if any(
                line.startswith(_CONFLICT_MARKER_PREFIXES)
                for line in contents.splitlines()
            ):
                return True
        return False


    def committed_diff_has_conflict_markers(
        self, cwd: Path, base: str, head: str
    ) -> bool:
        return _has_added_conflict_marker(
            self._run(
                "diff",
                "--no-ext-diff",
                f"{base}..{head}",
                cwd=cwd,
            ).stdout
        )

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

    def changed_path_endpoints(
        self, cwd: Path, base: str, head: str = "HEAD"
    ) -> tuple[str, ...]:
        completed = self._run(
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            f"{base}..{head}",
            cwd=cwd,
        )
        return _nul_records(completed.stdout)

    def commit(
        self,
        cwd: Path,
        message: str,
        *,
        allow_empty: bool = False,
        no_verify: bool = False,
    ) -> str:
        arguments = ["commit"]
        if allow_empty:
            arguments.append("--allow-empty")
        if no_verify:
            arguments.append("--no-verify")
        arguments.extend(("-m", message))
        self._run(*arguments, cwd=cwd)
        return self.head_sha(cwd)

    def commit_index_as_merge(
        self,
        cwd: Path,
        message: str,
        *,
        branch: str,
        target_parent: str,
        source_parent: str,
    ) -> str:
        if (
            re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", target_parent) is None
            or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source_parent) is None
            or target_parent == source_parent
            or not branch
            or "\0" in branch
        ):
            raise GitError("merge commit parents must be distinct object identifiers")
        tree = self.index_tree_sha(cwd)
        commit = self._text(
            self._run(
                "commit-tree",
                tree,
                "-p",
                target_parent,
                "-p",
                source_parent,
                "-m",
                message,
                cwd=cwd,
            ).stdout
        )
        self._run(
            "update-ref",
            f"refs/heads/{branch}",
            commit,
            target_parent,
            cwd=cwd,
        )
        return commit

    def amend_commit_no_edit(self, cwd: Path) -> str:
        self._run("commit", "--amend", "--no-edit", "--no-verify", cwd=cwd)
        return self.head_sha(cwd)

    def push_branch(self, cwd: Path, branch: str) -> None:
        try:
            self._run("push", "-u", "origin", f"HEAD:refs/heads/{branch}", cwd=cwd)
        except GitError as error:
            raise GitRemoteError(str(error)) from error

    def push_branch_create_if_absent(
        self, cwd: Path, branch: str, expected_sha: str
    ) -> None:
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", expected_sha) is None:
            raise GitRemoteError("expected branch head must be an object identifier")
        ref = f"refs/heads/{branch}"
        try:
            self._run(
                "push",
                "--atomic",
                "-u",
                f"--force-with-lease={ref}:",
                "origin",
                f"{expected_sha}:{ref}",
                cwd=cwd,
            )
        except GitError as error:
            raise GitRemoteError(str(error)) from error

    def push_branch_if_at(
        self, cwd: Path, branch: str, expected_remote_sha: str
    ) -> None:
        """Replace a managed remote branch only when it still has the expected head."""
        try:
            self._run(
                "push",
                "-u",
                f"--force-with-lease=refs/heads/{branch}:{expected_remote_sha}",
                "origin",
                f"HEAD:refs/heads/{branch}",
                cwd=cwd,
            )
        except GitError as error:
            raise GitRemoteError(str(error)) from error

    def _run(
        self,
        *args: str,
        cwd: Path | None = None,
        input_bytes: bytes | None = None,
        env: Mapping[str, str] | None = None,
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
                env=dict(env) if env is not None else None,
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
            if not detail:
                detail = _bounded_stderr(stdout)
            if "not a git repository" in detail.lower():
                detail = f"not a Git repository: {detail}"
            raise GitError(
                f"git {command} failed ({process.returncode}): {detail}",
                returncode=process.returncode,
            )
        return GitCompleted(process.returncode, stdout, stderr)

    @staticmethod
    def _text(value: bytes) -> str:
        return value.decode("utf-8", errors="replace").strip()


def _allocated_tree_usage(path: Path) -> GitPathUsage:
    """Count directory entries and allocated blocks without traversing symlinks."""
    allocated_bytes = 0
    entry_count = 0
    accounted_inodes: set[tuple[int, int]] = set()
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            metadata = current.lstat()
        except OSError as error:
            raise GitError(f"unable to inspect compact path {path!r}: {error}") from error
        entry_count += 1
        inode = (metadata.st_dev, metadata.st_ino)
        if inode not in accounted_inodes:
            accounted_inodes.add(inode)
            allocated_bytes += metadata.st_blocks * 512
        if not stat.S_ISDIR(metadata.st_mode):
            continue
        try:
            with os.scandir(current) as entries:
                pending.extend(Path(entry.path) for entry in entries)
        except OSError as error:
            raise GitError(f"unable to inspect compact path {path!r}: {error}") from error
    return GitPathUsage(
        allocated_bytes=allocated_bytes,
        entry_count=entry_count,
    )


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


@overload
def _stop_process_group(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes]: ...


@overload
def _stop_process_group(process: subprocess.Popen[str]) -> tuple[str, str]: ...


def _stop_process_group(
    process: subprocess.Popen[bytes] | subprocess.Popen[str],
) -> tuple[bytes | str, bytes | str]:
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


def _parse_status_porcelain(
    records: tuple[str, ...],
) -> tuple[GitStatusEntry, ...]:
    entries: list[GitStatusEntry] = []
    offset = 0
    while offset < len(records):
        record = records[offset]
        offset += 1
        if (
            len(record) < 4
            or record[2] != " "
            or record[0] not in " MADRCUT?!"
            or record[1] not in " MADCRUT?!"
            or (
                ("?" in record[:2] or "!" in record[:2])
                and record[:2] not in {"??", "!!"}
            )
        ):
            raise GitError("git status returned an invalid porcelain record")
        path = record[3:]
        if not path:
            raise GitError("git status returned an empty porcelain path")
        original_path: str | None = None
        if "R" in record[:2] or "C" in record[:2]:
            if offset == len(records) or not records[offset]:
                raise GitError("git status returned an incomplete rename record")
            original_path = records[offset]
            offset += 1
        entries.append(
            GitStatusEntry(
                index_status=record[0],
                worktree_status=record[1],
                path=path,
                original_path=original_path,
            )
        )
    return tuple(entries)


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

_CONFLICT_MARKER_PREFIXES = (b"<<<<<<<", b"=======", b">>>>>>>", b"|||||||")


def _has_added_conflict_marker(patch: bytes) -> bool:
    for line in patch.splitlines():
        if line.startswith(b"+") and not line.startswith(b"+++"):
            if line[1:].startswith(_CONFLICT_MARKER_PREFIXES):
                return True
    return False

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
