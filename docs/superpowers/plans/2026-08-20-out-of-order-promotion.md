# Out-of-Order Promotion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit, auditable promotion mode that reconstructs one reviewed staging PR on production without earlier staging changes and preserves a managed recovery path for real patch conflicts.

**Architecture:** Keep exact promotion as the default. `--out-of-order` creates a distinct managed lease, persists source/target/path provenance before patch application, validates the synthetic production delta against reviewed paths, and requires production verification. A failed three-way apply leaves a blocked unpublished lease; the same preview/apply request validates and finalizes edits made only to AWF-reported conflict files.

**Tech Stack:** Python 3.9+, argparse, frozen dataclasses, SQLite, Git CLI, GitHub CLI adapter, pytest.

---

## File map

- `cli/src/awf/worktrees/models.py`: promotion and resolution enums plus durable lease fields, JSON shape, and protected clean-apply index-entry pins.
- `cli/src/awf/worktrees/registry.py`: backward-compatible SQLite migration, protected-index-entry serialization, inserts, CAS transitions, and row mapping.
- `cli/src/awf/worktrees/git.py`: structured three-way patch conflicts, index blob snapshots, unmerged-path inspection, and AWF-owned staging.
- `cli/src/awf/cli.py`: `--out-of-order` parser surface.
- `cli/src/awf/commands/wt.py`: CLI-to-service forwarding.
- `cli/src/awf/worktrees/service.py`: validation, lease identity, automatic synthetic promotion, conflict preservation, protected-index-entry pinning, resolution preview/apply, provenance, and publication.
- `cli/tests/test_worktree_registry.py`: new and legacy registry contracts, protected-index-entry migration, and round trips.
- `cli/tests/test_worktree_git.py`: real Git conflict and staging contracts.
- `cli/tests/test_worktree_commands.py`: parser and forwarding contracts.
- `cli/tests/test_worktree_service.py`: automatic and manual out-of-order behavior, protected-index-entry pin validation, and exact-mode regressions.
- `cli/tests/test_release_worktree_smoke.py`: end-to-end divergent staging/production scenario.
- `cli/tests/test_docs_semantic_audit.py`: lifecycle command and decision-table semantics.
- `claude/skills/release-worktree-lifecycle/SKILL.md`: canonical operator procedure.
- `cli/src/awf/resources/release-worktree-lifecycle/SKILL.md`: byte-identical packaged Skill.
- `cli/README.md`: CLI usage and blockers.
- `CHANGELOG.md`: user-visible feature and safety constraints.

### Task 1: Persist promotion mode and conflict provenance

**Files:**
- Modify: `cli/src/awf/worktrees/models.py:31-126`
- Modify: `cli/src/awf/worktrees/registry.py:21-135,222-304,653-707`
- Test: `cli/tests/test_worktree_registry.py`

- [ ] **Step 1: Write failing model and registry tests**

Add enum imports, extend the test lease factory with explicit defaults, and add this round-trip contract:

```python
def test_registry_round_trips_out_of_order_provenance(tmp_path: Path) -> None:
    registry = WorktreeRegistry(tmp_path / "worktrees.sqlite3")
    created = replace(
        lease(tmp_path),
        promotion_mode=PromotionMode.OUT_OF_ORDER,
        resolution_state=ResolutionState.PENDING,
        source_base_sha="a" * 40,
        source_head_sha="b" * 40,
        target_base_sha="c" * 40,
        reviewed_paths=("src/a.py", "src/b.py"),
        conflicted_paths=("src/b.py",),
        protected_index_entries=(("src/a.py", ("100644", "d" * 40)),),
    )

    registry.create_lease(created)

    assert registry.get_lease(created.id) == created
    assert created.to_dict()["promotion_mode"] == "out_of_order"
    assert created.to_dict()["resolution_state"] == "pending"
    assert created.to_dict()["reviewed_paths"] == ["src/a.py", "src/b.py"]
    assert created.to_dict()["protected_index_entries"] == [
        {"path": "src/a.py", "mode": "100644", "blob_oid": "d" * 40}
    ]

Create a legacy `worktree_leases` table with the pre-feature columns, insert one exact lease row, call `registry.ensure()`, and assert the row loads with:

```python
assert loaded.promotion_mode is PromotionMode.EXACT
assert loaded.resolution_state is ResolutionState.NONE
assert loaded.source_base_sha is None
assert loaded.source_head_sha is None
assert loaded.target_base_sha is None
assert loaded.reviewed_paths == ()
assert loaded.conflicted_paths == ()
assert loaded.protected_index_entries == ()

Add a CAS transition test that changes `resolution_state`, `conflicted_paths`, and `protected_index_entries` and survives a reload.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
cd cli
uv run pytest tests/test_worktree_registry.py -q
```

Expected: collection or assertion failures because the enums, lease fields, migration, and transition parameters do not exist.

- [ ] **Step 3: Add enums and lease fields**

In `models.py`, add:

```python
class PromotionMode(str, Enum):
    EXACT = "exact"
    OUT_OF_ORDER = "out_of_order"


class ResolutionState(str, Enum):
    NONE = "none"
    PENDING = "pending"
    AUTOMATIC = "automatic"
    MANUAL_REVIEWED = "manual_reviewed"
```

Add these fields to `Lease` and `Lease.new()`:

```python
promotion_mode: PromotionMode
resolution_state: ResolutionState
source_base_sha: str | None
source_head_sha: str | None
target_base_sha: str | None
reviewed_paths: tuple[str, ...]
conflicted_paths: tuple[str, ...]
protected_index_entries: tuple[tuple[str, tuple[str, str] | None], ...]
```

Use exact/none/`None`/empty-tuple defaults in `Lease.new()`. Convert enum values, path tuples, and sorted protected path→stage-0 mode+blob-OID-or-null entries to additive JSON output.

- [ ] **Step 4: Add idempotent SQLite migration and mappings**

Extend `_SCHEMA` with checked mode/state columns, nullable SHA columns, JSON text path columns, and protected index-entry metadata. In `ensure()`, after `executescript`, inspect `PRAGMA table_info(worktree_leases)` and conditionally execute these additions for existing databases:

```python
migrations = {
    "promotion_mode": (
        "ALTER TABLE worktree_leases ADD COLUMN promotion_mode "
        "TEXT NOT NULL DEFAULT 'exact'"
    ),
    "resolution_state": (
        "ALTER TABLE worktree_leases ADD COLUMN resolution_state "
        "TEXT NOT NULL DEFAULT 'none'"
    ),
    "source_base_sha": "ALTER TABLE worktree_leases ADD COLUMN source_base_sha TEXT",
    "source_head_sha": "ALTER TABLE worktree_leases ADD COLUMN source_head_sha TEXT",
    "target_base_sha": "ALTER TABLE worktree_leases ADD COLUMN target_base_sha TEXT",
    "reviewed_paths": (
        "ALTER TABLE worktree_leases ADD COLUMN reviewed_paths "
        "TEXT NOT NULL DEFAULT '[]'"
    ),
    "conflicted_paths": (
        "ALTER TABLE worktree_leases ADD COLUMN conflicted_paths "
        "TEXT NOT NULL DEFAULT '[]'"
    ),
    "protected_index_entries": (
        "ALTER TABLE worktree_leases ADD COLUMN protected_index_entries "
        "TEXT NOT NULL DEFAULT '[]'"
    ),
}
```

Use JSON path arrays and sorted protected path→stage-0 mode+blob-OID-or-null entries on write, and validate decoded values on read. Reject malformed registry data with `ValueError`; do not silently discard it.

Extend `transition()` with optional `resolution_state`, `conflicted_paths`, and `protected_index_entries` arguments. Update only fields explicitly supplied, preserving CAS semantics.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_worktree_registry.py -q
```

Expected: all registry tests pass, including legacy migration.

- [ ] **Step 6: Commit**

```bash
git add cli/src/awf/worktrees/models.py cli/src/awf/worktrees/registry.py cli/tests/test_worktree_registry.py
git commit -m "feat(worktrees): persist promotion conflict provenance"
```

### Task 2: Report and stage real patch conflicts safely

**Files:**
- Modify: `cli/src/awf/worktrees/git.py:15-20,79-81,272-315`
- Test: `cli/tests/test_worktree_git.py`

- [ ] **Step 1: Write failing real-Git tests**

Create divergent branches that edit the same line. Generate a binary patch from the source base/head, apply it to the conflicting target worktree, and assert:

```python
with pytest.raises(GitPatchConflict) as caught:
    client.apply_indexed_patch(worktree, patch)

assert caught.value.paths == ("shared.txt",)
assert client.unmerged_paths(worktree) == ("shared.txt",)
assert any(
    entry.endswith("shared.txt") for entry in client.status_porcelain(worktree)
)
```

Then write a resolved `shared.txt`, call `client.stage_paths(worktree, ("shared.txt",))`, and assert `unmerged_paths()` is empty while the file remains staged.

- [ ] **Step 2: Run the tests and verify RED**

```bash
uv run pytest tests/test_worktree_git.py -k 'patch_conflict or stage_paths' -q
```

Expected: import or attribute failures for the new exception and methods.

- [ ] **Step 3: Implement the Git boundary**

Add:

```python
class GitPatchConflict(GitError):
    def __init__(self, paths: tuple[str, ...], detail: str) -> None:
        super().__init__(detail)
        self.paths = paths
```

Implement:

```python
def unmerged_paths(self, cwd: Path) -> tuple[str, ...]:
    completed = self._run(
        "diff", "--name-only", "--diff-filter=U", "-z", cwd=cwd
    )
    return tuple(sorted(_nul_records(completed.stdout)))


def stage_paths(self, cwd: Path, paths: tuple[str, ...]) -> None:
    if not paths:
        raise GitError("at least one path is required for staging")
    self._run("add", "--", *paths, cwd=cwd)
```

Wrap `apply_indexed_patch()`:

```python
try:
    self._run("apply", "--3way", "--index", "-", cwd=cwd, input_bytes=patch)
except GitError as error:
    paths = self.unmerged_paths(cwd)
    if paths:
        raise GitPatchConflict(paths, str(error)) from error
    raise
```

Do not parse localized stderr to discover paths.

- [ ] **Step 4: Run Git tests and verify GREEN**

```bash
uv run pytest tests/test_worktree_git.py -q
```

Expected: all Git worktree tests pass.

- [ ] **Step 5: Commit**

```bash
git add cli/src/awf/worktrees/git.py cli/tests/test_worktree_git.py
git commit -m "feat(worktrees): preserve structured patch conflicts"
```

### Task 3: Add the explicit CLI mode and request identity

**Files:**
- Modify: `cli/src/awf/cli.py:693-734`
- Modify: `cli/src/awf/commands/wt.py:128-138`
- Modify: `cli/src/awf/worktrees/service.py:210-315,2941-3159`
- Test: `cli/tests/test_worktree_commands.py:506-539`
- Test: `cli/tests/test_worktree_service.py`

- [ ] **Step 1: Write failing parser and validation tests**

Extend `test_wt_promote_parser_surface` to include `--out-of-order` and assert `args.out_of_order is True`. Add a forwarding test by monkeypatching `WorktreeService.promote` and asserting the received keyword is `out_of_order=True`.

Add service tests:

```python
@pytest.mark.parametrize(
    ("source_pr", "exclude_paths"),
    [((372, 373), ()), (372, ("feature.txt",))],
)
def test_promote_rejects_invalid_out_of_order_combinations(
    promotion_harness: PromotionHarness,
    source_pr: int | tuple[int, ...],
    exclude_paths: tuple[str, ...],
) -> None:
    result = promotion_harness.service.promote(
        source_pr=source_pr,
        exclude_paths=exclude_paths,
        target_branch="main",
        out_of_order=True,
        apply=False,
    )
    assert result.blockers[0]["code"] == "invalid_out_of_order_promotion"
```

Assert exact and out-of-order initiatives differ for the same source and target.

- [ ] **Step 2: Run tests and verify RED**

```bash
uv run pytest tests/test_worktree_commands.py::test_wt_promote_parser_surface tests/test_worktree_service.py -k 'out_of_order_combination or promotion_initiative' -q
```

Expected: parser/signature failures.

- [ ] **Step 3: Add parser and command forwarding**

Add a `store_true` parser argument:

```python
wt_promote_parser.add_argument(
    "--out-of-order",
    action="store_true",
    help=(
        "Reconstruct one reviewed staging PR on production without earlier "
        "staging changes. Requires separate production review."
    ),
)
```

Forward `out_of_order=args.out_of_order` in `run_wt_promote()`.

- [ ] **Step 4: Normalize mode and validate combinations**

Add `out_of_order: bool = False` to `WorktreeService.promote()`. Derive:

```python
promotion_mode = (
    PromotionMode.OUT_OF_ORDER if out_of_order else PromotionMode.EXACT
)
```

Before GitHub lookup, reject out-of-order requests unless there is exactly one source and no exclusions. Keep `_promotion_sources_blocker()` unchanged for exact multi-source requests.

Extend `_promotion_initiative()` with `promotion_mode`; append `-out-of-order` only for the new mode. Pass the mode through `_new_promotion_lease()` and set the out-of-order source/base/head/target/reviewed-path fields before registry creation.

Add `promotion_mode` to preview action JSON.

- [ ] **Step 5: Run tests and verify GREEN**

```bash
uv run pytest tests/test_worktree_commands.py tests/test_worktree_service.py -k 'parser_surface or forwarding or out_of_order_combination or promotion_initiative' -q
```

Expected: selected tests pass; existing exact parser behavior remains valid.

- [ ] **Step 6: Commit**

```bash
git add cli/src/awf/cli.py cli/src/awf/commands/wt.py cli/src/awf/worktrees/service.py cli/tests/test_worktree_commands.py cli/tests/test_worktree_service.py
git commit -m "feat(worktrees): add out-of-order promotion mode"
```

### Task 4: Promote a clean synthetic delta without earlier staging content

**Files:**
- Modify: `cli/src/awf/worktrees/service.py:336-608,3703-3820`
- Test: `cli/tests/test_worktree_service.py:319-534,3379-3910`

- [ ] **Step 1: Add a divergent-history harness helper and failing test**

Add a helper that places A and B changes in the same file on staging while main remains at S. B's reviewed base/head must be `S+A` and `S+A+B`, with A and B on non-overlapping lines.

Test exact mode first:

```python
exact = harness.service.promote(
    source_pr=source.number,
    target_branch="main",
    apply=True,
)
assert exact.blockers[0]["code"] == "promotion_content_mismatch"
```

Use a fresh harness for out-of-order mode:

```python
result = harness.service.promote(
    source_pr=source.number,
    target_branch="main",
    out_of_order=True,
    apply=True,
)
assert result.decision == "ready"
assert result.lease is not None
assert result.lease.promotion_mode is PromotionMode.OUT_OF_ORDER
assert result.lease.resolution_state is ResolutionState.AUTOMATIC
assert harness.git.path_blob(result.lease.head_sha, "shared.txt") == expected_b_only_blob
assert "A content" not in (result.lease.worktree_path / "shared.txt").read_text()
assert "B content" in (result.lease.worktree_path / "shared.txt").read_text()
```

Assert production verification ran and exactly one PR was created.

- [ ] **Step 2: Run the new test and verify RED**

```bash
uv run pytest tests/test_worktree_service.py -k 'out_of_order and clean' -q
```

Expected: exact mode blocks as before; out-of-order mode still returns `promotion_content_mismatch` or lacks automatic provenance.

- [ ] **Step 3: Split exact blob validation from synthetic path validation**

Keep patch-path equality before application. After commit:

```python
def validate_promotion_delta():
    if promotion_mode is PromotionMode.EXACT:
        if promoted_paths != expected_paths:
            return self._block_promotion_lease(
                lease,
                "promotion_delta_mismatch",
                "promotion paths do not exactly match the reviewed pull requests",
            )
        if any(
            expected_blobs[path] != self.git.path_blob(promotion_head, path)
            for path in expected_paths
        ):
            return self._block_promotion_lease(
                lease,
                "promotion_content_mismatch",
                "promotion contents do not exactly match the reviewed pull requests",
            )
    else:
        reviewed = set(lease.reviewed_paths)
        if not promoted_paths or not set(promoted_paths).issubset(reviewed):
            return self._block_promotion_lease(
                lease,
                "promotion_delta_mismatch",
                "out-of-order promotion changed paths outside the reviewed source",
            )
```

Transition a clean out-of-order lease to `ResolutionState.AUTOMATIC` before publication. Continue through the existing prepare, production verification, live-target recheck, push, find-or-create PR, and `PR_OPEN` flow.

- [ ] **Step 4: Add mode-aware provenance**

For out-of-order mode only, prepend/append strict trailers:

```text
AWF-Promotion-Mode: out-of-order
AWF-Resolution: automatic
```

Update `_promotion_trailers()`, `_promotion_message()`, `_promotion_body()`, and strict provenance parsing. Exact mode must emit and parse the existing byte-for-byte trailer shape. Reuse must compare `lease.promotion_mode` with the request before reading commit provenance.

- [ ] **Step 5: Run focused and exact regression tests**

```bash
uv run pytest tests/test_worktree_service.py -k 'promote and (out_of_order or content_mismatch or ordered_multi_source or provenance)' -q
```

Expected: clean out-of-order succeeds; exact content mismatch and ordered-chain behavior remain unchanged.

- [ ] **Step 6: Commit**

```bash
git add cli/src/awf/worktrees/service.py cli/tests/test_worktree_service.py
git commit -m "feat(worktrees): reconstruct reviewed deltas on production"
```

### Task 5: Preserve and resume managed conflict resolution

**Files:**
- Modify: `cli/src/awf/worktrees/service.py:317-608,3164-3693,3933-3955`
- Test: `cli/tests/test_worktree_service.py:3881-3910,4117-5319`

- [ ] **Step 1: Write failing conflict-preservation tests**

Use a source patch and main target that edit the same line. Assert the first apply returns:

```python
assert result.status == "blocked"
assert result.blockers[0]["code"] == "out_of_order_conflict"
assert result.lease is not None
assert result.lease.state is LeaseState.BLOCKED
assert result.lease.resolution_state is ResolutionState.PENDING
assert result.lease.conflicted_paths == ("shared.txt",)
assert result.lease.target_pr is None
assert harness.git.remote_branch_sha(result.lease.branch) is None
```

Assert exact mode still uses the existing generic blocked path and never becomes manually resolvable.

- [ ] **Step 2: Write failing preview/apply recovery tests**

Edit only the returned conflict file, without running Git commands. Repeat the preview request and expect a read-only action sequence:

```python
assert tuple(action["kind"] for action in preview.actions) == (
    "resolve_out_of_order_conflict",
    "stage_paths",
    "commit",
    "verify_production",
    "push_branch",
    "open_pull_request",
)
```

Repeat with `--apply` and assert:

```python
assert resumed.decision == "ready"
assert resumed.lease.id == blocked.lease.id
assert resumed.lease.resolution_state is ResolutionState.MANUAL_REVIEWED
assert resumed.lease.state is LeaseState.PR_OPEN
assert "AWF-Resolution: manual-reviewed" in harness.github.created[0]["body"]
```

Add blockers for operator unstaged or unmerged paths outside `conflicted_paths`, a remaining unmerged or unstaged entry after AWF staging, an exact protected-index-entry pin mismatch after direct index, chmod, or file-type mode tampering, current source base/head differing from persisted provenance, current target SHA differing from `target_base_sha`, an empty net production delta, a conflict marker, a published remote branch, or an existing target PR. Protected-index-entry tampering returns `promotion_resolution_scope_mismatch`; the marker guard must allow valid trailing whitespace.

- [ ] **Step 3: Run tests and verify RED**

```bash
uv run pytest tests/test_worktree_service.py -k 'out_of_order and (conflict or resolution)' -q
```

Expected: the first conflict is still a generic `promotion_apply_failed`, and no recovery path exists.

- [ ] **Step 4: Preserve structured conflicts**

Catch `GitPatchConflict` separately during new out-of-order application. Transition the lease once with:

```python
resolution_state=ResolutionState.PENDING
conflicted_paths=error.paths
protected_index_entries=git.index_entry_snapshot(
    worktree,
    reviewed_paths_outside(error.paths),
)
event_type="promotion_blocked"
summary="out_of_order_conflict: reviewed patch requires managed resolution"
```

Return the blocked lease and a concise message listing the paths. Persist the clean-applied reviewed stage-0 mode and blob OID entries outside `conflicted_paths`; do not reset, commit, push, or create a PR.

- [ ] **Step 5: Add read-only resolution preview**

Before normal preview returns, look up the exact active out-of-order initiative through a read-only registry path. If it is a pending blocked lease, call `_out_of_order_resolution_preview()`.

The helper must verify request identity, persisted source/target SHAs, registered worktree path, absent remote branch/PR, operator unstaged/unmerged scope, and exact protected index entries. It returns, without mutation, the planned action order: resolve the reported conflict, stage exactly `conflicted_paths`, commit, run production verification, push the managed branch, and open the target PR.

- [ ] **Step 6: Add apply recovery**

In `_reuse_promotion()`, branch on pending out-of-order resolution before reading a promotion commit message. `_resume_out_of_order_conflict()` must:

1. repeat every preview guard under the repository lock;
2. require operator unstaged and unmerged paths to be subsets of `lease.conflicted_paths`;
3. require AWF-pinned clean-applied reviewed stage-0 mode and blob OID entries to match on preview, apply, and retry;
4. stage exactly `lease.conflicted_paths` through `GitClient.stage_paths()`;
5. require no unmerged or unstaged resolution entry to remain;
6. reject staged and committed conflict markers while allowing valid trailing whitespace;
7. verify that the final indexed target-to-head paths are non-empty and a subset of `lease.reviewed_paths`;
8. commit using `AWF-Resolution: manual-reviewed`;
9. run prepare and production verification, then recheck the live target SHA before publication;
10. transition to publish pending with `MANUAL_REVIEWED` and use the existing idempotent publication helper.

Any failure stays BLOCKED and preserves the worktree. Target/source drift must not reset or transplant the resolution.

- [ ] **Step 7: Run recovery and publication regressions**

```bash
uv run pytest tests/test_worktree_service.py -k 'promote and (out_of_order or reconcile or recover or provenance)' -q
```

Expected: all selected tests pass, including exactly-one-PR recovery behavior.

- [ ] **Step 8: Commit**

```bash
git add cli/src/awf/worktrees/service.py cli/tests/test_worktree_service.py
git commit -m "feat(worktrees): resume reviewed promotion conflicts"
```

### Task 6: Encode the operator contract and smoke scenario

**Files:**
- Modify: `claude/skills/release-worktree-lifecycle/SKILL.md`
- Modify: `cli/src/awf/resources/release-worktree-lifecycle/SKILL.md`
- Modify: `cli/README.md`
- Modify: `CHANGELOG.md`
- Modify: `cli/tests/test_docs_semantic_audit.py`
- Modify: `cli/tests/test_release_worktree_smoke.py`

- [ ] **Step 1: Write failing semantic and smoke tests**

Extend the lifecycle JSON contract with commands for out-of-order preview/apply and resolution preview/apply. Assert:

```python
assert "--out-of-order" in commands["out_of_order_promote_preview"]
assert "--apply" not in commands["out_of_order_promote_preview"]
assert "--apply" in commands["out_of_order_promote_apply"]
assert contract["safety"]["out_of_order"]["single_source"] is True
assert contract["safety"]["out_of_order"]["production_pr_review"] == "required"
assert contract["safety"]["out_of_order"]["direct_cherry_pick"] == "forbidden"
```

Add a smoke test that proves a same-file B change reaches the managed production branch while A remains absent. Assert the production PR body contains mode, source/base/head/target, resolution, and lease trailers.

- [ ] **Step 2: Run tests and verify RED**

```bash
uv run pytest tests/test_docs_semantic_audit.py tests/test_release_worktree_smoke.py -q
```

Expected: missing contract keys and unsupported CLI mode.

- [ ] **Step 3: Update both Skill copies identically**

Document:

- feature-flag path when A code may ship inactive;
- code-isolated single-source `--out-of-order` preview/apply;
- initial preview provenance including `reviewed_paths`;
- managed file-only conflict editing, staged-delta boundaries, marker-only validation, and exact command replay;
- ordered resolution preview actions: resolve, stage, commit, verify, publish;
- mandatory review/checks on the synthetic production PR;
- dependency conflicts as a stop condition;
- live-target recheck after verification and before publish;
- direct staging squash cherry-picks as forbidden.

Update the JSON decision table with the exact displayed commands and additive preview/action safety fields. Copy the canonical Skill byte-for-byte to the packaged resource; do not maintain divergent prose.

- [ ] **Step 4: Update README and changelog**

Add the same command sequence, restrictions, blocker codes, and decision table to `cli/README.md`. Add one changelog entry that states default exact mode is unchanged and names the new opt-in mode.

- [ ] **Step 5: Run docs and smoke tests**

```bash
uv run pytest tests/test_docs_semantic_audit.py tests/test_release_worktree_smoke.py -q
```

Expected: all tests pass and Skill copies remain byte-identical.

- [ ] **Step 6: Commit**

```bash
git add claude/skills/release-worktree-lifecycle/SKILL.md cli/src/awf/resources/release-worktree-lifecycle/SKILL.md cli/README.md CHANGELOG.md cli/tests/test_docs_semantic_audit.py cli/tests/test_release_worktree_smoke.py
git commit -m "docs(worktrees): define out-of-order release lifecycle"
```

### Task 7: Final verification and review

**Files:**
- Review all files changed by Tasks 1-6.

- [ ] **Step 1: Run focused worktree tests**

```bash
cd cli
uv run pytest \
  tests/test_worktree_registry.py \
  tests/test_worktree_git.py \
  tests/test_worktree_commands.py \
  tests/test_worktree_service.py \
  tests/test_release_worktree_smoke.py \
  tests/test_docs_semantic_audit.py \
  -q
```

Expected: all tests pass.

- [ ] **Step 2: Run the complete non-live suite**

```bash
uv run pytest -q
```

Expected: all non-live tests pass.

- [ ] **Step 3: Exercise the actual CLI surface**

Run parser help and a repository-backed preview fixture or smoke invocation:

```bash
uv run awf wt promote --help
```

Expected: help contains `--out-of-order`, single-source restriction, and separate production review wording.

Run the smoke test without output capture:

```bash
uv run pytest tests/test_release_worktree_smoke.py -k out_of_order -s -q
```

Expected: the generated production worktree contains B without A and creates one managed PR record.

- [ ] **Step 4: Review safety invariants**

Confirm from code and tests:

- exact mode still compares exact reviewed blobs;
- out-of-order is explicit and single-source;
- path exclusions cannot combine with it;
- conflict recovery never changes source or target provenance;
- only AWF stages, commits, pushes, and publishes;
- no PR exists before production verification;
- active lease identity includes promotion mode;
- old SQLite registries migrate without losing leases;
- lifecycle Skill copies are identical.

- [ ] **Step 5: Commit any review fixes, then record final state**

If review finds a defect, fix only that defect, rerun its focused test and Step 1, then commit with a precise `fix(worktrees): describe the corrected invariant` message. Do not create a no-op commit when no fix is needed.
