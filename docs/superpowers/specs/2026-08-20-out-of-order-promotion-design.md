# Out-of-order promotion design

## Goal

Allow one approved staging pull request to reach production before an earlier, independent staging change. The operator must opt in explicitly. Default promotion keeps the existing exact reviewed-blob invariant.

The workflow must also replace unmanaged cherry-picks when a squash-merged staging PR was developed on top of changes that are not yet on the production branch.

## Problem

Assume production is at `S`, staging contains change `A`, and pull request `B` was developed from staging:

```text
production:       S
staging:          S + A
B reviewed base:  S + A
B reviewed head:  S + A + B
wanted production:S + B
```

`WorktreeService.promote()` already computes the source patch from the merge base of the reviewed base and head, so the patch contains `B` rather than the full staging branch. Exact content verification still expects every promoted path to equal the blob in B's reviewed head. A shared path therefore expects `A+B`, while an out-of-order application to production produces `B`. The command returns `promotion_content_mismatch` even when the B patch applies cleanly.

Squash merge makes direct cherry-pick recovery unreliable. The staging squash commit has staging context and a different identity from the reviewed feature commits. A cherry-pick can conflict when production lacks A, and the resulting manual resolution has no AWF provenance or path guard.

## Release policy

AWF supports two release policies:

1. If A's code may exist in production, preserve staging order and keep A inactive with a feature flag or equivalent runtime gate. Activation order may differ from merge order.
2. If A's code must not exist in production, use an explicit out-of-order promotion for B. AWF reconstructs B's reviewed patch on the production target and creates a separately reviewed production PR.

Out-of-order promotion is not an automatic fallback. AWF never infers semantic independence from disjoint paths or a clean patch application.

## Command surface

Add `--out-of-order` to `awf wt promote`:

```sh
awf wt promote \
  --source-pr <number> \
  --to <production-branch> \
  --out-of-order \
  --repo-root <repo-root> \
  --json
```

Preview and apply keep the existing two-step contract. The apply command repeats the same arguments and adds `--apply`.

Initial restrictions:

- exactly one `--source-pr`;
- no `--exclude-path`;
- the source PR must be merged into the configured staging branch;
- the existing review and check policies still apply;
- production verification commands remain mandatory.

Invalid combinations return `invalid_out_of_order_promotion` before worktree creation.

## Promotion modes

Promotion mode is explicit lease provenance with values `exact` and `out_of_order`. Existing and imported leases default to `exact`.

The mode participates in the promotion initiative identity, lease reuse checks, commit trailers, PR body, and recovery validation. An exact request must never reuse an out-of-order lease or the reverse.

Out-of-order promotion commits include:

```text
AWF-Promotion-Mode: out-of-order
AWF-Source-PR: <number>
AWF-Source-Base: <sha>
AWF-Source-Head: <sha>
AWF-Target-Base: <sha>
AWF-Resolution: automatic|manual-reviewed
AWF-Lease-ID: <uuid>
```

Exact promotions retain their current trailers to avoid changing existing provenance and recovery behavior.

## Clean application

For a new out-of-order promotion:

1. Fetch the reviewed source base and head plus the current production target.
2. Compute `merge_base(source.base_sha, source.head_sha)`.
3. Produce the binary patch from that merge base to the reviewed source head.
4. Verify that the patch paths exactly match the source PR's reviewed changed paths.
5. Create the managed promotion worktree at the production target SHA.
6. Apply the patch with the existing indexed three-way application.
7. Commit the synthetic production result with out-of-order provenance.
8. Require the target-to-promotion changed paths to be a non-empty subset of the reviewed source paths. Paths that are already identical on production may be absent from the net delta. No new path is allowed.
9. Run prepare and all production verification commands.
10. Recheck the live origin target SHA after verification. If it changed, block and preserve the managed worktree.
11. Push the managed branch and create the production PR.

Out-of-order mode deliberately does not compare the final blobs with the source head. The production PR is a synthetic result and requires its own review. Exact mode keeps the current full-blob comparison.

## Conflict handling

A failed three-way application must preserve a managed, unpublished promotion worktree instead of collapsing the failure into a generic apply error. AWF records:

- blocker code `out_of_order_conflict`;
- source and target provenance;
- conflicted paths;
- resolution state `pending`;
- no remote branch and no target PR.

The operator may edit only the conflicted files returned by AWF. Direct `git add`, `git commit`, `git reset`, `git cherry-pick`, and `git push` remain forbidden. The operator's unstaged and unmerged edits must be a subset of `conflicted_paths`; AWF may already have clean-applied and staged part of the patch. The final indexed delta must be non-empty and a subset of `reviewed_paths`.

When the first patch clean-stages reviewed paths outside `conflicted_paths`, AWF
persists their stage-0 mode and blob OID index entries in
`protected_index_entries`. Resolution preview, apply, and retry pin those
entries exactly; direct `git add` or chmod/file-type mode tampering is
`promotion_resolution_scope_mismatch`.

Initial preview exposes `source_base_sha`, `source_head_sha`, `target_base_sha`, and `reviewed_paths`. Repeating the same preview command for a pending lease reports the lease, conflicted paths, current changed paths, and the planned actions in this order: `resolve_out_of_order_conflict`, `stage_paths`, `commit`, `verify_production`, `push_branch`, and `open_pull_request`.

Repeating the same command with `--apply` finalizes the resolution only when all guards pass:

1. repository, source PR, promotion mode, target branch, source base/head, and target SHA are unchanged;
2. the worktree is the exact registered managed worktree;
3. operator unstaged and unmerged paths are within `conflicted_paths`;
4. AWF stages only the recorded conflicted paths, and no unmerged or unstaged resolution entry remains;
5. the final indexed delta is non-empty and a subset of `reviewed_paths`;
6. staged and committed deltas contain no conflict markers. This guard checks markers only; it does not prohibit valid trailing whitespace;
7. prepare and production verification succeed, then AWF rechecks the live target SHA before publication;
8. the promotion branch is still unpublished and no target PR exists.

AWF stages allowed resolution files, commits them, and publishes only after validation. The commit records `AWF-Resolution: manual-reviewed`.

If the source or target SHA changes while a manual resolution is pending, AWF returns `promotion_provenance_changed` and preserves the worktree. It does not reset or transplant a human resolution onto a different target.

## Production review requirement

An out-of-order production PR contains a tree that did not appear as the staging source head. The lifecycle Skill must require approval and successful checks on that exact production PR before merge. A clean automatic application does not waive this requirement.

Deployment health and cleanup keep the existing `status`, `finish`, and `gc` gates.

## State and compatibility

Registry schema migration adds eight lease metadata fields:

- `promotion_mode TEXT NOT NULL DEFAULT 'exact'` with values `exact` and `out_of_order`;
- `resolution_state TEXT NOT NULL DEFAULT 'none'` with values `none`, `pending`, `automatic`, and `manual_reviewed`;
- nullable `source_base_sha`, `source_head_sha`, and `target_base_sha` text columns;
- `reviewed_paths TEXT NOT NULL DEFAULT '[]'`, encoded as a sorted JSON string array;
- `conflicted_paths TEXT NOT NULL DEFAULT '[]'`, encoded as a sorted JSON string array;
- `protected_index_entries TEXT NOT NULL DEFAULT '[]'`, a sorted path→stage-0 mode+blob-OID-or-null mapping, stored as ordered `[path, [mode, blob_oid]|null]` JSON pairs.

New out-of-order leases persist all eight values before applying the patch, so a conflict has durable source, target, reviewed-path, conflict, and protected-index provenance without relying on a promotion commit that does not yet exist. When the first patch clean-stages reviewed paths outside `conflicted_paths`, AWF snapshots their stage-0 mode and blob OID index entries into `protected_index_entries` and pins them exactly on every resolution preview, apply, and retry. Direct `git add` or chmod/file-type mode tampering with a protected path returns `promotion_resolution_scope_mismatch`. Existing rows migrate to exact mode with no resolution, no out-of-order SHAs, and empty path/index-entry metadata. JSON output adds these fields without removing or renaming existing fields.

Blocked exact promotions keep their existing rebuild behavior. Out-of-order conflict recovery is eligible only for an unpublished blocked out-of-order lease with the exact request identity.

## Documentation changes

Update the release lifecycle Skill and CLI README with this decision table:

| Requirement | Workflow |
|---|---|
| A code may ship but must remain inactive | preserve staging promotion order; gate A at runtime |
| A code must stay out of production; B applies cleanly | single-source `--out-of-order` promotion |
| A code must stay out; B has a mechanical patch conflict | resolve only in the managed promotion worktree, then repeat preview/apply |
| B requires A's API, schema, or behavior | out-of-order promotion is invalid; remove the dependency or promote a compatible prerequisite first |

The Skill must state that staging squash commits are not production promotion inputs. AWF uses reviewed source PR deltas and managed production worktrees instead of direct cherry-picks.

## Tests

Add focused command and service tests for these contracts:

- parser and command forwarding for `--out-of-order`;
- exact mode remains the default;
- multiple source PRs and exclusions are rejected in out-of-order mode;
- a same-file, non-overlapping B delta promotes onto production without A;
- exact mode still blocks the same setup with `promotion_content_mismatch`;
- an overlapping hunk returns `out_of_order_conflict`, preserves the lease, and creates no PR;
- a valid managed resolution resumes through preview/apply and records `manual-reviewed`;
- edits outside conflicted and reviewed paths are blocked;
- source or target drift preserves and blocks the pending resolution;
- automatic and manual out-of-order promotions run production verification;
- promotion mode and resolution provenance survive registry round trips and appear in JSON;
- exact content-mismatch recovery and ordered multi-source promotion remain unchanged;
- documentation semantic checks include the new preview/apply and conflict-recovery sequence.

A smoke scenario must create divergent staging and production histories, squash or simulate A on staging, merge B from the A-containing staging base, and prove that the generated production branch contains B without A.

## Non-goals

- Automatically deciding that two changes are independent.
- Promoting multiple out-of-order source PRs in one command.
- Combining path exclusions with out-of-order mode.
- Automatically resolving a Git conflict.
- Allowing arbitrary edits in a blocked promotion worktree.
- Replacing feature flags when code-level isolation is unnecessary.
- Merging staging wholesale or permitting unmanaged production cherry-picks.
