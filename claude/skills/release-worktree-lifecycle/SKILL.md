---
name: release-worktree-lifecycle
version: 1.1.0
description: Use whenever handling deploy, production release, staging-to-main or staging-to-master promotion, release PR creation or merge, managed feature PR linkage, deployment worktree creation/reuse, or merged branch/worktree cleanup. Requires awf wt status/acquire/link-pr/promote/finish/gc and forbids bypassing CLI safety blockers.
type: deployment-safety
conditions:
  trigger:
    - handling deploy, production release, promotion, release PR, managed feature PR linkage, managed deployment worktree creation or reuse, or merged worktree cleanup
  skip:
    - no release, deployment, promotion, or worktree lifecycle action is involved
---

# Release Worktree Lifecycle

## Overview

The `awf wt` CLI is authoritative; this skill defines the operator procedure only. Use the managed lifecycle rather than direct Git worktree operations. The required status preflight is non-destructive; preview-first applies to lifecycle operations before their `--apply` mutations. Every JSON result MUST determine the next step.

## Required preflight

Before acquiring, linking, promoting, finishing, or collecting a worktree, MUST run:

```sh
awf wt status --repo-root <repo-root> --refresh --json
```

`status --refresh` is a required non-destructive state-refresh preflight. A `ready` status means inspect the refreshed leases and select the appropriate lifecycle action; it MUST NOT itself trigger `--apply`.

If status indicates a registry or Git mismatch, MUST inspect it without repair:

```sh
awf wt doctor --repo-root <repo-root> --json
```

A mismatch or any `blocked` result is a stop condition. Report its code and message; preserve the worktree.

## Feature worktree

Preview the managed feature lease:

```sh
awf wt acquire --initiative <initiative> --purpose feature --repo-root <repo-root> --json
```

On `reuse`, MUST use the exact returned lease and MUST NOT create another worktree. On `preview`, inspect the returned branch, base, path, and ownership before explicitly applying it:

```sh
awf wt acquire --initiative <initiative> --purpose feature --repo-root <repo-root> --apply --json
```

If `acquire --apply` returns `ready`, MUST use or report the returned lease and MUST NOT repeat `--apply`.

## Managed feature PR linkage

Use this only when an active managed feature worktree's PR was created and
merged outside AWF before the lease recorded `target_pr`. After the required
status preflight, preview the explicit link:

```sh
awf wt link-pr --lease <id> --pr <merged-pr> --json
```

The preview MUST identify the intended lease, PR, branch, and exact head SHA.
Only then explicitly apply:

```sh
awf wt link-pr --lease <id> --pr <merged-pr> --apply --json
```

`link-pr` accepts only a clean, managed `feature` lease that is `ACTIVE` with
no PR link, or the exact already-linked `CLEANABLE` lease for idempotent reuse.
The supplied PR MUST be merged and MUST exactly match the lease repository,
branch, and current registered/check-out worktree HEAD. The recorded
acquisition SHA may be older after normal feature commits. Apply revalidates
local Git after the GitHub lookup, replaces the recorded SHA with the
independently verified current PR/worktree SHA, then atomically records
`target_pr`, `CLEANABLE`, and `not_required`. The same linked PR returns
`reuse`; any lease-state, repository, branch, head, cleanliness, or merge
mismatch is `blocked`. A GitHub external failure is exit code `4`. MUST NOT
infer a PR from branch history, adopt the lease, or use direct Git or registry
mutation.

After `ready` or `reuse`, restart at the required status preflight, then use
the normal `finish` preview/apply procedure. The linked result is cleanup
evidence only; it is not permission to skip any finish gate.

```sh
awf wt status --repo-root <repo-root> --refresh --json
awf wt finish --repo-root <repo-root> --pr <merged-pr> --json
awf wt finish --repo-root <repo-root> --pr <merged-pr> --apply --json
```

## Production promotion

A production promotion MUST contain only the ordered source PR deltas, never
the entire staging branch. `--source-pr` is repeatable and MUST follow staging
merge order. Every previous PR merge SHA MUST equal the next PR base SHA; a gap
is `blocked`.

`--exclude-path` is repeatable. Every excluded value MUST be a unique, exact,
repository-relative path reviewed in the source PRs, and at least one reviewed
path MUST remain. MUST NOT use exclusions to substitute an unreviewed delta.
For a single-source promotion with no exclusions, pass one `--source-pr` and
omit `--exclude-path`.

Preview the isolated promotion:

```sh
awf wt promote --source-pr <number> --to <branch> --repo-root <repo-root> --json
```

Confirm the ordered `source_prs`, each source base/head/merge SHA, excluded
paths, and target branch in the JSON result. Each source MUST satisfy the
configured review policy, checks, and staging base. The promotion MUST pass the
configured prepare and production verification commands; a prepare command
that leaves the worktree dirty is `blocked`. Only then explicitly create the
managed promotion PR:

```sh
awf wt promote --source-pr <number> --to <branch> --repo-root <repo-root> --apply --json
```

If `promote --apply` returns `ready`, MUST use or report the returned lease and
MUST NOT repeat `--apply`. A blocked promotion is resumable only through the
CLI's verified prepare, verification, or publication recovery paths; MUST NOT
manually repair or recreate its lease.

## Out-of-order production promotion

Use this opt-in path only when one reviewed staging PR must reach production
without an earlier staging change. It is not a fallback from exact promotion.

| Situation | Required workflow |
| --- | --- |
| A code may ship but must remain inactive | Preserve staging promotion order and gate A at runtime with a feature flag or equivalent. |
| A code must stay out of production; B applies cleanly | Use the single-source `--out-of-order` promotion below. |
| A code must stay out; B has a mechanical patch conflict | Resolve only in the managed promotion worktree, then replay preview/apply. |
| B requires A's API, schema, or behavior | Stop. Out-of-order promotion is invalid until the dependency is removed or a compatible prerequisite is promoted. |

`--out-of-order` requires exactly one `--source-pr` and MUST NOT use
`--exclude-path`. The source PR must be merged into the configured staging
branch and still satisfy the configured source review and checks policy.
Multiple sources or exclusions stop with `invalid_out_of_order_promotion`.
Renamed source paths are unsupported and stop with
`unsupported_out_of_order_rename`. A source dependency on A is a stop
condition; a clean patch does not prove that B is independent.

Preview the code-isolated synthetic production result, inspect its source
base/head, target base, reviewed paths, mode, and verification actions, then
explicitly apply it:

```sh
# Initial preview and apply.
awf wt promote --source-pr <number> --to <branch> --out-of-order --repo-root <repo-root> --json
awf wt promote --source-pr <number> --to <branch> --out-of-order --repo-root <repo-root> --apply --json
# After an AWF-reported conflict, replay the same preview and apply commands.
awf wt promote --source-pr <number> --to <branch> --out-of-order --repo-root <repo-root> --json
awf wt promote --source-pr <number> --to <branch> --out-of-order --repo-root <repo-root> --apply --json
```

A failed three-way apply stops with `out_of_order_conflict`. AWF preserves an
unpublished managed worktree with pending source, target, reviewed-path, and
conflicted-path provenance. The operator may edit only the conflicted files
returned by AWF. MUST NOT use `git add`, `git commit`, `git reset`,
`git cherry-pick`, or `git push`.

Any direct cherry-pick is forbidden for production promotion. AWF reconstructs
reviewed PR deltas only through `awf wt promote`.

After editing, replay the same preview command. It reports the blocked lease,
conflicted paths, and current changed paths. Replay the same command with
`--apply` only when source and target provenance are unchanged and every
changed or unmerged path remains within the reported conflicted paths. A path
outside that set returns `promotion_resolution_scope_mismatch`. AWF stages the
allowed conflict files; an unmerged index entry that remains after staging
returns `promotion_resolution_unmerged`.

All conflict markers must be removed before apply. If a marker remains, AWF
does not publish and preserves the worktree. If either reviewed source SHA or
the target SHA changes, stop with `promotion_provenance_changed`; preserve the
worktree rather than transplanting a resolution.

AWF stages, commits, verifies, pushes, and publishes the eligible resolution.
The synthetic production PR requires approval and successful checks on that
exact production PR before merge, even after an automatic clean application.
Staging squash commits are not production promotion inputs. A direct staging
squash cherry-pick is forbidden.

## Deployment verification

After the production PR merges, MUST use the repository's existing CI and rollout path to prove the deployed revision is healthy. MUST NOT infer health from a merged PR, a passing local command, elapsed time, or an unavailable provider. Unknown or failed deployment health is `blocked`; preserve the release worktree and report the evidence gap.

## Imported legacy worktree cleanup

Use this pressure-safe procedure only for an imported worktree whose source
branch must be preserved until its exact merged PR has been linked and the
normal finish gates pass. In this section, `<root>` is the parent directory
whose direct-child repositories and worktrees are inventoried, `<id>` is the
selected imported lease ID, `<merged-pr>` is its already-merged PR number, and
`<repo-root>` is that repository's root.

Before this procedure, identify only the source worktree to remove. Before
removing a source worktree backing installed CLI or Skill links, MUST install
the CLI and Skill from a stable merged-main checkout. Verify that installed
`awf` and every Skill link no longer resolve to the source worktree and instead
resolve to that checkout. Do not remove unrelated imported worktrees or
branches.

```sh
awf wt import --root <root> --dry-run --json
awf wt import --root <root> --apply --json
awf wt adopt --lease <id> --pr <merged-pr> --json
awf wt adopt --lease <id> --pr <merged-pr> --apply --json
awf wt status --repo-root <repo-root> --refresh --json
awf wt finish --repo-root <repo-root> --pr <merged-pr> --json
awf wt finish --repo-root <repo-root> --pr <merged-pr> --apply --json
```

Inspect every JSON result before running the next command. `adopt --pr`
accepts only an already-merged PR whose number, branch, and head SHA exactly
match the imported lease. MUST NOT infer a PR automatically. The same linked
PR returns `reuse`; a different PR, a dirty worktree, or any Git/PR branch or
head mismatch is `blocked`. A GitHub external failure is exit code `4`. MUST
stop on any blocker or external error.

Import preserves the local and remote branch. `finish` removes only the explicitly
linked worktree after the normal merged-PR, clean-worktree, and deployment
health gates pass. MUST NOT use direct Git or filesystem cleanup.

## Cleanup

Only after deployment health is proven, preview the managed PR cleanup:

```sh
awf wt finish --repo-root <repo-root> --pr <merged-pr> --json
```

A `preview` finish result means review returned blockers, then explicitly apply only when none remain. A finish `--apply` result of `removed` ends cleanup and MUST be reported:

```sh
awf wt finish --repo-root <repo-root> --pr <merged-pr> --apply --json
```

If the PR is closed-unmerged, the worktree is dirty, or deployment health is unknown, MUST stop. Do not compensate with manual cleanup; imported branches remain preserved unless the explicitly linked worktree is removed through `finish`.

## Bulk cleanup

Bulk cleanup MUST begin with a preview:

```sh
awf wt gc --repo-root <repo-root> --merged --older-than 7d --dry-run --json
```

A `preview` GC result means review every candidate and blocker, then apply only the proven-safe set. A GC `--apply` result of `removed` MUST be reported:

```sh
awf wt gc --repo-root <repo-root> --merged --older-than 7d --apply --json
```

## Blocker response

For `blocked`, MUST report the result code, message, command, and deployment/PR evidence available. MUST preserve the worktree and branch. Resolve the reported condition through the managed lifecycle, then restart at preflight.

For `removed`, MUST report completion and take no further cleanup action for that lease.

## Forbidden fallbacks

MUST NOT use direct worktree creation, removal, pruning, direct Git or filesystem cleanup, or other unmanaged deletion. MUST NOT merge staging wholesale, use `git branch --merged` as cleanup proof, stash, reset, clean, force-delete, or bypass a CLI blocker. These actions are not a substitute for `awf wt` status, doctor, acquire, link-pr, promote, finish, or gc.

## JSON decision table

```json
{
  "schema": "awf.release-worktree-lifecycle/v1",
  "commands": {
    "status": "awf wt status --repo-root <repo-root> --refresh --json",
    "doctor": "awf wt doctor --repo-root <repo-root> --json",
    "import_preview": "awf wt import --root <root> --dry-run --json",
    "import_apply": "awf wt import --root <root> --apply --json",
    "adopt_preview": "awf wt adopt --lease <id> --pr <merged-pr> --json",
    "adopt_apply": "awf wt adopt --lease <id> --pr <merged-pr> --apply --json",
    "acquire_preview": "awf wt acquire --initiative <initiative> --purpose feature --repo-root <repo-root> --json",
    "acquire_apply": "awf wt acquire --initiative <initiative> --purpose feature --repo-root <repo-root> --apply --json",
    "link_pr_preview": "awf wt link-pr --lease <id> --pr <merged-pr> --json",
    "link_pr_apply": "awf wt link-pr --lease <id> --pr <merged-pr> --apply --json",
    "promote_preview": "awf wt promote --source-pr <number> --to <branch> --repo-root <repo-root> --json",
    "promote_apply": "awf wt promote --source-pr <number> --to <branch> --repo-root <repo-root> --apply --json",
    "out_of_order_promote_preview": "awf wt promote --source-pr <number> --to <branch> --out-of-order --repo-root <repo-root> --json",
    "out_of_order_promote_apply": "awf wt promote --source-pr <number> --to <branch> --out-of-order --repo-root <repo-root> --apply --json",
    "out_of_order_resolution_preview": "awf wt promote --source-pr <number> --to <branch> --out-of-order --repo-root <repo-root> --json",
    "out_of_order_resolution_apply": "awf wt promote --source-pr <number> --to <branch> --out-of-order --repo-root <repo-root> --apply --json",
    "finish_preview": "awf wt finish --repo-root <repo-root> --pr <merged-pr> --json",
    "finish_apply": "awf wt finish --repo-root <repo-root> --pr <merged-pr> --apply --json",
    "gc_preview": "awf wt gc --repo-root <repo-root> --merged --older-than 7d --dry-run --json",
    "gc_apply": "awf wt gc --repo-root <repo-root> --merged --older-than 7d --apply --json"
  },
  "safety": {
    "preflight": "required_non_destructive_status_refresh",
    "lease_reuse": "exact",
    "promotion_scope": "source_pr_delta_only",
    "deployment_health": "repository_rollout_evidence",
    "blocked_action": "preserve_worktree_report_code_message",
    "out_of_order": {
      "mode": "explicit_opt_in",
      "exact_mode": "default",
      "single_source": true,
      "exclude_paths": "forbidden",
      "production_pr_review": "required",
      "production_pr_checks": "required",
      "direct_cherry_pick": "forbidden",
      "staging_squash_input": "forbidden",
      "conflict_resolution": "managed_conflicted_files_only_replay_same_command",
      "dependency_conflict": "blocked",
      "rename": "unsupported",
      "blocker_codes": [
        "invalid_out_of_order_promotion",
        "unsupported_out_of_order_rename",
        "out_of_order_conflict",
        "promotion_provenance_changed",
        "promotion_resolution_scope_mismatch",
        "promotion_resolution_unmerged"
      ]
    },
    "managed_feature_pr_link": {
      "lease_state": "active_unlinked_or_cleanable_exact_reuse",
      "pr_provenance": "already_merged_exact_repository_branch_and_current_worktree_head",
      "apply_transition": "replace_recorded_head_then_cleanable_not_required",
      "same_pr": "reuse",
      "different_pr": "blocked",
      "github_external_failure": "exit_4"
    },
    "imported_pr_lifecycle": {
      "pr_provenance": "already_merged_exact_branch_and_head",
      "same_pr": "reuse",
      "different_pr": "blocked",
      "github_external_failure": "exit_4",
      "runtime_source_before_removal": "install_cli_and_skill_from_stable_merged_main_and_verify_links"
    },
    "preview_before_apply": [
      "acquire",
      "link-pr",
      "promote",
      "out_of_order_promote",
      "out_of_order_resolution",
      "import",
      "adopt",
      "finish",
      "gc"
    ],
    "stop_conditions": [
      "deployment_health_unknown",
      "closed_unmerged",
      "dirty_worktree"
    ],
    "forbidden_fallbacks": [
      "direct_worktree_mutation",
      "staging_wholesale_merge",
      "branch_merged_heuristic",
      "direct_cherry_pick",
      "stash",
      "reset",
      "force_delete",
      "unmanaged_deletion"
    ]
  },
  "decisions": {
    "reuse": "use_exact_lease",
    "preview": {
      "acquire": "review_then_apply_explicitly",
      "link_pr": "review_then_apply_explicitly",
      "promote": "review_then_apply_explicitly",
      "out_of_order_promote": "review_then_apply_explicitly",
      "out_of_order_resolution": "review_same_blocked_lease_then_apply_explicitly",
      "finish": "review_blockers_then_apply",
      "gc": "review_blockers_then_apply"
    },
    "ready": {
      "status": "inspect_select_lifecycle_action",
      "acquire_apply": "use_or_report_returned_lease",
      "link_pr_apply": "restart_status_preflight_then_finish",
      "promote_apply": "use_or_report_returned_lease",
      "out_of_order_promote_apply": "use_or_report_returned_lease",
      "out_of_order_resolution_apply": "use_or_report_returned_lease"
    },
    "removed": "report_completion",
    "blocked": "preserve_worktree_report_code_message"
  }
}
```
