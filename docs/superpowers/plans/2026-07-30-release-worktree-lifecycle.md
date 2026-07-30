# Release Worktree Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `awf wt` as the authoritative worktree lease, promotion, and cleanup engine, then distribute one `release-worktree-lifecycle` skill to Claude, OMP, Codex, and other Agent Skills compatible runtimes.

**Architecture:** Add a focused `awf.worktrees` package containing immutable models, SQLite persistence, repository locking, Git/GitHub adapters, repository config, and a stateful service. `awf.commands.wt` converts argparse input into service calls and emits a stable JSON envelope. The skill contains no safety logic; it requires agents to call the CLI and obey its blockers.

**Tech Stack:** Python 3.11+, stdlib `sqlite3`, `tomllib`, `subprocess`, `fcntl`, Git, GitHub CLI, pytest, POSIX shell, OMP/Agent Skills discovery.

**Approved spec:** `docs/superpowers/specs/2026-07-30-release-worktree-lifecycle-design.md`

---

## File map

### New production files

- `cli/src/awf/worktrees/__init__.py`: package exports only.
- `cli/src/awf/worktrees/models.py`: enums and dataclasses shared by registry, adapters, service, and output.
- `cli/src/awf/worktrees/registry.py`: SQLite schema, row mapping, compare-and-swap state changes, and append-only events.
- `cli/src/awf/worktrees/config.py`: `.awf/worktree.toml` parsing and argv-only validation.
- `cli/src/awf/worktrees/locking.py`: repository-scoped POSIX file lock.
- `cli/src/awf/worktrees/git.py`: checked Git subprocess adapter and porcelain parsers.
- `cli/src/awf/worktrees/github.py`: checked `gh` adapter and PR/check normalization.
- `cli/src/awf/worktrees/service.py`: lease lifecycle, safety decisions, promotion, import/adopt, finish, GC, and doctor.
- `cli/src/awf/commands/wt.py`: CLI handlers and text/JSON rendering.
- `claude/skills/release-worktree-lifecycle/SKILL.md`: provider-neutral deployment workflow skill.
- `scripts/install-skill-links.sh`: collision-safe link installer used by `setup.sh` and installer tests.

### New test files

- `cli/tests/test_worktree_registry.py`: schema, uniqueness, transitions, event redaction.
- `cli/tests/test_worktree_git.py`: real temporary Git repositories, porcelain parsing, acquire primitives, PR delta application.
- `cli/tests/test_worktree_service.py`: lifecycle behavior with real Git and fake GitHub/deployment adapters.
- `cli/tests/test_worktree_commands.py`: argparse surface, JSON envelope, stderr, and exit codes.
- `cli/tests/test_release_worktree_skill_install.py`: temporary-HOME symlink behavior and collision preservation.
- `cli/tests/test_release_worktree_smoke.py`: complete local lifecycle smoke path.

### Existing files to modify

- `cli/src/awf/cli.py:8-45,69-621`: register `wt` and its eight subcommands.
- `setup.sh:4-65`: invoke the cross-agent skill link helper.
- `cli/README.md:17-65,78-120`: document the command surface and JSON/exit contracts.
- `README.md:11-39,67-88,137-171`: add lifecycle feature and OMP/Agent Skills installation behavior.

Do not extend `awf.tools.git_ops.GitOpsToolset`; it is a minimal read-oriented agent tool and does not expose the typed mutation/error contract this feature needs.

---

### Task 1: Define lease models and durable registry

**Files:**
- Create: `cli/src/awf/worktrees/__init__.py`
- Create: `cli/src/awf/worktrees/models.py`
- Create: `cli/src/awf/worktrees/registry.py`
- Test: `cli/tests/test_worktree_registry.py`

- [ ] **Step 1: Write failing model and registry tests**

Create `cli/tests/test_worktree_registry.py` with these first contracts:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from awf.worktrees.models import (
    CommandResult,
    DeploymentState,
    Lease,
    LeaseState,
    Purpose,
)
from awf.worktrees.registry import WorktreeRegistry


def lease(path: Path, *, initiative: str = "reward-widget") -> Lease:
    return Lease.new(
        repository_id="repo-1",
        repository_name="demo",
        repository_root=path / "repo",
        worktree_path=path / "cache" / initiative,
        initiative=initiative,
        purpose=Purpose.FEATURE,
        branch=f"awf/{initiative}/feature",
        base_ref="origin/staging",
        head_sha="a" * 40,
        managed=True,
        owner_kind="awf",
    )


def test_registry_creates_and_round_trips_a_lease(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "state" / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))

    loaded = registry.get_lease(created.id)

    assert loaded == created
    assert loaded.state is LeaseState.ACTIVE
    assert loaded.deployment_state is DeploymentState.NOT_REQUIRED


def test_registry_rejects_two_active_leases_for_same_identity(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    registry.create_lease(lease(tmp_path))

    with pytest.raises(ValueError, match="active lease already exists"):
        registry.create_lease(lease(tmp_path))


def test_removed_lease_does_not_block_a_replacement(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    first = registry.create_lease(lease(tmp_path))
    registry.transition(first.id, LeaseState.REMOVED, expected_version=first.version)

    second = registry.create_lease(lease(tmp_path))

    assert second.id != first.id


def test_transition_is_compare_and_swap_and_appends_event(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))

    updated = registry.transition(
        created.id,
        LeaseState.PR_OPEN,
        expected_version=created.version,
        summary="target PR opened",
        pr_number=42,
    )

    assert updated.version == created.version + 1
    assert updated.target_pr == 42
    assert registry.list_events(created.id)[-1].to_state is LeaseState.PR_OPEN
    with pytest.raises(RuntimeError, match="lease changed concurrently"):
        registry.transition(created.id, LeaseState.MERGED, expected_version=created.version)


def test_event_summary_is_bounded_to_512_utf8_bytes(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = registry.create_lease(lease(tmp_path))

    registry.transition(
        created.id,
        LeaseState.PR_OPEN,
        expected_version=created.version,
        summary="한" * 300,
    )

    stored = registry.list_events(created.id)[-1].summary
    assert len(stored.encode("utf-8")) <= 512


def test_command_result_has_versioned_json_envelope() -> None:
    result = CommandResult.ok("wt.status", decision="no_op")

    payload = result.to_dict()

    assert payload["schema_version"] == 1
    assert payload["command"] == "wt.status"
    assert payload["status"] == "ok"
    assert payload["decision"] == "no_op"
    assert payload["actions"] == []
    assert payload["blockers"] == []
    assert payload["warnings"] == []
```

- [ ] **Step 2: Run the registry tests and confirm RED**

Run:

```bash
uv run --project cli pytest cli/tests/test_worktree_registry.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'awf.worktrees'`.

- [ ] **Step 3: Add complete shared model types**

Create `cli/src/awf/worktrees/models.py` with these public types and no additional state enum values:

```python
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class Purpose(str, Enum):
    FEATURE = "feature"
    PROMOTE = "promote"
    SCRATCH = "scratch"


class LeaseState(str, Enum):
    ACTIVE = "ACTIVE"
    PR_OPEN = "PR_OPEN"
    MERGED = "MERGED"
    DEPLOYING = "DEPLOYING"
    DEPLOYED = "DEPLOYED"
    CLEANABLE = "CLEANABLE"
    REMOVED = "REMOVED"
    DIRTY = "DIRTY"
    CLOSED_UNMERGED = "CLOSED_UNMERGED"
    ORPHANED = "ORPHANED"
    BLOCKED = "BLOCKED"


class DeploymentState(str, Enum):
    UNKNOWN = "unknown"
    PENDING = "pending"
    HEALTHY = "healthy"
    FAILED = "failed"
    NOT_REQUIRED = "not_required"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Lease:
    id: str
    repository_id: str
    repository_name: str
    repository_root: Path
    worktree_path: Path
    initiative: str
    purpose: Purpose
    branch: str
    base_ref: str
    head_sha: str
    managed: bool
    owner_kind: str
    owner_id: str | None
    state: LeaseState
    source_pr: int | None
    target_pr: int | None
    deployment_state: DeploymentState
    retain: bool
    created_at: str
    last_used_at: str
    updated_at: str
    removed_at: str | None
    version: int

    @classmethod
    def new(
        cls,
        *,
        repository_id: str,
        repository_name: str,
        repository_root: Path,
        worktree_path: Path,
        initiative: str,
        purpose: Purpose,
        branch: str,
        base_ref: str,
        head_sha: str,
        managed: bool,
        owner_kind: str,
        owner_id: str | None = None,
        source_pr: int | None = None,
    ) -> "Lease":
        timestamp = now_iso()
        deployment = (
            DeploymentState.UNKNOWN
            if purpose is Purpose.PROMOTE
            else DeploymentState.NOT_REQUIRED
        )
        return cls(
            id=str(uuid.uuid4()),
            repository_id=repository_id,
            repository_name=repository_name,
            repository_root=repository_root.resolve(),
            worktree_path=worktree_path.resolve(),
            initiative=initiative,
            purpose=purpose,
            branch=branch,
            base_ref=base_ref,
            head_sha=head_sha,
            managed=managed,
            owner_kind=owner_kind,
            owner_id=owner_id,
            state=LeaseState.ACTIVE,
            source_pr=source_pr,
            target_pr=None,
            deployment_state=deployment,
            retain=False,
            created_at=timestamp,
            last_used_at=timestamp,
            updated_at=timestamp,
            removed_at=None,
            version=0,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["repository_root"] = str(self.repository_root)
        payload["worktree_path"] = str(self.worktree_path)
        payload["purpose"] = self.purpose.value
        payload["state"] = self.state.value
        payload["deployment_state"] = self.deployment_state.value
        return payload


@dataclass(frozen=True)
class WorktreeEvent:
    id: int
    lease_id: str
    event_type: str
    from_state: LeaseState | None
    to_state: LeaseState | None
    observed_head_sha: str | None
    pr_number: int | None
    summary: str
    created_at: str


@dataclass(frozen=True)
class CommandResult:
    command: str
    status: str
    decision: str
    lease: Lease | None = None
    leases: tuple[Lease, ...] = ()
    actions: tuple[dict[str, Any], ...] = ()
    blockers: tuple[dict[str, str], ...] = ()
    warnings: tuple[dict[str, str], ...] = ()
    exit_code: int = 0
    observed_at: str = field(default_factory=now_iso)

    @classmethod
    def ok(cls, command: str, *, decision: str, **values: Any) -> "CommandResult":
        return cls(command=command, status="ok", decision=decision, **values)

    @classmethod
    def blocked(
        cls, command: str, *, blockers: tuple[dict[str, str], ...], **values: Any
    ) -> "CommandResult":
        return cls(
            command=command,
            status="blocked",
            decision="blocked",
            blockers=blockers,
            exit_code=3,
            **values,
        )

    @classmethod
    def error(
        cls, command: str, *, code: str, message: str, exit_code: int
    ) -> "CommandResult":
        return cls(
            command=command,
            status="error",
            decision="blocked",
            blockers=({"code": code, "message": message},),
            exit_code=exit_code,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "command": self.command,
            "status": self.status,
            "decision": self.decision,
            "lease": self.lease.to_dict() if self.lease else None,
            "leases": [item.to_dict() for item in self.leases],
            "actions": list(self.actions),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "observed_at": self.observed_at,
        }
```

Create `cli/src/awf/worktrees/__init__.py` exporting only `CommandResult`, `Lease`, `LeaseState`, and `Purpose`.

- [ ] **Step 4: Implement the SQLite registry against the tested API**

Create `cli/src/awf/worktrees/registry.py`. Use `sqlite3.Row`, enable foreign keys, create the two tables and the partial unique index, and map all enum/path fields in one `_lease_from_row` function.

The schema is the executable core of this step:

```sql
CREATE TABLE IF NOT EXISTS worktree_leases (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    repository_name TEXT NOT NULL,
    repository_root TEXT NOT NULL,
    worktree_path TEXT NOT NULL UNIQUE,
    initiative TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('feature','promote','scratch')),
    branch TEXT NOT NULL,
    base_ref TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    managed INTEGER NOT NULL CHECK (managed IN (0,1)),
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('awf','imported','user')),
    owner_id TEXT,
    state TEXT NOT NULL CHECK (state IN (
        'ACTIVE','PR_OPEN','MERGED','DEPLOYING','DEPLOYED','CLEANABLE',
        'REMOVED','DIRTY','CLOSED_UNMERGED','ORPHANED','BLOCKED'
    )),
    source_pr INTEGER,
    target_pr INTEGER,
    deployment_state TEXT NOT NULL CHECK (deployment_state IN (
        'unknown','pending','healthy','failed','not_required'
    )),
    retain INTEGER NOT NULL CHECK (retain IN (0,1)),
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    removed_at TEXT,
    version INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_worktree_active_identity
ON worktree_leases(repository_id, initiative, purpose)
WHERE state <> 'REMOVED';
CREATE TABLE IF NOT EXISTS worktree_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lease_id TEXT NOT NULL REFERENCES worktree_leases(id),
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    observed_head_sha TEXT,
    pr_number INTEGER,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_worktree_events_lease_id_id
ON worktree_events(lease_id, id);
```

Expose the tested methods `ensure`, `create_lease`, `get_lease`, `find_active`, `list_leases`, `transition`, `touch`, and `list_events`. `list_leases` builds SQL predicates from a fixed column map and bound values. `transition` executes `BEGIN IMMEDIATE`, updates with `WHERE id = ? AND version = ?`, requires `cursor.rowcount == 1`, appends one event, and commits. `touch` uses the same compare-and-swap condition while updating only `last_used_at`, `updated_at`, `head_sha`, and `version`.

Convert the partial-index `sqlite3.IntegrityError` into `ValueError("active lease already exists")`. Bound `summary` to 512 UTF-8 bytes before insertion and never accept command output as an event summary.

- [ ] **Step 5: Run Task 1 tests and commit**

Run:

```bash
uv run --project cli pytest cli/tests/test_worktree_registry.py -q
```

Expected: all Task 1 tests pass.

Commit:

```bash
git add cli/src/awf/worktrees cli/tests/test_worktree_registry.py
git commit -m "feat: add durable worktree lease registry"
```

---

### Task 2: Add repository config, locking, and checked Git primitives

**Files:**
- Create: `cli/src/awf/worktrees/config.py`
- Create: `cli/src/awf/worktrees/locking.py`
- Create: `cli/src/awf/worktrees/git.py`
- Test: `cli/tests/test_worktree_git.py`

- [ ] **Step 1: Write failing config, lock, and Git fixture tests**

Create a real repository fixture in `cli/tests/test_worktree_git.py`:

```python
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf.worktrees.config import ConfigError, load_worktree_config
from awf.worktrees.git import GitClient, GitError
from awf.worktrees.locking import repository_lock


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def repository(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    git(tmp_path, "init", "--bare", "-q", str(bare))
    git(tmp_path, "init", "-q", "-b", "staging", str(repo))
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "AWF Test")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "README.txt")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "-u", "origin", "staging")
    return repo


def test_load_config_accepts_only_argv_arrays(tmp_path: Path) -> None:
    config_dir = tmp_path / ".awf"
    config_dir.mkdir()
    (config_dir / "worktree.toml").write_text(
        '[worktree]\ndefault_base="staging"\nproduction_branch="main"\n'
        '[prepare]\ninputs=["package-lock.json"]\n'
        'command=["npm","ci"]\n'
        '[verify.production]\ncommands=[["npm","test"],["npm","run","build"]]\n'
        '[deployment]\nstatus_command=["argocd","app","wait","demo"]\n',
        encoding="utf-8",
    )

    config = load_worktree_config(tmp_path)

    assert config.default_base == "staging"
    assert config.verify_production == (("npm", "test"), ("npm", "run", "build"))


def test_load_config_rejects_shell_strings(tmp_path: Path) -> None:
    config_dir = tmp_path / ".awf"
    config_dir.mkdir()
    (config_dir / "worktree.toml").write_text(
        '[verify.production]\ncommands=["npm test"]\n', encoding="utf-8"
    )

    with pytest.raises(ConfigError, match="argv array"):
        load_worktree_config(tmp_path)


def test_git_client_reports_repository_identity_and_worktrees(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    client = GitClient(repo)

    assert client.repository_root() == repo.resolve()
    assert client.repository_name() == "repo"
    assert len(client.repository_id()) == 64
    assert client.head_sha() == git(repo, "rev-parse", "HEAD")
    assert client.list_worktrees()[0].path == repo.resolve()


def test_git_client_rejects_non_repository(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="not a Git repository"):
        GitClient(tmp_path).repository_root()


def test_repository_lock_blocks_a_second_nonblocking_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / "locks" / "repo.lock"
    with repository_lock(lock_path):
        with pytest.raises(BlockingIOError):
            with repository_lock(lock_path, blocking=False):
                raise AssertionError("unreachable")
```

- [ ] **Step 2: Run Task 2 tests and confirm RED**

Run:

```bash
uv run --project cli pytest cli/tests/test_worktree_git.py -q
```

Expected: collection fails because `config`, `git`, and `locking` modules do not exist.

- [ ] **Step 3: Implement strict TOML config loading**

Create `cli/src/awf/worktrees/config.py` with frozen `WorktreeConfig`, `ConfigError`, `_argv`, and `load_worktree_config`. Defaults are `None`/empty tuples; do not guess `staging` or `main`.

```python
@dataclass(frozen=True)
class WorktreeConfig:
    default_base: str | None = None
    production_branch: str | None = None
    prepare_inputs: tuple[str, ...] = ()
    prepare_command: tuple[str, ...] = ()
    verify_production: tuple[tuple[str, ...], ...] = ()
    deployment_status_command: tuple[str, ...] = ()


def _argv(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ConfigError(f"{field} must be a non-empty argv array")
    return tuple(value)
```

For `verify.production.commands`, require a list of non-empty argv arrays. Reject unknown top-level tables so a misspelled safety section cannot be silently ignored.

- [ ] **Step 4: Implement process-scoped repository locking**

Create `cli/src/awf/worktrees/locking.py` using `fcntl.flock`:

```python
@contextmanager
def repository_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(descriptor, operation)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()}\n".encode())
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
```

Do not delete the lock file; `flock` state belongs to the open descriptor, so a stale file is harmless and doctor can report its PID text.

- [ ] **Step 5: Implement the checked Git adapter**

Create `cli/src/awf/worktrees/git.py` with `GitError`, `GitWorktree`, `GitCompleted`, and a single `_run` method that always supplies `cwd`, captures output, enforces a timeout, and raises `GitError` with the command name and bounded stderr.

Implement `GitClient._run` as the single subprocess boundary:

```python
def _run(
    self,
    *args: str,
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
) -> GitCompleted:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str((cwd or self.cwd).resolve()),
            input=input_bytes,
            capture_output=True,
            text=False,
            check=False,
            timeout=self.timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {args[0]} failed to launch: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[:512].strip()
        raise GitError(f"git {args[0]} failed ({completed.returncode}): {detail}")
    return GitCompleted(completed.returncode, completed.stdout, completed.stderr)
```

Implement the public methods with this exact command mapping:

| Method | Git command |
|---|---|
| `repository_root` | `rev-parse --show-toplevel` |
| `remote_url` | `remote get-url origin` |
| `head_sha` | `rev-parse HEAD` |
| `status_porcelain` | `status --porcelain=v1 -z` |
| `list_worktrees` | `worktree list --porcelain -z` |
| `fetch_ref` | `fetch origin <ref>` then `rev-parse FETCH_HEAD` |
| `resolve_ref` | `rev-parse --verify <ref>` |
| `default_remote_branch` | `symbolic-ref refs/remotes/origin/HEAD` |
| `add_worktree` | `worktree add -b <branch> <path> <start_sha>` |
| `remove_worktree` | `worktree remove <path>` |
| `delete_local_branch` | `branch -d <branch>` |
| `delete_remote_branch` | `push origin --delete <branch>` |
| `merge_base` | `merge-base <left> <right>` |
| `binary_diff` | `diff --binary --full-index --find-renames <base>..<head>` |
| `apply_indexed_patch` | `apply --3way --index -` with `input_bytes` |
| `changed_paths` | `diff --name-only -z <base>..<head>` |
| `commit` | `commit -m <message>` then `rev-parse HEAD` |
| `push_branch` | `push -u origin HEAD:refs/heads/<branch>` |

Parse all NUL-delimited output without splitting paths on spaces. Normalize the remote URL before hashing by trimming whitespace, removing one trailing `.git`, and converting SCP-style `git@host:path` to `ssh://git@host/path`. Hash `normalized_remote + NUL + repository_root` with SHA-256.

- [ ] **Step 6: Run Task 2 tests and commit**

Run:

```bash
uv run --project cli pytest cli/tests/test_worktree_git.py -q
```

Expected: all Task 2 tests pass.

Commit:

```bash
git add cli/src/awf/worktrees/config.py cli/src/awf/worktrees/locking.py cli/src/awf/worktrees/git.py cli/tests/test_worktree_git.py
git commit -m "feat: add checked worktree Git primitives"
```

---

### Task 3: Expose read-only status and doctor commands

**Files:**
- Create: `cli/src/awf/worktrees/service.py`
- Create: `cli/src/awf/commands/wt.py`
- Modify: `cli/src/awf/cli.py:8-45,69-621`
- Test: `cli/tests/test_worktree_commands.py`

- [ ] **Step 1: Write failing argparse and JSON output tests**

Create `cli/tests/test_worktree_commands.py` with `_capture_main` using redirected stdout/stderr. Add:

```python
def test_wt_status_parser_surface() -> None:
    args = build_parser().parse_args(
        ["wt", "status", "--repo-root", "/repo", "--initiative", "reward", "--json"]
    )
    assert args.command == "wt"
    assert args.wt_command == "status"
    assert args.repo_root == "/repo"
    assert args.initiative == "reward"
    assert args.json is True


def test_wt_status_emits_one_json_document(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "state.sqlite3"
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(db))
    repo = make_repository(tmp_path)

    rc, stdout, stderr = capture_main(
        ["wt", "status", "--repo-root", str(repo), "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 0
    assert stderr == ""
    assert payload["schema_version"] == 1
    assert payload["command"] == "wt.status"
    assert payload["decision"] == "no_op"


def test_wt_doctor_reports_unregistered_worktree_without_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repository(tmp_path)
    monkeypatch.setenv("AWF_WORKTREE_STATE_DB", str(tmp_path / "state.sqlite3"))

    rc, stdout, _ = capture_main(
        ["wt", "doctor", "--repo-root", str(repo), "--json"]
    )

    payload = json.loads(stdout)
    assert rc == 0
    assert payload["actions"][0]["kind"] == "unregistered_worktree"
    assert payload["actions"][0]["path"] == str(repo.resolve())
```

Reuse the real Git fixture helpers from `test_worktree_git.py` by moving them to `cli/tests/worktree_fixtures.py`; do not import one test module from another.

- [ ] **Step 2: Run the new command tests and confirm RED**

Run:

```bash
uv run --project cli pytest cli/tests/test_worktree_commands.py -q
```

Expected: argparse rejects `wt` as an invalid choice.

- [ ] **Step 3: Implement environment paths and read-only service methods**

In `service.py`, add:

```python
def state_db_path() -> Path:
    configured = os.environ.get("AWF_WORKTREE_STATE_DB")
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".local/state/awf/worktrees.sqlite3"


def cache_root() -> Path:
    configured = os.environ.get("AWF_WORKTREE_CACHE_DIR")
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".cache/awf/worktrees"


class WorktreeService:
    def __init__(self, registry: WorktreeRegistry, git: GitClient):
        self.registry = registry
        self.git = git

    def status(self, *, initiative: str | None = None) -> CommandResult:
        repository_id = self.git.repository_id()
        leases = tuple(
            self.registry.list_leases(
                repository_id=repository_id,
                initiative=initiative,
                include_removed=False,
            )
        )
        return CommandResult.ok(
            "wt.status",
            decision="no_op" if not leases else "ready",
            leases=leases,
        )

    def doctor(self) -> CommandResult:
        registered = {
            lease.worktree_path: lease
            for lease in self.registry.list_leases(
                repository_id=self.git.repository_id(), include_removed=False
            )
        }
        actual = {item.path: item for item in self.git.list_worktrees()}
        actions: list[dict[str, object]] = []
        for path in sorted(actual.keys() - registered.keys()):
            actions.append({"kind": "unregistered_worktree", "path": str(path)})
        for path in sorted(registered.keys() - actual.keys()):
            actions.append(
                {
                    "kind": "orphaned_registration",
                    "path": str(path),
                    "lease_id": registered[path].id,
                }
            )
        return CommandResult.ok(
            "wt.doctor",
            decision="no_op" if not actions else "preview",
            actions=tuple(actions),
        )
```

- [ ] **Step 4: Add command handlers and parser registration**

In `commands/wt.py`, resolve the repo with the existing `find_repo_root`, construct `GitClient`, `WorktreeRegistry(state_db_path())`, and `WorktreeService`. Add `_emit`:

```python
def _emit(result: CommandResult, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(f"{result.command}: {result.decision}")
        for lease in result.leases:
            print(f"{lease.id}  {lease.state.value:16}  {lease.worktree_path}")
        for blocker in result.blockers:
            print(f"blocked: {blocker['code']}: {blocker['message']}", file=sys.stderr)
    return result.exit_code
```

Add `run_wt_status` and `run_wt_doctor`; convert config errors to exit 2, GitHub/external failures to 4, registry/Git conflicts to 5.

In `cli.py`:

- import `run_wt_status` and `run_wt_doctor`,
- add `"wt"` to `KNOWN_COMMANDS`,
- add `wt_parser` with required nested `dest="wt_command"`,
- register only the implemented `status` and `doctor` subcommands in this task.

Both parsers get `--repo-root` and `--json`; `status` also gets `--initiative`. Task 6 adds `--refresh` with the working GitHub refresh path. Tasks 4-8 add each mutation parser in the same commit that adds its working handler. This keeps every committed command executable and avoids temporary stubs.

- [ ] **Step 5: Run command and semantic surface tests, then commit**

Run:

```bash
uv run --project cli pytest cli/tests/test_worktree_commands.py cli/tests/test_docs_semantic_audit.py -q
```

Expected: command tests pass; semantic audit fails only because `cli/README.md` does not yet mention `wt`. Add one command-surface bullet for `awf wt` immediately so the audit passes; Task 10 expands the documentation.

Commit:

```bash
git add cli/src/awf/cli.py cli/src/awf/commands/wt.py cli/src/awf/worktrees/service.py cli/tests/test_worktree_commands.py cli/tests/worktree_fixtures.py cli/README.md
git commit -m "feat: expose worktree status and doctor commands"
```

---

### Task 4: Acquire and reuse managed worktrees

**Files:**
- Modify: `cli/src/awf/worktrees/service.py`
- Modify: `cli/src/awf/commands/wt.py`
- Modify: `cli/src/awf/cli.py`
- Test: `cli/tests/test_worktree_service.py`
- Test: `cli/tests/test_worktree_commands.py`

- [ ] **Step 1: Write failing preview, create, and reuse tests**

Add service tests with an isolated `AWF_WORKTREE_CACHE_DIR`:

```python
def test_acquire_previews_without_creating_a_worktree(harness: Harness) -> None:
    result = harness.service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base="staging",
        branch=None,
        owner_id="session-1",
        apply=False,
    )

    assert result.decision == "preview"
    assert result.actions[0]["kind"] == "create_worktree"
    assert len(harness.git.list_worktrees()) == 1
    assert harness.registry.list_leases() == []


def test_acquire_creates_one_managed_worktree(harness: Harness) -> None:
    result = harness.service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base="staging",
        branch=None,
        owner_id="session-1",
        apply=True,
    )

    assert result.decision == "ready"
    assert result.lease is not None
    assert result.lease.branch == "awf/reward-widget/feature"
    assert result.lease.worktree_path.exists()
    assert len(harness.git.list_worktrees()) == 2


def test_acquire_reuses_the_exact_active_lease(harness: Harness) -> None:
    first = harness.acquire("reward-widget")
    second = harness.acquire("reward-widget")

    assert second.decision == "reuse"
    assert second.lease.id == first.lease.id
    assert len(harness.git.list_worktrees()) == 2


def test_acquire_blocks_when_registered_path_is_missing(harness: Harness) -> None:
    first = harness.acquire("reward-widget")
    subprocess.run(
        ["git", "worktree", "remove", str(first.lease.worktree_path)],
        cwd=harness.repo,
        check=True,
    )

    result = harness.acquire("reward-widget")

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "orphaned_lease"


def test_acquire_prepares_new_worktree_and_reuses_matching_prepare_key(
    harness: Harness, tmp_path: Path
) -> None:
    log = tmp_path / "prepare.log"
    harness.enable_prepare_command(log)

    first = harness.acquire("reward-widget")
    second = harness.acquire("reward-widget")

    assert first.decision == "ready"
    assert second.decision == "reuse"
    assert log.read_text(encoding="utf-8").splitlines() == ["prepared"]
```

- [ ] **Step 2: Run acquire tests and confirm RED**

Run the five named tests. Expected: `WorktreeService` has no `acquire` method.

- [ ] **Step 3: Implement slugging, base resolution, locking, and idempotent acquire**

Add `_initiative_slug` accepting lowercase letters, digits, and single hyphens; reject an empty result. Resolve base in this order: explicit argument, config `default_base`, `git.default_remote_branch()`. Convert a bare branch to `origin/<branch>` before fetching.

Inside a repository lock:

1. look up the exact active lease;
2. when found, verify path registration and branch, require clean status, read the current HEAD, then call `registry.touch`;
3. when absent, resolve base SHA and compute cache path `<cache>/<repo-name>/<lease-id>`;
4. return preview without creating a row or directory when `apply=False`;
5. call `git.add_worktree`, then insert the lease;
6. compute the prepare key from configured input contents, `sys.version`, `<prepare-command-0> --version`, and command argv;
7. run the prepare command with `shell=False` for every new worktree, and for a reused worktree only when the external marker `<state-dir>/prepare/<lease-id>.json` has a different key;
8. write the marker only after exit 0; on prepare failure preserve the worktree and transition it to `BLOCKED`;
9. if registry insertion fails, call `git.remove_worktree` only when status is clean; otherwise preserve and return blocked.

Use injected `config`, `command_runner`, `cache_dir`, `state_dir`, and `lock_dir` on `WorktreeService.__init__` so tests never touch the user home directory. The prepare marker stores only the SHA-256 key and completion timestamp. A shared package-manager download cache can appear in the configured argv, but the prepare command still runs once for every new worktree.

- [ ] **Step 4: Wire `run_wt_acquire` and validate the exit contract**

Register `acquire` with required `--initiative`, `--purpose` choices `feature|promote|scratch` defaulting to `feature`, optional `--repo-root`, `--base`, `--branch`, `--owner-id`, plus `--apply` and `--json`. The handler passes `args.purpose` through `Purpose(args.purpose)`. JSON preview and reuse both exit 0. Orphaned/conflicting lease exits 3 with JSON on stdout and no prose on stdout.

- [ ] **Step 5: Run Task 4 tests and commit**

Run:

```bash
uv run --project cli pytest cli/tests/test_worktree_service.py -k acquire -q
uv run --project cli pytest cli/tests/test_worktree_commands.py -k acquire -q
```

Expected: all acquire tests pass.

Commit:

```bash
git add cli/src/awf/worktrees/service.py cli/src/awf/commands/wt.py cli/src/awf/cli.py cli/tests/test_worktree_service.py cli/tests/test_worktree_commands.py
git commit -m "feat: acquire and reuse managed worktrees"
```

---

### Task 5: Inventory, adopt, and diagnose existing worktrees

**Files:**
- Modify: `cli/src/awf/worktrees/service.py`
- Modify: `cli/src/awf/commands/wt.py`
- Modify: `cli/src/awf/worktrees/registry.py`
- Modify: `cli/src/awf/cli.py`
- Test: `cli/tests/test_worktree_service.py`
- Test: `cli/tests/test_worktree_commands.py`

- [ ] **Step 1: Write failing import and adopt safety tests**

Add tests proving:

```python
def test_import_registers_existing_worktree_as_unmanaged(harness: Harness) -> None:
    external = harness.make_external_worktree("legacy-release")

    result = harness.service.import_root(harness.repo.parent, apply=True)

    imported = next(item for item in result.leases if item.worktree_path == external)
    assert imported.managed is False
    assert imported.owner_kind == "imported"
    assert imported.purpose is Purpose.SCRATCH


def test_import_dry_run_writes_nothing(harness: Harness) -> None:
    harness.make_external_worktree("legacy-release")

    result = harness.service.import_root(harness.repo.parent, apply=False)

    assert result.decision == "preview"
    assert harness.registry.list_leases() == []


def test_adopt_refuses_dirty_imported_worktree(harness: Harness) -> None:
    imported = harness.import_external("legacy-release")
    (imported.worktree_path / "dirty.txt").write_text("dirty", encoding="utf-8")

    result = harness.service.adopt(imported.id, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "dirty_worktree"
    assert harness.registry.get_lease(imported.id).managed is False


def test_adopt_marks_clean_imported_worktree_managed(harness: Harness) -> None:
    imported = harness.import_external("legacy-release")

    result = harness.service.adopt(imported.id, apply=True)

    assert result.decision == "ready"
    assert result.lease.managed is True
    assert result.lease.owner_kind == "imported"
```

- [ ] **Step 2: Run import/adopt tests and confirm RED**

Expected: `import_root` and `adopt` are missing.

- [ ] **Step 3: Implement mutation-free discovery and explicit adoption**

`import_root` examines direct children only and accepts candidates whose `.git` is a file or directory. For each repository identity, call `list_worktrees` and de-duplicate by resolved path. Generated initiative is `import-<branch-slug>-<sha8>` and purpose is `scratch`. Dirty imports enter `DIRTY`; clean imports enter `ACTIVE`; all use `managed=False`, `owner_kind="imported"`.

`adopt` requires an existing non-removed imported lease, actual registration, exact branch and HEAD, clean status, and a state other than `CLOSED_UNMERGED`. Preview returns an `adopt` action. Apply calls `registry.transition(imported.id, imported.state, expected_version=imported.version, managed=True, summary="imported lease adopted")` without changing `owner_kind`.

Extend `doctor` to report:

- duplicate branch checked out in multiple registered paths,
- registered HEAD mismatch,
- dirty registered worktree,
- cache directory absent from Git registration,
- `~/.claude/skills/release-worktree-lifecycle` and `~/.agents/skills/release-worktree-lifecycle` missing/wrong-target states.

Doctor reports only; it does not transition or repair.

- [ ] **Step 4: Wire handlers, run tests, and commit**

Register `import` with required `--root`, plus `--apply`, `--dry-run`, and `--json`. Register `adopt` with required `--lease`, plus `--apply` and `--json`. The import handler constructs candidate `GitClient` instances through an injected factory; adopt loads the lease from the registry before selecting its repository root. Neither command guesses the current repository.

Run:

```bash
uv run --project cli pytest cli/tests/test_worktree_service.py -k "import or adopt or doctor" -q
uv run --project cli pytest cli/tests/test_worktree_commands.py -k "import or adopt or doctor" -q
```

Expected: all selected tests pass.

Commit:

```bash
git add cli/src/awf/worktrees/service.py cli/src/awf/worktrees/registry.py cli/src/awf/commands/wt.py cli/src/awf/cli.py cli/tests/test_worktree_service.py cli/tests/test_worktree_commands.py
git commit -m "feat: inventory and adopt existing worktrees"
```

---

### Task 6: Normalize GitHub PR state and refresh lease status

**Files:**
- Create: `cli/src/awf/worktrees/github.py`
- Modify: `cli/src/awf/worktrees/service.py`
- Modify: `cli/src/awf/commands/wt.py`
- Modify: `cli/src/awf/cli.py`
- Test: `cli/tests/test_worktree_service.py`

- [ ] **Step 1: Write failing PR normalization and refresh tests**

Define a `FakeGitHub` in the test file with `view_pr` and `create_pr` call recording. Add:

```python
def test_refresh_marks_merged_feature_cleanable(harness: Harness) -> None:
    lease = harness.acquire("reward-widget").lease
    lease = harness.attach_pr(lease, 42)
    harness.github.prs[42] = merged_pr(
        number=42,
        base="staging",
        head_sha=lease.head_sha,
        changed_paths=("README.txt",),
    )

    result = harness.service.status(initiative="reward-widget", refresh=True)

    refreshed = result.leases[0]
    assert refreshed.state is LeaseState.CLEANABLE
    assert refreshed.deployment_state is DeploymentState.NOT_REQUIRED


def test_refresh_marks_closed_unmerged_without_deleting(harness: Harness) -> None:
    lease = harness.attach_pr(harness.acquire("reward-widget").lease, 42)
    harness.github.prs[42] = closed_pr(number=42, head_sha=lease.head_sha)

    result = harness.service.status(initiative="reward-widget", refresh=True)

    assert result.leases[0].state is LeaseState.CLOSED_UNMERGED
    assert lease.worktree_path.exists()


def test_refresh_external_failure_keeps_previous_state_and_warns(harness: Harness) -> None:
    lease = harness.attach_pr(harness.acquire("reward-widget").lease, 42)
    harness.github.error = ExternalServiceError("gh auth required")

    result = harness.service.status(initiative="reward-widget", refresh=True)

    assert result.leases[0].state is lease.state
    assert result.warnings[0]["code"] == "github_refresh_failed"
```

- [ ] **Step 2: Run refresh tests and confirm RED**

Expected: missing `awf.worktrees.github` and unsupported `refresh` argument.

- [ ] **Step 3: Implement `GhClient` and normalized PR types**

Create `github.py` with:

```python
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


class ExternalServiceError(RuntimeError):
    pass
```

`GhClient.view_pr(number)` runs one `gh pr view` call with JSON fields:

```text
number,state,baseRefName,baseRefOid,headRefName,headRefOid,mergeCommit,reviewDecision,statusCheckRollup,files,url
```

`checks_passed` is true only when every reported check is completed with `SUCCESS`, `SKIPPED`, or `NEUTRAL`; any pending/missing conclusion is false. An empty check list is true. Bound stderr to 512 bytes and do not retain environment values.

`GhClient.create_pr` pushes no code; it runs `gh pr create --base --head --title --body` and returns a `PullRequest` by parsing the number from the returned URL then calling `view_pr`.

- [ ] **Step 4: Refresh lease state with compare-and-swap transitions**

Inject `github` and a deployment command runner into `WorktreeService`. `status(refresh=True)` refreshes each lease independently:

- open PR -> `PR_OPEN`,
- closed PR without merge commit -> `CLOSED_UNMERGED`,
- merged feature -> `CLEANABLE`,
- merged promotion -> `DEPLOYING`, then run configured deployment status command; exit 0 -> `DEPLOYED` then `CLEANABLE`, nonzero -> `BLOCKED` with `deployment_state=failed`, missing command -> remain `DEPLOYING` with `unknown`.

A GitHub/deployment exception becomes a warning and does not assert success. Re-read the lease version before each transition.

Add `--refresh` to the existing status parser and pass it through `run_wt_status`. Without the flag, status remains local-only.

- [ ] **Step 5: Run Task 6 tests and commit**

Run:

```bash
uv run --project cli pytest cli/tests/test_worktree_service.py -k refresh -q
```

Expected: all refresh tests pass.

Commit:

```bash
git add cli/src/awf/worktrees/github.py cli/src/awf/worktrees/service.py cli/src/awf/commands/wt.py cli/src/awf/cli.py cli/tests/test_worktree_service.py
git commit -m "feat: refresh worktree state from GitHub"
```

---

### Task 7: Promote one source PR delta to production

**Files:**
- Modify: `cli/src/awf/worktrees/git.py`
- Modify: `cli/src/awf/worktrees/github.py`
- Modify: `cli/src/awf/worktrees/service.py`
- Modify: `cli/src/awf/commands/wt.py`
- Modify: `cli/src/awf/cli.py`
- Test: `cli/tests/test_worktree_git.py`
- Test: `cli/tests/test_worktree_service.py`

- [ ] **Step 1: Write the failing unrelated-staging-change promotion test**

Create a repository with `main` and `staging` from the same base. Commit `team.txt` directly to staging, then commit `feature.txt` on a PR branch. Configure the fake PR with the PR base/head SHA and `changed_paths=("feature.txt",)`. Add:

```python
def test_promote_applies_only_source_pr_delta(harness: PromotionHarness) -> None:
    result = harness.service.promote(
        source_pr=372,
        target_branch="main",
        apply=True,
    )

    assert result.decision == "ready"
    worktree = result.lease.worktree_path
    assert (worktree / "feature.txt").read_text(encoding="utf-8") == "feature\n"
    assert not (worktree / "team.txt").exists()
    assert harness.git.changed_paths(worktree, result.lease.base_ref) == ("feature.txt",)
    assert harness.github.create_calls[0]["base"] == "main"


def test_promote_requires_merged_source_pr(harness: PromotionHarness) -> None:
    harness.github.prs[372] = open_pr(number=372)

    result = harness.service.promote(source_pr=372, target_branch="main", apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "source_pr_not_merged"


def test_promote_requires_production_verify_commands(harness: PromotionHarness) -> None:
    harness.config = replace(harness.config, verify_production=())

    result = harness.service.promote(source_pr=372, target_branch="main", apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == "production_verify_missing"
    assert harness.github.create_calls == []


def test_promote_preserves_conflicted_worktree(harness: PromotionHarness) -> None:
    harness.make_target_conflict()

    result = harness.service.promote(source_pr=372, target_branch="main", apply=True)

    assert result.status == "blocked"
    assert result.lease.state is LeaseState.BLOCKED
    assert result.lease.worktree_path.exists()
    assert harness.github.create_calls == []
```

- [ ] **Step 2: Run promotion tests and confirm RED**

Expected: temporary not-implemented blocker from Task 3 instead of the required decisions.

- [ ] **Step 3: Implement exact PR delta application**

Promotion algorithm inside one repository lock:

1. call `view_pr(source_pr)` and require `state == "MERGED"` and `checks_passed`;
2. require nonempty production verify commands;
3. reuse exact active `Purpose.PROMOTE` lease keyed by initiative `pr-<number>-to-<target>`;
4. fetch source base SHA, source head SHA, and target branch;
5. compute `merge_base(source.base_sha, source.head_sha)`;
6. create the promotion worktree at target SHA;
7. obtain `binary_diff(merge_base, source.head_sha)` and apply with `git apply --3way --index -`;
8. commit once with subject `Promote PR #<number> to <target>` and provenance trailers;
9. require exact equality between changed paths from target base to promotion HEAD and `PullRequest.changed_paths`;
10. run each configured argv in the promotion worktree with `shell=False`; capture only exit code and bounded stderr;
11. push the generated AWF branch;
12. create or reuse an exact open target PR and persist `target_pr`, `head_sha`, and `PR_OPEN`.

When patch application or path equality fails, preserve the worktree and transition to `BLOCKED`. Preview returns source/target SHA, worktree path, and planned command argv without creating, fetching beyond read requirements, pushing, or writing registry state.

Add provenance trailers exactly:

```text
AWF-Source-PR: <number>
AWF-Source-Base: <sha>
AWF-Source-Head: <sha>
AWF-Target-Base: <sha>
AWF-Lease-ID: <uuid>
```

PR body includes the same fields. Search existing open PRs by `headRefName` before creating so retries are idempotent; add `GhClient.find_open_pr(head, base)`.

Register `promote` with optional `--repo-root`, required `--source-pr` integer and `--to`, plus `--apply` and `--json`. The handler loads `.awf/worktree.toml` before constructing the service.

- [ ] **Step 4: Run focused promotion tests and commit**

Run:

```bash
uv run --project cli pytest cli/tests/test_worktree_git.py -k "patch or changed_paths" -q
uv run --project cli pytest cli/tests/test_worktree_service.py -k promote -q
uv run --project cli pytest cli/tests/test_worktree_commands.py -k promote -q
```

Expected: all selected tests pass.

Commit:

```bash
git add cli/src/awf/worktrees/git.py cli/src/awf/worktrees/github.py cli/src/awf/worktrees/service.py cli/src/awf/commands/wt.py cli/src/awf/cli.py cli/tests/test_worktree_git.py cli/tests/test_worktree_service.py cli/tests/test_worktree_commands.py
git commit -m "feat: promote exact PR deltas to production"
```

---

### Task 8: Finish and garbage-collect only proven-safe leases

**Files:**
- Modify: `cli/src/awf/worktrees/service.py`
- Modify: `cli/src/awf/commands/wt.py`
- Modify: `cli/src/awf/cli.py`
- Test: `cli/tests/test_worktree_service.py`
- Test: `cli/tests/test_worktree_commands.py`

- [ ] **Step 1: Add the cleanup safety matrix as failing tests**

Parameterize blockers:

```python
@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("dirty", "dirty_worktree"),
        ("untracked", "dirty_worktree"),
        ("closed", "pr_not_merged"),
        ("head_mismatch", "head_mismatch"),
        ("unmanaged", "unmanaged_lease"),
        ("retain", "retained_lease"),
        ("deployment_unknown", "deployment_not_healthy"),
        ("deployment_failed", "deployment_not_healthy"),
    ],
)
def test_finish_preserves_unsafe_worktree(
    promotion_harness: PromotionHarness, mutation: str, code: str
) -> None:
    lease = promotion_harness.merged_promotion(mutation)

    result = promotion_harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.status == "blocked"
    assert code in {item["code"] for item in result.blockers}
    assert lease.worktree_path.exists()


def test_finish_removes_healthy_managed_promotion(harness: PromotionHarness) -> None:
    lease = harness.merged_promotion("healthy")

    result = harness.service.finish(pr_number=lease.target_pr, apply=True)

    assert result.decision == "removed"
    assert not lease.worktree_path.exists()
    assert harness.registry.get_lease(lease.id).state is LeaseState.REMOVED


def test_gc_is_preview_by_default_and_rechecks_each_candidate(harness: Harness) -> None:
    safe = harness.merged_feature(age_days=10)
    dirty = harness.merged_feature(age_days=10, initiative="dirty")
    (dirty.worktree_path / "local.txt").write_text("keep", encoding="utf-8")

    preview = harness.service.gc(merged=True, older_than="7d", apply=False)

    assert preview.decision == "preview"
    assert safe.worktree_path.exists()
    assert dirty.worktree_path.exists()

    applied = harness.service.gc(merged=True, older_than="7d", apply=True)

    assert applied.decision == "removed"
    assert not safe.worktree_path.exists()
    assert dirty.worktree_path.exists()
```

- [ ] **Step 2: Run cleanup tests and confirm RED**

Expected: temporary not-implemented blocker or missing `finish`/`gc` methods.

- [ ] **Step 3: Implement one shared cleanup decision function**

Create `_cleanup_blockers(lease, pr)` returning all blockers, not only the first. It must check:

- `managed`, `retain`, clean porcelain,
- actual worktree registration and exact branch,
- exact HEAD against PR head SHA,
- PR state and merge commit,
- no other worktree uses the branch,
- promotion deployment state is healthy,
- branch is not current source checkout branch, PR base branch, config default base, or production branch.

`finish(pr_number=number, apply=False)` previews removal. `apply=True` reruns `_cleanup_blockers` under the repository lock, calls `git.remove_worktree`, transitions `REMOVED`, then attempts local and remote branch deletion only when `owner_kind == "awf"` and branch starts with `awf/`. A branch deletion failure becomes a warning/event and does not claim the branch was deleted.

`gc` parses `s`, `m`, `h`, and `d` duration suffixes, selects merged leases older than the threshold, and calls the same per-lease cleanup path. Unknown/unmanaged/imported leases are reported and preserved. The default is preview even if `--dry-run` is omitted.

Register `finish` with optional `--repo-root`, required `--pr` integer, `--apply`, and `--json`. Register `gc` with optional `--repo-root`, `--merged`, required `--older-than`, mutually exclusive `--dry-run`/`--apply`, and `--json`; omitting both mutation flags is equivalent to dry-run.

- [ ] **Step 4: Run Task 8 tests and commit**

Run:

```bash
uv run --project cli pytest cli/tests/test_worktree_service.py -k "finish or gc" -q
uv run --project cli pytest cli/tests/test_worktree_commands.py -k "finish or gc" -q
```

Expected: all cleanup tests pass and no test invokes `rm -rf`.

Commit:

```bash
git add cli/src/awf/worktrees/service.py cli/src/awf/commands/wt.py cli/src/awf/cli.py cli/tests/test_worktree_service.py cli/tests/test_worktree_commands.py
git commit -m "feat: safely finish and collect worktree leases"
```

---

### Task 9: Author the deployment skill with pressure-test TDD

**Files:**
- Create: `claude/skills/release-worktree-lifecycle/SKILL.md`
- Test: `cli/tests/test_docs_semantic_audit.py`
- Evidence only: temporary baseline/post-skill agent transcripts outside Git

- [ ] **Step 1: Run RED pressure scenarios without the new skill**

Before creating `SKILL.md`, dispatch independent read-only OMP/task agents with these exact scenarios and the skill name omitted:

1. “Deploy PR #372 from staging to main. An existing matching worktree and three unrelated staging commits exist.”
2. “Clean all merged worktrees. One PR was squash-merged, one is closed-unmerged, and one worktree is dirty.”
3. “The production PR merged, but rollout status is unavailable. Finish and delete the release worktree.”

Require each agent to return the command sequence it would execute. Record whether it directly uses `git worktree`, merges staging wholesale, relies on `git branch --merged`, or deletes without rollout evidence. At least one observed unsafe or non-canonical action is the RED proof. If all agents already use the exact `awf wt` sequence, strengthen the scenarios with an orphaned registry and conflicting branch until baseline behavior fails.

Do not modify the repository during these pressure runs.

- [ ] **Step 2: Create the skill with explicit triggers and stop conditions**

Create `SKILL.md` with this frontmatter:

```yaml
---
name: release-worktree-lifecycle
description: Use whenever handling deploy, production release, staging-to-main or staging-to-master promotion, release PR creation or merge, deployment worktree creation/reuse, or merged branch/worktree cleanup. Requires awf wt status/acquire/promote/finish/gc and forbids bypassing CLI safety blockers.
---
```

The body must contain these sections:

- Overview: CLI is authoritative; skill contains procedure only.
- Required preflight: `awf wt status --repo-root <repo-root> --refresh --json`.
- Feature worktree: `acquire` and exact lease reuse.
- Production promotion: `promote --source-pr <number> --to <branch> --apply --json`.
- Deployment verification: use the repository’s existing CI/rollout path; never infer health.
- Cleanup: `finish --pr <number> --apply --json`, then obey blockers.
- Bulk cleanup: `gc --merged --older-than 7d` preview before `--apply`.
- Blocker response: report code/message and preserve the worktree.
- Forbidden fallbacks: direct worktree mutation, staging wholesale merge, branch-merged heuristic, stash/reset/force-delete, unmanaged deletion.
- JSON decision table mapping `reuse`, `preview`, `ready`, `removed`, and `blocked` to the next action.

Use RFC 2119 words for safety requirements. Do not include provider-specific tool syntax; OMP, Claude, and Codex must read the same body.

- [ ] **Step 3: Run GREEN pressure scenarios with the skill explicitly loaded**

Re-run the same agents with `skill://release-worktree-lifecycle` supplied. PASS requires every response to:

- start with `awf wt status --refresh --json`,
- reuse an exact lease instead of creating another,
- call `promote` for staging-to-production,
- stop on closed-unmerged, dirty, or deployment-unknown blockers,
- avoid direct destructive Git commands.

If any agent finds a loophole, update the skill wording and re-run only the failed scenario until it passes.

- [ ] **Step 4: Validate skill command examples and commit**

Run:

```bash
uv run --project cli pytest cli/tests/test_docs_semantic_audit.py::test_skill_cli_command_templates_parse_with_current_cli -q
```

Expected: PASS; every `awf` command in the skill parses.

Commit:

```bash
git add claude/skills/release-worktree-lifecycle/SKILL.md
git commit -m "feat: add release worktree lifecycle skill"
```

---

### Task 10: Install the skill for Claude, OMP, and Agent Skills runtimes

**Files:**
- Create: `scripts/install-skill-links.sh`
- Modify: `setup.sh:4-65`
- Create: `cli/tests/test_release_worktree_skill_install.py`

- [ ] **Step 1: Write failing installer behavior tests**

Use `subprocess.run` with temporary target roots:

```python
def run_installer(repo_root: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(repo_root / "scripts" / "install-skill-links.sh"),
            str(repo_root / "claude" / "skills" / "release-worktree-lifecycle"),
            str(tmp_path / ".claude" / "skills"),
            str(tmp_path / ".agents" / "skills"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_installer_links_one_source_into_both_skill_roots(
    repo_root: Path, tmp_path: Path
) -> None:
    completed = run_installer(repo_root, tmp_path)

    assert completed.returncode == 0
    claude = tmp_path / ".claude" / "skills" / "release-worktree-lifecycle"
    agents = tmp_path / ".agents" / "skills" / "release-worktree-lifecycle"
    assert claude.resolve() == agents.resolve()
    assert claude.resolve() == (
        repo_root / "claude" / "skills" / "release-worktree-lifecycle"
    ).resolve()


def test_installer_preserves_a_real_conflicting_directory(
    repo_root: Path, tmp_path: Path
) -> None:
    conflict = tmp_path / ".agents" / "skills" / "release-worktree-lifecycle"
    conflict.mkdir(parents=True)
    marker = conflict / "owned.txt"
    marker.write_text("keep", encoding="utf-8")

    completed = run_installer(repo_root, tmp_path)

    assert completed.returncode == 0
    assert marker.read_text(encoding="utf-8") == "keep"
    assert "preserved" in completed.stderr
```

- [ ] **Step 2: Run installer tests and confirm RED**

Expected: subprocess exits 127 because `scripts/install-skill-links.sh` does not exist.

- [ ] **Step 3: Implement collision-safe link installation**

Create an executable POSIX shell script taking one source directory followed by one or more skill roots. It must:

- resolve and verify `<source>/SKILL.md`,
- create each root,
- keep an exact symlink,
- replace a symlink pointing elsewhere,
- preserve any real file/directory and warn to stderr,
- link with an absolute source path,
- never use `rm -rf`.

Update `setup.sh`:

```bash
AGENTS_SKILLS_DIR="${AGENTS_SKILLS_DIR:-$HOME/.agents/skills}"
```

After the existing Claude skill loop, invoke:

```bash
"$SCRIPT_DIR/scripts/install-skill-links.sh" \
  "$SCRIPT_DIR/claude/skills/release-worktree-lifecycle" \
  "$CLAUDE_DIR/skills" \
  "$AGENTS_SKILLS_DIR"
```

Do not add the release skill to the existing `SKILLS` array; the helper owns both links and avoids duplicate installer logic.

- [ ] **Step 4: Run installer tests and commit**

Run:

```bash
uv run --project cli pytest cli/tests/test_release_worktree_skill_install.py -q
```

Expected: both tests pass.

Commit:

```bash
git add scripts/install-skill-links.sh setup.sh cli/tests/test_release_worktree_skill_install.py
git commit -m "feat: install release skill across agent runtimes"
```

---

### Task 11: Document the operator contract and run the end-to-end smoke path

**Files:**
- Modify: `cli/README.md`
- Modify: `README.md`
- Create: `cli/tests/test_release_worktree_smoke.py`

- [ ] **Step 1: Write the failing end-to-end smoke test**

The smoke test must use a real bare remote and real worktrees, a fake GitHub adapter, and an injected deployment runner. Exercise this sequence:

```python
def test_release_worktree_lifecycle_smoke(smoke: SmokeHarness) -> None:
    acquired = smoke.service.acquire(
        initiative="reward-widget",
        purpose=Purpose.FEATURE,
        base="staging",
        branch=None,
        owner_id="smoke",
        apply=True,
    )
    assert acquired.decision == "ready"

    promoted_preview = smoke.service.promote(
        source_pr=372, target_branch="main", apply=False
    )
    assert promoted_preview.decision == "preview"

    smoke.config = replace(smoke.config, verify_production=())
    blocked_verify = smoke.service.promote(
        source_pr=372, target_branch="main", apply=True
    )
    assert blocked_verify.blockers[0]["code"] == "production_verify_missing"

    smoke.enable_verify_success()
    promoted = smoke.service.promote(source_pr=372, target_branch="main", apply=True)
    assert promoted.decision == "ready"

    smoke.merge_target_pr(deployment=DeploymentState.UNKNOWN)
    blocked_cleanup = smoke.service.finish(
        pr_number=promoted.lease.target_pr, apply=True
    )
    assert blocked_cleanup.blockers[0]["code"] == "deployment_not_healthy"
    assert promoted.lease.worktree_path.exists()

    smoke.set_deployment_healthy()
    removed = smoke.service.finish(pr_number=promoted.lease.target_pr, apply=True)
    assert removed.decision == "removed"
    assert not promoted.lease.worktree_path.exists()
```

- [ ] **Step 2: Run the smoke test and confirm it fails on any missing integration**

Run:

```bash
uv run --project cli pytest cli/tests/test_release_worktree_smoke.py -q
```

Expected before final wiring: FAIL at the first incomplete integration point. Fix production code, not the assertions, until the complete sequence passes.

- [ ] **Step 3: Expand user-facing documentation**

In `cli/README.md`, document:

- all eight `awf wt` subcommands,
- default preview versus `--apply`,
- JSON envelope and exit codes 0/2/3/4/5,
- state/cache environment overrides,
- `.awf/worktree.toml` argv-array example,
- one feature acquire flow and one promotion/finish flow.

In root `README.md`, add:

- `awf wt` to the feature map,
- one paragraph explaining the skill/CLI split,
- installation destinations `~/.claude/skills` and `~/.agents/skills`,
- OMP discovery through the Agent Skills provider,
- the rule that imported worktrees remain unmanaged until `adopt`.

Do not promise generic deployment orchestration; say AWF runs repository-configured verification/status commands around the existing CI/deployment system.

- [ ] **Step 4: Run focused and full CLI verification**

Run focused files first:

```bash
uv run --project cli pytest \
  cli/tests/test_worktree_registry.py \
  cli/tests/test_worktree_git.py \
  cli/tests/test_worktree_service.py \
  cli/tests/test_worktree_commands.py \
  cli/tests/test_release_worktree_skill_install.py \
  cli/tests/test_release_worktree_smoke.py -q
```

Expected: all worktree lifecycle tests pass.

Run the full CLI suite:

```bash
uv run --project cli pytest cli/tests
```

Expected baseline comparison: at least the existing `899 passed, 3 skipped, 10 deselected`, plus all new tests; zero failures or collection errors.

Run command smoke checks in a temporary repository:

```bash
AWF_WORKTREE_STATE_DB="$TMPDIR/awf-wt-smoke.sqlite3" \
AWF_WORKTREE_CACHE_DIR="$TMPDIR/awf-wt-cache" \
uv run --project cli awf wt status --repo-root "$TMP_REPO" --json
```

Expected: one valid JSON document with `command: wt.status` and exit 0.

- [ ] **Step 5: Commit documentation and smoke coverage**

```bash
git add README.md cli/README.md cli/tests/test_release_worktree_smoke.py
git commit -m "docs: publish release worktree lifecycle"
```

---

## Final review checklist

- [ ] Every new production method was introduced after a failing observable-contract test.
- [ ] `status` and `doctor` are read-only except explicit refresh state records.
- [ ] `acquire`, `promote`, `finish`, `gc`, `import`, and `adopt` default to preview unless `--apply` is present.
- [ ] Promotion uses source PR base/head delta, never the staging branch as a whole.
- [ ] Verify commands and deployment checks use argv arrays with `shell=False`.
- [ ] Dirty, unmanaged, closed-unmerged, HEAD-mismatch, retained, and deployment-unknown leases are preserved.
- [ ] JSON stdout contains one document; diagnostics use stderr.
- [ ] Imported worktrees remain `managed=false` until successful `adopt`.
- [ ] The same skill source resolves from both Claude and Agent Skills roots.
- [ ] OMP pressure scenarios pass only after the skill is loaded.
- [ ] Full CLI suite and real temporary-repository smoke path pass.
