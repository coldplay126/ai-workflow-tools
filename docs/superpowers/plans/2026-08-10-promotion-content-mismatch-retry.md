# Promotion content-mismatch retry implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an exact repeated `awf wt promote --apply` rebuild one unpublished `promotion_content_mismatch` lease after the production target advances, without weakening promotion provenance or exact-blob verification.

**Architecture:** Extend the existing `_reuse_promotion()` recovery branch rather than adding a command. A narrow helper validates the blocked commit and remote state, rebuilds the reviewed patches on the current target, records the rebuilt head on the same BLOCKED lease, and hands publication back to `_recover_unrecorded_promotion_publish()`. Add two small `GitClient` primitives for remote-branch inspection and guarded worktree reset.

**Tech Stack:** Python 3.14, pytest, Git CLI plumbing, SQLite-backed `WorktreeRegistry`.

---

## File map

- `cli/tests/test_worktree_service.py`: reproduce the real mismatch/target-advance sequence and cover retry guardrails.
- `cli/tests/test_worktree_git.py`: define the remote-branch lookup and hard-reset contracts.
- `cli/src/awf/worktrees/git.py`: implement those two Git operations with transport-error normalization.
- `cli/src/awf/worktrees/service.py`: recognize eligible mismatch leases, rebuild them, and reuse existing exact-delta verification/publication.
- `cli/README.md`: document the additional fail-closed retry case.

### Task 1: Reproduce the target-advance recovery contract

**Files:**
- Modify: `cli/tests/test_worktree_service.py:318-493,4076-4110`

- [ ] **Step 1: Add harness helpers for the real mismatch topology**

Add these methods to `PromotionHarness` after `make_target_conflict()`:

```python
    def add_content_mismatch_source(self, number: int = 373) -> PullRequest:
        git_command(self.repo, "checkout", "-q", "main")
        (self.repo / "feature.txt").write_text(
            "copy=old\nrank=old\n", encoding="utf-8"
        )
        git_command(self.repo, "add", "feature.txt")
        git_command(self.repo, "commit", "-q", "-m", "production prerequisite old")
        git_command(self.repo, "push", "-q", "origin", "main")

        git_command(self.repo, "checkout", "-q", "staging")
        (self.repo / "feature.txt").write_text(
            "copy=new\nrank=old\n", encoding="utf-8"
        )
        git_command(self.repo, "add", "feature.txt")
        git_command(self.repo, "commit", "-q", "-m", "staging prerequisite")
        git_command(self.repo, "push", "-q", "origin", "staging")
        return self.add_followup_source(
            number,
            feature_text="copy=new\nrank=new\n",
            change_feature=True,
            include_followup=False,
        )

    def advance_target_prerequisite(self) -> str:
        git_command(self.repo, "checkout", "-q", "main")
        (self.repo / "feature.txt").write_text(
            "copy=new\nrank=old\n", encoding="utf-8"
        )
        git_command(self.repo, "add", "feature.txt")
        git_command(self.repo, "commit", "-q", "-m", "land production prerequisite")
        target_sha = git_command(self.repo, "rev-parse", "HEAD")
        git_command(self.repo, "push", "-q", "origin", "main")
        git_command(self.repo, "checkout", "-q", "staging")
        return target_sha
```

- [ ] **Step 2: Write the failing end-to-end service test**

Place this beside the existing blocked-verification retry tests:

```python
def test_promote_rebuilds_content_mismatch_after_target_advances(
    promotion_harness: PromotionHarness,
) -> None:
    source = promotion_harness.add_content_mismatch_source()
    first = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )
    assert first.status == "blocked"
    assert first.blockers[0]["code"] == "promotion_content_mismatch"
    assert first.lease is not None
    blocked_head = first.lease.head_sha
    target_sha = promotion_harness.advance_target_prerequisite()

    second = promotion_harness.service.promote(
        source_pr=source.number,
        target_branch="main",
        apply=True,
    )

    assert second.decision == "ready"
    assert second.lease is not None
    assert second.lease.id == first.lease.id
    assert second.lease.state is LeaseState.PR_OPEN
    assert second.lease.head_sha != blocked_head
    assert promotion_harness.git.commit_parents(second.lease.head_sha) == (target_sha,)
    assert len(promotion_harness.github.create_calls) == 1
    assert (second.lease.worktree_path / "feature.txt").read_text(
        encoding="utf-8"
    ) == "copy=new\nrank=new\n"
```

- [ ] **Step 3: Run the test and confirm the current blocker**

Run:

```bash
uv run pytest tests/test_worktree_service.py::test_promote_rebuilds_content_mismatch_after_target_advances -q
```

Expected: FAIL because the second call returns `promotion_incomplete` for the existing BLOCKED lease.

- [ ] **Step 4: Commit the failing contract**

```bash
git add cli/tests/test_worktree_service.py
git commit -m "test(worktrees): cover target-advance promotion retry"
```

### Task 2: Add guarded Git operations

**Files:**
- Modify: `cli/tests/test_worktree_git.py`
- Modify: `cli/src/awf/worktrees/git.py:73-131,263-293`

- [ ] **Step 1: Add failing GitClient tests**

Add tests that push a branch named `retry-target`, compare `remote_branch_sha("retry-target")` with its local SHA, assert `remote_branch_sha("missing") is None`, and verify that `reset_hard(worktree, base_sha)` moves HEAD and clears staged/unstaged changes.

Use these assertions:

```python
assert git.remote_branch_sha("retry-target") == retry_sha
assert git.remote_branch_sha("missing") is None

git.reset_hard(worktree_path, base_sha)
assert git.head_sha(worktree_path) == base_sha
assert git.status_porcelain(worktree_path) == ()
```

- [ ] **Step 2: Run the GitClient tests and confirm missing methods**

Run:

```bash
uv run pytest tests/test_worktree_git.py -q
```

Expected: FAIL with `AttributeError` for `remote_branch_sha` or `reset_hard`.

- [ ] **Step 3: Implement remote lookup and reset**

Add to `GitClient`:

```python
    def remote_branch_sha(self, branch: str) -> str | None:
        ref = f"refs/heads/{branch}"
        try:
            completed = self._run("ls-remote", "--heads", "origin", ref)
        except GitError as error:
            raise GitRemoteError(str(error)) from error
        if not completed.stdout:
            return None
        rows = completed.stdout.decode("ascii", errors="strict").splitlines()
        if len(rows) != 1:
            raise GitRemoteError("git ls-remote returned multiple branch records")
        oid, separator, returned_ref = rows[0].partition("\t")
        if (
            not separator
            or returned_ref != ref
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", oid) is None
        ):
            raise GitRemoteError("git ls-remote returned an invalid branch record")
        return oid

    def reset_hard(self, cwd: Path, ref: str) -> None:
        self._run("reset", "--hard", "-q", ref, cwd=cwd)
```

- [ ] **Step 4: Run focused Git tests**

Run:

```bash
uv run pytest tests/test_worktree_git.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit the Git primitives**

```bash
git add cli/src/awf/worktrees/git.py cli/tests/test_worktree_git.py
git commit -m "feat(worktrees): add guarded promotion reset primitives"
```

### Task 3: Rebuild eligible content-mismatch promotions

**Files:**
- Modify: `cli/src/awf/worktrees/service.py:3164-3278,3279-3412`
- Test: `cli/tests/test_worktree_service.py`

- [ ] **Step 1: Route only the approved blocked event to rebuild**

In `_reuse_promotion()`, keep the existing provenance parsing. In the `LeaseState.BLOCKED` branch, inspect the latest event before the existing retryable-prefix check:

```text
            latest = events[-1] if events else None
            if (
                latest is not None
                and latest.event_type == "promotion_blocked"
                and latest.summary.startswith("promotion_content_mismatch:")
            ):
                return self._rebuild_content_mismatch_promotion(
                    lease,
                    github=github,
                    sources=sources,
                    excluded_paths=excluded_paths,
                    recorded_target_sha=target_base_sha,
                    target_branch=target_branch,
                )
```

- [ ] **Step 2: Implement the guarded rebuild helper**

Add `_rebuild_content_mismatch_promotion()` next to `_recover_unrecorded_promotion_publish()`. Its implementation must perform these operations in this order:

```python
    def _rebuild_content_mismatch_promotion(
        self,
        lease: Lease,
        *,
        github: GhClient,
        sources: Sequence[PullRequest],
        excluded_paths: Sequence[str],
        recorded_target_sha: str,
        target_branch: str,
    ) -> CommandResult:
        old_head = self.git.head_sha(lease.worktree_path)
        if (
            self._registered_worktree(lease) is None
            or self.git.status_porcelain(lease.worktree_path)
            or old_head != lease.head_sha
            or self.git.commit_parents(old_head) != (recorded_target_sha,)
        ):
            return self._promotion_blocked(
                "promotion_incomplete",
                f"lease {lease.id} was not verified for content-mismatch recovery",
                lease=lease,
            )
        if self.git.remote_branch_sha(lease.branch) is not None:
            return self._promotion_blocked(
                "promotion_incomplete",
                f"lease {lease.id} promotion branch is already published",
                lease=lease,
            )

        current_target_sha = self.git.fetch_ref(target_branch)
        if current_target_sha == recorded_target_sha:
            return self._promotion_blocked(
                "promotion_incomplete",
                f"lease {lease.id} target branch has not advanced",
                lease=lease,
            )

        patches: list[bytes] = []
        for source in sources:
            source_base_sha = self.git.fetch_ref(source.base_sha)
            source_head_sha = self.git.fetch_ref(source.head_sha)
            if source_base_sha != source.base_sha or source_head_sha != source.head_sha:
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} source provenance changed",
                    lease=lease,
                )
            merge_base = self.git.merge_base(source_base_sha, source_head_sha)
            included_paths = tuple(
                path for path in source.changed_paths if path not in excluded_paths
            )
            patch = self.git.binary_diff(
                merge_base,
                source_head_sha,
                paths=included_paths if excluded_paths else None,
            )
            if not patch:
                return self._promotion_blocked(
                    "promotion_incomplete",
                    f"lease {lease.id} reviewed source patch is empty",
                    lease=lease,
                )
            patches.append(patch)

        try:
            self.git.reset_hard(lease.worktree_path, current_target_sha)
            for patch in patches:
                self.git.apply_indexed_patch(lease.worktree_path, patch)
            promotion_head = self.git.commit(
                lease.worktree_path,
                self._promotion_message(
                    sources=sources,
                    excluded_paths=excluded_paths,
                    target_sha=current_target_sha,
                    lease=lease,
                    target_branch=target_branch,
                ),
                allow_empty=True,
            )
            lease = self.registry.transition(
                lease.id,
                LeaseState.BLOCKED,
                expected_version=lease.version,
                event_type="promotion_blocked",
                summary=(
                    "promotion_verification_failed: rebuilt content-mismatch "
                    "promotion on advanced target"
                ),
                observed_head_sha=old_head,
                head_sha=promotion_head,
            )
        except (GitError, OSError, RuntimeError, sqlite3.Error) as error:
            try:
                self.git.reset_hard(lease.worktree_path, old_head)
            except GitError:
                pass
            return self._promotion_blocked(
                "promotion_recovery_failed", str(error), lease=lease
            )
        return self._recover_unrecorded_promotion_publish(
            lease,
            github=github,
            sources=sources,
            excluded_paths=excluded_paths,
            target_branch=target_branch,
        )
```

Keep source-message validation in `_reuse_promotion()` before this helper. Do not add `promotion_content_mismatch` to the general retryable-prefix tuple.

- [ ] **Step 3: Run the recovery test**

Run:

```bash
uv run pytest tests/test_worktree_service.py::test_promote_rebuilds_content_mismatch_after_target_advances -q
```

Expected: PASS; one PR creation call, same lease ID, new commit parent equals the advanced target.

- [ ] **Step 4: Run existing promotion recovery tests**

Run:

```bash
uv run pytest tests/test_worktree_service.py -k 'promote and (retry or recover or provenance)' -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit the rebuild path**

```bash
git add cli/src/awf/worktrees/service.py cli/tests/test_worktree_service.py
git commit -m "fix(worktrees): rebuild blocked promotions on advanced targets"
```

### Task 4: Lock down fail-closed guardrails

**Files:**
- Modify: `cli/tests/test_worktree_service.py`
- Modify: `cli/src/awf/worktrees/service.py` only if a test exposes a missing guard

- [ ] **Step 1: Add no-target-advance and dirty-worktree tests**

Reuse `add_content_mismatch_source()`. In separate tests, call promote twice without `advance_target_prerequisite()`, and create `dirty.txt` in the blocked worktree before advancing. Assert `promotion_incomplete`, unchanged blocked head, and zero PR creation calls.

- [ ] **Step 2: Add published-branch and forged-parent tests**

After the first mismatch, push the blocked branch manually for one test. For another, replace the blocked commit with a commit carrying the original message but a foreign parent. Advance the target, retry, and assert both calls remain blocked with no PR creation.

- [ ] **Step 3: Add source-provenance mutation test**

After the first mismatch, replace the fake PR's `head_sha` with another valid commit SHA, advance the target, and retry. Assert `promotion_incomplete` and no PR creation.

- [ ] **Step 4: Run all promotion tests**

Run:

```bash
uv run pytest tests/test_worktree_service.py -k promote -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit guardrail coverage**

```bash
git add cli/tests/test_worktree_service.py cli/src/awf/worktrees/service.py
git commit -m "test(worktrees): enforce promotion rebuild guardrails"
```

### Task 5: Document and verify the release tool change

**Files:**
- Modify: `cli/README.md:163-166`

- [ ] **Step 1: Update the retry contract**

Replace the current retry paragraph with:

```markdown
A repeated `wt promote --apply` can resume a blocked prepare-command or
production-verification failure only when the managed worktree is clean and its
promotion commit still has exact provenance. It can also rebuild an unpublished
`promotion_content_mismatch` lease when the target branch has advanced, the
reviewed source SHAs are unchanged, and the original branch was never pushed.
The rebuilt commit must pass the same exact path/blob and production checks
before AWF publishes a pull request. Other blocked states remain fail-closed.
```

- [ ] **Step 2: Run syntax validation**

Run:

```bash
uv run python -m compileall -q src tests
```

Expected: exit 0 with no syntax errors.

- [ ] **Step 3: Run the full worktree suite**

Run:

```bash
uv run pytest tests/test_worktree_git.py tests/test_worktree_service.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Run the repository CI suite**

Run:

```bash
uv run --group dev pytest -q --ignore=tests/test_e2e_live.py
```

Expected: all non-live CLI tests pass.

- [ ] **Step 5: Run the CLI smoke check**

Run:

```bash
uv run awf wt --help
```

Expected: exit 0 with the existing worktree subcommands; no new public command is added.

- [ ] **Step 6: Commit documentation and final validation updates**

```bash
git add cli/README.md cli/src/awf/worktrees/git.py cli/src/awf/worktrees/service.py cli/tests/test_worktree_git.py cli/tests/test_worktree_service.py
git commit -m "docs(worktrees): document content mismatch recovery"
```

After implementation, install or invoke the patched CLI from this worktree, rerun the exact `blip-server` promotion command for source PR #409, inspect the generated main PR delta, and stop before merging it unless explicitly instructed.
