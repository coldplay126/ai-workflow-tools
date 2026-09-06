from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from awf.worktrees.config import ConfigError, WorktreeConfig, load_worktree_config


SCHEMA_VERSION = 1
_GIT_TIMEOUT_SECONDS = 5
_OMP_TIMEOUT_SECONDS = 10
_PREPARE_COMMAND = ("awf", "lsp", "materialize")
_BASE_EXCLUDE_ENTRIES = ("/.omp/lsp.json", "/.awf/worktree.toml")
_SKIP_DIRECTORIES = frozenset({
    ".git", ".gradle", ".hg", ".idea", ".next", ".nuxt", ".pytest_cache",
    ".svn", ".venv", "__pycache__", "build", "coverage", "dist", "node_modules",
    "out", "target", "vendor",
})


@dataclass(frozen=True)
class ServerDefinition:
    name: str
    language: str
    binary: str
    args: tuple[str, ...]
    extensions: tuple[str, ...]
    root_markers: tuple[str, ...]

    def config(self, root_markers: Sequence[str] | None = None) -> dict[str, Any]:
        return {
            "command": self.binary,
            "args": list(self.args),
            "fileTypes": list(self.extensions),
            "rootMarkers": list(root_markers or self.root_markers),
        }


_SERVERS = (
    ServerDefinition("pyright", "python", "pyright-langserver", ("--stdio",), (".py", ".pyi"), ("pyproject.toml", "pyrightconfig.json", "setup.py", "setup.cfg", "requirements.txt", "Pipfile")),
    ServerDefinition("typescript-language-server", "typescript-javascript", "typescript-language-server", ("--stdio",), (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"), ("package.json", "tsconfig.json", "jsconfig.json")),
    ServerDefinition("intelephense", "php", "intelephense", ("--stdio",), (".php", ".phtml"), ("composer.json", "composer.lock")),
    ServerDefinition("gopls", "go", "gopls", ("serve",), (".go", ".mod", ".sum"), ("go.mod", "go.work", "go.sum")),
    ServerDefinition("rust-analyzer", "rust", "rust-analyzer", (), (".rs",), ("Cargo.toml", "rust-analyzer.toml")),
    ServerDefinition("jdtls", "java-kotlin", "jdtls", (), (".java",), ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", ".project")),
    ServerDefinition("kotlin-lsp", "java-kotlin", "kotlin-lsp", ("--stdio",), (".kt", ".kts"), ("build.gradle", "build.gradle.kts", "pom.xml", "settings.gradle", "settings.gradle.kts")),
    ServerDefinition("vue-language-server", "vue", "vue-language-server", ("--stdio",), (".vue",), ("vue.config.js", "nuxt.config.js", "nuxt.config.ts", "package.json")),
)
_SERVER_BY_NAME = {server.name: server for server in _SERVERS}
_LANGUAGE_ORDER = ("python", "typescript-javascript", "php", "go", "rust", "java-kotlin", "vue")


class LspSetupError(RuntimeError):
    """An invalid or unsafe local LSP setup condition."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class GitIdentity:
    repository_root: Path
    common_directory: Path
    profile_id: str


@dataclass(frozen=True)
class SourceFingerprint:
    exists: bool
    kind: str
    digest: str | None = None
    link_target: str | None = None


@dataclass(frozen=True)
class TrackedState:
    relative_path: str
    tracked: bool



@dataclass(frozen=True)
class LocalConfigPlan:
    relative_path: str
    path: Path
    content: str
    source: SourceFingerprint
    tracked_state: TrackedState
    write_allowed: bool
    existing_status: str
@dataclass
class SetupPlan:
    identity: GitIdentity
    languages: list[str]
    definitions: list[ServerDefinition]
    profile_root: Path
    profile_directory: Path
    profile_metadata_path: Path
    profile_lsp_path: Path
    profile_metadata: dict[str, Any]
    profile_lsp: dict[str, Any]
    profile_metadata_source: SourceFingerprint
    profile_lsp_source: SourceFingerprint
    user_lsp_root: Path
    user_lsp_path: Path
    user_lsp: dict[str, Any]
    user_lsp_source: SourceFingerprint
    project_lsp_path: Path
    project_link_exists: bool
    project_link_source: SourceFingerprint
    project_lsp_tracked_state: TrackedState
    exclude_path: Path
    original_exclude_content: str
    exclude_content: str
    exclude_source: SourceFingerprint
    worktree_path: Path
    worktree_content: str
    worktree_source: SourceFingerprint
    worktree_tracked_state: TrackedState
    local_configs: tuple[LocalConfigPlan, ...]
    warnings: list[dict[str, str]]


@dataclass
class MaterializePlan:
    identity: GitIdentity
    languages: list[str]
    server_entries: list[dict[str, Any]]
    profile_root: Path
    profile_directory: Path
    profile_metadata_path: Path
    profile_metadata_source: SourceFingerprint
    profile_lsp_path: Path
    profile_lsp_source: SourceFingerprint
    project_lsp_path: Path
    project_link_exists: bool
    project_link_source: SourceFingerprint
    project_lsp_tracked_state: TrackedState
    exclude_path: Path
    original_exclude_content: str
    exclude_content: str
    exclude_source: SourceFingerprint
    warnings: list[dict[str, str]]
    local_configs: tuple[LocalConfigPlan, ...]


def setup_lsp(repo_root: str | Path | None = None, *, apply: bool = False) -> dict[str, Any]:
    """Preview or apply repository-local LSP setup.

    Preview is read-only.  Apply delays every write until all tracked-file,
    symlink, profile, JSON, and worktree configuration checks have passed.
    """
    try:
        plan = _build_setup_plan(repo_root)
    except LspSetupError as error:
        return _blocked_result("lsp.setup", error)

    result = _setup_result(plan)
    if not apply:
        return result

    actions = result["actions"]
    omp_action = _action_for(actions, "omp_isolation")
    blocker = _configure_omp_isolation(omp_action)
    if blocker is not None:
        return _with_apply_failure(result, blocker)

    try:
        _apply_setup_plan(plan, actions)
    except (LspSetupError, OSError) as error:
        return _with_apply_failure(
            result, _error_blocker(error, "could not persist LSP setup")
        )
    result["decision"] = "applied"
    return result


def status_lsp(repo_root: str | Path | None = None) -> dict[str, Any]:
    """Return an exact, read-only materialization state for the current repo."""
    try:
        identity = _git_identity(repo_root)
        profile_root = _profile_root()
        profile_directory = _profile_directory(identity.profile_id)
        _assert_path_within_root(
            profile_directory, profile_root, "shared LSP profile directory"
        )
        metadata, profile_lsp = _load_profile(
            profile_directory / "profile.json", profile_directory / "lsp.json", identity.profile_id
        )
        project_path = identity.repository_root / ".omp" / "lsp.json"
        _assert_path_within_root(
            project_path, identity.repository_root, "repository .omp/lsp.json"
        )
        linked = _validate_project_link(project_path, profile_directory, profile_directory / "lsp.json")
        exclude_path = identity.common_directory / "info" / "exclude"
        _assert_path_within_root(
            exclude_path, identity.common_directory, "Git local exclude"
        )
        exclude_content = _read_exclude(exclude_path)
        worktree_path = identity.repository_root / ".awf" / "worktree.toml"
        _assert_path_within_root(
            worktree_path, identity.repository_root, "repository .awf/worktree.toml"
        )
        worktree_state, _ = _worktree_state(worktree_path)
        local_files = _metadata_local_files(metadata)
        local_configs = _local_config_plans(
            identity.repository_root, local_files
        )
    except LspSetupError as error:
        return _blocked_result("lsp.status", error)

    if metadata is None or profile_lsp is None:
        languages, definitions, _ = _detect_languages(identity.repository_root)
        servers, warnings = _server_entries(definitions, identity.repository_root)
        return _result(
            "lsp.status",
            "not_configured",
            languages,
            servers,
            [
                {"kind": "profile", "status": "missing"},
                {"kind": "project_symlink", "status": "present" if linked else "missing"},
                {"kind": "git_exclude", "status": _exclude_status(exclude_content)},
                {"kind": "prepare_hook", "status": worktree_state},
            ],
            warnings=warnings,
        )

    languages = _metadata_languages(metadata)
    servers, server_warnings = _entries_from_profile(
        languages, profile_lsp, identity.repository_root
    )
    warnings = [*server_warnings, *_local_config_warnings(local_configs)]
    decision = (
        "configured"
        if linked
        and _exclude_is_complete(exclude_content, local_files)
        and worktree_state == "compatible"
        and all(config.source.exists for config in local_configs)
        else "incomplete"
    )
    return _result(
        "lsp.status",
        decision,
        languages,
        servers,
        [
            {"kind": "profile", "status": "present"},
            {"kind": "project_symlink", "status": "present" if linked else "missing"},
            {
                "kind": "git_exclude",
                "status": _exclude_status(exclude_content, local_files),
            },
            {"kind": "prepare_hook", "status": worktree_state},
            *[_local_config_status_action(config) for config in local_configs],
        ],
        warnings=warnings,
    )


def materialize_lsp(repo_root: str | Path | None = None) -> dict[str, Any]:
    """Materialize an existing shared profile in the current worktree."""
    try:
        plan = _build_materialize_plan(repo_root)
    except LspSetupError as error:
        return _blocked_result("lsp.materialize", error)

    result = _result(
        "lsp.materialize", "materialized", plan.languages, plan.server_entries,
        _materialize_actions(plan), warnings=plan.warnings,
    )
    try:
        _apply_materialize_plan(plan, result["actions"])
    except (LspSetupError, OSError) as error:
        return _with_apply_failure(
            result, _error_blocker(error, "could not materialize LSP profile")
        )
    return result


def _build_setup_plan(repo_root: str | Path | None) -> SetupPlan:
    identity = _git_identity(repo_root)
    languages, definitions, detected_markers = _detect_languages(
        identity.repository_root
    )
    profile_root = _profile_root()
    profile_directory = _profile_directory(identity.profile_id)
    _assert_path_within_root(
        profile_directory, profile_root, "shared LSP profile directory"
    )
    metadata_path = profile_directory / "profile.json"
    profile_lsp_path = profile_directory / "lsp.json"
    profile_metadata_source = _source_fingerprint(
        metadata_path, "LSP profile metadata"
    )
    profile_lsp_source = _source_fingerprint(
        profile_lsp_path, "LSP profile config"
    )
    existing_metadata, existing_profile_lsp = _load_profile(
        metadata_path,
        profile_lsp_path,
        identity.profile_id,
        allow_incomplete=True,
    )
    project_generated = {
        definition.name: definition.config(detected_markers[definition.name])
        for definition in definitions
    }
    user_generated = {
        definition.name: definition.config()
        for definition in definitions
    }
    generated_local_files = _generated_local_files(
        identity.repository_root, languages, detected_markers
    )
    profile_local_files = {
        **_metadata_local_files(existing_metadata),
        **generated_local_files,
    }
    local_configs = _local_config_plans(
        identity.repository_root, profile_local_files
    )
    profile_lsp = _merge_servers(
        existing_profile_lsp or {},
        project_generated,
        "profile LSP config",
    )
    profile_metadata = {
        "schema_version": SCHEMA_VERSION,
        "repo_identity": identity.profile_id,
        "languages": _stable_unique([*_metadata_languages(existing_metadata), *languages]),
        "servers": sorted(profile_lsp),
        "local_files": profile_local_files,
    }

    user_lsp_root, user_lsp_path = _omp_user_lsp_location()
    _assert_path_within_root(
        user_lsp_path, user_lsp_root, "user OMP LSP config"
    )
    user_lsp_source = _source_fingerprint(user_lsp_path, "user OMP LSP config")
    user_lsp = _merge_lsp_document(
        _load_json_object(user_lsp_path, "user OMP LSP config"),
        user_generated,
        "user OMP LSP config",
    )

    project_lsp_path = identity.repository_root / ".omp" / "lsp.json"
    _assert_path_within_root(
        project_lsp_path, identity.repository_root, "repository .omp/lsp.json"
    )
    _assert_project_parent(project_lsp_path.parent, ".omp")
    project_lsp_tracked_state = _untracked_state(
        identity.repository_root, ".omp/lsp.json"
    )
    project_link_source = _source_fingerprint(
        project_lsp_path, "repository .omp/lsp.json", allow_symlink=True
    )
    project_link_exists = _validate_project_link(
        project_lsp_path, profile_directory, profile_lsp_path
    )

    exclude_path = identity.common_directory / "info" / "exclude"
    _assert_path_within_root(
        exclude_path, identity.common_directory, "Git local exclude"
    )
    exclude_source = _source_fingerprint(exclude_path, "Git local exclude")
    original_exclude_content = _read_exclude(exclude_path)
    exclude_content = _merge_exclude(
        original_exclude_content, profile_local_files
    )

    worktree_path = identity.repository_root / ".awf" / "worktree.toml"
    _assert_path_within_root(
        worktree_path, identity.repository_root, "repository .awf/worktree.toml"
    )
    _assert_project_parent(worktree_path.parent, ".awf")
    worktree_tracked_state = _untracked_state(
        identity.repository_root, ".awf/worktree.toml"
    )
    worktree_source = _source_fingerprint(
        worktree_path, "repository .awf/worktree.toml"
    )
    worktree_content = _prepare_worktree_content(worktree_path)
    _, warnings = _server_entries(
        definitions, identity.repository_root, profile_lsp
    )

    warnings = [
        *warnings,
        *_local_config_warnings(local_configs),
    ]
    plan = SetupPlan(
        identity=identity,
        languages=profile_metadata["languages"],
        definitions=definitions,
        profile_root=profile_root,
        profile_directory=profile_directory,
        profile_metadata_path=metadata_path,
        profile_lsp_path=profile_lsp_path,
        profile_metadata=profile_metadata,
        profile_lsp=profile_lsp,
        profile_metadata_source=profile_metadata_source,
        profile_lsp_source=profile_lsp_source,
        user_lsp_root=user_lsp_root,
        user_lsp_path=user_lsp_path,
        user_lsp=user_lsp,
        user_lsp_source=user_lsp_source,
        project_lsp_path=project_lsp_path,
        project_link_exists=project_link_exists,
        project_link_source=project_link_source,
        project_lsp_tracked_state=project_lsp_tracked_state,
        exclude_path=exclude_path,
        original_exclude_content=original_exclude_content,
        exclude_content=exclude_content,
        exclude_source=exclude_source,
        worktree_path=worktree_path,
        worktree_content=worktree_content,
        worktree_source=worktree_source,
        worktree_tracked_state=worktree_tracked_state,
        local_configs=local_configs,
        warnings=warnings,
    )
    _revalidate_setup_sources(plan)
    return plan


def _build_materialize_plan(repo_root: str | Path | None) -> MaterializePlan:
    identity = _git_identity(repo_root)
    profile_root = _profile_root()
    profile_directory = _profile_directory(identity.profile_id)
    _assert_path_within_root(
        profile_directory, profile_root, "shared LSP profile directory"
    )
    profile_metadata_path = profile_directory / "profile.json"
    profile_metadata_source = _source_fingerprint(
        profile_metadata_path, "LSP profile metadata"
    )
    profile_lsp_path = profile_directory / "lsp.json"
    profile_lsp_source = _source_fingerprint(
        profile_lsp_path, "LSP profile config"
    )
    metadata, profile_lsp = _load_profile(
        profile_metadata_path, profile_lsp_path, identity.profile_id
    )
    if metadata is None or profile_lsp is None:
        raise LspSetupError("profile_missing", "no LSP profile exists for this Git identity; run awf lsp setup --apply first")
    local_files = _metadata_local_files(metadata)
    local_configs = _local_config_plans(identity.repository_root, local_files)
    project_lsp_path = identity.repository_root / ".omp" / "lsp.json"
    _assert_path_within_root(
        project_lsp_path, identity.repository_root, "repository .omp/lsp.json"
    )
    _assert_project_parent(project_lsp_path.parent, ".omp")
    project_lsp_tracked_state = _untracked_state(
        identity.repository_root, ".omp/lsp.json"
    )
    project_link_source = _source_fingerprint(
        project_lsp_path, "repository .omp/lsp.json", allow_symlink=True
    )
    linked = _validate_project_link(
        project_lsp_path, profile_directory, profile_lsp_path
    )
    exclude_path = identity.common_directory / "info" / "exclude"
    _assert_path_within_root(
        exclude_path, identity.common_directory, "Git local exclude"
    )
    exclude_source = _source_fingerprint(exclude_path, "Git local exclude")
    original_exclude_content = _read_exclude(exclude_path)
    languages = _metadata_languages(metadata)
    entries, warnings = _entries_from_profile(
        languages, profile_lsp, identity.repository_root
    )
    plan = MaterializePlan(
        identity=identity,
        languages=languages,
        server_entries=entries,
        profile_root=profile_root,
        profile_directory=profile_directory,
        profile_metadata_path=profile_metadata_path,
        profile_metadata_source=profile_metadata_source,
        profile_lsp_path=profile_lsp_path,
        profile_lsp_source=profile_lsp_source,
        project_lsp_path=project_lsp_path,
        project_link_exists=linked,
        project_link_source=project_link_source,
        project_lsp_tracked_state=project_lsp_tracked_state,
        exclude_path=exclude_path,
        original_exclude_content=original_exclude_content,
        exclude_content=_merge_exclude(original_exclude_content, local_files),
        exclude_source=exclude_source,
        warnings=[*warnings, *_local_config_warnings(local_configs)],
        local_configs=local_configs,
    )
    _revalidate_materialize_sources(plan)
    return plan


def _apply_setup_plan(plan: SetupPlan, actions: list[dict[str, Any]]) -> None:
    _revalidate_setup_sources(plan)
    profile_action = _action_for(actions, "profile")
    if profile_action["status"] == "planned":
        try:
            if _source_needs_write(
                plan.profile_metadata_source,
                _json_text(plan.profile_metadata),
            ):
                _atomic_write(
                    plan.profile_metadata_path,
                    plan.profile_metadata_source,
                    _json_text(plan.profile_metadata),
                    plan.profile_root,
                    "LSP profile metadata",
                )
                profile_action["status"] = "partial"
            if _source_needs_write(
                plan.profile_lsp_source, _json_text(plan.profile_lsp)
            ):
                _atomic_write(
                    plan.profile_lsp_path,
                    plan.profile_lsp_source,
                    _json_text(plan.profile_lsp),
                    plan.profile_root,
                    "LSP profile config",
                )
        except (LspSetupError, OSError):
            if profile_action["status"] == "planned":
                profile_action["status"] = "failed"
            raise
        profile_action["status"] = "applied"

    _assert_identity_current(plan.identity)
    _apply_write_action(
        _action_for(actions, "user_lsp_config"),
        plan.user_lsp_path,
        plan.user_lsp_source,
        _json_text(plan.user_lsp),
        plan.user_lsp_root,
        "user OMP LSP config",
    )
    _assert_identity_current(plan.identity)
    _apply_write_action(
        _action_for(actions, "git_exclude"),
        plan.exclude_path,
        plan.exclude_source,
        plan.exclude_content,
        plan.identity.common_directory,
        "Git local exclude",
    )
    _assert_identity_current(plan.identity)
    _apply_local_configs(
        plan.local_configs, actions, plan.identity.repository_root
    )
    _assert_identity_current(plan.identity)
    project_action = _action_for(actions, "project_symlink")
    if project_action["status"] == "planned":
        try:
            _atomic_symlink(
                plan.profile_lsp_path,
                plan.project_lsp_path,
                plan.project_link_source,
                plan.profile_directory,
                plan.identity.repository_root,
            )
        except (LspSetupError, OSError):
            project_action["status"] = "failed"
            raise
        project_action["status"] = "applied"
    _assert_identity_current(plan.identity)
    _assert_tracked_state_current(
        plan.identity.repository_root, plan.worktree_tracked_state
    )
    _apply_write_action(
        _action_for(actions, "prepare_hook"),
        plan.worktree_path,
        plan.worktree_source,
        plan.worktree_content,
        plan.identity.repository_root,
        "repository .awf/worktree.toml",
    )


def _apply_materialize_plan(
    plan: MaterializePlan, actions: list[dict[str, Any]]
) -> None:
    _revalidate_materialize_sources(plan)
    _assert_identity_current(plan.identity)
    _apply_write_action(
        _action_for(actions, "git_exclude"),
        plan.exclude_path,
        plan.exclude_source,
        plan.exclude_content,
        plan.identity.common_directory,
        "Git local exclude",
    )
    _assert_identity_current(plan.identity)
    _apply_local_configs(
        plan.local_configs, actions, plan.identity.repository_root
    )
    _assert_identity_current(plan.identity)
    project_action = _action_for(actions, "project_symlink")
    if project_action["status"] != "planned":
        return
    try:
        _atomic_symlink(
            plan.profile_lsp_path,
            plan.project_lsp_path,
            plan.project_link_source,
            plan.profile_directory,
            plan.identity.repository_root,
        )
    except (LspSetupError, OSError):
        project_action["status"] = "failed"
        raise
    project_action["status"] = "applied"


def _git_identity(repo_root: str | Path | None) -> GitIdentity:
    candidate = Path(repo_root if repo_root is not None else os.getcwd()).expanduser()
    if not candidate.is_dir():
        raise LspSetupError("repo_root_invalid", "repository root must be an existing directory")
    root = Path(_run_git(candidate, ("rev-parse", "--show-toplevel"))).resolve()
    common_text = _run_git(root, ("rev-parse", "--git-common-dir"))
    common_path = Path(common_text)
    common = (common_path if common_path.is_absolute() else root / common_path).resolve()
    if not common.is_dir():
        raise LspSetupError("git_common_dir_invalid", "Git common directory does not exist")
    return GitIdentity(root, common, hashlib.sha256(str(common).encode("utf-8")).hexdigest())


def _assert_identity_current(expected: GitIdentity) -> None:
    if _git_identity(expected.repository_root) != expected:
        raise LspSetupError(
            "concurrent_git_identity_change",
            "Git repository identity changed while preparing LSP setup",
        )


def _run_git(repository_root: Path, arguments: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository_root), *arguments], capture_output=True, text=True,
            timeout=_GIT_TIMEOUT_SECONDS, check=False, shell=False,
        )
    except FileNotFoundError as error:
        raise LspSetupError("git_unavailable", "git executable is required for LSP setup") from error
    except subprocess.TimeoutExpired as error:
        raise LspSetupError("git_timeout", "Git identity lookup timed out") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown git error"
        raise LspSetupError("git_identity_failed", f"could not resolve Git repository identity: {detail}")
    output = completed.stdout.strip()
    if not output:
        raise LspSetupError("git_identity_failed", "Git repository identity lookup returned no path")
    return output


def _detect_languages(
    repository_root: Path,
) -> tuple[list[str], list[ServerDefinition], dict[str, tuple[str, ...]]]:
    names: set[str] = set()
    extensions: set[str] = set()
    paths_by_name: dict[str, list[str]] = {}
    paths_by_extension: dict[str, list[str]] = {}
    for directory, dirnames, filenames in os.walk(repository_root, followlinks=False):
        current = Path(directory)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _SKIP_DIRECTORIES and not (current / name).is_symlink()
        )
        for filename in sorted(filenames):
            path = current / filename
            if path.is_symlink():
                continue
            relative = path.relative_to(repository_root).as_posix()
            names.add(filename)
            paths_by_name.setdefault(filename, []).append(relative)
            suffix = path.suffix.lower()
            if suffix:
                extensions.add(suffix)
                paths_by_extension.setdefault(suffix, []).append(relative)
    present = {
        "python": bool(
            {
                "pyproject.toml",
                "pyrightconfig.json",
                "setup.py",
                "setup.cfg",
                "requirements.txt",
                "Pipfile",
            }
            & names
        )
        or bool({".py", ".pyi"} & extensions),
        "typescript-javascript": "package.json" in names
        or bool({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"} & extensions),
        "php": bool({"composer.json", "composer.lock"} & names)
        or bool({".php", ".phtml"} & extensions),
        "go": bool({"go.mod", "go.work", "go.sum"} & names)
        or bool({".go", ".mod", ".sum"} & extensions),
        "rust": bool({"Cargo.toml", "rust-analyzer.toml"} & names)
        or ".rs" in extensions,
        "java-kotlin": bool(
            {
                "pom.xml",
                "build.gradle",
                "build.gradle.kts",
                "settings.gradle",
                "settings.gradle.kts",
                ".project",
            }
            & names
        )
        or bool({".java", ".kt", ".kts"} & extensions),
        "vue": ".vue" in extensions,
    }
    languages = [language for language in _LANGUAGE_ORDER if present[language]]
    definitions = [
        server
        for server in _SERVERS
        if _server_detected(server, names, extensions, present)
    ]
    detected_markers: dict[str, tuple[str, ...]] = {}
    for definition in definitions:
        markers = [
            path
            for marker in definition.root_markers
            for path in paths_by_name.get(marker, ())
        ]
        if not markers:
            markers = [
                path
                for extension in definition.extensions
                for path in paths_by_extension.get(extension, ())[:1]
            ]
        detected_markers[definition.name] = tuple(
            sorted(_stable_unique(markers))
        )
    return languages, definitions, detected_markers


def _generated_local_files(
    repository_root: Path,
    languages: Sequence[str],
    detected_markers: Mapping[str, Sequence[str]],
) -> dict[str, str]:
    if "python" not in languages:
        return {}
    project_markers = {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "Pipfile",
    }
    source_roots = {
        Path(marker).parent / "src"
        for marker in detected_markers.get("pyright", ())
        if Path(marker).name in project_markers
        and (repository_root / Path(marker).parent / "src").is_dir()
        and not (repository_root / Path(marker).parent / "src").is_symlink()
    }
    for directory, dirnames, filenames in os.walk(
        repository_root, followlinks=False
    ):
        current = Path(directory)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _SKIP_DIRECTORIES
            and not (current / name).is_symlink()
        )
        if not any(Path(filename).suffix.lower() in {".py", ".pyi"} for filename in filenames):
            continue
        relative_parts = current.relative_to(repository_root).parts
        if "src" not in relative_parts:
            continue
        src_index = relative_parts.index("src")
        source_roots.add(Path(*relative_parts[: src_index + 1]))
    environments = [
        {
            "root": source_root.parent.as_posix() or ".",
            "extraPaths": [source_root.as_posix()],
        }
        for source_root in sorted(
            source_roots, key=lambda path: path.as_posix()
        )
    ]
    if not environments:
        return {}
    return {
        "pyrightconfig.json": _json_text(
            {"executionEnvironments": environments}
        )
    }


def _metadata_local_files(metadata: Mapping[str, Any] | None) -> dict[str, str]:
    if metadata is None or "local_files" not in metadata:
        return {}
    value = metadata["local_files"]
    if not isinstance(value, Mapping):
        raise LspSetupError(
            "profile_malformed",
            "LSP profile metadata local_files must be an object",
        )
    files: dict[str, str] = {}
    for relative_path, content in value.items():
        if (
            not isinstance(relative_path, str)
            or relative_path != Path(relative_path).name
            or relative_path not in {"pyrightconfig.json"}
            or not isinstance(content, str)
        ):
            raise LspSetupError(
                "profile_malformed",
                "LSP profile metadata contains an unsupported local file",
            )
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as error:
            raise LspSetupError(
                "profile_malformed",
                f"LSP profile metadata {relative_path} is invalid JSON",
            ) from error
        if not isinstance(parsed, Mapping):
            raise LspSetupError(
                "profile_malformed",
                f"LSP profile metadata {relative_path} must be a JSON object",
            )
        files[relative_path] = content
    return files


def _local_config_plans(
    repository_root: Path,
    local_files: Mapping[str, str],
) -> tuple[LocalConfigPlan, ...]:
    plans: list[LocalConfigPlan] = []
    for relative_path, content in sorted(local_files.items()):
        path = repository_root / relative_path
        _assert_path_within_root(path, repository_root, relative_path)
        tracked_state = TrackedState(
            relative_path, _tracked_state(repository_root, relative_path)
        )
        source = _source_fingerprint(path, relative_path)
        if source.exists:
            try:
                _load_json_object(path, relative_path)
            except LspSetupError as error:
                raise LspSetupError(
                    "local_config_malformed",
                    f"{relative_path} is malformed: {error.message}",
                ) from error
        same_content = not _source_needs_write(source, content)
        write_allowed = not tracked_state.tracked and (
            not source.exists or same_content
        )
        existing_status = (
            "repository_managed"
            if tracked_state.tracked
            else "unchanged"
            if same_content
            else "preserved"
            if source.exists
            else "missing"
        )
        plans.append(
            LocalConfigPlan(
                relative_path=relative_path,
                path=path,
                content=content,
                source=source,
                tracked_state=tracked_state,
                write_allowed=write_allowed,
                existing_status=existing_status,
            )
        )
    return tuple(plans)


def _server_detected(server: ServerDefinition, names: set[str], extensions: set[str], present: Mapping[str, bool]) -> bool:
    if server.name == "jdtls":
        return ".java" in extensions or bool({"pom.xml", "build.gradle", "settings.gradle"} & names)
    if server.name == "kotlin-lsp":
        return bool({".kt", ".kts"} & extensions) or bool({"build.gradle.kts", "settings.gradle.kts", "build.gradle", "settings.gradle"} & names)
    return bool(present[server.language])


def _omp_user_lsp_location() -> tuple[Path, Path]:
    configured = os.environ.get("PI_CONFIG_DIR")
    if configured:
        root = Path(configured).expanduser()
        return root, root / "lsp.json"
    home = Path.home()
    return home, home / ".omp" / "agent" / "lsp.json"


def _profile_root() -> Path:
    return (
        Path(os.environ["XDG_CONFIG_HOME"]).expanduser()
        if os.environ.get("XDG_CONFIG_HOME")
        else Path.home() / ".config"
    )


def _profile_directory(profile_id: str) -> Path:
    return _profile_root() / "awf" / "lsp" / profile_id


def _load_profile(
    metadata_path: Path,
    lsp_path: Path,
    expected_identity: str,
    *,
    allow_incomplete: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    _assert_profile_directory(metadata_path.parent)
    metadata_exists = metadata_path.exists() or metadata_path.is_symlink()
    lsp_exists = lsp_path.exists() or lsp_path.is_symlink()
    recoverable = allow_incomplete and metadata_exists and not lsp_exists
    if metadata_exists != lsp_exists and not recoverable:
        raise LspSetupError(
            "profile_incomplete", "shared LSP profile is incomplete"
        )
    if not metadata_exists:
        return None, None
    metadata = _load_json_object(metadata_path, "LSP profile metadata")
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise LspSetupError(
            "profile_malformed",
            "LSP profile metadata has an unsupported schema version",
        )
    if metadata.get("repo_identity") != expected_identity:
        raise LspSetupError(
            "profile_identity_mismatch",
            "LSP profile metadata does not match the Git common directory",
        )
    _metadata_languages(metadata)
    _metadata_local_files(metadata)
    if not isinstance(metadata.get("servers"), list) or not all(
        isinstance(name, str) for name in metadata["servers"]
    ):
        raise LspSetupError(
            "profile_malformed",
            "LSP profile metadata servers must be a string list",
        )
    lsp = (
        _load_json_object(lsp_path, "LSP profile config")
        if lsp_exists
        else None
    )
    return metadata, lsp


def _metadata_languages(metadata: Mapping[str, Any] | None) -> list[str]:
    if metadata is None:
        return []
    languages = metadata.get("languages")
    if not isinstance(languages, list) or not all(isinstance(language, str) for language in languages):
        raise LspSetupError("profile_malformed", "LSP profile metadata languages must be a string list")
    unknown = [language for language in languages if language not in _LANGUAGE_ORDER]
    if unknown:
        raise LspSetupError("profile_malformed", f"LSP profile metadata has an unknown language: {unknown[0]}")
    return _stable_unique(languages)


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    _assert_leaf_safe(path, label)
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LspSetupError("config_malformed", f"{label} is not valid JSON") from error
    if not isinstance(loaded, dict):
        raise LspSetupError("config_malformed", f"{label} must be a JSON object")
    return loaded


def _merge_servers(existing: Mapping[str, Any], generated: Mapping[str, Mapping[str, Any]], label: str) -> dict[str, Any]:
    merged = dict(existing)
    for name, defaults in generated.items():
        current = merged.get(name)
        if current is None:
            merged[name] = dict(defaults)
            continue
        if not isinstance(current, Mapping):
            raise LspSetupError("config_malformed", f"{label} server {name!r} must be a JSON object")
        markers = current.get("rootMarkers", [])
        if not isinstance(markers, list) or not all(isinstance(marker, str) for marker in markers):
            raise LspSetupError("config_malformed", f"{label} server {name!r} rootMarkers must be a string list")
        combined = dict(defaults)
        combined.update(current)
        combined["rootMarkers"] = _stable_unique([*markers, *defaults["rootMarkers"]])
        merged[name] = combined
    return merged


def _merge_lsp_document(
    existing: Mapping[str, Any],
    generated: Mapping[str, Mapping[str, Any]],
    label: str,
) -> dict[str, Any]:
    if "servers" not in existing:
        return _merge_servers(existing, generated, label)
    servers = existing.get("servers")
    if not isinstance(servers, Mapping):
        raise LspSetupError(
            "config_malformed", f"{label} servers must be a JSON object"
        )
    merged = dict(existing)
    merged["servers"] = _merge_servers(servers, generated, label)
    return merged

def _assert_profile_directory(path: Path) -> None:
    if path.is_symlink():
        raise LspSetupError("profile_symlink_unsafe", "the shared LSP profile directory must not be a symlink")
    if path.exists() and not path.is_dir():
        raise LspSetupError("profile_path_invalid", "the shared LSP profile path is not a directory")


def _assert_path_within_root(path: Path, root: Path, label: str) -> None:
    """Reject symlink ancestors below a trusted root before accessing a path."""
    trusted_root = root.expanduser().absolute()
    candidate = path.expanduser().absolute()
    try:
        relative = candidate.relative_to(trusted_root)
    except ValueError as error:
        raise LspSetupError(
            "path_outside_trusted_root",
            f"{label} is outside its trusted local configuration root",
        ) from error
    if ".." in relative.parts:
        raise LspSetupError(
            "path_outside_trusted_root",
            f"{label} is outside its trusted local configuration root",
        )
    if trusted_root.is_symlink():
        raise LspSetupError(
            "ancestor_symlink_unsafe",
            f"the trusted root for {label} must not be a symlink",
        )
    if trusted_root.exists() and not trusted_root.is_dir():
        raise LspSetupError(
            "trusted_root_invalid",
            f"the trusted root for {label} is not a directory",
        )
    resolved_root = trusted_root.resolve(strict=False)
    current = resolved_root
    for component in relative.parts[:-1]:
        current /= component
        if current.is_symlink():
            raise LspSetupError(
                "ancestor_symlink_unsafe",
                f"{label} has a symlink ancestor below its trusted root",
            )
        if current.exists() and not current.is_dir():
            raise LspSetupError(
                "path_invalid", f"{label} has a non-directory ancestor"
            )
    try:
        (resolved_root.joinpath(*relative.parts[:-1])).resolve(
            strict=False
        ).relative_to(resolved_root)
    except ValueError as error:
        raise LspSetupError(
            "path_outside_trusted_root",
            f"{label} resolves outside its trusted local configuration root",
        ) from error


def _assert_project_parent(path: Path, label: str) -> None:
    if path.is_symlink():
        raise LspSetupError("project_symlink_unsafe", f"repository {label} directory must not be a symlink")
    if path.exists() and not path.is_dir():
        raise LspSetupError("project_path_invalid", f"repository {label} path is not a directory")


def _assert_leaf_safe(path: Path, label: str) -> None:
    if path.is_symlink():
        raise LspSetupError("symlink_unsafe", f"{label} must not be a symlink")
    if path.exists() and not path.is_file():
        raise LspSetupError("path_invalid", f"{label} is not a regular file")


def _validate_project_link(
    project_path: Path,
    profile_directory: Path,
    profile_lsp_path: Path,
) -> bool:
    if not project_path.exists() and not project_path.is_symlink():
        return False
    if not project_path.is_symlink():
        raise LspSetupError(
            "project_lsp_conflict",
            "repository .omp/lsp.json already exists and is not a managed symlink",
        )
    try:
        target = project_path.resolve(strict=True)
        expected = profile_lsp_path.resolve(strict=True)
        profile_root = profile_directory.resolve(strict=True)
        target.relative_to(profile_root)
    except (OSError, ValueError) as error:
        raise LspSetupError(
            "project_symlink_unsafe",
            "repository .omp/lsp.json points outside its shared LSP profile",
        ) from error
    if target != expected:
        raise LspSetupError(
            "project_symlink_unsafe",
            "repository .omp/lsp.json points to an unexpected profile file",
        )
    return True


def _source_fingerprint(
    path: Path, label: str, *, allow_symlink: bool = False
) -> SourceFingerprint:
    if path.is_symlink():
        if not allow_symlink:
            raise LspSetupError("symlink_unsafe", f"{label} must not be a symlink")
        try:
            return SourceFingerprint(
                True, "symlink", link_target=os.readlink(path)
            )
        except OSError as error:
            raise LspSetupError(
                "path_unreadable", f"could not read {label}: {error}"
            ) from error
    if not path.exists():
        return SourceFingerprint(False, "missing")
    if not path.is_file():
        raise LspSetupError("path_invalid", f"{label} is not a regular file")
    try:
        content = path.read_bytes()
    except OSError as error:
        raise LspSetupError(
            "path_unreadable", f"could not read {label}: {error}"
        ) from error
    return SourceFingerprint(
        True, "file", hashlib.sha256(content).hexdigest()
    )


def _assert_source_current(
    path: Path,
    expected: SourceFingerprint,
    label: str,
    *,
    allow_symlink: bool = False,
) -> None:
    if _source_fingerprint(
        path, label, allow_symlink=allow_symlink
    ) != expected:
        raise LspSetupError(
            "concurrent_local_change",
            f"{label} changed while preparing LSP setup",
        )


def _source_needs_write(source: SourceFingerprint, content: str) -> bool:
    return source != SourceFingerprint(
        True, "file", hashlib.sha256(content.encode("utf-8")).hexdigest()
    )


def _tracked_state(repository_root: Path, relative_path: str) -> bool:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository_root), "ls-files", "--error-unmatch", "--", relative_path],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS, check=False, shell=False,
        )
    except FileNotFoundError as error:
        raise LspSetupError("git_unavailable", "git executable is required for LSP setup") from error
    except subprocess.TimeoutExpired as error:
        raise LspSetupError("git_timeout", "Git tracked-file check timed out") from error
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    detail = completed.stderr.strip() or completed.stdout.strip() or "unknown git error"
    raise LspSetupError(
        "git_tracked_check_failed",
        f"could not check {relative_path}: {detail}",
    )


def _untracked_state(repository_root: Path, relative_path: str) -> TrackedState:
    state = TrackedState(
        relative_path, _tracked_state(repository_root, relative_path)
    )
    if state.tracked:
        raise LspSetupError(
            "tracked_file_conflict",
            f"{relative_path} is tracked and will not be replaced",
        )
    return state


def _assert_tracked_state_current(
    repository_root: Path, expected: TrackedState
) -> None:
    current = TrackedState(
        expected.relative_path,
        _tracked_state(repository_root, expected.relative_path),
    )
    if current != expected:
        raise LspSetupError(
            "concurrent_local_change",
            f"{expected.relative_path} Git tracked state changed while preparing LSP setup",
        )


def _revalidate_setup_sources(plan: SetupPlan) -> None:
    _assert_identity_current(plan.identity)
    _assert_path_within_root(
        plan.profile_directory, plan.profile_root, "shared LSP profile directory"
    )
    _assert_profile_directory(plan.profile_directory)
    _assert_path_within_root(
        plan.user_lsp_path, plan.user_lsp_root, "user OMP LSP config"
    )
    _assert_path_within_root(
        plan.project_lsp_path,
        plan.identity.repository_root,
        "repository .omp/lsp.json",
    )
    _assert_project_parent(plan.project_lsp_path.parent, ".omp")
    _assert_path_within_root(
        plan.exclude_path, plan.identity.common_directory, "Git local exclude"
    )
    _assert_path_within_root(
        plan.worktree_path,
        plan.identity.repository_root,
        "repository .awf/worktree.toml",
    )
    _assert_project_parent(plan.worktree_path.parent, ".awf")
    _assert_source_current(
        plan.profile_metadata_path,
        plan.profile_metadata_source,
        "LSP profile metadata",
    )
    _assert_source_current(
        plan.profile_lsp_path, plan.profile_lsp_source, "LSP profile config"
    )
    _assert_source_current(
        plan.user_lsp_path, plan.user_lsp_source, "user OMP LSP config"
    )
    _assert_source_current(
        plan.project_lsp_path,
        plan.project_link_source,
        "repository .omp/lsp.json",
        allow_symlink=True,
    )
    _assert_source_current(
        plan.exclude_path, plan.exclude_source, "Git local exclude"
    )
    _assert_source_current(
        plan.worktree_path,
        plan.worktree_source,
        "repository .awf/worktree.toml",
    )
    _assert_tracked_state_current(
        plan.identity.repository_root, plan.project_lsp_tracked_state
    )
    _assert_tracked_state_current(
        plan.identity.repository_root, plan.worktree_tracked_state
    )
    _revalidate_local_configs(
        plan.identity.repository_root, plan.local_configs
    )
    if plan.project_link_exists:
        _validate_project_link(
            plan.project_lsp_path,
            plan.profile_directory,
            plan.profile_lsp_path,
        )


def _revalidate_materialize_sources(plan: MaterializePlan) -> None:
    _assert_identity_current(plan.identity)
    _assert_path_within_root(
        plan.profile_directory, plan.profile_root, "shared LSP profile directory"
    )
    _assert_profile_directory(plan.profile_directory)
    _assert_path_within_root(
        plan.project_lsp_path,
        plan.identity.repository_root,
        "repository .omp/lsp.json",
    )
    _assert_project_parent(plan.project_lsp_path.parent, ".omp")
    _assert_path_within_root(
        plan.exclude_path, plan.identity.common_directory, "Git local exclude"
    )
    _assert_source_current(
        plan.profile_lsp_path, plan.profile_lsp_source, "LSP profile config"
    )
    _assert_source_current(
        plan.profile_metadata_path,
        plan.profile_metadata_source,
        "LSP profile metadata",
    )
    _assert_source_current(
        plan.project_lsp_path,
        plan.project_link_source,
        "repository .omp/lsp.json",
        allow_symlink=True,
    )
    _assert_source_current(
        plan.exclude_path, plan.exclude_source, "Git local exclude"
    )
    _assert_tracked_state_current(
        plan.identity.repository_root, plan.project_lsp_tracked_state
    )
    if plan.project_link_exists:
        _validate_project_link(
            plan.project_lsp_path,
            plan.profile_directory,
            plan.profile_lsp_path,
        )
    _revalidate_local_configs(
        plan.identity.repository_root, plan.local_configs
    )


def _revalidate_local_configs(
    repository_root: Path, configs: Sequence[LocalConfigPlan]
) -> None:
    for config in configs:
        _assert_path_within_root(
            config.path, repository_root, config.relative_path
        )
        _assert_source_current(
            config.path, config.source, config.relative_path
        )
        _assert_tracked_state_current(
            repository_root, config.tracked_state
        )


def _apply_local_configs(
    configs: Sequence[LocalConfigPlan],
    actions: Sequence[dict[str, Any]],
    repository_root: Path,
) -> None:
    for config in configs:
        action = _local_action_for(actions, config.relative_path)
        if action["status"] != "planned":
            continue
        _assert_tracked_state_current(repository_root, config.tracked_state)
        _apply_write_action(
            action,
            config.path,
            config.source,
            config.content,
            repository_root,
            config.relative_path,
        )


def _apply_write_action(
    action: dict[str, Any],
    path: Path,
    expected_source: SourceFingerprint,
    content: str,
    root: Path,
    label: str,
) -> None:
    if action["status"] != "planned":
        return
    try:
        _assert_source_current(path, expected_source, label)
        _atomic_write(
            path, expected_source, content, root, label
        )
    except (LspSetupError, OSError):
        action["status"] = "failed"
        raise
    action["status"] = "applied"


def _read_exclude(path: Path) -> str:
    _assert_leaf_safe(path, "Git local exclude")
    if not path.exists():
        return ""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise LspSetupError("exclude_unreadable", f"could not read Git local exclude: {error}") from error
    if "\0" in content:
        raise LspSetupError("exclude_malformed", "Git local exclude contains an embedded NUL")
    return content


def _exclude_entries(local_files: Mapping[str, str]) -> tuple[str, ...]:
    return (
        *_BASE_EXCLUDE_ENTRIES,
        *(f"/{relative_path}" for relative_path in sorted(local_files)),
    )


def _merge_exclude(content: str, local_files: Mapping[str, str]) -> str:
    existing = content.splitlines()
    additions = [
        entry for entry in _exclude_entries(local_files) if entry not in existing
    ]
    if not additions:
        return content
    prefix = content if not content or content.endswith("\n") else content + "\n"
    return prefix + "\n".join(additions) + "\n"


def _exclude_is_complete(
    content: str, local_files: Mapping[str, str]
) -> bool:
    existing = set(content.splitlines())
    return all(entry in existing for entry in _exclude_entries(local_files))


def _exclude_status(
    content: str, local_files: Mapping[str, str] | None = None
) -> str:
    return (
        "present"
        if _exclude_is_complete(content, local_files or {})
        else "missing"
    )


def _worktree_state(path: Path) -> tuple[str, WorktreeConfig | None]:
    _assert_leaf_safe(path, "repository .awf/worktree.toml")
    if not path.exists():
        return "missing", None
    try:
        config = load_worktree_config(path.parent.parent)
    except ConfigError as error:
        raise LspSetupError("worktree_config_malformed", f"repository .awf/worktree.toml is malformed: {error}") from error
    if not config.prepare_command:
        return "missing_prepare", config
    if config.prepare_command == _PREPARE_COMMAND:
        return "compatible", config
    raise LspSetupError("prepare_command_incompatible", "repository .awf/worktree.toml already has a different prepare.command and was not changed")


def _prepare_worktree_content(path: Path) -> str:
    state, config = _worktree_state(path)
    if state == "compatible":
        return path.read_text(encoding="utf-8")
    if not path.exists():
        return '[prepare]\ncommand = ["awf", "lsp", "materialize"]\n'
    if config is None:
        raise LspSetupError("worktree_config_malformed", "repository .awf/worktree.toml could not be loaded")
    source = path.read_text(encoding="utf-8")
    header = re.search(r"(?m)^\s*\[prepare\]\s*(?:#.*)?(?:\r?\n|$)", source)
    if header is not None:
        return source[:header.end()] + 'command = ["awf", "lsp", "materialize"]\n' + source[header.end():]
    if "prepare" not in source:
        separator = "" if not source or source.endswith("\n") else "\n"
        return source + separator + '\n[prepare]\ncommand = ["awf", "lsp", "materialize"]\n'
    return _serialize_worktree_config(config, _PREPARE_COMMAND)


def _serialize_worktree_config(config: WorktreeConfig, prepare_command: Sequence[str]) -> str:
    lines: list[str] = []
    if (
        config.default_base is not None
        or config.production_branch is not None
        or config.feature_base is not None
    ):
        lines.append("[worktree]")
        if config.default_base is not None:
            lines.append(f"default_base = {_toml_value(config.default_base)}")
        if config.production_branch is not None:
            lines.append(f"production_branch = {_toml_value(config.production_branch)}")
        if config.feature_base is not None:
            lines.append(f"feature_base = {_toml_value(config.feature_base)}")
        lines.append("")
    lines.append("[prepare]")
    if config.prepare_inputs:
        lines.append(f"inputs = {_toml_value(list(config.prepare_inputs))}")
    lines.append(f"command = {_toml_value(list(prepare_command))}")
    if config.verify_production:
        lines += ["", "[verify.production]", f"commands = {_toml_value([list(item) for item in config.verify_production])}"]
    if config.source_review_policy != "approved_or_self_merged":
        lines += ["", "[promotion]", f"source_review_policy = {_toml_value(config.source_review_policy)}"]
    return "\n".join(lines) + "\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


def _configure_omp_isolation(action: dict[str, Any]) -> dict[str, str] | None:
    executable = shutil.which("omp")
    if executable is None:
        action["status"] = "failed"
        return _blocker("omp_unavailable", "omp executable is required to configure isolation; install OMP and rerun awf lsp setup --apply")
    changed_steps = 0
    for key, value in (
        ("task.isolation.apply", "false"),
        ("task.isolation.merge", "patch"),
        ("task.isolation.mode", "auto"),
    ):
        try:
            current = subprocess.run(
                [executable, "config", "get", key],
                capture_output=True,
                text=True,
                timeout=_OMP_TIMEOUT_SECONDS,
                check=False,
                shell=False,
            )
            if current.returncode == 0 and current.stdout.strip() == value:
                continue
            completed = subprocess.run(
                [executable, "config", "set", key, value],
                capture_output=True,
                text=True,
                timeout=_OMP_TIMEOUT_SECONDS,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            action["status"] = "partial" if changed_steps else "failed"
            action["stage"] = key
            return _blocker("omp_config_timeout", f"omp config update for {key} timed out")
        except OSError as error:
            action["status"] = "partial" if changed_steps else "failed"
            action["stage"] = key
            return _blocker("omp_config_failed", f"could not run omp config for {key}: {error}")
        if completed.returncode != 0:
            action["status"] = "partial" if changed_steps else "failed"
            action["stage"] = key
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown OMP error"
            return _blocker("omp_config_failed", f"omp config set {key} failed: {detail}")
        changed_steps += 1
    action["status"] = "applied" if changed_steps else "unchanged"
    return None


def _open_directory_tree(path: Path, label: str) -> int:
    absolute = path.expanduser().absolute()
    descriptor = -1
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(absolute.anchor, flags)
        for component in absolute.parts[1:]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            next_descriptor = os.open(
                component, flags, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise LspSetupError(
            "ancestor_symlink_unsafe",
            f"could not safely open the trusted root for {label}: {error}",
        ) from error
    return descriptor


def _open_parent_directory(
    path: Path, root: Path, label: str
) -> tuple[int, str]:
    _assert_path_within_root(path, root, label)
    trusted_root = root.expanduser().absolute()
    relative = path.expanduser().absolute().relative_to(trusted_root)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = _open_directory_tree(trusted_root, label)
    try:
        for component in relative.parts[:-1]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            next_descriptor = os.open(
                component, flags, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError as error:
        os.close(descriptor)
        raise LspSetupError(
            "ancestor_symlink_unsafe",
            f"could not safely open an ancestor for {label}: {error}",
        ) from error
    return descriptor, relative.name


def _assert_parent_current(
    path: Path, parent_descriptor: int, label: str
) -> None:
    try:
        expected = os.fstat(parent_descriptor)
        current = os.stat(path.parent, follow_symlinks=False)
    except OSError as error:
        raise LspSetupError(
            "concurrent_local_change",
            f"an ancestor for {label} changed while preparing LSP setup",
        ) from error
    if (
        not stat.S_ISDIR(current.st_mode)
        or expected.st_dev != current.st_dev
        or expected.st_ino != current.st_ino
    ):
        raise LspSetupError(
            "concurrent_local_change",
            f"an ancestor for {label} changed while preparing LSP setup",
        )


def _source_fingerprint_at(
    parent_descriptor: int,
    name: str,
    label: str,
    *,
    allow_symlink: bool = False,
) -> SourceFingerprint:
    try:
        metadata = os.stat(
            name, dir_fd=parent_descriptor, follow_symlinks=False
        )
    except FileNotFoundError:
        return SourceFingerprint(False, "missing")
    if stat.S_ISLNK(metadata.st_mode):
        if not allow_symlink:
            raise LspSetupError(
                "symlink_unsafe", f"{label} must not be a symlink"
            )
        return SourceFingerprint(
            True,
            "symlink",
            link_target=os.readlink(name, dir_fd=parent_descriptor),
        )
    if not stat.S_ISREG(metadata.st_mode):
        raise LspSetupError(
            "path_invalid", f"{label} is not a regular file"
        )
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW,
        dir_fd=parent_descriptor,
    )
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return SourceFingerprint(True, "file", digest.hexdigest())


def _assert_source_current_at(
    parent_descriptor: int,
    name: str,
    expected: SourceFingerprint,
    label: str,
    *,
    allow_symlink: bool = False,
) -> None:
    if _source_fingerprint_at(
        parent_descriptor,
        name,
        label,
        allow_symlink=allow_symlink,
    ) != expected:
        raise LspSetupError(
            "concurrent_local_change",
            f"{label} changed while preparing LSP setup",
        )


def _atomic_exchange_at(
    parent_descriptor: int, first: str, second: str, label: str
) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    arguments = (
        parent_descriptor,
        first.encode(),
        parent_descriptor,
        second.encode(),
        0x00000002,
    )
    try:
        system_name = os.uname().sysname
        if system_name == "Darwin":
            function = library.renameatx_np
        elif system_name == "Linux":
            function = library.renameat2
        else:
            raise AttributeError
    except AttributeError as error:
        raise LspSetupError(
            "atomic_exchange_unsupported",
            f"{label} cannot be updated safely on this platform",
        ) from error
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    if function(*arguments) != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            f"{first} <-> {second}",
        )


def _leaf_inode(
    parent_descriptor: int, name: str, label: str
) -> tuple[int, int]:
    try:
        metadata = os.stat(
            name, dir_fd=parent_descriptor, follow_symlinks=False
        )
    except OSError as error:
        raise LspSetupError(
            "concurrent_local_change",
            f"{label} changed while preparing LSP setup",
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise LspSetupError(
            "concurrent_local_change",
            f"{label} changed while preparing LSP setup",
        )
    return metadata.st_dev, metadata.st_ino


def _publish_existing_file(
    path: Path,
    parent_descriptor: int,
    name: str,
    expected_source: SourceFingerprint,
    content: str,
    label: str,
) -> None:
    temporary_name = f".{name}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent_descriptor,
    )
    exchanged = False
    rollback_safe = False
    old_inode: tuple[int, int] | None = None
    new_inode: tuple[int, int] | None = None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        _assert_parent_current(path, parent_descriptor, label)
        _assert_source_current_at(
            parent_descriptor, name, expected_source, label
        )
        old_inode = _leaf_inode(parent_descriptor, name, label)
        new_inode = _leaf_inode(
            parent_descriptor, temporary_name, label
        )
        _atomic_exchange_at(
            parent_descriptor, temporary_name, name, label
        )
        exchanged = True
        rollback_safe = (
            _leaf_inode(parent_descriptor, temporary_name, label)
            == old_inode
            and _leaf_inode(parent_descriptor, name, label) == new_inode
        )
        if not rollback_safe:
            raise LspSetupError(
                "concurrent_local_change",
                f"{label} changed; recovery backup kept as {temporary_name}",
            )
        desired = SourceFingerprint(
            True,
            "file",
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )
        if (
            _source_fingerprint_at(
                parent_descriptor, temporary_name, label
            )
            != expected_source
            or _source_fingerprint_at(parent_descriptor, name, label)
            != desired
        ):
            raise LspSetupError(
                "concurrent_local_change",
                f"{label} changed while preparing LSP setup",
            )
        _assert_parent_current(path, parent_descriptor, label)
        os.unlink(temporary_name, dir_fd=parent_descriptor)
        exchanged = False
        os.fsync(parent_descriptor)
    except BaseException as error:
        if exchanged:
            raise LspSetupError(
                "concurrent_local_change",
                f"{label} changed; recovery backup kept as {temporary_name}",
            ) from error
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        raise


def _publish_new_file(
    path: Path,
    parent_descriptor: int,
    name: str,
    content: str,
    label: str,
) -> None:
    temporary_name = f".{name}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent_descriptor,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        _assert_parent_current(path, parent_descriptor, label)
        _assert_source_current_at(
            parent_descriptor,
            name,
            SourceFingerprint(False, "missing"),
            label,
        )
        try:
            os.link(
                temporary_name,
                name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise LspSetupError(
                "concurrent_local_change",
                f"{label} changed while preparing LSP setup",
            ) from error
        _assert_parent_current(path, parent_descriptor, label)
    finally:
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass


def _atomic_write(
    path: Path,
    expected_source: SourceFingerprint,
    content: str,
    root: Path,
    label: str,
) -> None:
    parent_descriptor, name = _open_parent_directory(path, root, label)
    try:
        _assert_parent_current(path, parent_descriptor, label)
        if expected_source.exists:
            _publish_existing_file(
                path,
                parent_descriptor,
                name,
                expected_source,
                content,
                label,
            )
        else:
            _publish_new_file(
                path, parent_descriptor, name, content, label
            )
    finally:
        os.close(parent_descriptor)


def _atomic_symlink(
    target: Path,
    path: Path,
    expected_source: SourceFingerprint,
    profile_root: Path,
    project_root: Path,
) -> None:
    if expected_source.exists:
        raise LspSetupError(
            "project_lsp_conflict",
            "repository .omp/lsp.json already exists",
        )
    _assert_path_within_root(target, profile_root, "LSP profile config")
    _assert_leaf_safe(target, "LSP profile config")
    try:
        resolved_target = target.resolve(strict=True)
        resolved_profile_root = profile_root.resolve(strict=True)
        resolved_target.relative_to(resolved_profile_root)
    except (OSError, ValueError) as error:
        raise LspSetupError(
            "project_symlink_unsafe",
            "refusing a project symlink target outside its LSP profile",
        ) from error
    parent_descriptor, name = _open_parent_directory(
        path, project_root, "repository .omp/lsp.json"
    )
    label = "repository .omp/lsp.json"
    try:
        _assert_parent_current(path, parent_descriptor, label)
        _assert_source_current_at(
            parent_descriptor,
            name,
            expected_source,
            label,
            allow_symlink=True,
        )
        try:
            os.symlink(str(resolved_target), name, dir_fd=parent_descriptor)
        except FileExistsError as error:
            raise LspSetupError(
                "concurrent_local_change",
                f"{label} changed while preparing LSP setup",
            ) from error
        _assert_parent_current(path, parent_descriptor, label)
    finally:
        os.close(parent_descriptor)


def _effective_command(
    definition: ServerDefinition, server_config: Mapping[str, Any] | None
) -> str:
    if server_config is None:
        return definition.binary
    configured = server_config.get(definition.name)
    if not isinstance(configured, Mapping) or "command" not in configured:
        return definition.binary
    command = configured["command"]
    if not isinstance(command, str) or not command:
        raise LspSetupError(
            "config_malformed",
            f"LSP server {definition.name!r} command must be a non-empty string",
        )
    return command


def _resolve_server_command(
    command: str, repository_root: Path | None
) -> tuple[bool, str | None]:
    if repository_root is not None and Path(command).name == command:
        for directory in (
            repository_root / "node_modules" / ".bin",
            repository_root / ".venv" / "bin",
            repository_root / "venv" / "bin",
            repository_root / "bin",
        ):
            candidate = directory / command
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return True, str(candidate)
    resolved = shutil.which(command)
    return resolved is not None, resolved


def _server_entries(
    definitions: Iterable[ServerDefinition],
    repository_root: Path | None = None,
    server_config: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    entries: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    for definition in definitions:
        command = _effective_command(definition, server_config)
        available, resolved_command = _resolve_server_command(
            command, repository_root
        )
        entries.append(
            {
                "name": definition.name,
                "language": definition.language,
                "binary": definition.binary,
                "command": command,
                "available": available,
                "resolved_command": resolved_command,
            }
        )
        if not available:
            warnings.append({"code": "server_binary_missing", "server": definition.name, "binary": command, "message": f"{command} is not available for {definition.language} LSP support", "suggestion": f"Install {command} or add it to this repository's local tool path, then rerun awf lsp status"})
    return entries, warnings


def _entries_from_profile(
    languages: Sequence[str],
    profile_lsp: Mapping[str, Any],
    repository_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    definitions = [_SERVER_BY_NAME[name] for name in sorted(profile_lsp) if name in _SERVER_BY_NAME and _SERVER_BY_NAME[name].language in languages]
    return _server_entries(definitions, repository_root, profile_lsp)


def _local_config_warnings(
    configs: Sequence[LocalConfigPlan],
) -> list[dict[str, str]]:
    return [
        {
            "code": "local_config_preserved",
            "path": config.relative_path,
            "message": (
                f"{config.relative_path} already exists with different content "
                "and was preserved"
            ),
            "suggestion": (
                "Keep the existing repository-local configuration or remove it "
                "before rerunning awf lsp setup --apply"
            ),
        }
        for config in configs
        if config.existing_status == "preserved"
    ]


def _local_config_action(config: LocalConfigPlan) -> dict[str, Any]:
    status = (
        "planned"
        if config.write_allowed
        and _source_needs_write(config.source, config.content)
        else config.existing_status
    )
    return {
        "kind": "local_config",
        "path": config.relative_path,
        "status": status,
    }


def _local_config_status_action(
    config: LocalConfigPlan,
) -> dict[str, Any]:
    return {
        "kind": "local_config",
        "path": config.relative_path,
        "status": config.existing_status,
    }


def _local_action_for(
    actions: Sequence[dict[str, Any]], relative_path: str
) -> dict[str, Any]:
    for action in actions:
        if (
            action["kind"] == "local_config"
            and action.get("path") == relative_path
        ):
            return action
    raise RuntimeError(f"missing local LSP config action: {relative_path}")


def _setup_actions(plan: SetupPlan) -> list[dict[str, Any]]:
    return [
        _action(
            "profile",
            _source_needs_write(
                plan.profile_metadata_source, _json_text(plan.profile_metadata)
            )
            or _source_needs_write(
                plan.profile_lsp_source, _json_text(plan.profile_lsp)
            ),
        ),
        _action(
            "user_lsp_config",
            _source_needs_write(plan.user_lsp_source, _json_text(plan.user_lsp)),
        ),
        _action(
            "git_exclude",
            _source_needs_write(plan.exclude_source, plan.exclude_content),
        ),
        _action("project_symlink", not plan.project_link_exists),
        _action(
            "prepare_hook",
            _source_needs_write(plan.worktree_source, plan.worktree_content),
        ),
        *[_local_config_action(config) for config in plan.local_configs],
        _action("omp_isolation", True),
    ]


def _materialize_actions(plan: MaterializePlan) -> list[dict[str, Any]]:
    return [
        _action(
            "git_exclude",
            _source_needs_write(plan.exclude_source, plan.exclude_content),
        ),
        *[_local_config_action(config) for config in plan.local_configs],
        _action("project_symlink", not plan.project_link_exists),
    ]


def _action(kind: str, changed: bool) -> dict[str, Any]:
    return {"kind": kind, "status": "planned" if changed else "unchanged"}


def _action_for(
    actions: Sequence[dict[str, Any]], kind: str
) -> dict[str, Any]:
    for action in actions:
        if action["kind"] == kind:
            return action
    raise RuntimeError(f"missing LSP setup action: {kind}")


def _with_apply_failure(
    result: dict[str, Any], blocker: Mapping[str, str]
) -> dict[str, Any]:
    result["blockers"].append(dict(blocker))
    result["decision"] = (
        "partial"
        if any(
            action["status"] in {"applied", "partial"}
            for action in result["actions"]
        )
        else "blocked"
    )
    return result


def _setup_result(plan: SetupPlan) -> dict[str, Any]:
    servers, _ = _server_entries(
        plan.definitions, plan.identity.repository_root, plan.profile_lsp
    )
    return _result(
        "lsp.setup",
        "preview",
        plan.languages,
        servers,
        _setup_actions(plan),
        warnings=plan.warnings,
    )


def _result(command: str, decision: str, languages: Sequence[str], servers: Sequence[Mapping[str, Any]], actions: Sequence[Mapping[str, Any]], *, blockers: Sequence[Mapping[str, str]] = (), warnings: Sequence[Mapping[str, str]] = ()) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "decision": decision,
        "languages": list(languages),
        "servers": [dict(server) for server in servers],
        "actions": [dict(action) for action in actions],
        "blockers": [dict(blocker) for blocker in blockers],
        "warnings": [dict(warning) for warning in warnings],
    }


def _blocked_result(command: str, error: LspSetupError) -> dict[str, Any]:
    return _result(command, "blocked", [], [], [], blockers=[_blocker(error.code, error.message)])


def _blocker(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _error_blocker(error: BaseException, prefix: str) -> dict[str, str]:
    if isinstance(error, LspSetupError):
        return _blocker(error.code, error.message)
    return _blocker("local_write_failed", f"{prefix}: {error}")


def _stable_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _json_text(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
