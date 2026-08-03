# Subscription-Backed AWF Skill Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the existing 45 host-discovery probes and 27 OMP field pairs with the operator's Claude, ChatGPT/Codex, and OMP subscriptions while keeping candidate Skills project-isolated and credential data out of evidence.

**Architecture:** Add one shared subscription-runtime contract that captures credential locations in memory, strips API-key fallbacks, builds per-host environments, pins subscription-compatible models, and normalizes provider failures. Discovery installs immutable candidate Skills into a temporary project workspace; OMP discovery gets only `read`, while OMP field pairs stay no-tool and inject the exact snapshot `SKILL.md` through `--append-system-prompt`. Evidence binds the full Skill directory hash separately from the injected file hash.

**Tech Stack:** Python 3.11+, pytest, subprocess, Claude CLI, Codex CLI, OMP CLI, JSON evidence reports.

---

## File Structure

- Create `cli/src/awf/core/skill_subscription.py`: in-memory subscription auth context, per-runtime environment construction, pinned model contract, and normalized host diagnostics.
- Modify `cli/tests/run_skill_discovery.py`: temporary project roots, host-specific subscription environments, safe discovery argv, and normalized records.
- Modify `cli/tests/run_skill_pressure.py`: subscription-backed OMP execution, no-tool Skill injection, and injection hash capture.
- Modify `cli/src/awf/core/skill_pressure.py`: evidence schema and source-binding validation for subscription mode and injected file hashes.
- Modify `cli/tests/test_skill_pressure_harness.py`: focused TDD coverage for all new runtime, argv, environment, normalization, and evidence contracts.
- Modify `docs/superpowers/specs/2026-07-30-awf-skill-validation-design.md` only if implementation proves an approved statement impossible; any such change requires renewed user approval before code proceeds.

### Task 1: Shared Subscription Runtime Contract

**Files:**
- Create: `cli/src/awf/core/skill_subscription.py`
- Modify: `cli/tests/test_skill_pressure_harness.py`

- [ ] **Step 1: Write failing tests for auth capture, environment isolation, model pinning, and diagnostic normalization**

Add imports for `SubscriptionAuthContext`, `build_subscription_environment`, `normalize_host_diagnostic`, and `require_subscription_model`, then add focused cases with explicit fake paths:

```python
def _subscription_auth(tmp_path: Path) -> SubscriptionAuthContext:
    original_home = tmp_path / "operator"
    return SubscriptionAuthContext(
        original_home=original_home,
        claude_config_dir=original_home / ".claude",
        codex_home=original_home / ".codex",
        omp_agent_dir=original_home / ".omp" / "agent",
    )


def test_subscription_environments_reference_only_the_required_store(tmp_path: Path) -> None:
    auth = _subscription_auth(tmp_path)
    temporary_home = tmp_path / "run" / "home"
    base = {
        "HOME": "/wrong",
        "CLAUDE_CONFIG_DIR": "/wrong/claude",
        "CODEX_HOME": "/wrong/codex",
        "PI_CODING_AGENT_DIR": "/wrong/omp",
        "ANTHROPIC_API_KEY": "must-be-removed",
        "OPENAI_API_KEY": "must-be-removed",
        "PATH": "/bin",
    }

    claude = build_subscription_environment("claude", auth, temporary_home, base)
    codex = build_subscription_environment("agent-skills", auth, temporary_home, base)
    omp = build_subscription_environment("omp", auth, temporary_home, base)

    assert claude["HOME"] == str(auth.original_home)
    assert claude["CLAUDE_CONFIG_DIR"] == str(auth.claude_config_dir)
    assert "CODEX_HOME" not in claude and "PI_CODING_AGENT_DIR" not in claude
    assert codex["HOME"] == str(temporary_home)
    assert codex["CODEX_HOME"] == str(auth.codex_home)
    assert "CLAUDE_CONFIG_DIR" not in codex and "PI_CODING_AGENT_DIR" not in codex
    assert omp["HOME"] == str(temporary_home)
    assert omp["PI_CODING_AGENT_DIR"] == str(auth.omp_agent_dir)
    assert "CLAUDE_CONFIG_DIR" not in omp and "CODEX_HOME" not in omp
    for environment in (claude, codex, omp):
        assert environment["PATH"] == "/bin"
        assert "ANTHROPIC_API_KEY" not in environment
        assert "OPENAI_API_KEY" not in environment


def test_subscription_models_are_pinned_per_host() -> None:
    require_subscription_model("claude", "sonnet")
    require_subscription_model("agent-skills", "gpt-5.4")
    require_subscription_model("omp", "openai-codex/gpt-5.6-sol")
    with pytest.raises(ValueError, match="subscription model mismatch"):
        require_subscription_model("agent-skills", "openai-codex/gpt-5.6-sol")


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "error_kind", "expected"),
    [
        (124, "", "", "timeout", "host_timeout"),
        (1, "", "Refresh token expired", None, "host_subscription_expired"),
        (1, "", "Not logged in", None, "host_auth_unavailable"),
        (1, "", "model is not supported with a ChatGPT account", None, "host_model_unsupported"),
        (7, "", "opaque provider text", None, "host_provider_exit"),
    ],
)
def test_host_diagnostics_are_allowlisted(
    returncode: int,
    stdout: str,
    stderr: str,
    error_kind: str | None,
    expected: str,
) -> None:
    assert normalize_host_diagnostic(returncode, stdout, stderr, error_kind) == expected
```

Also add a capture test proving default locations derive from the original `HOME`, explicit config variables win, and the resulting dataclass is never converted into a report payload.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --project cli pytest cli/tests/test_skill_pressure_harness.py -q -k 'subscription_environment or subscription_models or host_diagnostics'
```

Expected: collection fails because `awf.core.skill_subscription` does not exist.

- [ ] **Step 3: Implement the shared contract**

Create `cli/src/awf/core/skill_subscription.py` with this public surface:

```python
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

SUPPORTED_RUNTIMES = frozenset({"claude", "agent-skills", "omp"})
PINNED_SUBSCRIPTION_MODELS = MappingProxyType(
    {
        "claude": "sonnet",
        "agent-skills": "gpt-5.4",
        "omp": "openai-codex/gpt-5.6-sol",
    }
)
API_KEY_ENV_KEYS = frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY"})
CONFIG_ENV_KEYS = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR")

_EXPIRED_RE = re.compile(r"refresh token expired|subscription[^\n]*expired", re.IGNORECASE)
_AUTH_RE = re.compile(
    r"credential|authentication|authorize|not logged in|login required|not authenticated|api[ _-]?key",
    re.IGNORECASE,
)
_MODEL_RE = re.compile(r"model[^\n]*(?:not supported|unsupported)|unsupported[^\n]*model", re.IGNORECASE)


@dataclass(frozen=True)
class SubscriptionAuthContext:
    original_home: Path
    claude_config_dir: Path
    codex_home: Path
    omp_agent_dir: Path

    @classmethod
    def capture(cls, environment: Mapping[str, str] | None = None) -> "SubscriptionAuthContext":
        source = dict(os.environ if environment is None else environment)
        home_value = source.get("HOME")
        if not home_value:
            raise ValueError("subscription auth requires HOME")
        home = Path(home_value).expanduser().resolve()
        return cls(
            original_home=home,
            claude_config_dir=Path(source.get("CLAUDE_CONFIG_DIR", home / ".claude")).expanduser().resolve(),
            codex_home=Path(source.get("CODEX_HOME", home / ".codex")).expanduser().resolve(),
            omp_agent_dir=Path(source.get("PI_CODING_AGENT_DIR", home / ".omp" / "agent")).expanduser().resolve(),
        )


def require_subscription_model(runtime: str, model: str) -> None:
    expected = PINNED_SUBSCRIPTION_MODELS.get(runtime)
    if expected is None:
        raise ValueError(f"unsupported runtime: {runtime}")
    if model != expected:
        raise ValueError(f"subscription model mismatch for {runtime}: expected {expected}")


def build_subscription_environment(
    runtime: str,
    auth: SubscriptionAuthContext,
    temporary_home: Path,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    if runtime not in SUPPORTED_RUNTIMES:
        raise ValueError(f"unsupported runtime: {runtime}")
    environment = dict(os.environ if base_environment is None else base_environment)
    for key in (*CONFIG_ENV_KEYS, *API_KEY_ENV_KEYS):
        environment.pop(key, None)
    if runtime == "claude":
        environment["HOME"] = str(auth.original_home)
        environment["CLAUDE_CONFIG_DIR"] = str(auth.claude_config_dir)
    else:
        environment["HOME"] = str(temporary_home)
        if runtime == "agent-skills":
            environment["CODEX_HOME"] = str(auth.codex_home)
        else:
            environment["PI_CODING_AGENT_DIR"] = str(auth.omp_agent_dir)
    return environment


def normalize_host_diagnostic(
    returncode: int,
    stdout: str = "",
    stderr: str = "",
    error_kind: str | None = None,
) -> str:
    text = f"{stdout}\n{stderr}"
    if error_kind == "timeout" or returncode == 124:
        return "host_timeout"
    if _EXPIRED_RE.search(text):
        return "host_subscription_expired"
    if _MODEL_RE.search(text):
        return "host_model_unsupported"
    if _AUTH_RE.search(text):
        return "host_auth_unavailable"
    return "" if returncode == 0 else "host_provider_exit"
```

Keep this module free of filesystem reads other than path normalization. Do not add serialization methods or logging of the dataclass.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the Step 2 command again.

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add cli/src/awf/core/skill_subscription.py cli/tests/test_skill_pressure_harness.py
git commit -m "feat: add subscription Skill runtime contract"
```

### Task 2: Project-Isolated Subscription Discovery

**Files:**
- Modify: `cli/tests/run_skill_discovery.py:32-156,366-378,506-580,615-791`
- Modify: `cli/tests/test_skill_pressure_harness.py:1432-1976`

- [ ] **Step 1: Write failing discovery contract tests**

Update the fake discovery process to retain each call's `cwd` and environment. Add assertions covering the exact command shapes:

```python
def test_skill_discovery_uses_subscription_safe_project_argv() -> None:
    prompt = "describe the skill"
    assert claude_argv("claude", "sonnet", "wf-status", prompt) == [
        "claude", "-p", "--output-format", "text", "--tools", "",
        "--no-session-persistence", "--setting-sources", "project",
        "--model", "sonnet", "/wf-status\ndescribe the skill",
    ]
    assert agent_skills_argv("codex", "gpt-5.4", "wf-status", prompt) == [
        "codex", "exec", "--ephemeral", "--sandbox", "read-only",
        "--skip-git-repo-check", "--model", "gpt-5.4",
        "$wf-status\ndescribe the skill",
    ]
    assert omp_argv("omp", "openai-codex/gpt-5.6-sol", "wf-status", prompt) == [
        "omp", "-p", "--mode=text", "--tools=read", "--no-session",
        "--no-extensions", "--model=openai-codex/gpt-5.6-sol",
        "--skills=wf-status", prompt,
    ]
```

Add an end-to-end fake-run test asserting:

- all 45 model invocations use one temporary project workspace as `cwd`, not the repository root
- install targets are `<workspace>/.claude/skills`, `<workspace>/.agents/skills`, and `<workspace>/.omp/skills`
- Claude sees the captured original home/config only
- Codex sees a temporary home plus the captured original `CODEX_HOME`
- OMP sees a temporary home plus the captured original `PI_CODING_AGENT_DIR`
- `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` are absent in every model invocation
- no credential location appears in install or discovery report JSON
- temporary workspace and home are gone after `run_discovery` returns

Add failure tests proving auth and timeout diagnostics remain `BLOCKED`, while `host_model_unsupported` is `FAIL`, and no raw stderr appears in a written report.

- [ ] **Step 2: Run discovery-focused tests and verify RED**

Run:

```bash
uv run --project cli pytest cli/tests/test_skill_pressure_harness.py -q -k 'skill_discovery'
```

Expected failures: old home-level roots, old OMP `--no-tools` argv, missing Claude setting-source flag, repository `cwd`, and raw/legacy diagnostics.

- [ ] **Step 3: Move discovery installation into a temporary project workspace**

In `run_skill_discovery.py`:

```python
PROJECT_SKILL_ROOTS = {
    "claude": ".claude/skills",
    "agent-skills": ".agents/skills",
    "omp": ".omp/skills",
}
```

Capture `SubscriptionAuthContext` before creating the temporary directory. Inside it, create `home/` and `workspace/`, install all candidate links beneath `workspace`, and use `workspace` as the actual discovery process `cwd`. Keep installer execution and report publication rooted at the repository.

Replace `_base_environment` with `build_subscription_environment`. Accept an optional `auth_context` in `run_discovery` for deterministic tests; production defaults to `SubscriptionAuthContext.capture()`.

- [ ] **Step 4: Implement the three exact host command contracts**

Make these changes:

- Claude: add `--setting-sources project`; retain no tools and no session persistence.
- Codex: retain `$<skill>`, ephemeral execution, and read-only sandbox; call `require_subscription_model` so the OMP selector cannot reach Codex.
- OMP: replace `--no-tools` with `--tools=read`, add `--no-extensions`, retain the one-Skill allowlist, and change only the OMP prompt to permit reading the selected Skill.

The OMP prompt must be exact and side-effect-free:

```python
"Use only the read tool to load the selected Skill. Do not read any other path or mutate anything. "
"Return exactly one JSON object and no prose. Its schema is "
'{"name":"exact Skill name","description":"exact frontmatter description",'
'"body_heading":"exact first Markdown H1"}.'
```

Claude and Codex retain the no-tool prompt because their explicit Skill command injects the selected body.

- [ ] **Step 5: Normalize and classify host failures before report creation**

Set `auth_mode` to `subscription` in each discovery record. Replace raw regex branching with `normalize_host_diagnostic`. Use this verdict mapping:

```python
BLOCKED_DIAGNOSTICS = {
    "host_timeout",
    "host_auth_unavailable",
    "host_subscription_expired",
    "host_provider_exit",
}
```

`host_model_unsupported` and response/schema mismatches are `FAIL`. Persist only the normalized diagnostic.

- [ ] **Step 6: Run discovery-focused tests and verify GREEN**

Run the Step 2 command again.

Expected: all discovery-focused tests pass, including 45 unique identities and cleanup assertions.

- [ ] **Step 7: Commit Task 2**

```bash
git add cli/tests/run_skill_discovery.py cli/tests/test_skill_pressure_harness.py
git commit -m "feat: reuse subscriptions for isolated Skill discovery"
```

### Task 3: No-Tool OMP Skill Injection and Evidence Binding

**Files:**
- Modify: `cli/tests/run_skill_pressure.py:20-250,295-464`
- Modify: `cli/src/awf/core/skill_pressure.py:43-116,758-786,799-828,1038-1044,1076-1110`
- Modify: `cli/tests/test_skill_pressure_harness.py:241-262,427-487,537-715,879-1429`

- [ ] **Step 1: Write failing pair-run tests for exact injection and subscription environment**

Replace the old `--skills=<name>` expectation with these assertions:

```python
def test_execute_pair_uses_no_skill_baseline_then_exact_snapshot_injection(tmp_path: Path) -> None:
    calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def fake_run(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> ProviderResult:
        calls.append((argv, cwd, env))
        return _successful_wf_status_result()

    repo_root, source = _copied_wf_status_repo(tmp_path)
    auth = _subscription_auth(tmp_path)
    run = execute_pair(
        MATRIX.skills["wf-status"],
        repo_root=repo_root,
        omp_command="omp",
        model="openai-codex/gpt-5.6-sol",
        timeout_sec=30,
        auth_context=auth,
        run_process=fake_run,
    )

    baseline, with_skill = calls
    assert "--no-skills" in baseline[0]
    assert "--append-system-prompt" not in baseline[0]
    assert "--no-skills" in with_skill[0]
    injection_index = with_skill[0].index("--append-system-prompt") + 1
    injected = Path(with_skill[0][injection_index])
    assert injected.name == "SKILL.md"
    assert injected.is_relative_to(with_skill[1] / ".omp" / "skills" / "wf-status")
    assert all("--skills=" not in argument for argument in with_skill[0])
    assert baseline[1] == with_skill[1]
    assert baseline[2]["PI_CODING_AGENT_DIR"] == str(auth.omp_agent_dir)
    assert "OPENAI_API_KEY" not in baseline[2]
    assert run.injection_sha256 == run.skill_file_sha256
    assert run.skill_sha256 == sha256_skill(source)
```

Add mutation cases that change `snapshot/SKILL.md` before with-Skill launch and during with-Skill execution. Both must return `BLOCKED skill_snapshot_changed`, and the second process must not start in the pre-launch case.

Add a preflight assertion requiring `--append-system-prompt` and `--no-extensions` while no longer requiring `--skills` for field execution.

- [ ] **Step 2: Write failing evidence-schema tests**

Extend `valid_field_record()` and passing field fixtures with:

```python
field_record.update(
    {
        "auth_mode": "subscription",
        "skill_file_sha256": hashlib.sha256(b"skill-file").hexdigest(),
        "injection_sha256": hashlib.sha256(b"skill-file").hexdigest(),
    }
)
```

Add tests proving:

- missing either hash is rejected
- `auth_mode != "subscription"` is rejected
- `injection_sha256 != skill_file_sha256` is rejected
- `skill_file_sha256` must match the current canonical `SKILL.md`
- full `skill_sha256` still binds the complete Skill directory, including nested resources
- discovery records without `auth_mode=subscription` cannot enter a new source bundle
- `DETERMINISTIC_SOURCE_FILES` contains `skill_subscription.py`, both live runner scripts, and `build_skill_evidence.py`

- [ ] **Step 3: Run pair and evidence tests and verify RED**

Run:

```bash
uv run --project cli pytest cli/tests/test_skill_pressure_harness.py -q -k 'execute_pair or field_record or source_bundle or deterministic_source or discovery_record'
```

Expected failures: old OMP Skill selection, missing subscription environment, absent injection fields, and incomplete provenance inventory.

- [ ] **Step 4: Implement no-tool OMP injection**

Update `PairRun`:

```python
@dataclass(frozen=True)
class PairRun:
    evaluation: PairEvaluation
    baseline_result: ProviderResult
    with_skill_result: ProviderResult
    skill_sha256: str
    skill_file_sha256: str
    injection_sha256: str
```

In `execute_pair`:

- capture or accept `SubscriptionAuthContext`
- create `<temporary>/home` and `<temporary>/workspace`
- snapshot the candidate under `<workspace>/.omp/skills/<skill>`
- run both arms from `<workspace>`
- build the environment with `build_subscription_environment("omp", ...)`
- add `--no-extensions` to the common argv
- baseline: `--no-skills` and no append prompt
- with-Skill: `--no-skills --append-system-prompt <snapshot>/SKILL.md`
- calculate `skill_sha256` with `sha256_skill(snapshot)`
- calculate `skill_file_sha256` and `injection_sha256` with `sha256_file(snapshot / "SKILL.md")`
- recheck both the directory and file hashes before launch and after completion

The runtime record must include `auth_mode`, `skill_file_sha256`, and `injection_sha256`. `runner_flags` records the presence of `--append-system-prompt` without persisting its temporary absolute path.

- [ ] **Step 5: Harden evidence validation and provenance**

In `skill_pressure.py`:

- add `auth_mode`, `skill_file_sha256`, and `injection_sha256` to `FIELD_RECORD_REQUIRED`
- add those safe scalar/hash fields to `SAFE_FIELD_IDENTITY_KEYS`
- require exactly `auth_mode == "subscription"`
- validate all three SHA-256 fields
- require `injection_sha256 == skill_file_sha256`
- in `_validate_field_payload_binding`, require `skill_file_sha256 == sha256_file(current SKILL.md)` while retaining the full-directory `skill_sha256` check
- require `auth_mode == "subscription"` and a recognized safe flag set in every discovery record
- add these provenance sources to `DETERMINISTIC_SOURCE_FILES`:

```python
"cli/src/awf/core/skill_subscription.py",
"cli/tests/run_skill_discovery.py",
"cli/tests/run_skill_pressure.py",
"cli/tests/build_skill_evidence.py",
```

Keep prior append-only reports readable as historical files, but make them ineligible for the new batch because they lack the new binding fields.

- [ ] **Step 6: Run the focused tests and verify GREEN**

Run the Step 3 command again.

Expected: all selected tests pass.

- [ ] **Step 7: Run the complete focused validation set**

```bash
uv run --project cli pytest \
  cli/tests/test_skill_contract_matrix.py \
  cli/tests/test_skill_runtime_install.py \
  cli/tests/test_skill_pressure_harness.py \
  cli/tests/test_docs_semantic_audit.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit Task 3**

```bash
git add \
  cli/tests/run_skill_pressure.py \
  cli/src/awf/core/skill_pressure.py \
  cli/tests/test_skill_pressure_harness.py
git commit -m "feat: bind OMP Skill injection evidence"
```

### Task 4: Workstation Subscription Acceptance

**Files:**
- Runtime output only: `.awf-operations/skill-pressure/*.json` and redacted transcript artifacts
- No tracked source edits unless an acceptance failure proves a code defect

- [ ] **Step 1: Run focused and complete suites from the final source revision**

```bash
uv run --project cli pytest \
  cli/tests/test_skill_contract_matrix.py \
  cli/tests/test_skill_runtime_install.py \
  cli/tests/test_skill_pressure_harness.py \
  cli/tests/test_docs_semantic_audit.py -q
uv run --project cli pytest cli/tests -q
```

Expected: both commands exit 0. Repository-declared skips may remain; no failures or errors may remain.

- [ ] **Step 2: Create one fresh batch identifier and deterministic evidence**

```bash
export AWF_SKILL_BATCH_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
uv run --project cli python cli/tests/run_skill_deterministic.py \
  --batch-id "$AWF_SKILL_BATCH_ID" \
  --repo-root . \
  --timeout-sec 600 \
  --write-result \
  --json
```

Expected: exit 0 and one current-batch deterministic report with `exit_status: 0`.

- [ ] **Step 3: Run all 45 subscription-backed host discovery probes**

```bash
uv run --project cli python cli/tests/run_skill_discovery.py \
  --batch-id "$AWF_SKILL_BATCH_ID" \
  --all \
  --claude-model sonnet \
  --agent-skills-model gpt-5.4 \
  --omp-model openai-codex/gpt-5.6-sol \
  --timeout-sec 120 \
  --repo-root . \
  --write-result \
  --json
```

Expected: exit 0, install 45/45 `PASS`, discovery 45/45 `PASS`, and no raw credential/account data in reports.

- [ ] **Step 4: Run all 27 OMP field pairs with subscription auth**

```bash
uv run --project cli python cli/tests/run_skill_pressure.py \
  --batch-id "$AWF_SKILL_BATCH_ID" \
  --all \
  --model openai-codex/gpt-5.6-sol \
  --timeout-sec 120 \
  --repo-root . \
  --write-result \
  --json
```

Expected: 27 unique results. `FAIL` or `BLOCKED` is not accepted silently; inspect only normalized diagnostics and fix a harness defect at source. A genuine provider outage remains append-only `BLOCKED` evidence and requires a new batch after recovery.

- [ ] **Step 5: Build the exact 135-cell evidence summary**

```bash
uv run --project cli python cli/tests/build_skill_evidence.py \
  --batch-id "$AWF_SKILL_BATCH_ID" \
  --repo-root .
```

Expected: exit 0 and exactly 135 unique cells with no `FAIL` or `BLOCKED` verdict.

- [ ] **Step 6: Obtain two independent read-only reviews against the same final commit**

Review 1 checks every requirement in `docs/superpowers/specs/2026-07-30-awf-skill-validation-design.md`, especially Section 15, against the final diff and current-batch evidence.

Review 2 checks credential exposure, command/tool safety, subprocess environment construction, hash/provenance integrity, cleanup confinement, and regression risk.

Expected: both return `APPROVED` with no Critical or Important finding. If either review requires a source edit, apply focused TDD, commit the correction, discard the candidate batch from aggregation, and repeat Tasks 4.1-4.6 with a new batch identifier.

- [ ] **Step 7: Stop at the integration gate**

Do not create or merge a pull request. Report the final commit, exact test results, 45/45 discovery result, 27/27 pair result, 135-cell counts, residual Minor findings, and the separate PR-creation approval requirement.
