from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPO_ROOT / "scripts" / "install-skill-links.sh"
SKILL_NAME = "release-worktree-lifecycle"
CANONICAL_SKILL = REPO_ROOT / "claude" / "skills" / SKILL_NAME / "SKILL.md"


def run_installer(home: Path, *args: str, **environment: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({"HOME": str(home), **environment})
    return subprocess.run(
        ["bash", str(INSTALLER), *args],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def installed_skill_paths(home: Path) -> tuple[Path, Path, Path]:
    return (
        home / ".claude" / "skills" / SKILL_NAME,
        home / ".omp" / "agent" / "skills" / SKILL_NAME,
        home / ".agents" / "skills" / SKILL_NAME,
    )


def test_installer_links_canonical_skill_for_all_default_runtimes(tmp_path: Path) -> None:
    completed = run_installer(tmp_path)

    assert completed.returncode == 0, completed.stderr
    for installed in installed_skill_paths(tmp_path):
        assert installed.is_symlink()
        assert not Path(os.readlink(installed)).is_absolute()
        assert installed.resolve() == CANONICAL_SKILL.resolve()


def test_installer_uses_config_environment_roots_with_explicit_precedence(tmp_path: Path) -> None:
    claude_root = tmp_path / "claude-config"
    omp_root = tmp_path / "omp-config"
    agents_root = tmp_path / "agents-home"
    codex_root = tmp_path / "codex-home"

    completed = run_installer(
        tmp_path,
        CLAUDE_CONFIG_DIR=str(claude_root),
        OMP_CONFIG_DIR=str(omp_root),
        PI_CODING_AGENT_DIR=str(tmp_path / "pi-agent"),
        AGENTS_HOME=str(agents_root),
        CODEX_HOME=str(codex_root),
    )

    assert completed.returncode == 0, completed.stderr
    for installed in (
        claude_root / "skills" / SKILL_NAME,
        omp_root / "skills" / SKILL_NAME,
        agents_root / "skills" / SKILL_NAME,
    ):
        assert installed.is_symlink()
        assert installed.resolve() == CANONICAL_SKILL.resolve()
    assert not (tmp_path / "pi-agent" / "skills" / SKILL_NAME).exists()
    assert not (codex_root / "skills" / SKILL_NAME).exists()


def test_installer_uses_pi_then_codex_roots_when_higher_precedence_is_unset(tmp_path: Path) -> None:
    pi_root = tmp_path / "pi-agent"
    codex_root = tmp_path / "codex-home"

    completed = run_installer(
        tmp_path,
        PI_CODING_AGENT_DIR=str(pi_root),
        CODEX_HOME=str(codex_root),
    )

    assert completed.returncode == 0, completed.stderr
    assert (pi_root / "skills" / SKILL_NAME).resolve() == CANONICAL_SKILL.resolve()
    assert (codex_root / "skills" / SKILL_NAME).resolve() == CANONICAL_SKILL.resolve()


def test_installer_rerun_is_a_noop_success(tmp_path: Path) -> None:
    first = run_installer(tmp_path)
    links_before = {path: os.readlink(path) for path in installed_skill_paths(tmp_path)}

    completed = run_installer(tmp_path)

    assert first.returncode == 0, first.stderr
    assert completed.returncode == 0, completed.stderr
    assert {path: os.readlink(path) for path in installed_skill_paths(tmp_path)} == links_before


@pytest.mark.parametrize("conflict_kind", ["file", "directory", "wrong-link"])
def test_installer_refuses_existing_conflicts_without_overwriting(
    tmp_path: Path, conflict_kind: str
) -> None:
    conflict = tmp_path / ".agents" / "skills" / SKILL_NAME
    conflict.parent.mkdir(parents=True)
    if conflict_kind == "file":
        conflict.write_text("keep", encoding="utf-8")
    elif conflict_kind == "directory":
        conflict.mkdir()
        (conflict / "owned.txt").write_text("keep", encoding="utf-8")
    else:
        conflict.symlink_to("elsewhere")

    completed = run_installer(tmp_path)

    assert completed.returncode != 0
    assert "conflict" in completed.stderr.lower()
    assert not (tmp_path / ".claude" / "skills" / SKILL_NAME).exists()
    assert not (tmp_path / ".omp" / "agent" / "skills" / SKILL_NAME).exists()
    if conflict_kind == "file":
        assert conflict.read_text(encoding="utf-8") == "keep"
    elif conflict_kind == "directory":
        assert (conflict / "owned.txt").read_text(encoding="utf-8") == "keep"
    else:
        assert os.readlink(conflict) == "elsewhere"


@pytest.mark.parametrize(
    ("link_target", "outside_skill"),
    [("missing-skill", None), ("../../outside/SKILL.md", "outside")],
)
def test_installer_rejects_dangling_and_escape_links_without_repair(
    tmp_path: Path, link_target: str, outside_skill: str | None
) -> None:
    conflict = tmp_path / ".claude" / "skills" / SKILL_NAME
    conflict.parent.mkdir(parents=True)
    if outside_skill is not None:
        escaped_skill = tmp_path / outside_skill / "SKILL.md"
        escaped_skill.parent.mkdir()
        escaped_skill.write_text("outside", encoding="utf-8")
    conflict.symlink_to(link_target)

    completed = run_installer(tmp_path)

    assert completed.returncode != 0
    assert "conflict" in completed.stderr.lower()
    assert os.readlink(conflict) == link_target


def test_installer_dry_run_json_reports_plan_and_conflicts_without_mutation(tmp_path: Path) -> None:
    conflict = tmp_path / ".agents" / "skills" / SKILL_NAME
    conflict.parent.mkdir(parents=True)
    conflict.write_text("keep", encoding="utf-8")

    completed = run_installer(tmp_path, "--dry-run", "--json")

    assert completed.returncode != 0
    report = json.loads(completed.stdout)
    assert report["dry_run"] is True
    assert report["canonical_skill"] == str(CANONICAL_SKILL.resolve())
    assert report["conflicts"] == [str(conflict)]
    assert set(report["planned_links"]) == {
        str(tmp_path / ".claude" / "skills" / SKILL_NAME),
        str(tmp_path / ".omp" / "agent" / "skills" / SKILL_NAME),
    }
    assert not (tmp_path / ".claude" / "skills" / SKILL_NAME).exists()
    assert not (tmp_path / ".omp" / "agent" / "skills" / SKILL_NAME).exists()
    assert conflict.read_text(encoding="utf-8") == "keep"
