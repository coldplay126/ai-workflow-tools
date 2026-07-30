---
name: release-worktree-lifecycle
version: 1.0.0
description: Use whenever handling deploy, production release, staging-to-main or staging-to-master promotion, release PR creation or merge, deployment worktree creation/reuse, or merged branch/worktree cleanup. Requires awf wt status/acquire/promote/finish/gc and forbids bypassing CLI safety blockers.
type: deployment-safety
---

# Release Worktree Lifecycle

## Overview

The `awf wt` CLI is authoritative; this skill defines the operator procedure only. Use the managed lifecycle rather than direct Git worktree operations. The required status preflight is non-destructive; preview-first applies to lifecycle operations before their `--apply` mutations. Every JSON result MUST determine the next step.

## Required preflight

Before acquiring, promoting, finishing, or collecting a worktree, MUST run:

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

## Production promotion

A production promotion MUST contain the source PR delta only, never the entire staging branch. Preview the isolated promotion first:

```sh
awf wt promote --source-pr <number> --to <branch> --repo-root <repo-root> --json
```

Confirm the source PR and target branch in the JSON result, then explicitly create the managed promotion PR:

```sh
awf wt promote --source-pr <number> --to <branch> --repo-root <repo-root> --apply --json
```

If `promote --apply` returns `ready`, MUST use or report the returned lease and MUST NOT repeat `--apply`.

## Deployment verification

After the production PR merges, MUST use the repository's existing CI and rollout path to prove the deployed revision is healthy. MUST NOT infer health from a merged PR, a passing local command, elapsed time, or an unavailable provider. Unknown or failed deployment health is `blocked`; preserve the release worktree and report the evidence gap.

## Cleanup

Only after deployment health is proven, preview the managed PR cleanup:

```sh
awf wt finish --repo-root <repo-root> --pr <number> --json
```

A `preview` finish result means review returned blockers, then explicitly apply only when none remain. A finish `--apply` result of `removed` ends cleanup and MUST be reported:

```sh
awf wt finish --repo-root <repo-root> --pr <number> --apply --json
```

If the PR is closed-unmerged, the worktree is dirty, or deployment health is unknown, MUST stop. Do not compensate with manual cleanup.

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

MUST NOT use direct worktree creation, removal, pruning, or other unmanaged deletion. MUST NOT merge staging wholesale, use `git branch --merged` as cleanup proof, stash, reset, clean, force-delete, or bypass a CLI blocker. These actions are not a substitute for `awf wt` status, doctor, acquire, promote, finish, or gc.

## JSON decision table

```json
{
  "schema": "awf.release-worktree-lifecycle/v1",
  "commands": {
    "status": "awf wt status --repo-root <repo-root> --refresh --json",
    "doctor": "awf wt doctor --repo-root <repo-root> --json",
    "acquire_preview": "awf wt acquire --initiative <initiative> --purpose feature --repo-root <repo-root> --json",
    "acquire_apply": "awf wt acquire --initiative <initiative> --purpose feature --repo-root <repo-root> --apply --json",
    "promote_preview": "awf wt promote --source-pr <number> --to <branch> --repo-root <repo-root> --json",
    "promote_apply": "awf wt promote --source-pr <number> --to <branch> --repo-root <repo-root> --apply --json",
    "finish_preview": "awf wt finish --repo-root <repo-root> --pr <number> --json",
    "finish_apply": "awf wt finish --repo-root <repo-root> --pr <number> --apply --json",
    "gc_preview": "awf wt gc --repo-root <repo-root> --merged --older-than 7d --dry-run --json",
    "gc_apply": "awf wt gc --repo-root <repo-root> --merged --older-than 7d --apply --json"
  },
  "safety": {
    "preflight": "required_non_destructive_status_refresh",
    "lease_reuse": "exact",
    "promotion_scope": "source_pr_delta_only",
    "deployment_health": "repository_rollout_evidence",
    "blocked_action": "preserve_worktree_report_code_message",
    "preview_before_apply": ["acquire", "promote", "finish", "gc"],
    "stop_conditions": [
      "deployment_health_unknown",
      "closed_unmerged",
      "dirty_worktree"
    ],
    "forbidden_fallbacks": [
      "direct_worktree_mutation",
      "staging_wholesale_merge",
      "branch_merged_heuristic",
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
      "promote": "review_then_apply_explicitly",
      "finish": "review_blockers_then_apply",
      "gc": "review_blockers_then_apply"
    },
    "ready": {
      "status": "inspect_select_lifecycle_action",
      "acquire_apply": "use_or_report_returned_lease",
      "promote_apply": "use_or_report_returned_lease"
    },
    "removed": "report_completion",
    "blocked": "preserve_worktree_report_code_message"
  }
}
```
