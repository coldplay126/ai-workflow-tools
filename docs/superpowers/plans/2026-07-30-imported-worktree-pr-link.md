# Imported Worktree PR Link Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow an imported clean worktree lease to be explicitly linked to one already-merged GitHub PR during adoption, then reuse the existing refresh and finish safety paths.

**Architecture:** Extend `wt adopt` with an optional positive `--pr` argument. `WorktreeService.adopt` keeps the existing no-PR behavior, but when a PR is supplied it validates the imported lease and merged PR provenance in preview and again under the repository lock for apply, then records `managed=true` and `target_pr` in one registry CAS transition. Exact repeated links return `reuse`; no PR inference or unmanaged cleanup path is added.

**Tech Stack:** Python 3.9+, argparse, SQLite registry CAS, Git CLI through `GitClient`, GitHub CLI through `GhClient`, pytest real-Git fixtures.

---

## File map

- `cli/src/awf/cli.py`: parse `wt adopt --pr`.
- `cli/src/awf/commands/wt.py`: validate/forward PR number and inject `GhClient` for PR-linked adoption.
- `cli/src/awf/worktrees/service.py`: merged-PR provenance validation, locked revalidation, idempotent link transition.
- `cli/tests/test_worktree_service.py`: service contracts and race/error cases.
- `cli/tests/test_worktree_commands.py`: CLI JSON and exit-code contracts.
- `cli/tests/test_release_worktree_smoke.py`: import → adopt PR → refresh → finish integration.
- `cli/README.md`: operator command and legacy cleanup flow.
- `claude/skills/release-worktree-lifecycle/SKILL.md`: mandatory managed legacy-worktree procedure.
- `cli/tests/test_docs_semantic_audit.py`: current CLI examples and safety contract.

### Task 1: Add merged-PR adoption service contract

**Files:**
- Modify: `cli/src/awf/worktrees/service.py:2010-2136`
- Test: `cli/tests/test_worktree_service.py:1609-1743`

- [ ] **Step 1: Write failing preview and apply tests**

Add tests that create an imported external worktree, register a merged PR whose `head_ref` and `head_sha` match the imported lease, and call:

```python
preview = harness.service.adopt(imported.id, pr_number=129, apply=False)
result = harness.service.adopt(imported.id, pr_number=129, apply=True)

assert preview.decision == "preview"
assert preview.actions == (
    {
        "kind": "link_pr",
        "lease_id": imported.id,
        "path": str(imported.worktree_path),
        "pr_number": 129,
        "head_sha": imported.head_sha,
    },
)
assert result.decision == "ready"
assert result.lease.managed is True
assert result.lease.target_pr == 129
assert result.lease.state == imported.state
```

Construct the PR with an explicit matching branch:

```python
harness.github.prs[129] = replace(
    merged_pr(number=129, head_sha=imported.head_sha),
    head_ref=imported.branch,
)
```

For preview, snapshot lease/version/events and assert `harness.lock_dir` remains absent and every snapshot is unchanged.

- [ ] **Step 2: Run tests and confirm RED**

Run:

```bash
uv run --project cli pytest \
  cli/tests/test_worktree_service.py \
  -k 'adopt and (merged_pr or pr_preview)' -q
```

Expected: FAIL because `adopt()` does not accept `pr_number`.

- [ ] **Step 3: Implement PR validation helpers**

Change the public signature without breaking existing callers:

```python
def adopt(
    self,
    lease_id: str,
    *,
    pr_number: int | None = None,
    apply: bool,
) -> CommandResult:
```

Add a helper returning either the validated PR or a `CommandResult`:

```python
def _validate_adoption_pr(
    self,
    imported: Lease,
    pr_number: int,
) -> PullRequest | CommandResult:
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        return self._adopt_blocked(
            "invalid_pr_number",
            "pull request number must be a positive integer",
            lease=imported,
        )
    github = self.github or GhClient(imported.repository_root)
    try:
        pull_request = github.view_pr(pr_number)
    except ExternalServiceError as error:
        return CommandResult.external_error(
            "wt.adopt",
            code="github_adopt_failed",
            message=str(error),
            lease=imported,
        )
    if pull_request.number != pr_number:
        return self._adopt_blocked(
            "pr_number_mismatch",
            f"GitHub returned PR #{pull_request.number} for requested PR #{pr_number}",
            lease=imported,
        )
    if not (
        pull_request.state == "MERGED"
        or (
            pull_request.state == "CLOSED"
            and pull_request.merge_commit_sha is not None
        )
    ):
        return self._adopt_blocked(
            "pr_not_merged",
            f"pull request #{pr_number} is not merged",
            lease=imported,
        )
    if pull_request.head_ref != imported.branch:
        return self._adopt_blocked(
            "pr_branch_mismatch",
            f"pull request #{pr_number} head branch does not match lease {imported.id}",
            lease=imported,
        )
    if pull_request.head_sha != imported.head_sha:
        return self._adopt_blocked(
            "pr_head_mismatch",
            f"pull request #{pr_number} head SHA does not match lease {imported.id}",
            lease=imported,
        )
    return pull_request
```

Use concrete messages naming the lease/PR but never credentials. Import `PullRequest`, `GhClient`, and `ExternalServiceError` from the existing GitHub module.

- [ ] **Step 4: Implement preview and locked apply**

Keep `_adoption_blocker(imported)` before PR validation. Preview returns `link_pr` without entering `repository_lock`.

For apply:

```python
with repository_lock(self.lock_dir / f"{imported.repository_id}.lock"):
    imported = self.registry.get_lease(lease_id)
    blocker = self._adoption_blocker(imported)
    if blocker is not None:
        return blocker
    validated = self._validate_adoption_pr(imported, pr_number)
    if isinstance(validated, CommandResult):
        return validated
    adopted = self.registry.transition(
        imported.id,
        imported.state,
        expected_version=imported.version,
        event_type="imported_lease_pr_linked",
        summary=f"imported lease linked to merged PR #{pr_number}",
        observed_head_sha=validated.head_sha,
        pr_number=validated.number,
        managed=True,
    )
```

The existing `_adoption_blocker` rechecks registration, branch, HEAD, and clean status under the lock.

- [ ] **Step 5: Run focused tests and commit**

Run:

```bash
uv run --project cli pytest cli/tests/test_worktree_service.py -k adopt -q
```

Expected: all adopt tests pass.

Commit:

```bash
git add cli/src/awf/worktrees/service.py cli/tests/test_worktree_service.py
git commit -m "feat: link imported leases to merged PRs"
```

### Task 2: Enforce mismatch, external-error, and idempotency behavior

**Files:**
- Modify: `cli/src/awf/worktrees/service.py:2010-2136`
- Test: `cli/tests/test_worktree_service.py`

- [ ] **Step 1: Write the failure matrix tests**

Parameterize PR provenance blockers:

```python
@pytest.mark.parametrize(
    ("state", "merge_sha", "head_ref", "head_sha", "returned_number", "code"),
    [
        ("OPEN", None, "legacy-release", "imported", 129, "pr_not_merged"),
        ("CLOSED", None, "legacy-release", "imported", 129, "pr_not_merged"),
        ("MERGED", "merge-sha", "legacy-release", "imported", 130, "pr_number_mismatch"),
        ("MERGED", "merge-sha", "other", "imported", 129, "pr_branch_mismatch"),
        ("MERGED", "merge-sha", "legacy-release", "0" * 40, 129, "pr_head_mismatch"),
    ],
)
def test_adopt_pr_blocks_invalid_provenance(
    harness: Harness,
    state: str,
    merge_sha: str | None,
    head_ref: str,
    head_sha: str,
    returned_number: int,
    code: str,
) -> None:
    imported = harness.import_external("legacy-release")
    candidate_head = imported.head_sha if head_sha == "imported" else head_sha
    harness.github.prs[129] = replace(
        pull_request(
            number=returned_number,
            state=state,
            head_sha=candidate_head,
            merge_commit_sha=merge_sha,
        ),
        head_ref=head_ref,
    )

    result = harness.service.adopt(imported.id, pr_number=129, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == code
    assert harness.registry.get_lease(imported.id).managed is False
```

Add a GitHub error case:

```python
harness.github.error = ExternalServiceError("github unavailable")
result = harness.service.adopt(imported.id, pr_number=129, apply=True)
assert result.status == "error"
assert result.exit_code == 4
```

Retain existing dirty, detached, orphaned, branch-conflict, and changed-HEAD tests.

- [ ] **Step 2: Write exact-reuse tests**

After a successful linked adoption, call the same command twice:

```python
first = harness.service.adopt(imported.id, pr_number=129, apply=True)
before = harness.registry.get_lease(imported.id)
events_before = harness.registry.list_events(imported.id)
second = harness.service.adopt(imported.id, pr_number=129, apply=True)

assert second.decision == "reuse"
assert second.lease == before
assert harness.registry.list_events(imported.id) == events_before
```

Add different-PR rejection:

```python
result = harness.service.adopt(imported.id, pr_number=130, apply=True)
assert result.blockers[0]["code"] == "pr_link_mismatch"
```

Existing `adopt(imported.id, apply=True)` after adoption must continue returning `already_adopted`.

- [ ] **Step 3: Run tests and confirm RED**

Run:

```bash
uv run --project cli pytest cli/tests/test_worktree_service.py -k 'adopt and pr' -q
```

Expected: failure matrix or repeat calls fail before idempotency/mismatch handling exists.

- [ ] **Step 4: Implement exact linked-lease reuse**

Before `_adoption_blocker`, handle only the explicit PR case:

```python
if imported.managed and pr_number is not None:
    if imported.target_pr != pr_number:
        return self._adopt_blocked(
            "pr_link_mismatch",
            (
                f"lease {imported.id} is linked to PR #{imported.target_pr}, "
                f"not PR #{pr_number}"
            ),
            lease=imported,
        )
    validated = self._validate_linked_adoption(imported, pr_number)
    if isinstance(validated, CommandResult):
        return validated
    return CommandResult.ok("wt.adopt", decision="reuse", lease=imported)
```

The reuse validator must still verify current registration, branch, clean status, recorded/current HEAD, and the merged PR provenance, but must not reject solely because `managed=True`. Extract shared non-owner validation rather than duplicating `_adoption_blocker` logic.

Apply repeats must take the repository lock and reread the lease before deciding reuse, so a concurrent PR/HEAD change cannot be accepted from a stale snapshot. Preview repeat remains lock-free and mutation-free.

- [ ] **Step 5: Prove lock-held revalidation**

Use a fake GitHub adapter that returns a matching PR on the first call and a mismatched head on the second. Assert apply returns `pr_head_mismatch`, leaves `managed=false`, and records no adoption event.

- [ ] **Step 6: Run focused tests and commit**

Run:

```bash
uv run --project cli pytest cli/tests/test_worktree_service.py -k adopt -q
```

Commit:

```bash
git add cli/src/awf/worktrees/service.py cli/tests/test_worktree_service.py
git commit -m "fix: make imported PR adoption fail closed"
```

### Task 3: Wire `--pr` through the CLI

**Files:**
- Modify: `cli/src/awf/cli.py:797-816`
- Modify: `cli/src/awf/commands/wt.py:199-244`
- Test: `cli/tests/test_worktree_commands.py:697-750`

- [ ] **Step 1: Write failing command tests**

Add parser/handler tests:

```python
rc, stdout, stderr = capture_main(
    ["wt", "adopt", "--lease", lease.id, "--pr", "129", "--json"]
)
payload = json.loads(stdout)
assert rc == 0
assert payload["command"] == "wt.adopt"
assert payload["decision"] == "preview"
assert payload["actions"][0]["pr_number"] == 129
assert stderr == ""
```

Add apply/reuse, invalid `--pr 0` usage exit 2, GitHub external failure exit 4, and provenance blocker exit 3.

- [ ] **Step 2: Run tests and confirm RED**

Run:

```bash
uv run --project cli pytest cli/tests/test_worktree_commands.py -k 'wt_adopt' -q
```

Expected: argparse rejects `--pr`.

- [ ] **Step 3: Add parser and handler wiring**

In `cli.py`, add one reusable parser helper near the other module-level CLI helpers:

```python
def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "value must be a positive integer"
        ) from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed
```

Use it on the new argument:

```python
wt_adopt_parser.add_argument(
    "--pr",
    type=_positive_int,
    help="Already-merged pull request to link while adopting the lease.",
)
```

In `commands/wt.py`:

```python
service = WorktreeService(
    registry,
    GitClient(lease.repository_root),
    github=GhClient(lease.repository_root) if args.pr is not None else None,
)
result = service.adopt(args.lease, pr_number=args.pr, apply=args.apply)
```

Catch `ExternalServiceError` before local `GitError` only if the service can still raise it; otherwise verify service-created exit 4 results pass through unchanged.

- [ ] **Step 4: Run command tests and commit**

Run:

```bash
uv run --project cli pytest cli/tests/test_worktree_commands.py -k 'wt_adopt' -q
```

Commit:

```bash
git add cli/src/awf/cli.py cli/src/awf/commands/wt.py cli/tests/test_worktree_commands.py
git commit -m "feat: expose merged PR linking during adopt"
```

### Task 4: Prove the legacy cleanup integration

**Files:**
- Modify: `cli/tests/test_release_worktree_smoke.py`
- Modify if integration exposes a source defect: `cli/src/awf/worktrees/service.py`

- [ ] **Step 1: Add a real-Git integration test**

Extend the smoke harness or add one focused test that:

```python
imported = smoke.import_existing_release_worktree()
preview = smoke.service.adopt(imported.id, pr_number=129, apply=False)
adopted = smoke.service.adopt(imported.id, pr_number=129, apply=True)
assert preview.decision == "preview"
assert adopted.lease.target_pr == 129

smoke.refresh()
refreshed = smoke.registry.get_lease(imported.id)
assert refreshed.state is LeaseState.CLEANABLE

finish_preview = smoke.service.finish(pr_number=129, apply=False)
removed = smoke.service.finish(pr_number=129, apply=True)
assert finish_preview.decision == "preview"
assert removed.decision == "removed"
assert not imported.worktree_path.exists()
```

Use a real bare remote and actual worktree; fake only GitHub and deployment boundaries. Model PR #129 as squash-merged: `head_sha` equals the imported branch HEAD while `merge_commit_sha` is a different target commit.

- [ ] **Step 2: Run smoke RED/GREEN**

Run before implementation integration fixes:

```bash
uv run --project cli pytest cli/tests/test_release_worktree_smoke.py -q
```

Expected before the integration is complete: FAIL at adopt PR link or refresh/finish transition.

Fix production code only for a real contract defect; do not weaken the assertions.

- [ ] **Step 3: Commit smoke coverage**

```bash
git add cli/tests/test_release_worktree_smoke.py cli/src/awf/worktrees/service.py
git commit -m "test: cover imported worktree PR cleanup lifecycle"
```

### Task 5: Update the operator skill and documentation

**Files:**
- Modify: `cli/README.md:69-180`
- Modify: `claude/skills/release-worktree-lifecycle/SKILL.md:68-104`
- Modify: `cli/tests/test_docs_semantic_audit.py`

- [ ] **Step 1: Add failing semantic assertions**

Assert the Skill contains, in order:

```text
awf wt import --root <root> --dry-run --json
awf wt import --root <root> --apply --json
awf wt adopt --lease <id> --pr <merged-pr> --json
awf wt adopt --lease <id> --pr <merged-pr> --apply --json
awf wt status --repo-root <repo-root> --refresh --json
awf wt finish --repo-root <repo-root> --pr <merged-pr> --json
```

Also assert prose forbids automatic PR inference and requires stable runtime installation source before removing a source worktree.

- [ ] **Step 2: Run semantic tests and confirm RED**

```bash
uv run --project cli pytest cli/tests/test_docs_semantic_audit.py -q
```

Expected: new legacy-cleanup contract is absent.

- [ ] **Step 3: Update docs and Skill**

Document:

- `adopt --pr` accepts only merged PRs.
- PR branch/head must exactly match the imported lease.
- preview precedes apply.
- `status --refresh` must run after adoption before finish.
- same PR returns reuse; different PR is blocked.
- GitHub failure is exit 4.
- runtime CLI/Skill links must be moved to stable merged-main source before deleting the old source worktree.

Add a complete command example to `cli/README.md`; do not add generic deployment promises.

- [ ] **Step 4: Run docs tests and commit**

```bash
uv run --project cli pytest \
  cli/tests/test_docs_semantic_audit.py \
  cli/tests/test_docs_links.py -q
```

Commit:

```bash
git add cli/README.md claude/skills/release-worktree-lifecycle/SKILL.md \
  cli/tests/test_docs_semantic_audit.py
git commit -m "docs: publish imported worktree PR linking"
```

### Task 6: Verify, review, and integrate

**Files:**
- No new files expected

- [ ] **Step 1: Run focused lifecycle tests**

```bash
uv run --project cli pytest \
  cli/tests/test_worktree_registry.py \
  cli/tests/test_worktree_git.py \
  cli/tests/test_worktree_service.py \
  cli/tests/test_worktree_commands.py \
  cli/tests/test_release_worktree_smoke.py \
  cli/tests/test_docs_semantic_audit.py \
  cli/tests/test_docs_links.py -q
```

Expected: zero failures.

- [ ] **Step 2: Run the full CLI suite**

```bash
uv run --project cli pytest cli/tests
```

Expected: zero failures or collection errors; existing skips/deselections only.

- [ ] **Step 3: Request independent spec and quality reviews**

Review the complete branch diff against:

- `docs/superpowers/specs/2026-07-30-imported-worktree-pr-link-design.md`
- this plan

Fix every Critical/Important finding and rerun focused tests.

- [ ] **Step 4: Push and open a PR**

```bash
git push -u origin awf/imported-worktree-pr-link/feature
gh pr create --base main --head awf/imported-worktree-pr-link/feature \
  --title "feat: link imported worktrees to merged PRs"
```

Wait for required CI and review. Merge with head-SHA protection; do not delete the branch/worktree yet.

### Task 7: Migrate the runtime source and clean the legacy worktree

**Files:**
- Runtime installation and managed registry only; no repository code edits expected

- [ ] **Step 1: Verify merged-main CI and prepare a stable source**

Use a clean managed checkout based on the new merged `origin/main`; do not modify `/Users/steven/Documents/GitHub/ai-workflow-tools`, which contains user-owned local commits/untracked files.

- [ ] **Step 2: Reinstall CLI and Skill links**

```bash
uv tool install --force --editable <stable-main-worktree>/cli
sh <stable-main-worktree>/scripts/install-skill-links.sh \
  <stable-main-worktree>/claude/skills/release-worktree-lifecycle \
  "$HOME/.claude/skills" "$HOME/.omp/agent/skills" "$HOME/.agents/skills"
```

Verify every link resolves to the stable merged-main source and `awf wt status --repo-root /Users/steven/Documents/GitHub/ai-workflow-tools --refresh --json` exits 0.

- [ ] **Step 3: Import and identify the legacy release worktree**

```bash
awf wt status --repo-root /Users/steven/Documents/GitHub/ai-workflow-tools --refresh --json
awf wt import --root /Users/steven/.cache/awf/worktrees/ai-workflow-tools --dry-run --json
awf wt import --root /Users/steven/.cache/awf/worktrees/ai-workflow-tools --apply --json
```

Select only the imported lease whose exact path is:

```text
/Users/steven/.cache/awf/worktrees/ai-workflow-tools/release-worktree-lifecycle
```

Do not adopt or remove other imported worktrees.

- [ ] **Step 4: Adopt against PR #129**

```bash
awf wt adopt --lease <legacy-lease-id> --pr 129 --json
awf wt adopt --lease <legacy-lease-id> --pr 129 --apply --json
awf wt status --repo-root /Users/steven/Documents/GitHub/ai-workflow-tools --refresh --json
```

Require `preview`, then `ready` or exact `reuse`, then `CLEANABLE/not_required`. Stop on any blocker.

- [ ] **Step 5: Preview and apply finish**

```bash
awf wt finish --repo-root /Users/steven/Documents/GitHub/ai-workflow-tools --pr 129 --json
awf wt finish --repo-root /Users/steven/Documents/GitHub/ai-workflow-tools --pr 129 --apply --json
```

Require preview with no blockers, then `removed`. Verify the legacy path and only its safe branch are removed. Do not run direct Git or filesystem cleanup.

- [ ] **Step 6: Record closure evidence**

Record merged PR, main CI, stable installation source, adopt result, finish result, and preserved unrelated worktrees in the follow-up PR comment.
