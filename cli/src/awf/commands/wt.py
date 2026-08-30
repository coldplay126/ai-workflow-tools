from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

from awf.core.paths import find_repo_root
from awf.worktrees.config import ConfigError, load_worktree_config
from awf.worktrees.git import GitClient, GitError, GitRemoteError
from awf.worktrees.github import GhClient
from awf.worktrees.models import CommandResult, Purpose
from awf.worktrees.registry import WorktreeRegistry
from awf.worktrees.service import WorktreeService, state_db_path


def _emit(result: CommandResult, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(f"{result.command}: {result.decision}")
        for lease in result.leases:
            print(f"{lease.id}  {lease.state.value:16}  {lease.worktree_path}")
        for action in result.actions:
            detail = (
                action.get("path")
                or action.get("branch")
                or action.get("lease_id")
                or ""
            )
            print(f"{action['kind']}: {detail}")
        for blocker in result.blockers:
            print(
                f"blocked: {blocker['code']}: {blocker['message']}",
                file=sys.stderr,
            )
        for warning in result.warnings:
            print(
                f"warning: {warning['code']}: {warning['message']}",
                file=sys.stderr,
            )
    return result.exit_code


def _run(
    args: argparse.Namespace,
    command: str,
    operation: Callable[[WorktreeService], CommandResult],
) -> int:
    try:
        repository_root = find_repo_root(args.repo_root)
        config = load_worktree_config(repository_root)
        service = WorktreeService(
            WorktreeRegistry(state_db_path()),
            GitClient(repository_root),
            config=config,
        )
        result = operation(service)
    except (ConfigError, FileNotFoundError) as error:
        result = CommandResult.error(
            command,
            code="config_error",
            message=str(error),
            exit_code=2,
        )
    except GitRemoteError as error:
        result = CommandResult.external_error(
            command,
            code="git_remote_error",
            message=str(error),
        )
    except GitError as error:
        result = CommandResult.error(
            command,
            code="git_error",
            message=str(error),
            exit_code=5,
        )
    except OSError as error:
        result = CommandResult.error(
            command,
            code="filesystem_error",
            message=str(error),
            exit_code=5,
        )
    except (sqlite3.Error, ValueError) as error:
        result = CommandResult.error(
            command,
            code="registry_conflict",
            message=str(error),
            exit_code=5,
        )
    return _emit(result, as_json=bool(args.json))


def run_wt_status(args: argparse.Namespace) -> int:
    return _run(
        args,
        "wt.status",
        lambda service: service.status(
            initiative=args.initiative,
            refresh=args.refresh,
        ),
    )


def run_wt_doctor(args: argparse.Namespace) -> int:
    return _run(args, "wt.doctor", lambda service: service.doctor())


def run_wt_acquire(args: argparse.Namespace) -> int:
    return _run(
        args,
        "wt.acquire",
        lambda service: service.acquire(
            initiative=args.initiative,
            purpose=Purpose(args.purpose),
            base=args.base,
            branch=args.branch,
            owner_id=args.owner_id,
            apply=args.apply,
        ),
    )

def run_wt_sync(args: argparse.Namespace) -> int:
    return _run(
        args,
        "wt.sync",
        lambda service: service.sync(
            source_branch=args.from_branch,
            target_branch=args.to,
            apply=args.apply,
        ),
    )



def run_wt_promote(args: argparse.Namespace) -> int:
    return _run(
        args,
        "wt.promote",
        lambda service: service.promote(
            source_pr=args.source_pr,
            exclude_paths=args.exclude_path,
            target_branch=args.to,
            out_of_order=args.out_of_order,
            apply=args.apply,
        ),
    )


def run_wt_release_open(args: argparse.Namespace) -> int:
    return _run(
        args,
        "wt.release.open",
        lambda service: service.release_open(
            release_id=args.release,
            target_branch=args.to,
            apply=args.apply,
        ),
    )


def run_wt_release_add(args: argparse.Namespace) -> int:
    return _run(
        args,
        "wt.release.add",
        lambda service: service.release_add(
            release_id=args.release,
            source_pr=args.source_pr,
            apply=args.apply,
        ),
    )


def run_wt_release_seal(args: argparse.Namespace) -> int:
    return _run(
        args,
        "wt.release.seal",
        lambda service: service.release_seal(
            release_id=args.release,
            apply=args.apply,
        ),
    )


def run_wt_release_publish(args: argparse.Namespace) -> int:
    return _run(
        args,
        "wt.release.publish",
        lambda service: service.release_publish(
            release_id=args.release,
            apply=args.apply,
        ),
    )


def run_wt_recover_promotion(args: argparse.Namespace) -> int:
    return _run(
        args,
        "wt.recover-promotion",
        lambda service: service.recover_promotion(
            args.lease,
            apply=args.apply,
        ),
    )


def run_wt_finish(args: argparse.Namespace) -> int:
    return _run(
        args,
        "wt.finish",
        lambda service: service.finish(pr_number=args.pr, apply=args.apply),
    )


def run_wt_gc(args: argparse.Namespace) -> int:
    return _run(
        args,
        "wt.gc",
        lambda service: service.gc(
            merged=args.merged,
            older_than=args.older_than,
            apply=args.apply,
        ),
    )


def run_wt_compact(args: argparse.Namespace) -> int:
    return _run(
        args,
        "wt.compact",
        lambda service: service.compact(
            lease_id=args.lease,
            paths=args.path,
            older_than=args.older_than,
            apply=args.apply,
        ),
    )


def run_wt_import(
    args: argparse.Namespace,
    *,
    git_factory: Callable[[Path], GitClient] = GitClient,
) -> int:
    try:
        registry = WorktreeRegistry(state_db_path())
        service = WorktreeService(registry, None, git_factory=git_factory)
        result = service.import_root(Path(args.root), apply=args.apply)
    except (ConfigError, FileNotFoundError) as error:
        result = CommandResult.error(
            "wt.import",
            code="config_error",
            message=str(error),
            exit_code=2,
        )
    except GitError as error:
        result = CommandResult.error(
            "wt.import",
            code="git_error",
            message=str(error),
            exit_code=5,
        )
    except OSError as error:
        result = CommandResult.error(
            "wt.import",
            code="filesystem_error",
            message=str(error),
            exit_code=5,
        )
    except (sqlite3.Error, ValueError) as error:
        result = CommandResult.error(
            "wt.import",
            code="registry_conflict",
            message=str(error),
            exit_code=5,
        )
    return _emit(result, as_json=bool(args.json))


def run_wt_link_pr(args: argparse.Namespace) -> int:
    registry = WorktreeRegistry(state_db_path())
    try:
        lease = registry.get_lease_read_only(args.lease)
        if lease is None:
            result = CommandResult.blocked(
                "wt.link-pr",
                blockers=(
                    {
                        "code": "unknown_lease",
                        "message": f"lease {args.lease} does not exist",
                    },
                ),
            )
        else:
            service = WorktreeService(
                registry,
                GitClient(lease.repository_root),
                github=GhClient(lease.repository_root),
            )
            result = service.link_pr(
                args.lease, pr_number=args.pr, apply=args.apply
            )
    except (ConfigError, FileNotFoundError) as error:
        result = CommandResult.error(
            "wt.link-pr",
            code="config_error",
            message=str(error),
            exit_code=2,
        )
    except GitError as error:
        result = CommandResult.error(
            "wt.link-pr",
            code="git_error",
            message=str(error),
            exit_code=5,
        )
    except OSError as error:
        result = CommandResult.error(
            "wt.link-pr",
            code="filesystem_error",
            message=str(error),
            exit_code=5,
        )
    except (sqlite3.Error, ValueError) as error:
        result = CommandResult.error(
            "wt.link-pr",
            code="registry_conflict",
            message=str(error),
            exit_code=5,
        )
    return _emit(result, as_json=bool(args.json))


def run_wt_adopt(args: argparse.Namespace) -> int:
    registry = WorktreeRegistry(state_db_path())
    try:
        lease = registry.get_lease_read_only(args.lease)
        if lease is None:
            result = CommandResult.blocked(
                "wt.adopt",
                blockers=(
                    {
                        "code": "unknown_lease",
                        "message": f"lease {args.lease} does not exist",
                    },
                ),
            )
        else:
            service = WorktreeService(
                registry,
                GitClient(lease.repository_root),
                github=GhClient(lease.repository_root) if args.pr is not None else None,
            )
            result = service.adopt(
                args.lease, pr_number=args.pr, apply=args.apply
            )
    except (ConfigError, FileNotFoundError) as error:
        result = CommandResult.error(
            "wt.adopt",
            code="config_error",
            message=str(error),
            exit_code=2,
        )
    except GitError as error:
        result = CommandResult.error(
            "wt.adopt",
            code="git_error",
            message=str(error),
            exit_code=5,
        )
    except OSError as error:
        result = CommandResult.error(
            "wt.adopt",
            code="filesystem_error",
            message=str(error),
            exit_code=5,
        )
    except (sqlite3.Error, ValueError) as error:
        result = CommandResult.error(
            "wt.adopt",
            code="registry_conflict",
            message=str(error),
            exit_code=5,
        )
    return _emit(result, as_json=bool(args.json))
