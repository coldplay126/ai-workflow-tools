from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable

from awf.core.paths import find_repo_root
from awf.worktrees.config import ConfigError
from awf.worktrees.git import GitClient, GitError
from awf.worktrees.models import CommandResult
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
            print(f"{action['kind']}: {action['path']}")
        for blocker in result.blockers:
            print(
                f"blocked: {blocker['code']}: {blocker['message']}",
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
        service = WorktreeService(
            WorktreeRegistry(state_db_path()),
            GitClient(repository_root),
        )
        result = operation(service)
    except (ConfigError, FileNotFoundError) as error:
        result = CommandResult.error(
            command,
            code="config_error",
            message=str(error),
            exit_code=2,
        )
    except GitError as error:
        result = CommandResult.error(
            command,
            code="git_error",
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
        lambda service: service.status(initiative=args.initiative),
    )


def run_wt_doctor(args: argparse.Namespace) -> int:
    return _run(args, "wt.doctor", lambda service: service.doctor())
