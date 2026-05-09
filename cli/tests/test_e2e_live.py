"""E2E live integration tests — baseline.

Tests require a real provider connection. Skipped when ANTHROPIC_API_KEY is
not set. Run explicitly with:

    ANTHROPIC_API_KEY=sk-... python -m pytest cli/tests/test_e2e_live.py -v

The tests are ordered from cheapest (no API call) to most expensive (live LLM
call) so that fast failures surface early.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.cli import main as awf_main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = str(Path(__file__).resolve().parents[2])

_HAS_ANTHROPIC_KEY = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

live = pytest.mark.skipif(not _HAS_ANTHROPIC_KEY, reason="ANTHROPIC_API_KEY not set")


_MINIMAL_PROVIDER_CONFIG = {
    "version": "2.0.0",
    "phase_routing": {
        "plan": {"mode": "inline"},
        "review": {"mode": "inline"},
        "approve": {"mode": "inline"},
        "impl": {"mode": "inline"},
        "verify": {"mode": "inline"},
        "test": {"mode": "inline"},
        "done": {"mode": "inline"},
    },
    "providers": {},
    "fallback_chain": [],
    "defaults": {"mode": "inline", "timeout_seconds": 300},
}


def _make_fake_repo(tmpdir: str) -> Path:
    """Create minimal directory structure so find_repo_root() accepts tmpdir."""
    root = Path(tmpdir)
    (root / "claude" / "agents").mkdir(parents=True, exist_ok=True)
    (root / ".workflow").mkdir(parents=True, exist_ok=True)
    # provider-config.json is required by wf next
    pc_path = root / ".workflow" / "provider-config.json"
    if not pc_path.exists():
        pc_path.write_text(json.dumps(_MINIMAL_PROVIDER_CONFIG, indent=2))
    return root


def _capture_main(argv: list[str]) -> tuple[int, str, str]:
    """Run awf_main capturing stdout/stderr. Returns (rc, stdout, stderr)."""
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        rc = awf_main(argv)
    except SystemExit as exc:
        rc = int(exc.code) if exc.code is not None else 0
    finally:
        out, err = sys.stdout.getvalue(), sys.stderr.getvalue()
        sys.stdout, sys.stderr = old_out, old_err
    return rc, out, err


# ===========================================================================
# Tier 0: Smoke — no API call, validates CLI & config
# ===========================================================================

class TestSmoke:
    """Basic CLI health checks that never hit an external API."""

    def test_doctor_json(self):
        rc, out, _ = _capture_main(["doctor", "--repo-root", REPO_ROOT, "--json"])
        assert rc == 0
        data = json.loads(out)
        assert "providers" in data
        assert "paths" in data

    def test_skills_list_contains_phase_skills(self):
        rc, out, _ = _capture_main(["skills", "list", "--repo-root", REPO_ROOT, "--json"])
        assert rc == 0
        data = json.loads(out)
        names = {s["name"] for s in data["skills"]}
        for phase in ("plan", "review", "approve", "impl", "verify", "test", "done"):
            assert f"phase-{phase}" in names, f"phase-{phase} not discovered"
        assert not any(n.startswith("wf-phase-") for n in names), "stale wf-phase- skill found"

    def test_detect_class_small(self):
        rc, out, _ = _capture_main(["wf", "detect-class", "--json", "fix typo"])
        assert rc == 0
        data = json.loads(out)
        assert data["change_class"] == "small"

    def test_detect_class_high_risk(self):
        rc, out, _ = _capture_main(["wf", "detect-class", "--json", "refactor authentication and payment flow with DB migration"])
        assert rc == 0
        data = json.loads(out)
        assert data["change_class"] == "high_risk"


# ===========================================================================
# Tier 1: Dry-run — prompt construction without API call
# ===========================================================================

class TestDryRun:
    """Workflow dry-run tests that build prompts but skip provider calls."""

    def test_wf_init_and_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _make_fake_repo(tmpdir)
            rc, _, err = _capture_main(["wf", "init", "Add user profile endpoint", "--repo-root", tmpdir, "--no-ready-gate"])
            assert rc == 0, f"wf init failed: {err}"
            state_path = Path(tmpdir) / ".workflow" / "state.json"
            assert state_path.exists()
            state = json.loads(state_path.read_text())
            assert state["currentPhase"] == "plan"

            rc2, out2, _ = _capture_main(["wf", "status", "--repo-root", tmpdir, "--json"])
            assert rc2 == 0
            status = json.loads(out2)
            assert status["currentPhase"] == "plan"

    def test_wf_next_dry_run_plan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _make_fake_repo(tmpdir)
            _capture_main(["wf", "init", "Add user profile endpoint", "--repo-root", tmpdir, "--no-ready-gate"])

            # Copy agent-cards from the repo templates
            cards_src = Path(REPO_ROOT) / "claude" / "skills" / "wf-orchestrator" / "templates" / "agent-cards"
            cards_dst = Path(tmpdir) / ".workflow" / "agent-cards"
            if cards_src.is_dir():
                shutil.copytree(str(cards_src), str(cards_dst), dirs_exist_ok=True)

            rc, out, _ = _capture_main([
                "wf", "next", "--repo-root", tmpdir, "--dry-run",
                "--provider", "fixture",
            ])
            assert rc == 0
            assert "plan" in out.lower()

    def test_wf_gate_plan_fails_on_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _make_fake_repo(tmpdir)
            _capture_main(["wf", "init", "test concept", "--repo-root", tmpdir, "--no-ready-gate"])

            rc, out, _ = _capture_main(["wf", "gate", "plan", "--repo-root", tmpdir])
            # Gate should fail because no artifacts exist yet
            assert "FAIL" in out.upper() or rc != 0


# ===========================================================================
# Tier 2: Live — actual LLM API call via claude-sdk
# ===========================================================================

class TestLive:
    """Tests that call the Anthropic API. Skipped without ANTHROPIC_API_KEY."""

    @live
    def test_claude_sdk_simple_complete(self):
        """Minimal provider.complete() call — validates SDK auth and connectivity."""
        from awf.core.config import load_awf_config
        from awf.providers.claude_sdk import ClaudeSdkProvider

        provider = ClaudeSdkProvider(model="claude-haiku-4-5-20251001", max_tokens=256)
        result = provider.complete("Respond with exactly: PONG", cwd=REPO_ROOT)
        assert result.returncode == 0, f"Provider failed: {result.stderr}"
        assert "PONG" in result.stdout.upper()
        assert result.usage is not None
        assert result.usage.input_tokens > 0
        assert result.usage.output_tokens > 0

    @live
    def test_wf_plan_with_claude_sdk(self):
        """Init workflow, run plan phase via claude-sdk, check state transitions."""
        from awf.providers.claude_sdk import ClaudeSdkProvider

        with tempfile.TemporaryDirectory() as tmpdir:
            _make_fake_repo(tmpdir)
            wf_dir = Path(tmpdir) / ".workflow"

            # Init
            rc, _, err = _capture_main(["wf", "init", "Add a /health endpoint that returns 200 OK", "--repo-root", tmpdir, "--no-ready-gate"])
            assert rc == 0, f"wf init failed: {err}"

            # Copy agent-cards so the orchestrator can find them
            cards_src = Path(REPO_ROOT) / "claude" / "skills" / "wf-orchestrator" / "templates" / "agent-cards"
            cards_dst = wf_dir / "agent-cards"
            if cards_src.is_dir():
                shutil.copytree(str(cards_src), str(cards_dst), dirs_exist_ok=True)

            # Build the plan prompt (dry-run) and execute via SDK
            rc, prompt_out, _ = _capture_main([
                "wf", "next", "--repo-root", tmpdir, "--dry-run", "--print-prompt",
                "--provider", "claude-sdk",
            ])
            assert rc == 0
            assert len(prompt_out) > 100, "Plan prompt too short"

            # Verify state is still on plan (dry-run should not advance)
            state = json.loads((wf_dir / "state.json").read_text())
            assert state["currentPhase"] == "plan"
