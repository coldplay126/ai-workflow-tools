# P0 Database Safety Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fail closed on database-affecting workflows unless they are high risk, use fresh production-schema metadata, compare maintain/query/schema choices, and provide deterministic G1/G5/G6 evidence.

**Architecture:** A new stdlib-only `db_validation` module detects DB signals, validates the project profile and DB decision artifact, runs project-owned commands without a shell, rejects unsafe output, and atomically stores sanitized evidence. `db-check` promotes the workflow to high risk and restores policy-skipped phases. G1/G5/G6 read canonical evidence through mandatory conditions outside normal risk skip rules.

**Tech Stack:** Python 3.9+, argparse, JSON, subprocess, SHA-256, pytest.

---

## File map

- `cli/src/awf/core/db_validation.py`: signals, schemas, command runner, sanitized evidence, hashes, stage verdicts.
- `cli/src/awf/core/state.py`: manifest profile and audited high-risk escalation with policy-skip restoration.
- `cli/src/awf/cli.py`: `wf db-check` parser.
- `cli/src/awf/commands/wf.py`: `db-check` handler and exit codes.
- `cli/src/awf/core/gates.py`: mandatory G1/G5/G6 DB conditions.
- `cli/src/awf/core/workflow_results.py`: sanitized DB evidence summary in verify/test reports.
- `claude/skills/phase-{plan,verify,test}/SKILL.md`: stage command and DB decision/evidence rules.
- `claude/agents/{spec-writer,spec-verifier,happy-path-tester}.md`: planner/verifier/tester contracts.
- `claude/skills/wf-orchestrator/templates/agent-cards/{plan,verify,test}.json`: DB evidence inputs/conditions.
- `claude/skills/wf-orchestrator/templates/agent-card.schema.json`: typed DB declaration.
- `docs/reference/workflow-pipeline.md`, `docs/patterns/workflow-pipeline/03-risk-routing.md`, `cli/README.md`, `CHANGELOG.md`: operator and safety policy.
- New focused tests: `test_db_validation.py`, `test_wf_db_check.py`.
- Existing regressions: workflow policy, spec-kit, gates, result rendering, Skill/card/docs semantics.

### Task 1: Detect DB work and restore mandatory high-risk phases

**Files:**
- Modify: `cli/src/awf/core/state.py`
- Create: `cli/src/awf/core/db_validation.py`
- Test: `cli/tests/test_db_validation.py`
- Test: `cli/tests/test_wf_policy.py`

- [ ] **Step 1: Write failing signal tests**

Create temporary `.workflow` artifacts and assert strong text/path reasons without treating a frontend `src/index.ts` as a DB signal:

```python
def test_detect_database_signal_from_query_and_migration_path(tmp_path: Path) -> None:
    write_workflow_artifacts(
        tmp_path,
        concept="Improve fan log ordering",
        tasks="- [ ] T001 Update ORDER BY [FR-001]",
        allowed_files=["src/database/migrations/add_fan_log_index.sql"],
    )

    signal = detect_database_signal(tmp_path)

    assert signal.detected is True
    assert "text:order by" in signal.reasons
    assert "path:src/database/migrations/add_fan_log_index.sql" in signal.reasons


def test_frontend_index_file_is_not_a_database_signal(tmp_path: Path) -> None:
    write_workflow_artifacts(tmp_path, allowed_files=["src/index.ts"])
    assert detect_database_signal(tmp_path).detected is False
```

- [ ] **Step 2: Write failing risk escalation tests**

Start a small workflow whose review/approve/verify phases and G2/G3/G5 were auto-passed by policy. Call the wished-for escalation API and assert:

```python
updated = promote_database_change_to_high_risk(tmp_path, ("text:query",))
assert updated["changeClass"] == "high_risk"
for phase in ("review", "approve", "verify"):
    assert updated["phases"][phase]["status"] == "pending"
for gate in ("G2", "G3", "G5"):
    assert updated["gates"][gate]["passed"] is None
assert updated["history"][-1]["action"] == "database_risk_escalated"
```

Only policy-skipped phases/gates may be restored; completed human/provider work must not revert.

- [ ] **Step 3: Run RED**

```bash
cd cli
uv run pytest tests/test_db_validation.py tests/test_wf_policy.py -k 'database_signal or database_risk' -q
```

Expected: imports or assertions fail because the detector/escalator do not exist.

- [ ] **Step 4: Implement signal detection**

Add immutable `DatabaseSignal(detected: bool, reasons: tuple[str, ...], snapshot_hash: str)`. Scan only the known workflow files with bounded UTF-8 reads. Normalize reasons, replace raw paths with semantic category plus hash, cap the reason set deterministically, use contextual DB terms, and use strong extension/directory patterns. Hash all classification inputs so evidence can be bound to the exact signal snapshot. Do not scan the entire repository.

- [ ] **Step 5: Implement audited escalation**

Add `promote_database_change_to_high_risk()` in `state.py`. Under the workflow state lock, set `changeClass=high_risk`, restore only phases whose `skipReason` starts with `policy:change_class=small`, reset their policy auto-pass gates to the initial shape, append one idempotent history event, and atomically save. Repeating with the same reasons is a no-op.

Add the manifest default:

```python
database_validation_default = {
    "enabled": False,
    "schema_command": [],
    "verify_command": [],
    "test_command": [],
    "command_timeout_seconds": 30,
    "max_schema_age_hours": 24,
    "allow_production_replica_sample": False,
}
```

- [ ] **Step 6: Run GREEN and commit**

```bash
uv run pytest tests/test_db_validation.py tests/test_wf_policy.py -k 'database_signal or database_risk' -q
git add cli/src/awf/core/db_validation.py cli/src/awf/core/state.py cli/tests/test_db_validation.py cli/tests/test_wf_policy.py
git commit -m "feat(workflow): classify database changes as high risk"
```

### Task 2: Validate profile, decisions, commands, and canonical evidence

**Files:**
- Modify: `cli/src/awf/core/db_validation.py`
- Test: `cli/tests/test_db_validation.py`

- [ ] **Step 1: Write failing profile and decision tests**

Cover disabled/incomplete profiles, unsafe command shapes, candidate counts, missing maintain baseline, broken IDs, missing equivalence/integrity, and unavailable candidates without a concrete reason. Require exact structured `denormalization_assessment` and `physical_design_assessment` objects only for their matching kinds.

Use a valid fixture containing two materially different candidates and assert `load_database_decision()` returns the selected ID and normalized surfaces. Reject candidates whose canonical content differs only by ID; allow same-kind candidates when their plans, costs, or risks differ substantively.

- [ ] **Step 2: Write failing command-safety tests**

Use hermetic Python argv commands. Assert fail-closed behavior for shell strings, timeout, non-zero exit, output over 128 KiB, multiple JSON documents, duplicate keys, unknown fields, and nested sensitive keys such as `password`, `dsn`, `ddl`, `rows`, or `samples`.

Assert commands receive no interpolated shell and persist no raw stdout on failure.

- [ ] **Step 3: Write failing stage-schema tests**

Validate the exact production schema, verify, and test command contracts from the design. Include freshness, SHA-256 format, target class, read-only/schema-only, selected-option match, surface-specific applicability, masked/local target policy, and explicit waiver.

- [ ] **Step 4: Run RED**

```bash
uv run pytest tests/test_db_validation.py -k 'profile or decision or command or schema_evidence or verify_evidence or test_evidence' -q
```

- [ ] **Step 5: Implement strict parsers and runner**

Use duplicate-key rejecting `json.loads(..., object_pairs_hook=...)`, recursive sensitive-key rejection, bounded `subprocess.Popen`/`communicate` with process-group termination, `shell=False`, and canonical JSON hashing. Normalize timestamps to timezone-aware UTC and reject future or stale schema evidence.

- [ ] **Step 6: Implement atomic evidence merge**

`run_database_check(root, stage)` executes the required commands, invokes the optional plan risk-promotion callback immediately after its single signal detection and before any command, then acquires the evidence lock. Under that lock it re-detects the signal, validates the current signal/profile/decision hashes, merges one sanitized stage record into `database-validation-evidence.json`, and atomically replaces the file. Verify/test must preserve a valid plan record. Signal drift or a schema-hash change after plan fails without overwriting the last valid evidence.

No-signal returns `not_applicable` without callback, command execution, evidence, or risk promotion.

- [ ] **Step 7: Run GREEN and commit**

```bash
uv run pytest tests/test_db_validation.py -q
git add cli/src/awf/core/db_validation.py cli/tests/test_db_validation.py
git commit -m "feat(workflow): validate production database evidence"
```

### Task 3: Add `awf wf db-check`

**Files:**
- Modify: `cli/src/awf/cli.py`
- Modify: `cli/src/awf/commands/wf.py`
- Test: `cli/tests/test_wf_db_check.py`

- [ ] **Step 1: Write failing parser and command tests**

Assert parser values for all three stages, repo root, and JSON. Test:

- no signal → exit 0, `not_applicable`;
- DB signal + valid plan → exit 0 and high-risk state;
- invalid profile/evidence → exit 1 with stable blocker code;
- malformed CLI usage → exit 2;
- JSON output contains only schema version, stage, status, evidence path/hash, signal reasons, and blockers.

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/test_wf_db_check.py -q
```

- [ ] **Step 3: Add parser and handler**

Add:

```text
awf wf db-check --stage plan|verify|test --repo-root <root> --json
```

The handler calls `run_database_check`, maps configuration/evidence failures to exit 1, operational parse/usage errors to exit 2, and never prints command stdout or secrets. Plan-stage success invokes audited high-risk escalation before emitting success.

- [ ] **Step 4: Run GREEN and commit**

```bash
uv run pytest tests/test_wf_db_check.py tests/test_wf_policy.py -q
git add cli/src/awf/cli.py cli/src/awf/commands/wf.py cli/tests/test_wf_db_check.py
git commit -m "feat(workflow): add database evidence command"
```

### Task 4: Make DB conditions mandatory at G1, G5, and G6

**Files:**
- Modify: `cli/src/awf/core/gates.py`
- Modify: `cli/src/awf/core/workflow_results.py`
- Test: `cli/tests/test_wf_speckit.py`
- Test: `cli/tests/test_gate_impl_test_conditions.py`
- Test: `cli/tests/test_workflow_results_apply.py`

- [ ] **Step 1: Write failing G1 tests**

For a DB signal, assert G1 fails on missing decision, missing plan evidence, stale evidence, hash mismatch, or non-high-risk evidence. Assert G1 passes for valid evidence and remains unchanged for no signal.

- [ ] **Step 2: Write failing G5/G6 mandatory tests**

Use `change_class="small"` and `standard` to prove DB conditions cannot be skipped. G5 must fail missing/failed verify evidence even when worker scope/compliance/quality pass. G6 must fail missing/failed test evidence unless a valid waiver is present.

- [ ] **Step 3: Run RED**

```bash
uv run pytest tests/test_wf_speckit.py tests/test_gate_impl_test_conditions.py -k database -q
```

- [ ] **Step 4: Implement gate evaluation**

Add `evaluate_database_gate(root, stage)` in `db_validation.py`. Call it directly from G1 and after normal worker conditions for verify/test. Append its evaluations after risk-skip processing so no `skip_checks` branch can bypass them.

Use stable conditions:

```text
database.signal
database.risk_class
database.decision
database.production_schema
database.equivalence
database.integrity
database.query_plan
database.migration
database.rollback
database.local_test
```

- [ ] **Step 5: Render sanitized summaries**

Verify/test reports may show schema hash prefix, engine/version, selected option, stage status, local target, and only the fixed flag `waiver_present=true`. Every worker-controlled field must pass bounded single-line Markdown/HTML escaping and secret/URI/environment/SQL-DDL/sample redaction. Malformed or escaped envelopes record fixed reason codes without raw payloads. They must never render waiver reason text, command argv, raw stdout, environment, data samples, DDL, or unbounded result files.

- [ ] **Step 6: Run GREEN and commit**

```bash
uv run pytest tests/test_wf_speckit.py tests/test_gate_impl_test_conditions.py tests/test_workflow_results_apply.py -q
git add cli/src/awf/core/gates.py cli/src/awf/core/workflow_results.py cli/tests/test_wf_speckit.py cli/tests/test_gate_impl_test_conditions.py cli/tests/test_workflow_results_apply.py
git commit -m "feat(workflow): enforce database evidence gates"
```

### Task 5: Update phase, agent, card, and operator contracts

**Files:**
- Modify: `claude/skills/phase-plan/SKILL.md`
- Modify: `claude/skills/phase-verify/SKILL.md`
- Modify: `claude/skills/phase-test/SKILL.md`
- Modify: `claude/agents/spec-writer.md`
- Modify: `claude/agents/spec-verifier.md`
- Modify: `claude/agents/happy-path-tester.md`
- Modify: `claude/skills/multi-agent/protocols/spec_writer.md`
- Modify: `claude/skills/wf-orchestrator/templates/agent-cards/plan.json`
- Modify: `claude/skills/wf-orchestrator/templates/agent-cards/verify.json`
- Modify: `claude/skills/wf-orchestrator/templates/agent-cards/test.json`
- Modify: `claude/skills/wf-orchestrator/templates/agent-card.schema.json`
- Modify: `docs/reference/workflow-pipeline.md`
- Modify: `docs/patterns/workflow-pipeline/03-risk-routing.md`
- Modify: `cli/README.md`
- Modify: `CHANGELOG.md`
- Test: `cli/tests/test_skill_contract_matrix.py`
- Test: `cli/tests/test_docs_semantic_audit.py`
- Test: `cli/tests/test_omp_agents.py`

- [ ] **Step 1: Write failing semantic tests**

Assert plan/verify/test Skills display each `db-check` before its gate, cards declare canonical decision/evidence artifacts and mandatory conditions, source spec-writer exposes AskUserQuestion/OMP `ask`, and docs contain the production-schema minimum plus local same-engine/DuckDB policy.

Assert the planning contract requires 2–3 options including maintain, equivalence/integrity hard gates, query/index/column/ERD/normalization/denormalization comparison, and no automatic index recommendation.

- [ ] **Step 2: Run RED**

```bash
uv run pytest tests/test_skill_contract_matrix.py tests/test_docs_semantic_audit.py tests/test_omp_agents.py -q
```

- [ ] **Step 3: Update contracts**

Plan creates `database-decision.json`, asks only for a material option choice, then runs plan db-check. Verify and test run their commands before deterministic gates and treat raw prose as insufficient.

Document production metadata as mandatory, same-engine local DB for DDL/planner behavior, DuckDB for profiling/equivalence, project-specific approved replica access, and explicit local-data waiver.

- [ ] **Step 4: Run GREEN and commit**

```bash
uv run pytest tests/test_skill_contract_matrix.py tests/test_docs_semantic_audit.py tests/test_omp_agents.py -q
git add claude/skills/phase-plan/SKILL.md claude/skills/phase-verify/SKILL.md claude/skills/phase-test/SKILL.md claude/agents/spec-writer.md claude/agents/spec-verifier.md claude/agents/happy-path-tester.md claude/skills/multi-agent/protocols/spec_writer.md claude/skills/wf-orchestrator/templates/agent-cards/plan.json claude/skills/wf-orchestrator/templates/agent-cards/verify.json claude/skills/wf-orchestrator/templates/agent-cards/test.json claude/skills/wf-orchestrator/templates/agent-card.schema.json docs/reference/workflow-pipeline.md docs/patterns/workflow-pipeline/03-risk-routing.md cli/README.md CHANGELOG.md cli/tests/test_skill_contract_matrix.py cli/tests/test_docs_semantic_audit.py cli/tests/test_omp_agents.py
git commit -m "docs(workflow): require production database safety evidence"
```

### Task 6: Smoke, full verification, and review

**Files:**
- Add smoke coverage to `cli/tests/test_wf_db_check.py` or a focused workflow runtime test.
- Review all changed files.

- [ ] **Step 1: Run a real CLI smoke**

Create a temporary workflow with a small DB concept, a safe Python schema command, valid decision, verify command, and masked local test command. Execute:

```bash
uv run awf wf db-check --stage plan --repo-root <fixture> --json
uv run awf wf gate plan --repo-root <fixture> --json
uv run awf wf db-check --stage verify --repo-root <fixture> --json
uv run awf wf db-check --stage test --repo-root <fixture> --json
```

Assert state is high risk, review/approve/verify are pending, evidence is sanitized, and all DB gates pass.

- [ ] **Step 2: Run focused tests**

```bash
uv run pytest tests/test_db_validation.py tests/test_wf_db_check.py tests/test_wf_policy.py tests/test_wf_speckit.py tests/test_gate_impl_test_conditions.py tests/test_workflow_results_apply.py tests/test_skill_contract_matrix.py tests/test_docs_semantic_audit.py tests/test_omp_agents.py -q
```

- [ ] **Step 3: Run the complete non-live suite**

```bash
uv run pytest -q
```

- [ ] **Step 4: Review P0 invariants**

Confirm:

- DB work cannot retain a small/standard skip path;
- production schema metadata is mandatory and fresh;
- production primary rows are never accepted evidence;
- maintain/query/schema/normalization/denormalization candidates are compared;
- correctness and integrity are hard gates;
- G1/G5/G6 DB conditions cannot be skipped;
- raw command output, secrets, DDL, and rows never persist;
- no-signal repositories retain existing behavior;
- local production-shaped tests use masked evidence or an explicit waiver.

- [ ] **Step 5: Commit only review fixes**

If final review finds a defect, add a failing regression, fix that defect, rerun its focused test and the complete focused set, then commit with a precise `fix(workflow): describe the database invariant` message. Do not create a no-op commit.
