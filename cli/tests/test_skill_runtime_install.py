from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO_ROOT / "claude" / "skills"
EXPECTED_SKILLS = sorted(path.parent.name for path in SKILLS_ROOT.glob("*/SKILL.md"))


def run_linker(source: Path, *roots: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sh",
            str(REPO_ROOT / "scripts" / "install-skill-links.sh"),
            str(source),
            *(str(root) for root in roots),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_linker_installs_every_skill_into_all_three_roots(tmp_path: Path) -> None:
    roots = [tmp_path / "claude", tmp_path / "agents", tmp_path / "omp"]
    for skill in EXPECTED_SKILLS:
        completed = run_linker(SKILLS_ROOT / skill, *roots)
        assert completed.returncode == 0, completed.stderr

    for root in roots:
        assert sorted(path.name for path in root.iterdir()) == EXPECTED_SKILLS
        for skill in EXPECTED_SKILLS:
            target = root / skill
            assert target.is_symlink()
            assert target.resolve() == (SKILLS_ROOT / skill).resolve()
            assert (target / "SKILL.md").is_file()


def test_linker_preserves_every_nested_supporting_file(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    for skill in EXPECTED_SKILLS:
        assert run_linker(SKILLS_ROOT / skill, root).returncode == 0
        source = SKILLS_ROOT / skill
        for source_file in sorted(path for path in source.rglob("*") if path.is_file()):
            relative = source_file.relative_to(source)
            assert (root / skill / relative).is_file()


def test_linker_fails_closed_for_missing_source_and_skill_file(tmp_path: Path) -> None:
    root = tmp_path / "root"
    missing = run_linker(tmp_path / "missing", root)
    assert missing.returncode == 1
    assert "does not exist" in missing.stderr

    invalid = tmp_path / "invalid"
    invalid.mkdir()
    no_skill = run_linker(invalid, root)
    assert no_skill.returncode == 1
    assert "missing SKILL.md" in no_skill.stderr


def test_linker_replaces_wrong_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    wrong = root / "analysis"
    wrong.symlink_to("wrong")
    corrected = run_linker(SKILLS_ROOT / "analysis", root)
    assert corrected.returncode == 0
    assert wrong.resolve() == (SKILLS_ROOT / "analysis").resolve()


@pytest.mark.parametrize("owned_kind", ["file", "directory"])
def test_linker_preserves_user_owned_file_or_directory_as_blocked(
    tmp_path: Path, owned_kind: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    owned = root / "multi-agent"
    if owned_kind == "file":
        owned.write_text("keep")
    else:
        owned.mkdir()
        (owned / "owned.txt").write_text("keep")

    preserved = run_linker(SKILLS_ROOT / "multi-agent", root)

    assert preserved.returncode == 3
    assert (owned.read_text() if owned_kind == "file" else (owned / "owned.txt").read_text()) == "keep"
    assert f"AWF_SKILL_INSTALL_RESULT\tBLOCKED\t{owned}\tuser_owned" in preserved.stderr


def test_linker_rerun_is_idempotent(tmp_path: Path) -> None:
    roots = [tmp_path / "claude", tmp_path / "agents", tmp_path / "omp"]
    first = run_linker(SKILLS_ROOT / "release-worktree-lifecycle", *roots)
    second = run_linker(SKILLS_ROOT / "release-worktree-lifecycle", *roots)
    assert first.returncode == second.returncode == 0
    assert second.stdout.count("unchanged:") == 3


def run_setup(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    awf = fake_bin / "awf"
    awf.write_text("#!/bin/sh\nexit 0\n")
    awf.chmod(0o755)
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        "if [ \"$1 $2 $3\" = \"tool dir --bin\" ]; then printf '%s\\n' \"$FAKE_BIN\"; fi\n"
        "exit 0\n"
    )
    uv.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "FAKE_BIN": str(fake_bin),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AGENTS_SKILLS_DIR": str(home / ".agents" / "skills"),
        "OMP_SKILLS_DIR": str(home / ".omp" / "agent" / "skills"),
        "OMP_AGENT_DIR": str(home / ".omp" / "agent" / "agents"),
    }
    return subprocess.run(
        ["bash", str(REPO_ROOT / "setup.sh")],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_setup_installs_exact_inventory_into_three_runtime_roots(tmp_path: Path) -> None:
    completed = run_setup(tmp_path)
    assert completed.returncode == 0, completed.stderr
    home = tmp_path / "home"
    roots = [
        home / ".claude" / "skills",
        home / ".agents" / "skills",
        home / ".omp" / "agent" / "skills",
    ]
    for root in roots:
        assert sorted(path.name for path in root.iterdir()) == EXPECTED_SKILLS
        assert all((root / skill / "SKILL.md").is_file() for skill in EXPECTED_SKILLS)
        assert (root / "release-worktree-lifecycle").resolve() == (
            REPO_ROOT
            / "cli"
            / "src"
            / "awf"
            / "resources"
            / "release-worktree-lifecycle"
        ).resolve()


def test_setup_ignores_unrelated_command_files(tmp_path: Path) -> None:
    unrelated = tmp_path / "home" / ".claude" / "commands" / "sc" / "analyze.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("unrelated command\n")

    completed = run_setup(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert "Commands는 deprecated되었습니다." not in completed.stdout


def test_setup_warns_for_legacy_awf_command_files(tmp_path: Path) -> None:
    legacy = tmp_path / "home" / ".claude" / "commands" / "analysis.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy AWF command\n")

    completed = run_setup(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert "Commands는 deprecated되었습니다." in completed.stdout


def test_built_wheel_resolves_packaged_release_skill(tmp_path: Path) -> None:
    wheel_dir = tmp_path / "wheel"
    completed = subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--out-dir",
            str(wheel_dir),
        ],
        cwd=REPO_ROOT / "cli",
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    [wheel_path] = wheel_dir.glob("*.whl")
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(wheel_path) as wheel:
        wheel.extractall(extracted)

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path;"
                "from awf.worktrees.registry import WorktreeRegistry;"
                "from awf.worktrees.service import WorktreeService;"
                "root=Path(__import__('sys').argv[1]);"
                "service=WorktreeService("
                "WorktreeRegistry(root/'registry.db'),None,"
                "cache_dir=root/'cache',state_dir=root/'state',"
                "lock_dir=root/'locks',home_dir=root/'home');"
                "assert (service.skill_source_dir/'SKILL.md').is_file();"
                "print(service.skill_source_dir)"
            ),
            str(tmp_path),
        ],
        env={
            **os.environ,
            "PYTHONPATH": str(extracted),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert probe.returncode == 0, probe.stderr
    assert Path(probe.stdout.strip()).is_relative_to(extracted)


def test_setup_reports_exact_blocked_runtime_and_continues_other_installs(
    tmp_path: Path,
) -> None:
    owned = tmp_path / "home" / ".agents" / "skills" / "multi-agent"
    owned.parent.mkdir(parents=True)
    owned.write_text("keep")

    completed = run_setup(tmp_path)

    assert completed.returncode == 3
    assert owned.read_text() == "keep"
    assert f"runtime=agent-skills skill=multi-agent path={owned}" in completed.stderr
    assert (tmp_path / "home" / ".claude" / "skills" / "multi-agent" / "SKILL.md").is_file()
    assert (tmp_path / "home" / ".omp" / "agent" / "skills" / "multi-agent" / "SKILL.md").is_file()
