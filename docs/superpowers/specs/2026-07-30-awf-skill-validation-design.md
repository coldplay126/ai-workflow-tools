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

## 15. Subscription-Backed Live Validation Amendment

### 15.1 Problem

The live probes must use the operator's existing Claude, ChatGPT/Codex, and OMP subscriptions. Replacing `HOME` and every runtime config directory with empty temporary directories also hides the OAuth credential stores, so the previous isolated-host design cannot produce live evidence.

Authentication and Skill-source isolation are separate concerns:

- subscription credential stores may be read from their existing locations
- credential files must not be copied, linked, hashed, listed in reports, or modified directly by the validation harness
- candidate Skills must still come only from the immutable source snapshot installed into a temporary project workspace
- model sessions and mutation-capable tools remain disabled

API keys are not a supported fallback for this validation.

### 15.2 Shared live-run contract

At process start, the runner captures the effective subscription config locations from the parent environment without reading credential contents. A live host adapter may expose only the minimum location required for that host to authenticate.

Every live process must:

- run from a newly created temporary project workspace
- install only the immutable candidate Skill snapshot into the host's project-level Skill root
- use a fresh model context with session persistence disabled
- use a read-only sandbox where the host supports one
- expose no mutation-capable tool
- leave the credential store in place and perform no direct credential mutation
- persist only normalized auth diagnostics, never raw auth status output, tokens, account identifiers, or credential paths

The temporary workspace and Skill links are deleted after evidence is durably written. The original credential store and global Skill roots are never cleanup targets.

The provider runtimes may still rotate an OAuth token or write their normal auth and usage bookkeeping while authenticating. This design treats credentials as read-only harness inputs; it does not promise filesystem-level immutability of provider-owned stores. A requirement for byte-for-byte immutable credential storage would instead block subscription-backed runs until a separately configured broker or provider-supported read-only credential interface exists.

### 15.3 Host launch contracts

#### Claude

- Preserve the existing Claude subscription authentication context.
- Install the candidate snapshot under `<workspace>/.claude/skills/<skill>`.
- Start Claude in `<workspace>` with `--setting-sources project`.
- Use `--tools ""` and `--no-session-persistence`.
- Invoke the selected Skill explicitly as `/<skill>`.
- Claude slash invocation injects only the selected Skill body beginning at its H1; it does not inject YAML frontmatter. For every immutable Skill snapshot, create a separate regular metadata projection beneath the temporary validation root and outside the project Skill snapshot.
- Read and hash-verify the projection immediately before launch, then pass its verified JSON text through `--append-system-prompt <projection-text>` while retaining `--tools ""`; do not pass the temporary path, enable `Read`, or enable any other tool.
- The projection contains only the exact decoded `name` and `description` as deterministic JSON. It contains no body H1, credential data, or filesystem path data.
- Bind creation to the snapshotted `ExpectedSkill`: immediately verify the regular snapshot tree's source hash and its decoded name, description, and H1 before writing the projection. Symlink, nonregular, write, or hash failures are fail-closed.
- Immediately before and after every Claude process, re-read the projection as a regular file and require its byte hash to match the creation hash. A missing, unreadable, or changed projection is the harness-defect `FAIL` diagnostic `metadata_projection_changed`, never `PASS`, `BLOCKED`, or a raw provider error.
- Claude's prompt must copy `name` and `description` only from the injected metadata projection and must copy `body_heading` only from the selected slash Skill body, including its exact leading `# `.
- Reports record `--append-system-prompt` only as a safety flag; they never persist a projection path or projection body.
- Do not copy or link the existing Claude config directory into the workspace.

#### Agent Skills through Codex

- Set `HOME` to a temporary directory.
- Set `CODEX_HOME` to the effective pre-run Codex home, defaulting to `<original-home>/.codex` when no explicit value exists.
- Install the candidate snapshot under `<workspace>/.agents/skills/<skill>`.
- Start Codex in `<workspace>` with `--ephemeral`, `--sandbox read-only`, and `--skip-git-repo-check`.
- Use the ChatGPT-subscription model identifier `gpt-5.4`; do not pass the OMP registry identifier `openai-codex/gpt-5.6-sol` to Codex.
- Invoke the selected Skill explicitly as `$<skill>`.

#### OMP

- Set `HOME` to a temporary directory.
- Preserve only the effective existing `PI_CODING_AGENT_DIR` required for OMP subscription authentication.
- Install the candidate snapshot under `<workspace>/.omp/skills/<skill>`.
- Start OMP in `<workspace>` with `--no-session`, `--no-extensions`, and the pinned OMP model `openai-codex/gpt-5.6-sol`.
- Do not use the OMP auth broker or API-key environment variables for this validation.

### 15.4 OMP discovery and field execution

#### Claude discovery identity proof

Claude discovery proves identity from two immutable inputs: the injected metadata projection supplies decoded `name` and `description`, while the selected project-root slash Skill body supplies `body_heading`. The projection's creation-time snapshot-hash/metadata check and its pre/post-launch byte-hash checks bind those inputs to the same candidate without granting filesystem access to the model.

OMP omits the Skill discovery prompt when no read tool is available. Therefore one OMP command shape cannot prove both host discovery and no-tool field behavior.

OMP discovery probes must:

- allow only the `read` tool
- retain the single-Skill allowlist
- ask the host to load the selected project Skill and return the exact name, description, and first Markdown H1
- fail if any tool other than `read` is configured

OMP field pairs must:

- run both baseline and with-Skill arms with `--no-tools` and `--no-session`
- run the baseline with `--no-skills` and no Skill injection
- run the with-Skill arm with the immutable snapshot `SKILL.md` supplied through `--append-system-prompt`
- record the full snapshot `skill_sha256`, the injected `SKILL.md` file's `skill_file_sha256`, and `injection_sha256`; require `injection_sha256 == skill_file_sha256`
- fail closed if the injected file changes before launch or before evidence publication

This split proves real project-root discovery separately from behavioral use of the exact Skill snapshot while preventing model-initiated filesystem access during pressure scenarios.

### 15.5 Failure and reporting semantics

The host adapter returns `BLOCKED`, not `FAIL`, for an unavailable or expired subscription. Unsupported model identifiers, missing project Skill selection, unexpected tool exposure, credential-copy attempts, and Skill hash mismatches are harness defects and return `FAIL`.

Raw provider stderr is transient. Before any diagnostic is persisted, the runner maps it to an allowlisted reason code such as:

- `host_auth_unavailable`
- `host_subscription_expired`
- `host_model_unsupported`
- `host_timeout`
- `host_provider_exit`

Reports may record the host, model identifier, command safety flags, normalized reason code, exit status, elapsed time, and candidate Skill hashes. They must not record credential environment values, credential paths, auth-status JSON, provider account metadata, or unredacted stderr.

### 15.6 Required regression coverage

Focused tests must prove:

1. credential locations are referenced but never copied, linked, hashed, serialized, or directly mutated by harness code
2. each host runs from the temporary project workspace and selects the project Skill
3. Claude disables user settings and mutation tools while preserving subscription authentication
4. Claude uses a temporary regular metadata projection through `--append-system-prompt`, without `Read`; the fake live shape combines projection name/description with slash-body H1
5. Claude rejects a missing, unreadable, symlinked, nonregular, mutated, write-failed, or source-hash-unbound projection as `FAIL metadata_projection_changed` before or after process launch
6. Claude records only the append-system-prompt safety flag, never a projection path or body, and removes the projection with the temporary validation root
7. Codex receives `gpt-5.4`, an ephemeral session, a read-only sandbox, and the effective original `CODEX_HOME`
8. OMP discovery exposes only `read`
9. OMP baseline exposes neither Skills nor tools
10. OMP with-Skill injects the exact immutable `SKILL.md`, verifies its file hash, and separately preserves the full Skill snapshot hash
11. auth/provider errors are normalized before persistence
12. cleanup can remove only paths created beneath the temporary validation root
13. a changed source or snapshot between execution and publication blocks evidence

Subscription-backed smoke tests are workstation acceptance tests, not default CI tests. Deterministic CI continues to use fake hosts and temporary credential-free homes.

### 15.7 Acceptance evidence

After implementing this amendment, create a new batch identifier and rerun:

1. the deterministic fifteen-Skill audit
2. all 45 host discovery probes
3. all 27 OMP baseline/with-Skill pairs
4. the 135-cell evidence summary
5. the focused validation suites and complete AWF suite
6. independent specification-conformance and quality/safety reviews against one final commit

Prior `BLOCKED host_auth_unavailable` reports remain append-only historical evidence but must not be mixed into the new batch's aggregate verdict.
