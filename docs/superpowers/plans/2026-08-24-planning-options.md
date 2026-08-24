# Planning Options Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make material planning alternatives explicit, recommended, user-selected, durable, and safely replanned without interrupting users for reversible details.

**Architecture:** A strict `planning_options` core owns artifact parsing, validation, locking, selection mutation, and hashes. G1 consumes the artifact when enabled by new-workflow manifest policy. `wf select-option` atomically persists a selection, then uses existing continue/replan state transitions. Planner Skills, agents, cards, and docs share the canonical schema.

**Tech Stack:** Python 3.9+, argparse, JSON, fcntl/dirfd filesystem safety, SHA-256, pytest.

---

### Task 1: Validate planning-options artifacts

**Files:**
- Create: `cli/src/awf/core/planning_options.py`
- Modify: `cli/src/awf/core/state.py`
- Create: `cli/tests/test_planning_options.py`
- Test: `cli/tests/test_wf_policy.py`

- [ ] Write failing tests for all three statuses, strict integer version, exact fields, duplicate keys, bounded UTF-8, no-follow paths, IDs, 2–3 options, recommendation-first, materially distinct normalized options, valid selections, history, and sensitive text rejection.
- [ ] Add `planning_options: {"required": True}` to `DEFAULT_MANIFEST` and exact default tests.
- [ ] Implement immutable normalized records and `load_planning_options(root)` with canonical SHA-256.
- [ ] Implement legacy policy resolution: missing profile plus missing artifact is `legacy_not_required`; malformed profile fails; artifact presence always invokes validation.
- [ ] Use dirfd/O_NOFOLLOW reads and reject unsafe `.workflow`/artifacts/file links.
- [ ] Run:

```bash
cd cli
uv run pytest tests/test_planning_options.py tests/test_wf_policy.py -q
```

- [ ] Commit:

```bash
git add cli/src/awf/core/planning_options.py cli/src/awf/core/state.py cli/tests/test_planning_options.py cli/tests/test_wf_policy.py
git commit -m "feat(workflow): validate planning option decisions"
```

### Task 2: Enforce planning decisions at G1

**Files:**
- Modify: `cli/src/awf/core/gates.py`
- Test: `cli/tests/test_wf_speckit.py`
- Test: `cli/tests/test_planning_options.py`

- [ ] Add failing G1 tests for required missing artifact, malformed artifact, selection_required, selected, no_decision_required, bad recommendation/materiality, and legacy behavior.
- [ ] Add stable conditions:

```text
planning_options.artifact
planning_options.shape
planning_options.selection
planning_options.recommendation
planning_options.materiality
```

- [ ] Append planning checks to existing G1 conditions without replacing artifact/FR/constitution/DB checks.
- [ ] Return `decision_selection_required` detail for valid unselected material decisions; malformed data uses fixed non-sensitive codes.
- [ ] Run:

```bash
uv run pytest tests/test_wf_speckit.py tests/test_planning_options.py -q
```

- [ ] Commit:

```bash
git add cli/src/awf/core/gates.py cli/tests/test_wf_speckit.py cli/tests/test_planning_options.py
git commit -m "feat(workflow): require planning selections at G1"
```

### Task 3: Persist selections and safely replan

**Files:**
- Modify: `cli/src/awf/core/planning_options.py`
- Modify: `cli/src/awf/cli.py`
- Modify: `cli/src/awf/commands/wf.py`
- Modify: `cli/src/awf/core/workflow_loop.py` only if a narrow public transition helper is required
- Test: `cli/tests/test_planning_options.py`
- Test: `cli/tests/test_wf_policy.py`
- Create or modify: `cli/tests/test_wf_select_option.py`

- [ ] Add parser tests for exact required `--decision-id`, `--option-id`, `--actor`, optional root/json.
- [ ] Add failing initial-selection tests: selection_required + Plan deciding → durable selection → Plan in_progress, no replan increment.
- [ ] Add failing post-G1 change tests: selected option changes → artifact history append → `replan_workflow(currentPhase, "plan")` → G1–G6 and G3 scope hash reset.
- [ ] Add no-op reuse and invalid ID/actor/state tests.
- [ ] Add failure-window tests where artifact publication succeeds but state transition fails; retry reconciles without duplicate history.
- [ ] Add symlink/hardlink, concurrent selection, owner/link-count, random temp, fsync/replace tests.
- [ ] Implement `select_planning_option()` with exact artifact validation before/after mutation and sanitized result containing only IDs, status, action, and hashes.
- [ ] Add `awf wf select-option` handler with exit 0 success/reuse, 1 validation/state blocker, 2 operational/usage.
- [ ] Run:

```bash
uv run pytest tests/test_planning_options.py tests/test_wf_select_option.py tests/test_wf_policy.py -q
```

- [ ] Commit:

```bash
git add cli/src/awf/core/planning_options.py cli/src/awf/cli.py cli/src/awf/commands/wf.py cli/src/awf/core/workflow_loop.py cli/tests/test_planning_options.py cli/tests/test_wf_select_option.py cli/tests/test_wf_policy.py
git commit -m "feat(workflow): select planning options and replan"
```

### Task 4: Align planner runtime contracts

**Files:**
- Modify: `claude/skills/phase-plan/SKILL.md`
- Modify: `claude/agents/spec-writer.md`
- Modify: `claude/skills/multi-agent/protocols/spec_writer.md`
- Modify: `claude/skills/wf-orchestrator/templates/agent-cards/plan.json`
- Modify: `claude/skills/wf-orchestrator/templates/agent-card.schema.json`
- Modify: `docs/reference/workflow-pipeline.md`
- Modify: `docs/architecture/wf-architecture.md`
- Modify: `cli/README.md`
- Modify: `CHANGELOG.md`
- Modify: generated `.omp/agents/spec-writer.md` and manifest only through canonical sync
- Test: `cli/tests/test_skill_contract_matrix.py`
- Test: `cli/tests/test_docs_semantic_audit.py`
- Test: `cli/tests/test_omp_agents.py`

- [ ] Add semantic RED tests for canonical artifact fields, materiality axes, 2–3 options, recommendation-first, work/transition risks, no-decision policy, selection escape, selected rerun, and no reversible-detail questions.
- [ ] Require plan card conditional planning-options output and `user_decision` escape while preserving DB decision artifacts.
- [ ] Give source spec-writer Ask capability only for material selections and sync generated OMP through `awf agents sync-omp`.
- [ ] Document `wf select-option`, initial continue, post-G1 replan, legacy compatibility, and artifact inventory.
- [ ] Validate examples with the real parser and canonical artifact loader rather than substring-only checks.
- [ ] Run:

```bash
uv run pytest tests/test_skill_contract_matrix.py tests/test_docs_semantic_audit.py tests/test_omp_agents.py -q
```

- [ ] Commit:

```bash
git add claude/skills/phase-plan/SKILL.md claude/agents/spec-writer.md claude/skills/multi-agent/protocols/spec_writer.md claude/skills/wf-orchestrator/templates/agent-cards/plan.json claude/skills/wf-orchestrator/templates/agent-card.schema.json docs/reference/workflow-pipeline.md docs/architecture/wf-architecture.md cli/README.md CHANGELOG.md .omp/agents/spec-writer.md .omp/agents/.awf-generated-agents.json cli/tests/test_skill_contract_matrix.py cli/tests/test_docs_semantic_audit.py cli/tests/test_omp_agents.py
git commit -m "docs(workflow): define planning option lifecycle"
```

### Task 5: Smoke, full verification, and review

**Files:**
- Add permanent lifecycle smoke to `cli/tests/test_wf_select_option.py` or an appropriate workflow runtime smoke file.
- Review all changes.

- [ ] Run a hermetic lifecycle:

```text
Plan writes selection_required
→ escaped user_decision
→ state deciding
→ wf select-option chooses a non-recommended option
→ Plan continues
→ selected artifact drives regenerated Plan outputs
→ G1 passes
→ change selection after G1
→ Plan reopens and G1–G6/G3 scope reset
```

- [ ] Run focused tests:

```bash
uv run pytest tests/test_planning_options.py tests/test_wf_select_option.py tests/test_wf_policy.py tests/test_wf_speckit.py tests/test_skill_contract_matrix.py tests/test_docs_semantic_audit.py tests/test_omp_agents.py -q
```

- [ ] Run the complete non-live suite:

```bash
uv run pytest -q
```

- [ ] Final review invariants:

```text
No material choice is silently selected.
No reversible detail pauses Plan.
Recommendation is first and concrete.
Selection is durable and auditable.
Changing selection invalidates all derived scope/gates.
Legacy active workflows remain compatible.
Artifact/state writes are symlink/hardlink/concurrency safe.
Source/generated planner contracts are identical.
```
