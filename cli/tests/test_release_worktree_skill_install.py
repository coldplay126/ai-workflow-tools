from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_NAME = "release-worktree-lifecycle"


def run_installer(repo_root: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sh",
            str(repo_root / "scripts" / "install-skill-links.sh"),
            str(repo_root / "claude" / "skills" / SKILL_NAME),
            str(tmp_path / ".claude" / "skills"),
            str(tmp_path / ".agents" / "skills"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_installer_links_one_source_into_both_skill_roots(tmp_path: Path) -> None:
    completed = run_installer(REPO_ROOT, tmp_path)

    assert completed.returncode == 0, completed.stderr
    claude = tmp_path / ".claude" / "skills" / SKILL_NAME
    agents = tmp_path / ".agents" / "skills" / SKILL_NAME
    canonical = (REPO_ROOT / "claude" / "skills" / SKILL_NAME).resolve()
    assert claude.resolve() == agents.resolve() == canonical
    assert os.readlink(claude) == str(canonical)
    assert os.readlink(agents) == str(canonical)

    rerun = run_installer(REPO_ROOT, tmp_path)

    assert rerun.returncode == 0, rerun.stderr
    assert rerun.stdout.count("unchanged:") == 2


def test_installer_replaces_a_wrong_symlink(tmp_path: Path) -> None:
    target = tmp_path / ".agents" / "skills" / SKILL_NAME
    target.parent.mkdir(parents=True)
    target.symlink_to("wrong-source")

    completed = run_installer(REPO_ROOT, tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert target.resolve() == (REPO_ROOT / "claude" / "skills" / SKILL_NAME).resolve()


def test_installer_preserves_a_real_conflicting_directory(tmp_path: Path) -> None:
    conflict = tmp_path / ".agents" / "skills" / SKILL_NAME
    conflict.mkdir(parents=True)
    marker = conflict / "owned.txt"
    marker.write_text("keep", encoding="utf-8")

    completed = run_installer(REPO_ROOT, tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert marker.read_text(encoding="utf-8") == "keep"
    assert "preserved" in completed.stderr


def test_installer_preserves_a_real_conflicting_file(tmp_path: Path) -> None:
    conflict = tmp_path / ".agents" / "skills" / SKILL_NAME
    conflict.parent.mkdir(parents=True)
    conflict.write_text("keep", encoding="utf-8")

    completed = run_installer(REPO_ROOT, tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert conflict.read_text(encoding="utf-8") == "keep"
    assert "preserved" in completed.stderr
