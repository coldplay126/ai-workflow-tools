"""Deterministic, fail-closed workspaces for supervised jobs.

The local adapter deliberately treats canonical clones as read-only sources.  It
creates one named worktree per requested repository and records only the paths
and commits that it owns.  The agentctl adapter has the same public contract,
but delegates workspace lifecycle ownership to ``agentctl``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple, Union

from awf.supervisor.client import RepoRef
from awf.supervisor.recovery import (
    RecoveryCheckpointError,
    normalize_recovery_checkpoint,
)


_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REPO_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_BASE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,254}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_MANIFEST_NAME = "manifest.json"
_AGENTCTL_MANIFEST_NAME = ".awf-supervisor-workspace.json"
_MANIFEST_KIND = "awf-supervisor-workspace"


class WorkspaceError(RuntimeError):
    """A workspace could not be safely prepared or inspected."""


class WorkspaceValidationError(ValueError):
    """An untrusted workspace input does not meet the local safety contract."""


class WorkspaceConflict(WorkspaceError):
    """An existing task or branch cannot safely be reused."""


class WorkspaceRecoveryError(WorkspaceError):
    """A paused job checkpoint cannot safely recover a workspace."""


@dataclass(frozen=True)
class PreparedWorkspace:
    """The only workspace paths an executor may pass to a native OMP batch."""

    cwd: Path
    manifest_path: Path
    repo_paths: Tuple[Path, ...]
    cleanup_token: str


@dataclass(frozen=True)
class RecoveredWorkspace:
    """A retained native workspace or a fresh commit-boundary workspace."""

    prepared: PreparedWorkspace
    resume_native: bool


class WorkspaceAdapter(Protocol):
    """Port used by the Supervisor executor for workspace lifecycle operations."""

    def prepare(
        self,
        *,
        job_id: str,
        generation: int,
        repo_refs: Sequence[RepoRef],
    ) -> PreparedWorkspace:
        ...

    def cleanup(self, prepared: PreparedWorkspace) -> bool:
        ...

    def checkpoint_repositories(
        self,
        prepared: PreparedWorkspace,
        repo_refs: Sequence[Union[RepoRef, Tuple[str, str]]],
    ) -> list[Dict[str, Any]]:
        ...

    def recover(
        self,
        *,
        job_id: str,
        generation: int,
        repo_refs: Sequence[RepoRef],
        checkpoint: Mapping[str, Any],
        current_agent_id: str,
        current_environment: str,
    ) -> RecoveredWorkspace:
        ...


def _command(argv: Sequence[str], *, cwd: Optional[Path] = None) -> subprocess.CompletedProcess[str]:
    """Run one argv-only command, never through a shell."""
    try:
        return subprocess.run(
            list(argv),
            cwd=None if cwd is None else str(cwd),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as error:
        raise WorkspaceError("could not execute workspace command") from error


def _required_command(
    argv: Sequence[str], *, cwd: Optional[Path] = None, error: str
) -> subprocess.CompletedProcess[str]:
    completed = _command(argv, cwd=cwd)
    if completed.returncode != 0:
        raise WorkspaceError(error)
    return completed


def _require_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise WorkspaceValidationError("{} must be a safe identifier".format(field))
    return value


def _require_generation(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise WorkspaceValidationError("generation must be a non-negative integer")
    return value




def _require_mapping(value: Any, field: str, error_type: type[Exception] = WorkspaceRecoveryError) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise error_type("{} must be a mapping".format(field))
    return value


def _normalize_checkpoint_for_recovery(
    checkpoint: Any,
    *,
    job_id: str,
    generation: int,
    repo_refs: Sequence[Tuple[str, str]],
) -> Mapping[str, Any]:
    try:
        return normalize_recovery_checkpoint(
            checkpoint,
            job_id=job_id,
            checkpoint_generation=generation - 1,
            repo_refs=repo_refs,
        )
    except RecoveryCheckpointError as error:
        raise WorkspaceRecoveryError(str(error)) from error


def _safe_ref_value(value: Any, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise WorkspaceValidationError("{} is not safe".format(field))
    return value


def _validate_repo_refs(repo_refs: Sequence[RepoRef]) -> Tuple[RepoRef, ...]:
    if isinstance(repo_refs, (str, bytes)) or not isinstance(repo_refs, Sequence):
        raise WorkspaceValidationError("repo_refs must be a sequence of RepoRef values")
    if not repo_refs:
        raise WorkspaceValidationError("repo_refs must not be empty")

    validated = []
    seen = set()
    for repo_ref in repo_refs:
        if not isinstance(repo_ref, RepoRef):
            raise WorkspaceValidationError("repo_refs must contain only RepoRef values")
        repo = _safe_ref_value(repo_ref.repo, "repo", _REPO_PATTERN)
        base = _safe_ref_value(repo_ref.base, "base", _BASE_PATTERN)
        if repo in seen:
            raise WorkspaceValidationError("repo_refs contains duplicate repo names")
        seen.add(repo)
        checked = _command(("git", "check-ref-format", "--branch", base))
        if checked.returncode != 0:
            raise WorkspaceValidationError("base is not a valid Git branch")
        validated.append(RepoRef(repo=repo, base=base))
    return tuple(validated)


def _branch_name(job_id: str, generation: int) -> str:
    suffix = sha256("{}\n{}".format(job_id, generation).encode("utf-8")).hexdigest()[:20]
    return "awf/supervisor-{}".format(suffix)


def _agentctl_task_name(job_id: str, generation: int) -> str:
    suffix = sha256("{}\n{}".format(job_id, generation).encode("utf-8")).hexdigest()[:20]
    return "awf-{}".format(suffix)


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise WorkspaceRecoveryError("workspace metadata is not canonical JSON") from error


def _read_json_file(path: Path, *, conflict: bool = False) -> Mapping[str, Any]:
    safe_path = _safe_absolute_path(path)
    if safe_path is None or safe_path != path or not _has_no_symlink_components(path):
        if conflict:
            raise WorkspaceConflict("workspace manifest is missing or unsafe")
        raise WorkspaceRecoveryError("workspace manifest is missing or unsafe")
    if path.is_symlink() or not path.is_file():
        if conflict:
            raise WorkspaceConflict("workspace manifest is missing or unsafe")
        raise WorkspaceRecoveryError("workspace manifest is missing or unsafe")
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        if conflict:
            raise WorkspaceConflict("workspace manifest is unreadable") from error
        raise WorkspaceRecoveryError("workspace manifest is unreadable") from error
    if not isinstance(decoded, Mapping):
        if conflict:
            raise WorkspaceConflict("workspace manifest must be an object")
        raise WorkspaceRecoveryError("workspace manifest must be an object")
    return decoded


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Durably publish one complete manifest without a partially written file."""
    safe_path = _safe_absolute_path(path)
    if safe_path is None or safe_path != path or not _has_no_symlink_components(path):
        raise WorkspaceError("workspace manifest path is unsafe")
    encoded = _canonical_json(payload)
    temporary_name: Optional[str] = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".manifest-", dir=str(path.parent))
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    except OSError as error:
        raise WorkspaceError("could not atomically write workspace manifest") from error
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _has_no_symlink_components(path: Path) -> bool:
    """Return whether an absolute lexical path contains no symlink component."""
    if not path.is_absolute() or ".." in path.parts:
        return False
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            if current.is_symlink():
                return False
        except OSError:
            return False
    return True


def _no_symlink_components(root: Path, path: Path) -> bool:
    """Reject a path outside root or with a symlink in any component."""
    return (
        _has_no_symlink_components(root)
        and _has_no_symlink_components(path)
        and _is_under(path, root)
    )


def _safe_absolute_path(value: Any) -> Optional[Path]:
    """Return an absolute path only when its raw spelling has no traversal."""
    if not isinstance(value, (str, os.PathLike)):
        return None
    raw = os.fspath(value)
    if not isinstance(raw, str) or "\x00" in raw:
        return None
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        return None
    return path.absolute()


def _normal_path(path: Path) -> Path:
    return path.absolute()


def _repository_records(value: Any, *, recovery: bool) -> Tuple[Mapping[str, Any], ...]:
    exception = WorkspaceRecoveryError if recovery else WorkspaceConflict
    if not isinstance(value, list) or not value:
        raise exception("workspace repositories must be a non-empty list")
    records = []
    names = set()
    for record in value:
        if not isinstance(record, Mapping):
            raise exception("workspace repository is not an object")
        repo = record.get("repo")
        base = record.get("base")
        if not isinstance(repo, str) or not _REPO_PATTERN.fullmatch(repo):
            raise exception("workspace repository name is unsafe")
        if not isinstance(base, str) or not _BASE_PATTERN.fullmatch(base):
            raise exception("workspace repository base is unsafe")
        if repo in names:
            raise exception("workspace repository names are not unique")
        names.add(repo)
        records.append(record)
    return tuple(records)







def _remote_head_ref(base: str) -> str:
    return "refs/heads/{}".format(base)


def _tracking_ref(base: str) -> str:
    return "refs/remotes/origin/{}".format(base)


def _checkpoint_repo_refs(
    repo_refs: Sequence[Union[RepoRef, Tuple[str, str]]],
) -> Tuple[Tuple[str, str], ...]:
    if isinstance(repo_refs, (str, bytes)) or not isinstance(repo_refs, Sequence):
        raise WorkspaceError("checkpoint repo refs must be a sequence")
    normalized = []
    names = set()
    for reference in repo_refs:
        if isinstance(reference, RepoRef):
            repo, base = reference.repo, reference.base
        elif (
            isinstance(reference, tuple)
            and len(reference) == 2
            and isinstance(reference[0], str)
            and isinstance(reference[1], str)
        ):
            repo, base = reference
        else:
            raise WorkspaceError("checkpoint repo ref is invalid")
        if not _REPO_PATTERN.fullmatch(repo) or not _BASE_PATTERN.fullmatch(base) or repo in names:
            raise WorkspaceError("checkpoint repo ref is unsafe")
        names.add(repo)
        normalized.append((repo, base))
    if not normalized:
        raise WorkspaceError("checkpoint repo refs must not be empty")
    return tuple(normalized)


def _checkpoint_row(
    *, worktree: Path, repo: str, base: str, initial_commit: Optional[str]
) -> Dict[str, Any]:
    """Return fail-closed checkpoint evidence without fetching or changing Git state."""
    remote_ref = _remote_head_ref(base)
    unsafe = {
        "repo": repo,
        "base": base,
        "head": initial_commit if isinstance(initial_commit, str) else "",
        "remote_ref": remote_ref,
        "clean": False,
        "pushed": False,
    }
    if worktree.is_symlink() or not worktree.is_dir():
        return unsafe
    head_result = _command(
        ("git", "-C", str(worktree), "rev-parse", "--verify", "HEAD^{commit}")
    )
    head = head_result.stdout.strip()
    if head_result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
        return unsafe
    row = dict(unsafe)
    row["head"] = head
    status = _command(("git", "-C", str(worktree), "status", "--porcelain"))
    if status.returncode != 0:
        return row
    row["clean"] = status.stdout == ""
    tracking = _command(
        (
            "git",
            "-C",
            str(worktree),
            "rev-parse",
            "--verify",
            "{}^{{commit}}".format(_tracking_ref(base)),
        )
    )
    remote_head = tracking.stdout.strip()
    if tracking.returncode != 0 or remote_head != head:
        return row
    ahead = _command(
        (
            "git",
            "-C",
            str(worktree),
            "rev-list",
            "--count",
            "{}..HEAD".format(_tracking_ref(base)),
        )
    )
    try:
        no_commits_ahead = ahead.returncode == 0 and int(ahead.stdout.strip()) == 0
    except ValueError:
        no_commits_ahead = False
    if initial_commit is not None and head != initial_commit:
        no_commits_ahead = False
    row["pushed"] = row["clean"] is True and no_commits_ahead
    return row


class LocalGitWorkspaceAdapter:
    """Prepare deterministic Git worktrees without changing canonical clones."""

    def __init__(
        self,
        *,
        github_root: Union[str, os.PathLike[str]],
        state_root: Union[str, os.PathLike[str]],
    ) -> None:
        github_path = _safe_absolute_path(github_root)
        state_path = _safe_absolute_path(state_root)
        if (
            github_path is None
            or state_path is None
            or not _no_symlink_components(github_path, github_path)
            or not _no_symlink_components(state_path, state_path)
        ):
            raise WorkspaceValidationError("workspace roots must be symlink-free absolute paths")
        self._github_root = github_path
        self._state_root = state_path

    def prepare(
        self,
        *,
        job_id: str,
        generation: int,
        repo_refs: Sequence[RepoRef],
    ) -> PreparedWorkspace:
        validated_job_id = _require_identifier(job_id, "job_id")
        validated_generation = _require_generation(generation)
        validated_refs = _validate_repo_refs(repo_refs)
        return self._prepare(validated_job_id, validated_generation, validated_refs, expected_commits=None)

    def cleanup(self, prepared: PreparedWorkspace) -> bool:
        """Remove only an unchanged, agent-owned local worktree task."""
        try:
            manifest, task_dir, workspace, branch = self._validate_cleanup_prepared(prepared)
            records = _repository_records(manifest.get("repositories"), recovery=False)
            allowed_workspace_names = {"AGENTS.md"}
            allowed_workspace_names.update(record["repo"] for record in records)
            if {entry.name for entry in workspace.iterdir()} != allowed_workspace_names:
                return False
            if {entry.name for entry in task_dir.iterdir()} != {_MANIFEST_NAME, "workspace"}:
                return False

            removals = []
            for record in records:
                canonical = _safe_absolute_path(record["canonical_path"])
                worktree = _safe_absolute_path(record["worktree_path"])
                repo = record["repo"]
                remote_ref = record["remote_ref"]
                initial_commit = record["commit"]
                if canonical is None or worktree is None or not self._safe_canonical_path(repo, canonical):
                    return False
                if worktree != workspace / repo or not _no_symlink_components(workspace, worktree):
                    return False
                if not worktree.is_dir() or worktree.is_symlink():
                    return False
                if _command(("git", "-C", str(worktree), "status", "--porcelain")).stdout:
                    return False
                if _command(("git", "-C", str(worktree), "status", "--porcelain")).returncode != 0:
                    return False
                branch_result = _command(
                    ("git", "-C", str(worktree), "branch", "--show-current")
                )
                if branch_result.returncode != 0 or branch_result.stdout.strip() != branch:
                    return False
                if self._has_unpushed_commits(worktree, _tracking_ref(record["base"]), initial_commit):
                    return False
                removals.append((canonical, worktree))

            for canonical, worktree in removals:
                removed = _command(
                    ("git", "-C", str(canonical), "worktree", "remove", str(worktree))
                )
                if removed.returncode != 0:
                    return False

            agents_path = workspace / "AGENTS.md"
            if agents_path.is_symlink() or not agents_path.is_file():
                return False
            agents_path.unlink()
            manifest_path = task_dir / _MANIFEST_NAME
            if manifest_path.is_symlink() or not manifest_path.is_file():
                return False
            manifest_path.unlink()
            workspace.rmdir()
            task_dir.rmdir()
            return True
        except (OSError, TypeError, ValueError, WorkspaceError):
            return False

    def checkpoint_repositories(
        self,
        prepared: PreparedWorkspace,
        repo_refs: Sequence[Union[RepoRef, Tuple[str, str]]],
    ) -> list[Dict[str, Any]]:
        """Attest exact retained Git state without fetching or mutating it."""
        manifest, _task_dir, workspace, _branch = self._validate_cleanup_prepared(prepared)
        records = _repository_records(manifest.get("repositories"), recovery=False)
        expected_refs = _checkpoint_repo_refs(repo_refs)
        actual_refs = tuple((record["repo"], record["base"]) for record in records)
        if actual_refs != expected_refs:
            raise WorkspaceError("checkpoint repo refs do not match workspace manifest")
        rows = []
        for record in records:
            worktree = _safe_absolute_path(record["worktree_path"])
            if worktree is None or worktree != workspace / record["repo"] or not _no_symlink_components(
                workspace, worktree
            ):
                raise WorkspaceError("checkpoint worktree path is unsafe")
            rows.append(
                _checkpoint_row(
                    worktree=worktree,
                    repo=record["repo"],
                    base=record["base"],
                    initial_commit=record["commit"],
                )
            )
        return rows

    def recover(
        self,
        *,
        job_id: str,
        generation: int,
        repo_refs: Sequence[RepoRef],
        checkpoint: Mapping[str, Any],
        current_agent_id: str,
        current_environment: str,
    ) -> RecoveredWorkspace:
        validated_job_id = _require_identifier(job_id, "job_id")
        validated_generation = _require_generation(generation)
        _require_identifier(current_agent_id, "current_agent_id")
        _require_identifier(current_environment, "current_environment")
        validated_refs = _validate_repo_refs(repo_refs)
        checkpoint = _normalize_checkpoint_for_recovery(
            checkpoint,
            job_id=validated_job_id,
            generation=validated_generation,
            repo_refs=tuple((ref.repo, ref.base) for ref in validated_refs),
        )
        prior_generation = checkpoint["generation"]
        checkpoint_agent = checkpoint["origin_agent_id"]
        checkpoint_environment = checkpoint["origin_environment"]

        if checkpoint_agent == current_agent_id and checkpoint_environment == current_environment:
            return RecoveredWorkspace(
                prepared=self._recover_retained_native(
                    validated_job_id, prior_generation, checkpoint
                ),
                resume_native=True,
            )

        if checkpoint["cross_node_eligible"] is not True:
            raise WorkspaceRecoveryError("checkpoint is not eligible for cross-node recovery")
        records = tuple(checkpoint["repos"])
        for record in records:
            if record["clean"] is not True or record["pushed"] is not True:
                raise WorkspaceRecoveryError("checkpoint has uncommitted or unpushed source state")
        refs = validated_refs
        expected_commits = {record["repo"]: record["head"] for record in records}
        prepared = self._prepare(
            validated_job_id,
            validated_generation,
            refs,
            expected_commits=expected_commits,
        )
        return RecoveredWorkspace(prepared=prepared, resume_native=False)

    def _prepare(
        self,
        job_id: str,
        generation: int,
        repo_refs: Tuple[RepoRef, ...],
        *,
        expected_commits: Optional[Mapping[str, str]],
    ) -> PreparedWorkspace:
        task_dir, workspace, manifest_path = self._task_paths(job_id, generation)
        branch = _branch_name(job_id, generation)

        if manifest_path.exists() or manifest_path.is_symlink():
            prepared = self._prepared_from_manifest(
                manifest_path, job_id=job_id, generation=generation, conflict=True
            )
            manifest = _read_json_file(manifest_path, conflict=True)
            records = _repository_records(manifest.get("repositories"), recovery=False)
            if tuple((record["repo"], record["base"]) for record in records) != tuple(
                (ref.repo, ref.base) for ref in repo_refs
            ):
                raise WorkspaceConflict("existing workspace has different repository bases")
            if expected_commits is not None and any(
                record.get("commit") != expected_commits.get(record["repo"]) for record in records
            ):
                raise WorkspaceConflict("existing workspace has different immutable commits")
            return prepared
        if task_dir.exists() or task_dir.is_symlink():
            raise WorkspaceConflict("workspace generation directory already exists without a manifest")

        resolved = []
        for repo_ref in repo_refs:
            canonical = self._canonical_clone(repo_ref.repo)
            self._assert_canonical_clean(canonical)
            remote_ref, commit = self._fetch_and_resolve(canonical, repo_ref.base)
            if expected_commits is not None and commit != expected_commits.get(repo_ref.repo):
                raise WorkspaceRecoveryError("remote ref no longer resolves to checkpoint commit")
            resolved.append((repo_ref, canonical, remote_ref, commit))

        for _repo_ref, canonical, _remote_ref, _commit in resolved:
            branch_check = _command(
                ("git", "-C", str(canonical), "show-ref", "--verify", "--quiet", "refs/heads/{}".format(branch))
            )
            if branch_check.returncode == 0:
                raise WorkspaceConflict("deterministic workspace branch already exists")
            if branch_check.returncode not in (0, 1):
                raise WorkspaceError("could not inspect deterministic workspace branch")

        try:
            task_dir.mkdir(parents=True, exist_ok=False)
            if not _no_symlink_components(self._state_root, task_dir):
                raise WorkspaceConflict("workspace generation path contains a symlink")
            workspace.mkdir()
            repo_paths = []
            records = []
            for repo_ref, canonical, remote_ref, commit in resolved:
                worktree = workspace / repo_ref.repo
                added = _command(
                    (
                        "git",
                        "-C",
                        str(canonical),
                        "worktree",
                        "add",
                        "-b",
                        branch,
                        str(worktree),
                        _tracking_ref(repo_ref.base),
                    )
                )
                if added.returncode != 0:
                    raise WorkspaceError("could not create isolated Git worktree")
                repo_paths.append(worktree)
                records.append(
                    {
                        "repo": repo_ref.repo,
                        "base": repo_ref.base,
                        "branch": branch,
                        "canonical_path": str(canonical),
                        "worktree_path": str(worktree),
                        "remote_ref": remote_ref,
                        "commit": commit,
                    }
                )
            self._write_agents_instructions(workspace, records)
            manifest: Dict[str, Any] = {
                "schema_version": 1,
                "kind": _MANIFEST_KIND,
                "adapter": "local-git",
                "job_id": job_id,
                "generation": generation,
                "cleanup_token": branch,
                "cwd": str(workspace),
                "repositories": records,
            }
            _atomic_json(manifest_path, manifest)
        except FileExistsError as error:
            raise WorkspaceConflict("workspace generation directory already exists") from error

        return PreparedWorkspace(
            cwd=workspace,
            manifest_path=manifest_path,
            repo_paths=tuple(repo_paths),
            cleanup_token=branch,
        )

    def _task_paths(self, job_id: str, generation: int) -> Tuple[Path, Path, Path]:
        if self._state_root.is_symlink() or self._github_root.is_symlink():
            raise WorkspaceConflict("configured workspace root must not be a symlink")
        task_dir = self._state_root / "jobs" / job_id / "g{}".format(generation)
        workspace = task_dir / "workspace"
        manifest_path = task_dir / _MANIFEST_NAME
        if not _no_symlink_components(self._state_root, task_dir):
            raise WorkspaceConflict("workspace generation path contains a symlink")
        return task_dir, workspace, manifest_path

    def _canonical_clone(self, repo: str) -> Path:
        canonical = self._github_root / repo
        if not self._safe_canonical_path(repo, canonical) or not canonical.is_dir():
            raise WorkspaceError("canonical repository clone is missing or unsafe")
        return canonical

    def _safe_canonical_path(self, repo: str, canonical: Path) -> bool:
        expected = self._github_root / repo
        return (
            _normal_path(canonical) == expected
            and _no_symlink_components(self._github_root, expected)
            and expected.is_dir()
            and not expected.is_symlink()
        )

    @staticmethod
    def _assert_canonical_clean(canonical: Path) -> None:
        clean = _command(("git", "-C", str(canonical), "status", "--porcelain"))
        if clean.returncode != 0:
            raise WorkspaceError("canonical repository is not a usable Git clone")
        if clean.stdout:
            raise WorkspaceError("canonical repository has uncommitted changes")

    @staticmethod
    def _fetch_and_resolve(canonical: Path, base: str) -> Tuple[str, str]:
        fetched = _command(("git", "-C", str(canonical), "fetch", "--prune", "origin", base))
        if fetched.returncode != 0:
            raise WorkspaceError("could not fetch requested origin base")
        tracking_ref = _tracking_ref(base)
        resolved = _command(
            (
                "git",
                "-C",
                str(canonical),
                "rev-parse",
                "--verify",
                "{}^{{commit}}".format(tracking_ref),
            )
        )
        commit = resolved.stdout.strip()
        if resolved.returncode != 0 or not _COMMIT_PATTERN.fullmatch(commit):
            raise WorkspaceError("requested origin base does not resolve to a commit")
        return _remote_head_ref(base), commit

    @staticmethod
    def _write_agents_instructions(workspace: Path, records: Sequence[Mapping[str, Any]]) -> None:
        lines = ["# AWF Supervisor workspace", ""]
        for record in records:
            lines.extend(
                (
                    "- repository: {}".format(record["repo"]),
                    "  base: {}".format(record["base"]),
                    "  branch: {}".format(record["branch"]),
                    "  worktree: {}".format(record["worktree_path"]),
                )
            )
        try:
            (workspace / "AGENTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as error:
            raise WorkspaceError("could not write task instructions") from error

    def _prepared_from_manifest(
        self, manifest_path: Path, *, job_id: str, generation: int, conflict: bool
    ) -> PreparedWorkspace:
        safe_manifest_path = _safe_absolute_path(manifest_path)
        if safe_manifest_path is None or safe_manifest_path != manifest_path:
            error_type = WorkspaceConflict if conflict else WorkspaceRecoveryError
            raise error_type("workspace manifest path is unsafe")
        manifest = _read_json_file(manifest_path, conflict=conflict)
        error_type = WorkspaceConflict if conflict else WorkspaceRecoveryError
        task_dir, workspace, expected_manifest_path = self._task_paths(job_id, generation)
        if manifest_path != expected_manifest_path:
            raise error_type("workspace manifest path is not generated for this task")
        if (
            manifest.get("schema_version") != 1
            or manifest.get("kind") != _MANIFEST_KIND
            or manifest.get("adapter") != "local-git"
            or manifest.get("job_id") != job_id
            or manifest.get("generation") != generation
            or manifest.get("cwd") != str(workspace)
        ):
            raise error_type("workspace manifest identity does not match")
        branch = _branch_name(job_id, generation)
        if manifest.get("cleanup_token") != branch:
            raise error_type("workspace manifest cleanup token does not match")
        records = _repository_records(manifest.get("repositories"), recovery=not conflict)
        repo_paths = []
        for record in records:
            repo = record["repo"]
            canonical_value = record.get("canonical_path")
            worktree_value = record.get("worktree_path")
            remote_ref = record.get("remote_ref")
            commit = record.get("commit")
            if (
                record.get("branch") != branch
                or not isinstance(canonical_value, str)
                or not isinstance(worktree_value, str)
                or remote_ref != _remote_head_ref(record["base"])
                or not isinstance(commit, str)
                or not _COMMIT_PATTERN.fullmatch(commit)
            ):
                raise error_type("workspace manifest repository identity does not match")
            canonical = _safe_absolute_path(canonical_value)
            worktree = _safe_absolute_path(worktree_value)
            if canonical is None or worktree is None or not self._safe_canonical_path(repo, canonical):
                raise error_type("workspace manifest canonical path is unsafe")
            if worktree != workspace / repo or not _no_symlink_components(workspace, worktree):
                raise error_type("workspace manifest worktree path is unsafe")
            if not worktree.is_dir() or worktree.is_symlink():
                raise error_type("workspace worktree is not retained")
            repo_paths.append(worktree)
        agents_path = workspace / "AGENTS.md"
        if agents_path.is_symlink() or not agents_path.is_file():
            raise error_type("workspace task instructions are missing or unsafe")
        return PreparedWorkspace(
            cwd=workspace,
            manifest_path=manifest_path,
            repo_paths=tuple(repo_paths),
            cleanup_token=branch,
        )

    def _validate_cleanup_prepared(
        self, prepared: PreparedWorkspace
    ) -> Tuple[Mapping[str, Any], Path, Path, str]:
        if not isinstance(prepared, PreparedWorkspace):
            raise WorkspaceConflict("cleanup requires a prepared workspace")
        manifest_path = _safe_absolute_path(prepared.manifest_path)
        if manifest_path is None:
            raise WorkspaceConflict("cleanup manifest path is unsafe")
        manifest = _read_json_file(manifest_path, conflict=True)
        job_id = _require_identifier(manifest.get("job_id"), "job_id")
        generation = _require_generation(manifest.get("generation"))
        task_dir, workspace, expected_manifest_path = self._task_paths(job_id, generation)
        if manifest_path != expected_manifest_path:
            raise WorkspaceConflict("cleanup manifest path is not generated")
        checked = self._prepared_from_manifest(
            manifest_path, job_id=job_id, generation=generation, conflict=True
        )
        if (
            prepared.cwd != checked.cwd
            or prepared.manifest_path != checked.manifest_path
            or prepared.repo_paths != checked.repo_paths
            or prepared.cleanup_token != checked.cleanup_token
        ):
            raise WorkspaceConflict("cleanup prepared workspace does not match manifest")
        return manifest, task_dir, workspace, checked.cleanup_token

    @staticmethod
    def _has_unpushed_commits(worktree: Path, remote_ref: str, initial_commit: str) -> bool:
        upstream = _command(
            (
                "git",
                "-C",
                str(worktree),
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                "@{upstream}",
            )
        )
        comparison_ref = upstream.stdout.strip() if upstream.returncode == 0 else remote_ref
        compared = _command(
            ("git", "-C", str(worktree), "rev-list", "--count", "{}..HEAD".format(comparison_ref))
        )
        if compared.returncode != 0:
            return True
        try:
            return int(compared.stdout.strip()) > 0
        except ValueError:
            return True


    def _recover_retained_native(
        self, job_id: str, prior_generation: int, checkpoint: Mapping[str, Any]
    ) -> PreparedWorkspace:
        _task_dir, _workspace, manifest_path = self._task_paths(job_id, prior_generation)
        digest = checkpoint["workspace_manifest_sha256"]
        try:
            actual_digest = sha256(manifest_path.read_bytes()).hexdigest()
        except OSError as error:
            raise WorkspaceRecoveryError("retained workspace manifest is unavailable") from error
        if actual_digest != digest:
            raise WorkspaceRecoveryError("retained workspace manifest digest does not match")
        prepared = self._prepared_from_manifest(
            manifest_path, job_id=job_id, generation=prior_generation, conflict=False
        )
        manifest = _read_json_file(manifest_path, conflict=False)
        manifest_records = _repository_records(manifest.get("repositories"), recovery=True)
        checkpoint_records = checkpoint["repos"]
        if len(manifest_records) != len(checkpoint_records):
            raise WorkspaceRecoveryError("retained workspace repository count does not match")
        for manifest_record, checkpoint_record in zip(manifest_records, checkpoint_records):
            for field in ("repo", "base", "commit", "remote_ref"):
                checkpoint_field = "head" if field == "commit" else field
                if manifest_record.get(field) != checkpoint_record[checkpoint_field]:
                    raise WorkspaceRecoveryError("retained workspace repository identity does not match")
        return prepared


class AgentctlWorkspaceAdapter:
    """Use agentctl tasks while preserving the common workspace safety contract."""

    def __init__(
        self,
        *,
        repo_root: Union[str, os.PathLike[str]],
        workspace_root: Union[str, os.PathLike[str]],
        agentctl_path: Union[str, os.PathLike[str]] = "agentctl",
    ) -> None:
        self._repo_root = self._configured_root(repo_root, "repo_root")
        self._workspace_root = self._configured_root(workspace_root, "workspace_root")
        self._agentctl_path = os.fspath(agentctl_path)

    def prepare(
        self,
        *,
        job_id: str,
        generation: int,
        repo_refs: Sequence[RepoRef],
    ) -> PreparedWorkspace:
        validated_job_id = _require_identifier(job_id, "job_id")
        validated_generation = _require_generation(generation)
        validated_refs = _validate_repo_refs(repo_refs)
        self._assert_roots(WorkspaceError)
        for ref in validated_refs:
            self._repository_path(ref.repo, WorkspaceError)

        task_name = _agentctl_task_name(validated_job_id, validated_generation)
        arguments = [self._agentctl_path, "task-create", task_name]
        arguments.extend("{}:{}".format(ref.repo, ref.base) for ref in validated_refs)
        _command(arguments)

        task_path, records = self._live_task(
            task_name, validated_refs, error_type=WorkspaceConflict
        )
        return self._write_or_load_manifest(
            task_path,
            task_name,
            validated_job_id,
            validated_generation,
            records,
        )

    def cleanup(self, prepared: PreparedWorkspace) -> bool:
        try:
            manifest, checked = self._checked_prepared(prepared)
            job_id = _require_identifier(manifest.get("job_id"), "job_id")
            generation = _require_generation(manifest.get("generation"))
            task_name = _agentctl_task_name(job_id, generation)
            refs = tuple(
                RepoRef(repo=record["repo"], base=record["base"])
                for record in _repository_records(manifest.get("repositories"), recovery=False)
            )
            task_path, records = self._live_task(
                task_name, refs, error_type=WorkspaceConflict
            )
            if task_path != checked.cwd or tuple(
                Path(record["worktree_path"]) for record in records
            ) != checked.repo_paths:
                return False
            removed = _command((self._agentctl_path, "task-remove", task_name))
            return removed.returncode == 0
        except (OSError, TypeError, ValueError, WorkspaceError):
            return False

    def checkpoint_repositories(
        self,
        prepared: PreparedWorkspace,
        repo_refs: Sequence[Union[RepoRef, Tuple[str, str]]],
    ) -> list[Dict[str, Any]]:
        """Attest task Git state through its manifest, without network access."""
        manifest, checked = self._checked_prepared(prepared)
        records = _repository_records(manifest.get("repositories"), recovery=False)
        expected_refs = _checkpoint_repo_refs(repo_refs)
        actual_refs = tuple((record["repo"], record["base"]) for record in records)
        if actual_refs != expected_refs:
            raise WorkspaceError("checkpoint repo refs do not match task manifest")
        refs = tuple(RepoRef(repo=repo, base=base) for repo, base in expected_refs)
        task_name = _agentctl_task_name(
            _require_identifier(manifest.get("job_id"), "job_id"),
            _require_generation(manifest.get("generation")),
        )
        task_path, live_records = self._live_task(
            task_name, refs, error_type=WorkspaceConflict
        )
        if task_path != checked.cwd or tuple(
            Path(record["worktree_path"]) for record in live_records
        ) != checked.repo_paths:
            raise WorkspaceError("agentctl task no longer matches workspace manifest")
        return self._checkpoint_rows(records, checked.repo_paths)

    def recover(
        self,
        *,
        job_id: str,
        generation: int,
        repo_refs: Sequence[RepoRef],
        checkpoint: Mapping[str, Any],
        current_agent_id: str,
        current_environment: str,
    ) -> RecoveredWorkspace:
        validated_job_id = _require_identifier(job_id, "job_id")
        validated_generation = _require_generation(generation)
        _require_identifier(current_agent_id, "current_agent_id")
        _require_identifier(current_environment, "current_environment")
        validated_refs = _validate_repo_refs(repo_refs)
        checkpoint = _normalize_checkpoint_for_recovery(
            checkpoint,
            job_id=validated_job_id,
            generation=validated_generation,
            repo_refs=tuple((ref.repo, ref.base) for ref in validated_refs),
        )
        prior_generation = checkpoint["generation"]
        checkpoint_agent = checkpoint["origin_agent_id"]
        checkpoint_environment = checkpoint["origin_environment"]
        if checkpoint_agent == current_agent_id and checkpoint_environment == current_environment:
            prepared = self._recover_retained_native(
                validated_job_id, prior_generation, checkpoint
            )
            return RecoveredWorkspace(prepared=prepared, resume_native=True)

        if checkpoint["cross_node_eligible"] is not True:
            raise WorkspaceRecoveryError("checkpoint is not eligible for cross-node recovery")
        records = tuple(checkpoint["repos"])
        for record in records:
            if record["clean"] is not True or record["pushed"] is not True:
                raise WorkspaceRecoveryError("checkpoint has uncommitted or unpushed source state")
        prepared = self.prepare(
            job_id=validated_job_id,
            generation=validated_generation,
            repo_refs=validated_refs,
        )
        for record, repo_path in zip(records, prepared.repo_paths):
            fetched = _command(
                ("git", "-C", str(repo_path), "fetch", "--prune", "origin", record["base"])
            )
            if fetched.returncode != 0:
                raise WorkspaceRecoveryError("could not fetch agentctl workspace remote ref")
            resolved = _command(
                (
                    "git",
                    "-C",
                    str(repo_path),
                    "rev-parse",
                    "--verify",
                    "{}^{{commit}}".format(_tracking_ref(record["base"])),
                )
            )
            if resolved.returncode != 0 or resolved.stdout.strip() != record["head"]:
                raise WorkspaceRecoveryError("agentctl workspace remote ref does not match checkpoint")
        return RecoveredWorkspace(prepared=prepared, resume_native=False)

    @staticmethod
    def _configured_root(
        value: Union[str, os.PathLike[str]], field: str
    ) -> Path:
        root = _safe_absolute_path(value)
        if root is None or not root.is_dir() or not _no_symlink_components(root, root):
            raise WorkspaceValidationError("{} must be an existing symlink-free directory".format(field))
        return root

    def _assert_roots(self, error_type: type[Exception]) -> None:
        for root, field in (
            (self._repo_root, "repository root"),
            (self._workspace_root, "workspace root"),
        ):
            if not root.is_dir() or not _no_symlink_components(root, root):
                raise error_type("{} is missing or unsafe".format(field))

    def _repository_path(self, repo: str, error_type: type[Exception]) -> Path:
        self._assert_roots(error_type)
        path = self._repo_root / repo
        if (
            not _no_symlink_components(self._repo_root, path)
            or path.is_symlink()
            or not path.is_dir()
        ):
            raise error_type("agentctl repository path is missing or unsafe")
        return path

    def _expected_task_path(
        self, task_name: str, error_type: type[Exception], *, exists: bool
    ) -> Path:
        self._assert_roots(error_type)
        path = self._workspace_root / "tasks" / task_name
        if not _no_symlink_components(self._workspace_root, path):
            raise error_type("agentctl task path is unsafe")
        if exists and (path.is_symlink() or not path.is_dir()):
            raise error_type("agentctl task path is missing or unsafe")
        return path

    def _worktree_path(
        self,
        repo: str,
        task_name: str,
        value: Any,
        error_type: type[Exception],
    ) -> Path:
        path = _safe_absolute_path(value)
        expected = self._workspace_root / "worktrees" / repo / task_name
        if (
            path is None
            or path != expected
            or not _no_symlink_components(self._workspace_root, path)
            or path.is_symlink()
            or not path.is_dir()
        ):
            raise error_type("agentctl worktree path is missing or unsafe")
        return path

    def _task_path(self, task_name: str, error_type: type[Exception]) -> Path:
        expected = self._expected_task_path(task_name, error_type, exists=True)
        located = _command((self._agentctl_path, "task-path", task_name))
        if located.returncode != 0:
            raise error_type("agentctl did not return a task path")
        path = _safe_absolute_path(located.stdout.strip())
        if (
            path is None
            or path != expected
            or not _no_symlink_components(self._workspace_root, path)
            or path.is_symlink()
            or not path.is_dir()
        ):
            raise error_type("agentctl returned an unsafe task path")
        return path

    def _task_status(
        self, task_name: str, error_type: type[Exception]
    ) -> Mapping[str, Any]:
        status = _command((self._agentctl_path, "task-status", task_name))
        if status.returncode != 0:
            raise error_type("agentctl could not prove task identity")
        try:
            decoded = json.loads(status.stdout)
        except json.JSONDecodeError as error:
            raise error_type("agentctl task status is not JSON") from error
        if not isinstance(decoded, Mapping):
            raise error_type("agentctl task status is not an object")
        return decoded

    def _status_records(
        self,
        status: Mapping[str, Any],
        task_name: str,
        refs: Sequence[RepoRef],
        error_type: type[Exception],
    ) -> Tuple[Dict[str, str], ...]:
        if status.get("task_name") != task_name:
            raise error_type("agentctl task status has an invalid task name")
        source_records = status.get("repositories")
        if not isinstance(source_records, list) or len(source_records) != len(refs):
            raise error_type("agentctl task status has invalid repositories")
        records = []
        seen = set()
        for source, ref in zip(source_records, refs):
            if not isinstance(source, Mapping):
                raise error_type("agentctl task status repository is invalid")
            repo = source.get("repo")
            base = source.get("base")
            if (
                repo != ref.repo
                or base != ref.base
                or not isinstance(repo, str)
                or not _REPO_PATTERN.fullmatch(repo)
                or repo in seen
            ):
                raise error_type("agentctl task status repository is invalid")
            seen.add(repo)
            canonical = self._repository_path(repo, error_type)
            worktree = self._worktree_path(
                repo, task_name, source.get("worktree_path"), error_type
            )
            records.append(
                {
                    "repo": repo,
                    "base": base,
                    "canonical_path": str(canonical),
                    "worktree_path": str(worktree),
                }
            )
        return tuple(records)

    def _live_task(
        self,
        task_name: str,
        refs: Sequence[RepoRef],
        *,
        error_type: type[Exception],
    ) -> Tuple[Path, Tuple[Dict[str, str], ...]]:
        task_path = self._task_path(task_name, error_type)
        records = self._status_records(
            self._task_status(task_name, error_type),
            task_name,
            refs,
            error_type,
        )
        return task_path, records

    def _manifest_path(
        self, task_name: str, error_type: type[Exception], *, exists: bool
    ) -> Path:
        task_path = self._expected_task_path(task_name, error_type, exists=True)
        path = task_path / _AGENTCTL_MANIFEST_NAME
        if not _no_symlink_components(self._workspace_root, path):
            raise error_type("agentctl manifest path is unsafe")
        if exists and (path.is_symlink() or not path.is_file()):
            raise error_type("agentctl manifest path is missing or unsafe")
        return path

    def _write_or_load_manifest(
        self,
        task_path: Path,
        task_name: str,
        job_id: str,
        generation: int,
        records: Tuple[Dict[str, str], ...],
    ) -> PreparedWorkspace:
        expected_task_path = self._expected_task_path(
            task_name, WorkspaceConflict, exists=True
        )
        if task_path != expected_task_path:
            raise WorkspaceConflict("agentctl task path is not generated for this task")
        manifest_path = self._manifest_path(task_name, WorkspaceConflict, exists=False)
        if manifest_path.exists() or manifest_path.is_symlink():
            prepared = self._prepared_from_manifest(
                manifest_path, job_id=job_id, generation=generation
            )
            if prepared.repo_paths != tuple(
                Path(record["worktree_path"]) for record in records
            ):
                raise WorkspaceConflict("existing agentctl manifest does not match task status")
            return prepared
        manifest: Dict[str, Any] = {
            "schema_version": 1,
            "kind": _MANIFEST_KIND,
            "adapter": "agentctl",
            "job_id": job_id,
            "generation": generation,
            "cleanup_token": task_name,
            "task_name": task_name,
            "cwd": str(task_path),
            "repositories": list(records),
        }
        _atomic_json(manifest_path, manifest)
        return PreparedWorkspace(
            cwd=task_path,
            manifest_path=manifest_path,
            repo_paths=tuple(Path(record["worktree_path"]) for record in records),
            cleanup_token=task_name,
        )

    def _prepared_from_manifest(
        self,
        manifest_path: Path,
        *,
        job_id: str,
        generation: int,
        error_type: type[Exception] = WorkspaceConflict,
    ) -> PreparedWorkspace:
        task_name = _agentctl_task_name(job_id, generation)
        expected_manifest_path = self._manifest_path(task_name, error_type, exists=True)
        path = _safe_absolute_path(manifest_path)
        if (
            path is None
            or path != expected_manifest_path
            or not _no_symlink_components(self._workspace_root, path)
            or path.is_symlink()
            or not path.is_file()
        ):
            raise error_type("agentctl manifest path is not generated for this task")
        manifest = _read_json_file(path, conflict=error_type is not WorkspaceRecoveryError)
        task_path = self._expected_task_path(task_name, error_type, exists=True)
        if (
            manifest.get("schema_version") != 1
            or manifest.get("kind") != _MANIFEST_KIND
            or manifest.get("adapter") != "agentctl"
            or manifest.get("job_id") != job_id
            or manifest.get("generation") != generation
            or manifest.get("cleanup_token") != task_name
            or manifest.get("task_name") != task_name
            or manifest.get("cwd") != str(task_path)
        ):
            raise error_type("agentctl workspace manifest identity does not match")
        records = _repository_records(
            manifest.get("repositories"), recovery=error_type is WorkspaceRecoveryError
        )
        repo_paths = []
        for record in records:
            canonical = _safe_absolute_path(record.get("canonical_path"))
            expected_canonical = self._repository_path(record["repo"], error_type)
            if canonical != expected_canonical:
                raise error_type("agentctl manifest repository path is unsafe")
            repo_paths.append(
                self._worktree_path(
                    record["repo"],
                    task_name,
                    record.get("worktree_path"),
                    error_type,
                )
            )
        return PreparedWorkspace(
            cwd=task_path,
            manifest_path=path,
            repo_paths=tuple(repo_paths),
            cleanup_token=task_name,
        )

    def _checked_prepared(
        self, prepared: PreparedWorkspace
    ) -> Tuple[Mapping[str, Any], PreparedWorkspace]:
        if not isinstance(prepared, PreparedWorkspace):
            raise WorkspaceConflict("workspace must be prepared by this adapter")
        candidate = _safe_absolute_path(prepared.manifest_path)
        if (
            candidate is None
            or candidate.name != _AGENTCTL_MANIFEST_NAME
            or not _no_symlink_components(self._workspace_root, candidate)
            or candidate.is_symlink()
            or not candidate.is_file()
        ):
            raise WorkspaceConflict("prepared manifest path is unsafe")
        manifest = _read_json_file(candidate, conflict=True)
        job_id = _require_identifier(manifest.get("job_id"), "job_id")
        generation = _require_generation(manifest.get("generation"))
        checked = self._prepared_from_manifest(
            candidate, job_id=job_id, generation=generation
        )
        if (
            prepared.cwd != checked.cwd
            or prepared.manifest_path != checked.manifest_path
            or prepared.repo_paths != checked.repo_paths
            or prepared.cleanup_token != checked.cleanup_token
        ):
            raise WorkspaceConflict("prepared workspace does not match manifest")
        return manifest, checked

    @staticmethod
    def _checkpoint_rows(
        records: Sequence[Mapping[str, Any]], repo_paths: Sequence[Path]
    ) -> list[Dict[str, Any]]:
        return [
            _checkpoint_row(
                worktree=worktree,
                repo=record["repo"],
                base=record["base"],
                initial_commit=None,
            )
            for record, worktree in zip(records, repo_paths)
        ]

    def _recover_retained_native(
        self, job_id: str, prior_generation: int, checkpoint: Mapping[str, Any]
    ) -> PreparedWorkspace:
        task_name = _agentctl_task_name(job_id, prior_generation)
        checkpoint_records = checkpoint["repos"]
        refs = tuple(
            RepoRef(repo=record["repo"], base=record["base"])
            for record in checkpoint_records
        )
        task_path, live_records = self._live_task(
            task_name, refs, error_type=WorkspaceRecoveryError
        )
        manifest_path = self._manifest_path(
            task_name, WorkspaceRecoveryError, exists=True
        )
        digest = checkpoint["workspace_manifest_sha256"]
        try:
            if sha256(manifest_path.read_bytes()).hexdigest() != digest:
                raise WorkspaceRecoveryError("agentctl retained manifest digest does not match")
        except OSError as error:
            raise WorkspaceRecoveryError("agentctl retained manifest is unavailable") from error
        prepared = self._prepared_from_manifest(
            manifest_path,
            job_id=job_id,
            generation=prior_generation,
            error_type=WorkspaceRecoveryError,
        )
        if task_path != prepared.cwd or tuple(
            Path(record["worktree_path"]) for record in live_records
        ) != prepared.repo_paths:
            raise WorkspaceRecoveryError("agentctl retained task does not match manifest")
        if self._checkpoint_rows(checkpoint_records, prepared.repo_paths) != checkpoint_records:
            raise WorkspaceRecoveryError("agentctl retained repository state does not match checkpoint")
        return prepared


__all__ = [
    "AgentctlWorkspaceAdapter",
    "LocalGitWorkspaceAdapter",
    "PreparedWorkspace",
    "RecoveredWorkspace",
    "WorkspaceAdapter",
    "WorkspaceConflict",
    "WorkspaceError",
    "WorkspaceRecoveryError",
    "WorkspaceValidationError",
]
