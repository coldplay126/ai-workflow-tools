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
CORE_SKILLS = sorted(
    [
        "analysis",
        "lsp-worktree-setup",
        "multi-agent",
        "release-worktree-lifecycle",
        "wf-discovery",
    ]
)
WF_SKILLS = sorted(set(EXPECTED_SKILLS) - set(CORE_SKILLS))


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


def run_unlinker(source: Path, *roots: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sh",
            str(REPO_ROOT / "scripts" / "uninstall-skill-links.sh"),
            str(source),
            *(str(root) for root in roots),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_unlinker_removes_only_exact_owned_links(tmp_path: Path) -> None:
    source = SKILLS_ROOT / "wf"
    owned, foreign, file_root, dir_root, absent = (
        tmp_path / name for name in ("owned", "foreign", "file", "directory", "absent")
    )
    for root in (owned, foreign, file_root, dir_root):
        root.mkdir()
    (owned / "wf").symlink_to(source.resolve())
    (foreign / "wf").symlink_to("different-target")
    (file_root / "wf").write_text("user data")
    (dir_root / "wf").mkdir()
    (dir_root / "wf" / "keep").write_text("user data")

    completed = run_unlinker(source, owned, foreign, file_root, dir_root, absent)

    assert completed.returncode == 0, completed.stderr
    assert not (owned / "wf").exists()
    assert f"removed: {owned / 'wf'}" in completed.stdout
    assert (foreign / "wf").is_symlink()
    assert os.readlink(foreign / "wf") == "different-target"
    assert f"kept: {foreign / 'wf'} (foreign link -> different-target)" in completed.stdout
    assert (file_root / "wf").read_text() == "user data"
    assert (dir_root / "wf" / "keep").read_text() == "user data"
    assert completed.stdout.count("(user_owned)") == 2
    assert f"absent: {absent / 'wf'}" in completed.stdout
    assert not absent.exists()


def test_unlinker_requires_source_and_root(tmp_path: Path) -> None:
    missing_args = run_unlinker(SKILLS_ROOT / "wf")
    assert missing_args.returncode == 2
    assert "usage:" in missing_args.stderr
    missing_source = REPO_ROOT / "claude" / "skills" / "missing-skill"
    root = tmp_path / "root"
    root.mkdir()
    dangling = root / "missing-skill"
    dangling.symlink_to(missing_source)
    removed = run_unlinker(missing_source, root)
    assert removed.returncode == 0, removed.stderr
    assert not dangling.is_symlink()
    assert f"removed: {dangling}" in removed.stdout


def test_unlinker_resolves_logical_checkout_alias(tmp_path: Path) -> None:
    alias = tmp_path / "checkout-alias"
    alias.symlink_to(REPO_ROOT, target_is_directory=True)
    root = tmp_path / "skills"
    root.mkdir()
    target = root / "wf"
    target.symlink_to(alias / "claude" / "skills" / "wf")

    removed = run_unlinker(alias / "claude" / "skills" / "wf", root)

    assert removed.returncode == 0, removed.stderr
    assert not target.is_symlink()
    assert (SKILLS_ROOT / "wf" / "SKILL.md").is_file()


def runtime_roots(home: Path) -> list[Path]:
    return [
        home / ".claude" / "skills",
        home / ".agents" / "skills",
        home / ".omp" / "agent" / "skills",
    ]


def run_setup(
    tmp_path: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
    setup_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
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
        "CLAUDE_DIR": str(home / ".claude"),
        "AGENTS_SKILLS_DIR": str(home / ".agents" / "skills"),
        "OMP_SKILLS_DIR": str(home / ".omp" / "agent" / "skills"),
        "OMP_AGENT_DIR": str(home / ".omp" / "agent" / "agents"),
    }
    env.pop("AWF_WITH_WF", None)
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(setup_path or REPO_ROOT / "setup.sh"), *args],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_setup_installs_only_core_into_three_runtime_roots(tmp_path: Path) -> None:
    completed = run_setup(tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert "setup.sh --with-wf" in completed.stdout
    for root in runtime_roots(tmp_path / "home"):
        assert sorted(path.name for path in root.iterdir()) == CORE_SKILLS
        assert all((root / skill / "SKILL.md").is_file() for skill in CORE_SKILLS)
        assert (root / "release-worktree-lifecycle").resolve() == (
            REPO_ROOT
            / "cli"
            / "src"
            / "awf"
            / "resources"
            / "release-worktree-lifecycle"
        ).resolve()


@pytest.mark.parametrize(
    ("args", "extra_env"),
    [
        (("--with-wf",), {}),
        ((), {"AWF_WITH_WF": "1"}),
        ((), {"AWF_WITH_WF": "true"}),
        ((), {"AWF_WITH_WF": "YES"}),
    ],
)
def test_setup_opt_in_installs_all_skills(
    tmp_path: Path, args: tuple[str, ...], extra_env: dict[str, str]
) -> None:
    completed = run_setup(tmp_path, *args, extra_env=extra_env)
    assert completed.returncode == 0, completed.stderr
    for root in runtime_roots(tmp_path / "home"):
        assert sorted(path.name for path in root.iterdir()) == EXPECTED_SKILLS


def test_setup_opt_in_default_opt_in_transition_across_overridden_roots(tmp_path: Path) -> None:
    overrides = {
        "CLAUDE_DIR": str(tmp_path / "custom-claude"),
        "AGENTS_SKILLS_DIR": str(tmp_path / "custom-agents"),
        "OMP_SKILLS_DIR": str(tmp_path / "custom-omp"),
    }
    roots = [
        tmp_path / "custom-claude" / "skills",
        tmp_path / "custom-agents",
        tmp_path / "custom-omp",
    ]
    first = run_setup(tmp_path, "--with-wf", extra_env=overrides)
    assert first.returncode == 0, first.stderr
    for root in roots:
        assert sorted(path.name for path in root.iterdir()) == EXPECTED_SKILLS

    second = run_setup(tmp_path, extra_env=overrides)
    assert second.returncode == 0, second.stderr
    for root in roots:
        assert sorted(path.name for path in root.iterdir()) == CORE_SKILLS
        assert all(f"removed: {root / skill}" in second.stdout for skill in WF_SKILLS)

    third = run_setup(tmp_path, "--with-wf", extra_env=overrides)
    assert third.returncode == 0, third.stderr
    for root in roots:
        assert sorted(path.name for path in root.iterdir()) == EXPECTED_SKILLS


def test_setup_alias_removes_old_logical_checkout_links(tmp_path: Path) -> None:
    alias = tmp_path / "checkout-alias"
    alias.symlink_to(REPO_ROOT, target_is_directory=True)
    setup = alias / "setup.sh"
    first = run_setup(tmp_path, "--with-wf", setup_path=setup)
    assert first.returncode == 0, first.stderr
    roots = runtime_roots(tmp_path / "home")
    for root in roots:
        for skill in WF_SKILLS:
            target = root / skill
            target.unlink()
            target.symlink_to(alias / "claude" / "skills" / skill)

    second = run_setup(tmp_path, setup_path=setup)

    assert second.returncode == 0, second.stderr
    for root in roots:
        assert sorted(path.name for path in root.iterdir()) == CORE_SKILLS
        assert all(f"removed: {root / skill}" in second.stdout for skill in WF_SKILLS)


def test_setup_blocked_core_does_not_uninstall_wf(tmp_path: Path) -> None:
    first = run_setup(tmp_path, "--with-wf")
    assert first.returncode == 0, first.stderr
    roots = runtime_roots(tmp_path / "home")
    owned = roots[1] / "multi-agent"
    owned.unlink()
    owned.write_text("keep")

    blocked = run_setup(tmp_path)

    assert blocked.returncode == 3
    assert owned.read_text() == "keep"
    for root in roots:
        assert all((root / skill).is_symlink() for skill in WF_SKILLS)
    assert "removed:" not in blocked.stdout


def test_setup_keeps_unowned_wf_paths_in_default_mode(tmp_path: Path) -> None:
    claude, agents, omp = runtime_roots(tmp_path / "home")
    for root in (claude, agents, omp):
        root.mkdir(parents=True)
    (claude / "wf").mkdir()
    (claude / "wf" / "keep").write_text("owned directory")
    (agents / "wf").write_text("owned file")
    (omp / "wf").symlink_to("foreign-target")

    completed = run_setup(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert (claude / "wf" / "keep").read_text() == "owned directory"
    assert (agents / "wf").read_text() == "owned file"
    assert (omp / "wf").is_symlink()
    assert os.readlink(omp / "wf") == "foreign-target"
    for root in (claude, agents, omp):
        assert f"kept: {root / 'wf'}" in completed.stdout


def test_setup_rejects_unknown_argument(tmp_path: Path) -> None:
    completed = run_setup(tmp_path, "--unexpected")
    assert completed.returncode == 2
    assert "usage:" in completed.stderr


@pytest.mark.parametrize("value", ["maybe", "10"])
def test_setup_rejects_unknown_opt_in_value(tmp_path: Path, value: str) -> None:
    completed = run_setup(tmp_path, extra_env={"AWF_WITH_WF": value})
    assert completed.returncode == 2
    assert "usage:" in completed.stderr
    assert not runtime_roots(tmp_path / "home")[0].exists()


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_setup_help_exits_without_installing(tmp_path: Path, flag: str) -> None:
    completed = run_setup(tmp_path, flag)
    assert completed.returncode == 0
    assert "usage:" in completed.stdout
    assert not runtime_roots(tmp_path / "home")[0].exists()


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
