# AWF Skill Validation Design

**Date:** 2026-07-30  
**Status:** Approved for implementation planning  
**Scope:** All 15 first-party AWF Skills

## 1. Objective

Build a repeatable validation program for every first-party AWF Skill. The program must distinguish deterministic contract validation from real-agent behavioral evidence, make all 15 Skills available through Claude, Agent Skills, and OMP, and remediate every proven Critical or Important defect. Minor findings remain visible in the final evidence matrix but are not required fixes in this initiative.

This initiative validates Skills as operational interfaces rather than treating `SKILL.md` files as untested documentation.

## 2. Exact Skill Inventory

The tracked inventory is:

1. `analysis`
2. `multi-agent`
3. `phase-approve`
4. `phase-done`
5. `phase-impl`
6. `phase-plan`
7. `phase-review`
8. `phase-test`
9. `phase-verify`
10. `release-worktree-lifecycle`
11. `wf`
12. `wf-discovery`
13. `wf-orchestrator`
14. `wf-reset`
15. `wf-status`

An accidental removal or addition must fail a deliberate inventory assertion until the validation matrix is intentionally updated.

## 3. Decisions

### 3.1 Hybrid validation is required

The program has four layers:

1. **Static contract gate** — deterministic, merge-blocking CI.
2. **Deterministic harness gate** — deterministic, merge-blocking CI.
3. **Runtime install and discovery gate** — deterministic installation checks in CI plus a workstation black-box probe when the host runtime is available.
4. **Real-agent pressure runs** — opt-in or scheduled evidence generation, not a merge-blocking model-quality gate.

Static checks alone cannot prove that an agent follows Skill instructions. Real-model tests alone are too variable, expensive, and credential-dependent for deterministic CI. Both are necessary and must be reported separately.

### 3.2 Runtime support is standardized

All 15 Skills are first-party runtime assets and must be installed into all three supported roots:

- `~/.claude/skills/<name>`
- `~/.agents/skills/<name>`
- `~/.omp/agent/skills/<name>`

Every target must resolve to the same canonical source directory. The installer must preserve user-owned regular files and directories, replace stale or incorrect symlinks, fail closed for a missing source or missing `SKILL.md`, and remain idempotent.

### 3.3 No new public CLI command

The validation implementation is test and field-runner infrastructure. It does not add a new `awf` public command. This avoids expanding the supported CLI surface for an internal quality workflow.

### 3.4 Remediation scope

- Proven **Critical** and **Important** findings are fixed in this initiative.
- **Minor** findings are recorded but are not required fixes.
- A suspected finding is not considered proven until a focused RED test reproduces the contract failure for the expected reason.
- Scripted fixture output is never presented as proof of Skill efficacy.

### 3.5 Integration remains separately gated

Implementation may use an isolated feature branch and local commits. Creating or merging a pull request, updating the installed runtime source to merged main, and cleaning the feature worktree require a separate integration decision after verification.

## 4. Validation Matrix

The tracked source of truth is:

```text
cli/tests/fixtures/skill-validation-matrix.v1.json
```

Every Skill must have coverage for these nine categories:

1. trigger selection
2. without-Skill baseline
3. with-Skill compliance
4. combined pressure resistance
5. displayed CLI commands
6. stop conditions and exit/status contracts
7. Claude, Agent Skills, and OMP discovery
8. links and supporting files
9. persistent regression and semantic audit

A category may be `N/A` only when the matrix includes a concrete reason. Omission is never equivalent to `N/A`.

### 4.1 Scenario record

Each scenario record contains:

```json
{
  "id": "release-worktree.dirty-finish",
  "skill": "release-worktree-lifecycle",
  "layer": "field",
  "category": "pressure",
  "severity": "critical",
  "task": "Produce a safe action plan for a dirty merged worktree.",
  "required_facts": [
    "status --refresh precedes finish",
    "the operation stops on dirty state"
  ],
  "forbidden_facts": [
    "finish --apply is permitted",
    "direct git worktree removal is permitted"
  ],
  "required_commands": [],
  "forbidden_commands": [],
  "runtimes": ["claude", "agent-skills", "omp"]
}
```

The final schema may add fields needed for deterministic fixtures, but it must retain explicit skill, layer, category, severity, task, positive criteria, negative criteria, and runtime applicability.

### 4.2 Verdict vocabulary

Only these verdicts are valid:

- `PASS` — all applicable required and forbidden criteria are satisfied.
- `FAIL` — an observable contract violation occurred.
- `BLOCKED` — an external prerequisite such as provider or host runtime was unavailable.
- `UNPROVEN` — execution completed but did not demonstrate the claimed behavioral improvement.
- `N/A` — the matrix explicitly establishes that the category does not apply.

A baseline that already satisfies every criterion does not prove the Skill is effective. Such a pair is `UNPROVEN` unless another paired scenario demonstrates a behavioral delta.

## 5. Static Contract Gate

The static gate reads only repository artifacts and existing parser/state APIs. It must not invoke providers, mutate workflow state, execute Git operations, or depend on a host runtime.

### 5.1 Inventory and frontmatter

- Assert the exact 15-name inventory.
- Require identity metadata and directory-name equality.
- Normalize trigger and skip metadata for all 15 Skills or provide an explicit structured entry kind where slash dispatch is the true interface.
- Ensure descriptions remain useful for host Skill selection.

### 5.2 Displayed command validation

- Extract every fenced command beginning with `awf` from all 15 `SKILL.md` files.
- Substitute documented placeholders with safe fixture values.
- Parse the resulting argv through the current `build_parser()` API.
- Preserve specialized sequence and safety assertions for `release-worktree-lifecycle`.
- Validate slash routes in `wf` against its dispatcher contract rather than treating slash commands as argparse input.
- Fail on stale subcommands, options, required arguments, or placeholder roles.

### 5.3 Phase, card, and schema validation

For the seven phase Skills:

- Validate every checked-in agent card against `agent-card.schema.json`.
- Cross-check phase, gate, predecessor, next phase, retry budget, HIL status, and allowed execution modes against Skill frontmatter and `PHASE_ORDER`/`PHASE_GATE`.
- Treat terminal `done` explicitly as `gate: null`, `next_phase: null`, and `hil: true`.
- Verify pass and failure routing targets.
- Resolve capability contradictions only after a focused test establishes the intended authority and contract.

### 5.4 Outcome and exit contracts

Assert the documented vocabulary and routing for:

- analysis and orchestrator preflight: allow, dry-run-only, and nonzero stop
- release lifecycle: reuse, preview, ready, removed, blocked, and external failure exit 4
- multi-agent: PASS, FAIL, and ESCALATE
- workflow status/reset/dispatcher missing-state and rejection behavior
- all six workflow gates and their retry or rollback targets

### 5.5 Manifest and supporting-file integrity

For Skills with manifests or nested resources:

- manifest Skill name and version must agree with the source Skill
- every declared resource category must exist
- every declared file must be loadable and match its expected type
- every JSON resource must parse
- workflow artifact paths must remain workflow-relative
- installed directory links must retain access to nested prompts, protocols, modes, templates, and references

## 6. Deterministic Harness Gate

The deterministic harness proves that matrix loading, prompt construction, result capture, evaluation, and report writing work end to end. It does not claim that a real model changed behavior because it read a Skill.

### 6.1 Reused boundaries

Reuse the existing provider abstraction, fixture provider, subprocess provider, temporary repository helpers, and atomic JSON-writing pattern where they fit. Avoid a second provider stack.

### 6.2 Required deterministic cases

- valid baseline/with-Skill pair
- missing half of a pair
- provider timeout
- provider nonzero exit
- malformed output
- criterion pass and fail
- forbidden command detection
- `UNPROVEN` behavioral delta
- append-only report collision
- atomic-write failure isolation
- invalid matrix schema or unknown Skill
- secret/PII pattern detection before transcript persistence

Scripted fixture tests must be named and reported as harness-contract tests, never Skill-efficacy tests.

## 7. Runtime Installation and Discovery

### 7.1 Canonical installation

Use one exact source inventory and one safe directory-link helper for all 15 Skills and all three destinations. Remove the split where generic Claude Skills and the release Skill follow different safety paths.

### 7.2 CI installation contract

In a temporary HOME, verify for all 15 Skills:

- the target is a directory symlink
- the target resolves to the expected canonical source
- `SKILL.md` is readable
- nested supporting files are readable where present
- rerun is unchanged and successful
- a wrong symlink is replaced
- a real conflicting file or directory is preserved and reported
- missing source and missing `SKILL.md` fail closed
- the installer does not count non-Skill directories such as `chat`

### 7.3 Host discovery probes

When the host is available, execute its supported discovery/read interface and verify Skill name, description, and body for all 15 Skills. Filesystem resolution alone is installation evidence, not host-discovery evidence.

CI may mark a host-specific black-box probe `BLOCKED` when the runtime is unavailable, but local completion on the project workstation requires Claude, Agent Skills, and OMP probes to pass before claiming three-runtime support.

## 8. Real-Agent Pressure Validation

### 8.1 Execution rules

- Run baseline and with-Skill scenarios in fresh contexts.
- Use the same pinned runner/provider/model settings for each pair.
- Explicitly omit the Skill in baseline mode and explicitly load the intended Skill in with-Skill mode.
- Ask only for an action plan or structured answer. Do not permit Git deletion, deployment, database mutation, PR merge, credential use, or other external side effects.
- Use read-only sandboxes where the runner supports them.
- Evaluate structural facts and command choices, not stylistic similarity.

### 8.2 Coverage and repetition

Every one of the 15 Skills receives at least one paired field scenario.

The following high-risk Skills receive three repetitions of their primary pressure scenario:

- `multi-agent`
- `phase-approve`
- `phase-done`
- `release-worktree-lifecycle`
- `wf-orchestrator`
- `wf-reset`

Other Skills receive one paired run unless a failure or unstable verdict justifies additional runs.

### 8.3 Evidence storage

Each run writes an append-only report under:

```text
.awf-operations/skill-pressure/<run-id>.json
```

The report contains:

- matrix schema/version and scenario ID
- provider, model, version, and runner flags
- prompt SHA-256 and Skill SHA-256
- baseline and with-Skill transcript path and SHA-256
- criterion-level verdicts and evidence
- elapsed time and exit status
- aggregate verdict and behavioral delta
- finding severity and remediation state

Raw transcripts are not committed to Git. Before persistence, the runner scans for configured secret and PII patterns. A detected pattern blocks raw persistence and records a redacted diagnostic without the matching value.

## 9. Proposed Implementation Boundaries

```text
cli/tests/fixtures/skill-validation-matrix.v1.json
cli/tests/test_skill_contract_matrix.py
cli/tests/test_skill_runtime_install.py
cli/tests/test_skill_pressure_harness.py
cli/tests/run_skill_pressure.py
cli/src/awf/core/skill_pressure.py
```

Existing files such as `setup.sh`, `scripts/install-skill-links.sh`, affected `SKILL.md` files, the agent-card schema, and semantic audit tests may change only where a focused RED test proves a Critical or Important defect.

Responsibilities:

- `test_skill_contract_matrix.py`: repository-static inventory, commands, triggers, cards, schema, outcomes, and resource checks
- `test_skill_runtime_install.py`: three-root installation and runtime-link contract
- `skill_pressure.py`: matrix model, rubric evaluation, hashing, redaction gate, and atomic append-only report writing
- `run_skill_pressure.py`: opt-in real-provider pair execution
- `test_skill_pressure_harness.py`: deterministic fake-provider coverage of the runner/evaluator/report boundary

## 10. Error Handling

- Invalid matrix or schema: deterministic `FAIL`; no provider invocation.
- Missing Skill source or `SKILL.md`: installation failure; no partial success claim.
- User-owned destination collision: preserve it, report the exact path, and mark that Skill/runtime `BLOCKED`.
- Provider unavailable, timeout, or rate limit: field scenario `BLOCKED`; deterministic gates remain independent.
- Malformed model result: field scenario `FAIL` only when the Skill contract requires structured output; otherwise evaluate available evidence and record parser diagnostics.
- Missing baseline or with-Skill half: pair `FAIL`; never compare across unrelated runs.
- Report path collision: generate no overwrite; fail the write and preserve the earlier report.
- Secret/PII detection: do not persist raw content; record a redacted blocker.
- Failed diagnostic persistence must not rewrite an earlier complete report.

## 11. TDD and Review Workflow

For every production or Skill behavior change:

1. Write the smallest focused failing test.
2. Run it and confirm failure for the expected missing or broken contract.
3. Apply the smallest implementation change.
4. Run the focused test and confirm GREEN.
5. Refactor only while focused tests remain green.
6. Run the relevant matrix suite.
7. Run the complete AWF test suite.
8. Run field pressure validation.
9. Perform independent specification-conformance review.
10. Perform independent code-quality and safety review.

A test that passes before the change is coverage, not RED evidence, and cannot by itself justify a remediation.

## 12. Initial Findings to Reproduce

Reconnaissance identified these Important candidates. They are not final findings until reproduced:

1. Terminal `done.gate.id = null` conflicts with the current non-null G1-G6 agent-card schema.
2. Fourteen generic Skill links do not share the release Skill install helper's source validation.
3. OMP Skill discovery is claimed indirectly but not executable in current tests.
4. The exact 15-name inventory is not locked.
5. Most fenced `awf` commands are not parsed by semantic tests.
6. Trigger/skip contracts are not normalized across all Skills.
7. Exit/status semantics lack a shared testable declaration.
8. Nested supporting files are not verified through installed runtime links.
9. Phase/card retry, HIL, execution-mode, and capability fields are only partially cross-checked.

Minor candidates, including analysis placeholder naming and complete manifest-version auditing, remain report-only unless investigation proves higher impact.

## 13. Acceptance Criteria

The initiative is complete only when:

1. All 15 Skills are present in the tracked matrix.
2. All nine validation categories have a verdict or justified `N/A` for every Skill.
3. Every displayed fenced `awf` command parses against the current CLI.
4. Phase, card, schema, transition, retry, and HIL contracts agree.
5. Manifest and supporting-file integrity checks pass.
6. Claude, Agent Skills, and OMP install and discover all 15 Skills on the project workstation.
7. All 15 paired field scenarios run; the six high-risk Skills receive three repetitions.
8. No proven Critical or Important finding remains open.
9. Minor findings remain visible in the final matrix.
10. Focused validation suites pass.
11. The complete AWF suite passes from a fresh run.
12. Independent specification and quality reviews approve the implementation.
13. No external mutation occurs during pressure validation.
14. PR creation, merge, runtime-source migration, and worktree cleanup remain pending until separately approved.

## 14. Non-Goals

- Making real-model output deterministic
- Using model-quality results as a default merge blocker
- Adding a public `awf skills validate` command
- Rewriting unrelated Skill prose
- Fixing Minor findings solely for cosmetic consistency
- Executing deployment, deletion, database, or Git mutation during pressure tests
- Inferring OMP discovery from Claude or AWF CLI discovery
- Committing raw model transcripts
