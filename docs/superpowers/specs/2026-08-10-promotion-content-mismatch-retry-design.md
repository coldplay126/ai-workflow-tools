# Promotion content-mismatch retry design

## Goal

Allow a repeated `awf wt promote --apply` command to recover one unpublished promotion lease after its target branch advances and removes the original content mismatch. Recovery must preserve the reviewed source PR delta and every existing promotion safety check.

The motivating case is a staging PR whose reviewed head already contains a prerequisite copy change. The first promotion attempt was based on an older `main`, so exact blob verification failed. After the prerequisite landed on `main`, the existing BLOCKED lease prevented a new attempt even though rebuilding the same source delta on the new target could now pass.

## Current behavior

`WorktreeService.promote()` reuses an active lease with the same repository, initiative, and promotion purpose. `_reuse_promotion()` only retries BLOCKED leases when the last failure is a preparation or production-verification failure. A `promotion_content_mismatch` event instead returns `promotion_incomplete` before checking whether `main` advanced.

The blocked worktree contains a clean, local promotion commit based on the old target SHA. It has no target pull request and was never published because content verification failed before the push step.

## Chosen approach

Keep the existing command surface. Repeating the exact `wt promote --apply` request is the retry signal.

When `_reuse_promotion()` sees an eligible content-mismatch lease, it rebuilds that lease's promotion commit on the current target SHA. It then runs the same exact-delta checks and production verification used for a new promotion. Only a fully verified commit may be pushed and used to create the production PR.

This is narrower than a general retry or cleanup command. It changes one failed-state transition and leaves other BLOCKED states unchanged.

## Eligibility checks

Automatic rebuild is allowed only when all conditions hold:

1. The lease is managed, has purpose `promote`, and still matches the requested source PRs, target ref, branch, and path exclusions.
2. The lease state is `BLOCKED` and it has no target PR.
3. The latest registry event is `promotion_blocked` with a `promotion_content_mismatch:` summary.
4. The registered worktree exists and is clean.
5. The local promotion branch is not published remotely.
6. The source PR numbers, base SHAs, head SHAs, changed paths, and recorded exclusions still match the original promotion provenance.
7. The current target SHA differs from the target SHA recorded in the blocked promotion commit.
8. The blocked commit has exactly one parent, and that parent is the recorded target SHA.

If any condition fails, the command returns `promotion_incomplete` and preserves the blocked lease and worktree.

## Rebuild flow

1. Fetch the current target SHA and every reviewed source base/head SHA.
2. Recompute the source patches and expected final blobs with the same functions used by a new promotion.
3. Move the local promotion branch and worktree to the current target SHA. This guarded reset is internal to the managed retry path; it is never exposed as a generic destructive command.
4. Apply the reviewed source patches in order.
5. Create a new promotion commit with an updated `AWF-Target-Base` trailer and unchanged source/exclusion provenance.
6. Compare changed paths against the expected path set.
7. Compare every changed path's blob against the expected blob derived from the reviewed source heads and current target.
8. Run `_prepare_promotion(..., force=True)` and all configured production-verification commands.
9. Record `promotion_publish_pending`, push the branch, and create the target PR through the existing publication flow.

A failure at steps 1-8 leaves the lease BLOCKED. No push or PR creation occurs.

## State and evidence

The retry keeps the same lease ID so the registry retains the original failure and the subsequent recovery in one event stream. The successful transition records the rebuilt promotion head and uses a summary that identifies target-advance recovery.

The old commit remains reachable in the registry event history through its observed head SHA even after the managed branch moves to the rebuilt commit.

## Tests

Add service-level tests for these observable contracts:

- A content-mismatch lease retries successfully after the target advances with the missing prerequisite blob.
- The retry reruns configured production verification and creates exactly one target PR.
- No target movement leaves the lease BLOCKED.
- A dirty worktree leaves the lease BLOCKED.
- Changed source provenance leaves the lease BLOCKED.
- A published remote promotion branch leaves the lease BLOCKED.
- A forged target parent leaves the lease BLOCKED.
- A rebuilt path or blob mismatch leaves the lease BLOCKED and creates no PR.
- Existing verification-failure recovery behavior remains unchanged.

The original failing test must reproduce the real sequence: first apply returns `promotion_content_mismatch`, target receives the prerequisite, and the same promote request succeeds.

## Non-goals

- Retrying arbitrary BLOCKED lease states.
- Deleting blocked worktrees or registry evidence.
- Force-pushing an existing remote promotion branch.
- Changing source PR ordering, path-exclusion rules, or production verification commands.
- Bypassing exact path/blob checks to make a promotion pass.
