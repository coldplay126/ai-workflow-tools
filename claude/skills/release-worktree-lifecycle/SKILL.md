---
name: release-worktree-lifecycle
version: 1.0.0
description: Use whenever handling deploy, production release, staging-to-main or staging-to-master promotion, release PR creation or merge, deployment worktree creation/reuse, or merged branch/worktree cleanup. Requires awf wt status/acquire/promote/finish/gc and forbids bypassing CLI safety blockers.
type: deployment-safety
conditions:
  trigger:
    - handling deploy, production release, promotion, release PR, managed deployment worktree creation or reuse, or merged worktree cleanup
  skip:
    - no release, deployment, promotion, or worktree lifecycle action is involved
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

MUST NOT use direct worktree creation, removal, pruning, direct Git or filesystem cleanup, or other unmanaged deletion. MUST NOT merge staging wholesale, use `git branch --merged` as cleanup proof, stash, reset, clean, force-delete, or bypass a CLI blocker. These actions are not a substitute for `awf wt` status, doctor, acquire, promote, finish, or gc.

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
    "promote_preview": "awf wt promote --source-pr <number> --to <branch> --repo-root <repo-root> --json",
    "promote_apply": "awf wt promote --source-pr <number> --to <branch> --repo-root <repo-root> --apply --json",
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
    "imported_pr_lifecycle": {
      "pr_provenance": "already_merged_exact_branch_and_head",
      "same_pr": "reuse",
      "different_pr": "blocked",
      "github_external_failure": "exit_4",
      "runtime_source_before_removal": "install_cli_and_skill_from_stable_merged_main_and_verify_links"
    },
    "preview_before_apply": ["acquire", "promote", "import", "adopt", "finish", "gc"],
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
