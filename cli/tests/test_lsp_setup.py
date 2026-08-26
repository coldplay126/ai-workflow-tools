from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from awf.cli import main
from awf.commands.lsp import _render
from awf.core import lsp_setup


def _git(*arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _repository(tmp_path: Path, *, files: dict[str, str] | None = None) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git("init", str(repository))
    _git("config", "user.email", "lsp-tests@example.invalid", cwd=repository)
    _git("config", "user.name", "LSP Tests", cwd=repository)
    for relative_path, content in (files or {"pyproject.toml": "[project]\nname = 'fixture'\n"}).items():
        path = repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git("add", ".", cwd=repository)
    _git("commit", "-m", "fixture", cwd=repository)
    return repository


def _local_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    home.mkdir()
    xdg.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return home, xdg


def _fake_omp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "omp.log"
    executable = bin_dir / "omp"
    executable.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "state=\"$OMP_LOG.state\"\n"
        "printf '%s\\n' \"$*\" >> \"$OMP_LOG\"\n"
        "if [ \"${2:-}\" = get ]; then\n"
        "  value=$(grep \"^${3}=\" \"$state\" 2>/dev/null | tail -n 1 | cut -d= -f2- || true)\n"
        "  [ -n \"$value\" ] || exit 1\n"
        "  printf '%s\\n' \"$value\"\n"
        "elif [ \"${2:-}\" = set ]; then\n"
        "  printf '%s=%s\\n' \"$3\" \"$4\" >> \"$state\"\n"
        "fi\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("OMP_LOG", str(log_path))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log_path


def _blocker_codes(result: dict) -> set[str]:
    return {blocker["code"] for blocker in result["blockers"]}


def test_setup_preview_has_no_mutation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _, xdg = _local_environment(monkeypatch, tmp_path)
    exclude = repository / ".git" / "info" / "exclude"
    before = exclude.read_text(encoding="utf-8") if exclude.exists() else ""

    result = lsp_setup.setup_lsp(repository)

    assert result["decision"] == "preview"
    assert not result["blockers"]
    assert not (xdg / "awf").exists()
    assert not (repository / ".omp").exists()
    assert not (repository / ".awf").exists()
    assert (exclude.read_text(encoding="utf-8") if exclude.exists() else "") == before
    assert all(action["status"] in {"planned", "unchanged"} for action in result["actions"])


def test_apply_is_idempotent_and_preserves_user_server_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    home, xdg = _local_environment(monkeypatch, tmp_path)
    omp_log = _fake_omp(monkeypatch, tmp_path)
    user_lsp = home / ".omp" / "agent" / "lsp.json"
    user_lsp.parent.mkdir(parents=True)
    user_lsp.write_text(
        json.dumps(
            {
                "pyright": {
                    "command": "custom-pyright",
                    "rootMarkers": ["custom.marker", "pyproject.toml"],
                    "customSetting": True,
                },
                "unrelated": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )

    first = lsp_setup.setup_lsp(repository, apply=True)
    profile_directory = next((xdg / "awf" / "lsp").iterdir())
    first_user_lsp = user_lsp.read_text(encoding="utf-8")
    first_exclude = (repository / ".git" / "info" / "exclude").read_text(encoding="utf-8")

    assert first["decision"] == "applied"
    assert (profile_directory / "profile.json").is_file()
    assert (profile_directory / "lsp.json").is_file()
    assert (repository / ".omp" / "lsp.json").is_symlink()
    assert (repository / ".omp" / "lsp.json").resolve() == (profile_directory / "lsp.json").resolve()
    profile_lsp = json.loads(
        (profile_directory / "lsp.json").read_text(encoding="utf-8")
    )
    assert profile_lsp["pyright"]["fileTypes"] == [".py", ".pyi"]
    assert profile_lsp["pyright"]["rootMarkers"] == ["pyproject.toml"]
    assert "extensions" not in profile_lsp["pyright"]
    merged = json.loads(first_user_lsp)
    assert merged["pyright"]["command"] == "custom-pyright"
    assert merged["pyright"]["customSetting"] is True
    assert merged["pyright"]["rootMarkers"][:2] == ["custom.marker", "pyproject.toml"]
    assert merged["unrelated"] == {"enabled": True}
    assert first_exclude.count(".omp/lsp.json") == 1
    assert first_exclude.count(".awf/worktree.toml") == 1
    assert 'command = ["awf", "lsp", "materialize"]' in (repository / ".awf" / "worktree.toml").read_text(encoding="utf-8")

    second = lsp_setup.setup_lsp(repository, apply=True)

    assert second["decision"] == "applied"
    assert user_lsp.read_text(encoding="utf-8") == first_user_lsp
    assert (repository / ".git" / "info" / "exclude").read_text(encoding="utf-8") == first_exclude
    assert (repository / ".git" / "info" / "exclude").read_text(encoding="utf-8").count(".omp/lsp.json") == 1
    assert omp_log.read_text(encoding="utf-8").splitlines() == [
        "config get task.isolation.apply",
        "config set task.isolation.apply false",
        "config get task.isolation.merge",
        "config set task.isolation.merge patch",
        "config get task.isolation.mode",
        "config set task.isolation.mode auto",
        "config get task.isolation.apply",
        "config get task.isolation.merge",
        "config get task.isolation.mode",
    ]


def test_status_reports_exact_configured_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repository = _repository(tmp_path)
    _local_environment(monkeypatch, tmp_path)
    _fake_omp(monkeypatch, tmp_path)
    assert lsp_setup.setup_lsp(repository, apply=True)["decision"] == "applied"

    exit_code = main(["lsp", "status", "--repo-root", str(repository), "--json"])
    rendered = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert rendered["command"] == "lsp.status"
    assert rendered["decision"] == "configured"
    assert rendered["languages"] == ["python"]
    assert {action["kind"]: action["status"] for action in rendered["actions"]} == {
        "profile": "present",
        "project_symlink": "present",
        "git_exclude": "present",
        "prepare_hook": "compatible",
    }


def test_materialize_reuses_profile_for_linked_worktree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repository = _repository(
        tmp_path,
        files={
            "pyproject.toml": "[project]\nname = 'fixture'\n",
            "src/fixture/__init__.py": "",
        },
    )
    _, xdg = _local_environment(monkeypatch, tmp_path)
    _fake_omp(monkeypatch, tmp_path)
    assert lsp_setup.setup_lsp(repository, apply=True)["decision"] == "applied"
    linked = tmp_path / "linked"
    _git("worktree", "add", "-b", "lsp-linked-fixture", str(linked), cwd=repository)

    result = lsp_setup.materialize_lsp(linked)
    profile_directory = next((xdg / "awf" / "lsp").iterdir())

    assert result["decision"] == "materialized"
    assert (linked / ".omp" / "lsp.json").is_symlink()
    assert (linked / ".omp" / "lsp.json").resolve() == (profile_directory / "lsp.json").resolve()
    assert (repository / ".omp" / "lsp.json").resolve() == (linked / ".omp" / "lsp.json").resolve()
    assert (linked / "pyrightconfig.json").read_text(encoding="utf-8") == (
        repository / "pyrightconfig.json"
    ).read_text(encoding="utf-8")


def test_detection_supports_multiple_languages_and_missing_servers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repository = _repository(
        tmp_path,
        files={
            "pyproject.toml": "[project]\nname = 'fixture'\n",
            "web/package.json": "{}\n",
            "php/composer.json": "{}\n",
            "go/go.mod": "module fixture\n",
            "rust/Cargo.toml": "[package]\nname = 'fixture'\nversion = '0.1.0'\n",
            "jvm/App.java": "class App {}\n",
            "jvm/App.kt": "class App\n",
            "web/App.vue": "<template/>\n",
        },
    )
    _local_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(lsp_setup.shutil, "which", lambda _: None)

    result = lsp_setup.setup_lsp(repository)

    assert result["languages"] == ["python", "typescript-javascript", "php", "go", "rust", "java-kotlin", "vue"]
    assert {warning["code"] for warning in result["warnings"]} == {"server_binary_missing"}
    assert {server["name"] for server in result["servers"]} == {
        "pyright",
        "typescript-language-server",
        "intelephense",
        "gopls",
        "rust-analyzer",
        "jdtls",
        "kotlin-lsp",
        "vue-language-server",
    }


@pytest.mark.parametrize(
    ("prepare", "expect"),
    [
        ("tracked", "tracked_file_conflict"),
        ("unsafe_symlink", "project_symlink_unsafe"),
        ("malformed_user_config", "config_malformed"),
        ("malformed_worktree_config", "worktree_config_malformed"),
        ("incompatible_prepare", "prepare_command_incompatible"),
    ],
)
def test_setup_fails_closed_for_unsafe_existing_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prepare: str,
    expect: str,
) -> None:
    repository = _repository(tmp_path)
    home, _ = _local_environment(monkeypatch, tmp_path)
    if prepare == "tracked":
        target = repository / ".omp" / "lsp.json"
        target.parent.mkdir()
        target.write_text("{}\n", encoding="utf-8")
        _git("add", ".omp/lsp.json", cwd=repository)
    elif prepare == "unsafe_symlink":
        target = repository / ".omp" / "lsp.json"
        target.parent.mkdir()
        outside = tmp_path / "outside.json"
        outside.write_text("{}\n", encoding="utf-8")
        target.symlink_to(outside)
    elif prepare == "malformed_user_config":
        target = home / ".omp" / "agent" / "lsp.json"
        target.parent.mkdir(parents=True)
        target.write_text("[not-json", encoding="utf-8")
    else:
        target = repository / ".awf" / "worktree.toml"
        target.parent.mkdir()
        content = "[prepare\n" if prepare == "malformed_worktree_config" else '[prepare]\ncommand = ["other", "prepare"]\n'
        target.write_text(content, encoding="utf-8")

    result = lsp_setup.setup_lsp(repository)

    assert result["decision"] == "blocked"
    assert expect in _blocker_codes(result)
    assert not (repository / ".awf" / "worktree.toml").is_symlink()


def test_omp_configuration_uses_bounded_argv_and_blocks_before_file_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _, xdg = _local_environment(monkeypatch, tmp_path)
    captured: list[tuple[list[str], bool, int]] = []
    real_run = subprocess.run

    def fail_omp(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "git":
            return real_run(command, **kwargs)  # type: ignore[arg-type]
        captured.append((command, kwargs["shell"], kwargs["timeout"]))  # type: ignore[arg-type]
        return subprocess.CompletedProcess(command, 7, "", "fixture failure")

    monkeypatch.setattr(lsp_setup.shutil, "which", lambda name: "/fixture/omp" if name == "omp" else None)
    monkeypatch.setattr(lsp_setup.subprocess, "run", fail_omp)

    result = lsp_setup.setup_lsp(repository, apply=True)

    assert result["decision"] == "blocked"
    assert "omp_config_failed" in _blocker_codes(result)
    assert captured == [
        (["/fixture/omp", "config", "get", "task.isolation.apply"], False, 10),
        (["/fixture/omp", "config", "set", "task.isolation.apply", "false"], False, 10),
    ]
    assert not (xdg / "awf").exists()


def test_setup_preserves_wrapped_user_lsp_config_and_pi_config_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    home, _ = _local_environment(monkeypatch, tmp_path)
    omp_log = _fake_omp(monkeypatch, tmp_path)
    config_dir = home / "profiles" / "work"
    monkeypatch.setenv("PI_CONFIG_DIR", str(config_dir))
    user_lsp = config_dir / "lsp.json"
    user_lsp.parent.mkdir(parents=True)
    user_lsp.write_text(
        json.dumps(
            {
                "servers": {
                    "pyright": {
                        "command": "custom-pyright",
                        "rootMarkers": ["custom.marker"],
                    }
                },
                "idleTimeoutMs": 12345,
            }
        ),
        encoding="utf-8",
    )

    result = lsp_setup.setup_lsp(repository, apply=True)

    assert result["decision"] == "applied"
    merged = json.loads(user_lsp.read_text(encoding="utf-8"))
    assert set(merged) == {"servers", "idleTimeoutMs"}
    assert merged["idleTimeoutMs"] == 12345
    assert merged["servers"]["pyright"]["command"] == "custom-pyright"
    assert merged["servers"]["pyright"]["rootMarkers"] == [
        "custom.marker",
        "pyproject.toml",
        "pyrightconfig.json",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "Pipfile",
    ]
    assert omp_log.is_file()


def test_nested_language_markers_are_repo_relative_and_not_generic_git(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(
        tmp_path,
        files={
            "services/web/package.json": "{}\n",
            "services/api/composer.json": "{}\n",
        },
    )
    _home, xdg = _local_environment(monkeypatch, tmp_path)
    _fake_omp(monkeypatch, tmp_path)

    result = lsp_setup.setup_lsp(repository, apply=True)

    assert result["decision"] == "applied"
    profile = next((xdg / "awf" / "lsp").iterdir())
    config = json.loads((profile / "lsp.json").read_text(encoding="utf-8"))
    assert config["typescript-language-server"]["rootMarkers"] == [
        "services/web/package.json"
    ]
    assert config["intelephense"]["rootMarkers"] == [
        "services/api/composer.json"
    ]
    assert all(
        ".git" not in server["rootMarkers"] for server in config.values()
    )


def test_global_markers_remain_canonical_when_project_markers_are_nested(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(
        tmp_path,
        files={"services/web/package.json": "{}\n"},
    )
    home, xdg = _local_environment(monkeypatch, tmp_path)
    _fake_omp(monkeypatch, tmp_path)

    result = lsp_setup.setup_lsp(repository, apply=True)

    assert result["decision"] == "applied"
    profile = next((xdg / "awf" / "lsp").iterdir())
    project_config = json.loads((profile / "lsp.json").read_text(encoding="utf-8"))
    user_config = json.loads(
        (home / ".omp" / "agent" / "lsp.json").read_text(encoding="utf-8")
    )
    assert project_config["typescript-language-server"]["rootMarkers"] == [
        "services/web/package.json"
    ]
    assert user_config["typescript-language-server"]["rootMarkers"] == [
        "package.json",
        "tsconfig.json",
        "jsconfig.json",
    ]


def test_apply_fails_closed_when_user_config_changes_during_omp_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    home, xdg = _local_environment(monkeypatch, tmp_path)
    user_lsp = home / ".omp" / "agent" / "lsp.json"

    def configure_then_change_user_config(action: dict[str, object]) -> None:
        user_lsp.parent.mkdir(parents=True)
        user_lsp.write_text('{"external": {"enabled": true}}\n', encoding="utf-8")
        action["status"] = "applied"
        return None

    monkeypatch.setattr(
        lsp_setup, "_configure_omp_isolation", configure_then_change_user_config
    )

    result = lsp_setup.setup_lsp(repository, apply=True)

    assert result["decision"] == "partial"
    assert "concurrent_local_change" in _blocker_codes(result)
    assert json.loads(user_lsp.read_text(encoding="utf-8")) == {
        "external": {"enabled": True}
    }
    assert not (xdg / "awf").exists()
    assert {
        action["kind"]: action["status"] for action in result["actions"]
    }["omp_isolation"] == "applied"


def test_apply_fails_closed_when_git_index_changes_during_omp_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _, xdg = _local_environment(monkeypatch, tmp_path)
    worktree = repository / ".awf" / "worktree.toml"
    worktree.parent.mkdir()
    worktree.write_text(
        '[prepare]\ncommand = ["awf", "lsp", "materialize"]\n',
        encoding="utf-8",
    )

    def configure_then_stage_worktree(action: dict[str, object]) -> None:
        _git("add", ".awf/worktree.toml", cwd=repository)
        action["status"] = "applied"
        return None

    monkeypatch.setattr(
        lsp_setup, "_configure_omp_isolation", configure_then_stage_worktree
    )

    result = lsp_setup.setup_lsp(repository, apply=True)

    assert result["decision"] == "partial"
    assert "concurrent_local_change" in _blocker_codes(result)
    assert not (xdg / "awf").exists()
    assert worktree.read_text(encoding="utf-8") == (
        '[prepare]\ncommand = ["awf", "lsp", "materialize"]\n'
    )


@pytest.mark.parametrize("root_name", ["home", "xdg", "git_common"])
def test_setup_rejects_symlinked_ancestor_below_trusted_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    root_name: str,
) -> None:
    repository = _repository(tmp_path)
    home, xdg = _local_environment(monkeypatch, tmp_path)
    outside = tmp_path / f"{root_name}-outside"
    outside.mkdir()
    if root_name == "home":
        (home / ".omp").symlink_to(outside, target_is_directory=True)
    elif root_name == "xdg":
        (xdg / "awf").symlink_to(outside, target_is_directory=True)
    else:
        info = repository / ".git" / "info"
        info.rename(outside / "info")
        info.symlink_to(outside / "info", target_is_directory=True)

    result = lsp_setup.setup_lsp(repository)

    assert result["decision"] == "blocked"
    assert "ancestor_symlink_unsafe" in _blocker_codes(result)


def test_omp_failure_reports_the_partial_safe_configuration_stage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _, xdg = _local_environment(monkeypatch, tmp_path)
    captured: list[list[str]] = []
    real_run = subprocess.run

    def fail_merge(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "git":
            return real_run(command, **kwargs)  # type: ignore[arg-type]
        captured.append(command)
        return subprocess.CompletedProcess(
            command,
            9 if command[3] == "task.isolation.merge" else 0,
            "",
            "fixture failure",
        )

    monkeypatch.setattr(
        lsp_setup.shutil,
        "which",
        lambda name: "/fixture/omp" if name == "omp" else None,
    )
    monkeypatch.setattr(lsp_setup.subprocess, "run", fail_merge)

    result = lsp_setup.setup_lsp(repository, apply=True)

    actions = {action["kind"]: action for action in result["actions"]}
    assert result["decision"] == "partial"
    assert "omp_config_failed" in _blocker_codes(result)
    assert captured == [
        ["/fixture/omp", "config", "get", "task.isolation.apply"],
        ["/fixture/omp", "config", "set", "task.isolation.apply", "false"],
        ["/fixture/omp", "config", "get", "task.isolation.merge"],
        ["/fixture/omp", "config", "set", "task.isolation.merge", "patch"],
    ]
    assert actions["omp_isolation"]["status"] == "partial"
    assert actions["omp_isolation"]["stage"] == "task.isolation.merge"
    assert not (xdg / "awf").exists()


def test_file_write_failure_preserves_applied_local_actions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    home, xdg = _local_environment(monkeypatch, tmp_path)
    _fake_omp(monkeypatch, tmp_path)

    def reject_project_symlink(*_: object) -> None:
        raise OSError("fixture symlink failure")

    monkeypatch.setattr(lsp_setup, "_atomic_symlink", reject_project_symlink)

    result = lsp_setup.setup_lsp(repository, apply=True)

    statuses = {action["kind"]: action["status"] for action in result["actions"]}
    assert result["decision"] == "partial"
    assert "local_write_failed" in _blocker_codes(result)
    assert statuses == {
        "profile": "applied",
        "user_lsp_config": "applied",
        "git_exclude": "applied",
        "project_symlink": "failed",
        "prepare_hook": "planned",
        "omp_isolation": "applied",
    }
    assert (xdg / "awf").exists()
    assert (home / ".omp" / "agent" / "lsp.json").is_file()
    assert not (repository / ".awf" / "worktree.toml").exists()


def test_reapply_skips_unchanged_file_actions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _local_environment(monkeypatch, tmp_path)
    _fake_omp(monkeypatch, tmp_path)
    assert lsp_setup.setup_lsp(repository, apply=True)["decision"] == "applied"
    writes: list[Path] = []
    real_atomic_write = lsp_setup._atomic_write

    def capture_write(
        path: Path, content: str, root: Path, label: str
    ) -> None:
        writes.append(path)
        real_atomic_write(path, content, root, label)

    monkeypatch.setattr(lsp_setup, "_atomic_write", capture_write)

    result = lsp_setup.setup_lsp(repository, apply=True)

    assert result["decision"] == "applied"
    assert writes == []
    assert all(
        action["status"] == "unchanged"
        for action in result["actions"]
        if action["kind"] != "omp_isolation"
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "node_modules/.bin/pyright-langserver",
        ".venv/bin/pyright-langserver",
        "venv/bin/pyright-langserver",
        "bin/pyright-langserver",
    ],
)
def test_server_availability_checks_repository_local_tool_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    relative_path: str,
) -> None:
    repository = _repository(tmp_path)
    _local_environment(monkeypatch, tmp_path)
    executable = repository / relative_path
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(lsp_setup.shutil, "which", lambda _: None)

    result = lsp_setup.setup_lsp(repository)

    pyright = next(
        server for server in result["servers"] if server["name"] == "pyright"
    )
    assert pyright["command"] == "pyright-langserver"
    assert pyright["available"] is True
    assert pyright["resolved_command"] == str(executable)


def test_server_availability_prefers_node_modules_bin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _local_environment(monkeypatch, tmp_path)
    node_binary = repository / "node_modules" / ".bin" / "pyright-langserver"
    venv_binary = repository / ".venv" / "bin" / "pyright-langserver"
    for executable in (node_binary, venv_binary):
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    monkeypatch.setattr(lsp_setup.shutil, "which", lambda _: None)

    result = lsp_setup.setup_lsp(repository)

    pyright = next(
        server for server in result["servers"] if server["name"] == "pyright"
    )
    assert pyright["resolved_command"] == str(node_binary)


def test_server_availability_prefers_effective_merged_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _, xdg = _local_environment(monkeypatch, tmp_path)
    identity = lsp_setup._git_identity(repository)
    profile = xdg / "awf" / "lsp" / identity.profile_id
    profile.mkdir(parents=True)
    (profile / "profile.json").write_text(
        json.dumps(
            {
                "schema_version": lsp_setup.SCHEMA_VERSION,
                "repo_identity": identity.profile_id,
                "languages": ["python"],
                "servers": ["pyright"],
            }
        ),
        encoding="utf-8",
    )
    (profile / "lsp.json").write_text(
        json.dumps(
            {
                "pyright": {
                    "command": "project-pyright",
                    "rootMarkers": ["pyproject.toml"],
                }
            }
        ),
        encoding="utf-8",
    )
    executable = repository / "bin" / "project-pyright"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(lsp_setup.shutil, "which", lambda _: None)

    result = lsp_setup.setup_lsp(repository)

    pyright = next(
        server for server in result["servers"] if server["name"] == "pyright"
    )
    assert pyright["command"] == "project-pyright"
    assert pyright["available"] is True
    assert pyright["resolved_command"] == str(executable)


def test_partial_decision_returns_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = {
        "schema_version": 1,
        "command": "lsp.materialize",
        "decision": "partial",
        "languages": [],
        "servers": [],
        "actions": [{"kind": "git_exclude", "status": "applied"}],
        "blockers": [{"code": "fixture", "message": "fixture failure"}],
        "warnings": [],
    }

    assert _render(result, as_json=True) == 2
    assert json.loads(capsys.readouterr().out)["decision"] == "partial"


def test_server_definitions_match_supported_omp_contract() -> None:
    definitions = {definition.name: definition for definition in lsp_setup._SERVERS}

    assert definitions["intelephense"].extensions == (".php", ".phtml")
    assert definitions["gopls"].args == ("serve",)
    assert definitions["gopls"].extensions == (".go", ".mod", ".sum")
    assert definitions["kotlin-lsp"].args == ("--stdio",)
    assert "pyrightconfig.json" in definitions["pyright"].root_markers
    assert "rust-analyzer.toml" in definitions["rust-analyzer"].root_markers


def test_apply_generates_local_pyright_config_for_src_layouts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(
        tmp_path,
        files={
            "cli/pyproject.toml": "[project]\nname = 'cli'\n",
            "cli/src/awf/__init__.py": "",
            "worker/pyproject.toml": "[project]\nname = 'worker'\n",
            "worker/src/worker/__init__.py": "",
        },
    )
    _local_environment(monkeypatch, tmp_path)
    _fake_omp(monkeypatch, tmp_path)

    preview = lsp_setup.setup_lsp(repository)
    local_action = next(
        action for action in preview["actions"]
        if action["kind"] == "local_config"
    )
    assert local_action == {
        "kind": "local_config",
        "path": "pyrightconfig.json",
        "status": "planned",
    }

    result = lsp_setup.setup_lsp(repository, apply=True)

    assert result["decision"] == "applied"
    assert json.loads(
        (repository / "pyrightconfig.json").read_text(encoding="utf-8")
    ) == {
        "executionEnvironments": [
            {"root": "cli", "extraPaths": ["cli/src"]},
            {"root": "worker", "extraPaths": ["worker/src"]},
        ]
    }
    exclude = Path(
        _git(
            "rev-parse", "--git-path", "info/exclude", cwd=repository
        ).stdout.strip()
    )
    if not exclude.is_absolute():
        exclude = repository / exclude
    assert "/pyrightconfig.json" in exclude.read_text(encoding="utf-8")
    ignored = subprocess.run(
        ["git", "-C", str(repository), "check-ignore", "pyrightconfig.json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored.returncode == 0


def test_setup_preserves_existing_untracked_pyright_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(
        tmp_path,
        files={
            "pyproject.toml": "[project]\nname = 'fixture'\n",
            "src/fixture/__init__.py": "",
        },
    )
    existing = '{"include": ["src"]}\n'
    (repository / "pyrightconfig.json").write_text(existing, encoding="utf-8")
    _local_environment(monkeypatch, tmp_path)
    _fake_omp(monkeypatch, tmp_path)

    result = lsp_setup.setup_lsp(repository, apply=True)

    assert result["decision"] == "applied"
    assert (repository / "pyrightconfig.json").read_text(
        encoding="utf-8"
    ) == existing
    assert any(
        warning["code"] == "local_config_preserved"
        for warning in result["warnings"]
    )
    action = next(
        action for action in result["actions"]
        if action["kind"] == "local_config"
    )
    assert action["status"] == "preserved"


def test_atomic_write_blocks_ancestor_swap_after_directory_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "trusted"
    parent = root / "config"
    moved_parent = root / "config-original"
    outside = tmp_path / "outside"
    parent.mkdir(parents=True)
    outside.mkdir()
    target = parent / "lsp.json"
    original = lsp_setup._open_parent_directory

    def open_then_swap(
        path: Path, trusted_root: Path, label: str
    ) -> tuple[int, str]:
        descriptor, name = original(path, trusted_root, label)
        parent.rename(moved_parent)
        parent.symlink_to(outside, target_is_directory=True)
        return descriptor, name

    monkeypatch.setattr(
        lsp_setup, "_open_parent_directory", open_then_swap
    )

    with pytest.raises(lsp_setup.LspSetupError) as raised:
        lsp_setup._atomic_write(
            target,
            lsp_setup.SourceFingerprint(False, "missing"),
            "{}\n",
            root,
            "fixture LSP config",
        )

    assert raised.value.code == "concurrent_local_change"
    assert not (outside / "lsp.json").exists()
    assert not (moved_parent / "lsp.json").exists()


def test_partial_profile_write_recovers_on_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _local_environment(monkeypatch, tmp_path)
    _fake_omp(monkeypatch, tmp_path)
    original = lsp_setup._atomic_write
    failed = False

    def fail_lsp_once(
        path: Path,
        expected_source: lsp_setup.SourceFingerprint,
        content: str,
        root: Path,
        label: str,
    ) -> None:
        nonlocal failed
        if label == "LSP profile config" and not failed:
            failed = True
            raise OSError("fixture LSP failure")
        original(path, expected_source, content, root, label)

    monkeypatch.setattr(lsp_setup, "_atomic_write", fail_lsp_once)
    first = lsp_setup.setup_lsp(repository, apply=True)

    assert first["decision"] == "partial"
    assert next(
        action for action in first["actions"]
        if action["kind"] == "profile"
    )["status"] == "partial"
    profile_root = tmp_path / "xdg" / "awf" / "lsp"
    assert any(profile_root.rglob("profile.json"))
    assert not any(profile_root.rglob("lsp.json"))

    monkeypatch.setattr(lsp_setup, "_atomic_write", original)
    second = lsp_setup.setup_lsp(repository, apply=True)

    assert second["decision"] == "applied"


def test_materialize_rejects_concurrent_profile_metadata_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(
        tmp_path,
        files={
            "pyproject.toml": "[project]\nname = 'fixture'\n",
            "src/fixture/__init__.py": "",
        },
    )
    _local_environment(monkeypatch, tmp_path)
    _fake_omp(monkeypatch, tmp_path)
    assert lsp_setup.setup_lsp(repository, apply=True)["decision"] == "applied"
    linked = tmp_path / "metadata-linked"
    _git(
        "worktree",
        "add",
        "-b",
        "metadata-linked-fixture",
        str(linked),
        cwd=repository,
    )
    plan = lsp_setup._build_materialize_plan(linked)
    plan.profile_metadata_path.write_text(
        plan.profile_metadata_path.read_text(encoding="utf-8") + " ",
        encoding="utf-8",
    )

    with pytest.raises(lsp_setup.LspSetupError) as raised:
        lsp_setup._apply_materialize_plan(
            plan, lsp_setup._materialize_actions(plan)
        )

    assert raised.value.code == "concurrent_local_change"
    assert not (linked / ".omp" / "lsp.json").exists()
    assert not (linked / "pyrightconfig.json").exists()


def test_apply_rejects_concurrent_git_identity_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    home, xdg = _local_environment(monkeypatch, tmp_path)
    _fake_omp(monkeypatch, tmp_path)
    original = lsp_setup._git_identity
    calls = 0

    def changing_identity(
        repo_root: str | Path | None,
    ) -> lsp_setup.GitIdentity:
        nonlocal calls
        calls += 1
        current = original(repo_root)
        if calls < 3:
            return current
        return lsp_setup.GitIdentity(
            current.repository_root,
            tmp_path / "different-common-directory",
            "different-profile",
        )

    monkeypatch.setattr(lsp_setup, "_git_identity", changing_identity)

    result = lsp_setup.setup_lsp(repository, apply=True)

    assert result["decision"] == "partial"
    assert "concurrent_git_identity_change" in _blocker_codes(result)
    assert not (xdg / "awf").exists()
    assert not (home / ".omp" / "agent" / "lsp.json").exists()


def test_materialize_rejects_malformed_local_config_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(
        tmp_path,
        files={
            "pyproject.toml": "[project]\nname = 'fixture'\n",
            "src/fixture/__init__.py": "",
        },
    )
    _, xdg = _local_environment(monkeypatch, tmp_path)
    _fake_omp(monkeypatch, tmp_path)
    assert lsp_setup.setup_lsp(repository, apply=True)["decision"] == "applied"
    metadata_path = next((xdg / "awf" / "lsp").iterdir()) / "profile.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["local_files"]["pyrightconfig.json"] = "{"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    result = lsp_setup.materialize_lsp(repository)

    assert result["decision"] == "blocked"
    assert "profile_malformed" in _blocker_codes(result)


def test_setup_rejects_malformed_existing_local_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(
        tmp_path,
        files={"src/fixture/__init__.py": ""},
    )
    (repository / "pyrightconfig.json").write_text("{", encoding="utf-8")
    _local_environment(monkeypatch, tmp_path)

    result = lsp_setup.setup_lsp(repository)

    assert result["decision"] == "blocked"
    assert "local_config_malformed" in _blocker_codes(result)
    assert (repository / "pyrightconfig.json").read_text(
        encoding="utf-8"
    ) == "{"


def test_markerless_python_src_layout_generates_local_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(
        tmp_path,
        files={"src/fixture/__init__.py": ""},
    )
    _local_environment(monkeypatch, tmp_path)

    preview = lsp_setup.setup_lsp(repository)

    assert next(
        action for action in preview["actions"]
        if action["kind"] == "local_config"
    )["status"] == "planned"


def test_status_reports_missing_local_config_without_planning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(
        tmp_path,
        files={
            "pyproject.toml": "[project]\nname = 'fixture'\n",
            "src/fixture/__init__.py": "",
        },
    )
    _local_environment(monkeypatch, tmp_path)
    _fake_omp(monkeypatch, tmp_path)
    assert lsp_setup.setup_lsp(repository, apply=True)["decision"] == "applied"
    (repository / "pyrightconfig.json").unlink()

    status = lsp_setup.status_lsp(repository)

    assert status["decision"] == "incomplete"
    local_action = next(
        action for action in status["actions"]
        if action["kind"] == "local_config"
    )
    assert local_action["status"] == "missing"


def test_apply_creates_missing_xdg_config_root_safely(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "missing" / "xdg"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    _fake_omp(monkeypatch, tmp_path)

    result = lsp_setup.setup_lsp(repository, apply=True)

    assert result["decision"] == "applied"
    assert any((xdg / "awf" / "lsp").rglob("profile.json"))


def test_new_file_publish_never_overwrites_concurrent_leaf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _local_environment(monkeypatch, tmp_path)
    _fake_omp(monkeypatch, tmp_path)
    original = lsp_setup.os.link
    injected = False

    def create_destination_before_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        nonlocal injected
        if destination == "profile.json" and not injected:
            injected = True
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dst_dir_fd,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write('{"concurrent": true}\n')
        original(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(lsp_setup.os, "link", create_destination_before_link)

    result = lsp_setup.setup_lsp(repository, apply=True)

    assert result["decision"] == "partial"
    assert "concurrent_local_change" in _blocker_codes(result)
    profile_path = next(
        (tmp_path / "xdg" / "awf" / "lsp").rglob("profile.json")
    )
    assert json.loads(profile_path.read_text(encoding="utf-8")) == {
        "concurrent": True
    }


def test_existing_file_exchange_keeps_backup_after_failed_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "trusted"
    root.mkdir()
    target = root / "lsp.json"
    original_content = '{"original": true}\n'
    target.write_text(original_content, encoding="utf-8")
    expected = lsp_setup._source_fingerprint(target, "fixture")
    original_fingerprint_at = lsp_setup._source_fingerprint_at
    calls = 0

    def fail_new_content_validation(
        parent_descriptor: int,
        name: str,
        label: str,
        *,
        allow_symlink: bool = False,
    ) -> lsp_setup.SourceFingerprint:
        nonlocal calls
        calls += 1
        if calls == 3:
            return lsp_setup.SourceFingerprint(
                True, "file", "concurrent-content"
            )
        return original_fingerprint_at(
            parent_descriptor,
            name,
            label,
            allow_symlink=allow_symlink,
        )

    monkeypatch.setattr(
        lsp_setup,
        "_source_fingerprint_at",
        fail_new_content_validation,
    )

    with pytest.raises(lsp_setup.LspSetupError) as raised:
        lsp_setup._atomic_write(
            target,
            expected,
            '{"updated": true}\n',
            root,
            "fixture",
        )

    assert raised.value.code == "concurrent_local_change"
    assert target.read_text(encoding="utf-8") == '{"updated": true}\n'
    backups = list(root.glob(".lsp.json.*.tmp"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original_content


def test_existing_file_exchange_preserves_concurrent_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "trusted"
    root.mkdir()
    target = root / "lsp.json"
    original_content = '{"original": true}\n'
    concurrent_content = '{"concurrent": true}\n'
    target.write_text(original_content, encoding="utf-8")
    expected = lsp_setup._source_fingerprint(target, "fixture")
    original_fingerprint_at = lsp_setup._source_fingerprint_at
    calls = 0

    def replace_target_during_validation(
        parent_descriptor: int,
        name: str,
        label: str,
        *,
        allow_symlink: bool = False,
    ) -> lsp_setup.SourceFingerprint:
        nonlocal calls
        calls += 1
        if calls == 3:
            replacement = ".concurrent-replacement"
            descriptor = os.open(
                replacement,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_descriptor,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(concurrent_content)
            os.replace(
                replacement,
                name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
        return original_fingerprint_at(
            parent_descriptor,
            name,
            label,
            allow_symlink=allow_symlink,
        )

    monkeypatch.setattr(
        lsp_setup,
        "_source_fingerprint_at",
        replace_target_during_validation,
    )

    with pytest.raises(lsp_setup.LspSetupError) as raised:
        lsp_setup._atomic_write(
            target,
            expected,
            '{"updated": true}\n',
            root,
            "fixture",
        )

    assert raised.value.code == "concurrent_local_change"
    assert target.read_text(encoding="utf-8") == concurrent_content
    backups = list(root.glob(".lsp.json.*.tmp"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original_content
