"""Tests for awf.core.version_check — install drift detection.

Tests four state transitions: in_sync, stale, no_source_found, editable.
Each test uses an isolated tmp_path to fabricate source + installed package
copies and monkeypatches `awf.__file__` to point at the fake installed path.

See docs/gaps/2026-05-14-dogfood-d-findings.md §3 (G-OPS-001) for the
operational motivation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf.core import version_check


def _make_fake_source_repo(root: Path, contents: dict[str, str]) -> Path:
    """Create an ai-workflow-tools-shaped tree under root and return cli/src/awf."""
    cli = root / "cli"
    pkg = cli / "src" / "awf"
    pkg.mkdir(parents=True)
    (cli / "pyproject.toml").write_text("[project]\nname = 'awf-cli'\n", encoding="utf-8")
    for rel, body in contents.items():
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return pkg


def _make_fake_installed_pkg(root: Path, contents: dict[str, str]) -> Path:
    pkg = root / "site-packages" / "awf"
    pkg.mkdir(parents=True)
    for rel, body in contents.items():
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return pkg


class TestDetectSourceRoot:
    def test_finds_source_at_repo_root(self, tmp_path: Path) -> None:
        pkg = _make_fake_source_repo(tmp_path, {"__init__.py": "x = 1"})
        assert version_check.detect_source_root(tmp_path) == pkg

    def test_finds_source_when_started_deep_inside(self, tmp_path: Path) -> None:
        pkg = _make_fake_source_repo(tmp_path, {"__init__.py": "x = 1"})
        deep = tmp_path / "docs" / "gaps"
        deep.mkdir(parents=True)
        assert version_check.detect_source_root(deep) == pkg

    def test_returns_none_when_no_source(self, tmp_path: Path) -> None:
        assert version_check.detect_source_root(tmp_path) is None

    def test_requires_both_pyproject_and_init(self, tmp_path: Path) -> None:
        pkg = tmp_path / "cli" / "src" / "awf"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("x = 1", encoding="utf-8")
        # missing pyproject.toml
        assert version_check.detect_source_root(tmp_path) is None


class TestComputePackageHash:
    def test_same_contents_produce_same_hash(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "x.py").write_text("hello", encoding="utf-8")
        (b / "x.py").write_text("hello", encoding="utf-8")
        ha, ca = version_check.compute_package_hash(a)
        hb, cb = version_check.compute_package_hash(b)
        assert ha == hb
        assert ca == cb == 1

    def test_different_contents_produce_different_hash(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "x.py").write_text("hello", encoding="utf-8")
        (b / "x.py").write_text("world", encoding="utf-8")
        ha, _ = version_check.compute_package_hash(a)
        hb, _ = version_check.compute_package_hash(b)
        assert ha != hb

    def test_skips_pycache(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "x.py").write_text("hello", encoding="utf-8")
        cache = pkg / "__pycache__"
        cache.mkdir()
        (cache / "x.cpython-313.pyc").write_text("ignored", encoding="utf-8")
        _, count = version_check.compute_package_hash(pkg)
        assert count == 1

    def test_path_difference_affects_hash(self, tmp_path: Path) -> None:
        """Same content under a different filename → different hash."""
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "x.py").write_text("hello", encoding="utf-8")
        (b / "y.py").write_text("hello", encoding="utf-8")
        ha, _ = version_check.compute_package_hash(a)
        hb, _ = version_check.compute_package_hash(b)
        assert ha != hb


class TestCheckInstallFreshness:
    def _patch_installed(self, monkeypatch: pytest.MonkeyPatch, pkg: Path) -> None:
        # `check_install_freshness` reads `awf.__file__` to find the installed
        # package. The real package's __init__.py is fine — we only need a path
        # whose parent is the fake "installed" dir.
        import awf
        fake_init = pkg / "__init__.py"
        if not fake_init.exists():
            fake_init.write_text("x = 1", encoding="utf-8")
        monkeypatch.setattr(awf, "__file__", str(fake_init))

    def test_in_sync_when_hashes_match(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        contents = {"__init__.py": "x = 1\n", "core/wf_scope.py": "# scope\n"}
        source_pkg = _make_fake_source_repo(tmp_path, contents)
        installed_pkg = _make_fake_installed_pkg(tmp_path, contents)
        self._patch_installed(monkeypatch, installed_pkg)

        result = version_check.check_install_freshness(tmp_path)

        assert result["status"] == "in_sync"
        assert result["file_count_installed"] == 2
        assert result["installed_hash"] == result["source_hash"]
        assert result["reinstall_command"] is None
        assert result["source_path"] == str(source_pkg)

    def test_stale_when_source_has_new_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source_contents = {
            "__init__.py": "x = 1\n",
            "core/version_check.py": "# new\n",
            "core/wf_scope.py": "# updated\n",
        }
        installed_contents = {
            "__init__.py": "x = 1\n",
            "core/wf_scope.py": "# old\n",
        }
        _make_fake_source_repo(tmp_path, source_contents)
        installed_pkg = _make_fake_installed_pkg(tmp_path, installed_contents)
        self._patch_installed(monkeypatch, installed_pkg)

        result = version_check.check_install_freshness(tmp_path)

        assert result["status"] == "stale"
        assert result["installed_hash"] != result["source_hash"]
        assert result["file_count_installed"] == 2
        assert result["file_count_source"] == 3
        assert result["reinstall_command"] is not None
        assert "uv tool install --reinstall" in result["reinstall_command"]
        assert tmp_path.name in result["reinstall_command"] or str(tmp_path) in result["reinstall_command"]

    def test_no_source_found_when_outside_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        installed_pkg = _make_fake_installed_pkg(tmp_path, {"__init__.py": "x = 1\n"})
        self._patch_installed(monkeypatch, installed_pkg)

        elsewhere = tmp_path / "not-awf-checkout"
        elsewhere.mkdir()
        result = version_check.check_install_freshness(elsewhere)

        assert result["status"] == "no_source_found"
        assert result["source_path"] is None
        assert result["installed_hash"] is None
        assert result["reinstall_command"] is None

    def test_editable_when_installed_path_equals_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate `pip install -e .`: the installed module path IS the source
        # path. version_check must recognize this and skip drift comparison.
        pkg = _make_fake_source_repo(tmp_path, {"__init__.py": "x = 1\n"})
        self._patch_installed(monkeypatch, pkg)

        result = version_check.check_install_freshness(tmp_path)

        assert result["status"] == "editable"
        assert result["source_path"] == str(pkg)
        assert result["installed_path"] == str(pkg)
        assert result["reinstall_command"] is None
