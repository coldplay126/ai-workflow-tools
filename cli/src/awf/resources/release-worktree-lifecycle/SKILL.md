---
name: release-worktree-lifecycle
version: 1.6.0
description: Use whenever handling deploy, production release, staging-to-main or staging-to-master promotion (synthetic or same-branch --source-branch), release PR creation or merge, managed feature PR linkage, managed deployment worktree creation or reuse, archive-backed explicit abandonment, local or remote branch discard, archive repack or restore, stale sync conflict disposal or recovery, or merged branch/worktree cleanup. Requires awf wt status/acquire/link-pr/sync/recover-sync/discard-sync/promote/release/discard-promotion/archive-discard/discard-local-branch/discard-remote-branch/archive-repack/archive-restore/finish/gc and forbids bypassing CLI safety blockers. Ordinary commits and non-force pushes on a developer's own development branch are not lifecycle actions and are not restricted by this skill.
type: deployment-safety
conditions:
  trigger:
    - handling deploy, production release, promotion, release PR, managed feature PR linkage, managed deployment worktree creation or reuse, local or remote branch discard, or merged worktree cleanup
  skip:
    - no release, deployment, promotion, or worktree lifecycle action is involved
    - only ordinary add, commit, or non-force push work on a developer-owned development branch is requested
---

# Release Worktree Lifecycle

## Overview

The `awf wt` CLI is authoritative; this skill defines the operator procedure only. It governs lifecycle actions: acquiring or reusing a managed worktree, linking a merged PR, synchronizing, promoting, publishing a release bridge, finishing, and collecting. Use the managed lifecycle rather than direct Git worktree operations for those actions. The required status preflight is non-destructive; preview-first applies to lifecycle operations before their `--apply` mutations. Every JSON result MUST determine the next step.

This skill does not govern ordinary development commits. Read the scope
boundary below before applying any restriction in this document to a branch.

## Scope boundary: ordinary development versus AWF-owned synthetic branches

Two kinds of branches appear in this workflow, and the restrictions in this
skill apply to only one of them.

**Ordinary development branches** are branches a developer commits to: the
user's own branches in any worktree, and the checked-out branch inside a
managed `feature` lease (`awf/<initiative>/feature` or an explicit `--branch`).
On these branches, normal `git add`, `git commit`, `git fetch`, `git pull`,
and non-force `git push` are ordinary work, and the repository's own hooks run
as configured. A commit needs no separate approval turn; the task that asked
for the change already grants commit permission. A branch whose name contains
`feature`, `release`, `hotfix`, or a version is still an ordinary development
branch: the name alone never makes it an AWF sealed release bridge, synthetic
promotion branch, or protected AWF object. Do not require `awf wt` preflight,
preview, or a JSON result before an ordinary commit or push.

**AWF-owned synthetic branches and worktrees** are created by AWF itself:
`PROMOTE` leases with their `awf/<initiative>/promote` or release-bridge
branches, `awf/sync-<pair>-<source>/feature` synchronization branches, and any
managed worktree that AWF reports as blocked in out-of-order conflict
resolution or sync recovery. Their commits are reconstructed from reviewed PR
deltas and their index entries are pinned. Only there do the immutable index
and commit guards apply: MUST NOT `git add`, `git commit`, `git reset`,
`git cherry-pick`, `git stash`, or `git push` in them, and MUST NOT mutate
them outside the `awf wt` commands documented here.

**Commit permission is separate from merge and deployment permission.**
Committing to a development branch never authorizes merging into the
production branch, publishing a promotion, or deploying. Those remain
lifecycle actions that follow the preflight, preview, and blocker rules below.

**Data-loss operations keep their evidence requirement on managed objects.**
Ordinary managed cleanup requires recorded PR and deployment evidence and an
explicit request: use `finish`, `gc`, `discard-promotion`, or `discard-sync`,
never direct deletion. Explicit abandonment is different: `archive-discard`
may remove an eligible lease without PR/deployment evidence only after a
verified private archive; `discard-local-branch` and
`discard-remote-branch` may delete one approved ref without that evidence only
after a verified commit bundle, matching token, and all command blockers.
Neither exception permits force-pushing, `git reset --hard`, or bulk cleanup.

**Unmanaged user worktrees are not AWF property.** The registry protects the
leases it records. It is not a reason to forbid ordinary Git operations in a
worktree AWF does not manage, and a `status` warning about an unmanaged lease
is information, not a stop condition for the user's own work.

**Preview, then apply in the same turn.** When the user has already requested
a lifecycle operation, run its preview. If the preview returns `preview` with
no blocker, no warning that changes the outcome, and no choice the user has not
already made, run the matching `--apply` in the same turn; a `reuse` result
needs no apply at all. Preview-first is a verification step, not a second
approval round. Stop and report instead when the preview is `blocked`, when it
exposes an option the user did not choose (a different lease, branch, base,
target, path set, or deletion), or when the user asked only for an inspection.

## Required preflight

Before acquiring, linking, synchronizing, opening or publishing a release bridge, promoting, finishing, or collecting a worktree, MUST run:

```sh
awf wt status --repo-root <repo-root> --refresh --json
```

`status --refresh` is a required non-destructive state-refresh preflight for lifecycle actions; it is not required before an ordinary commit or push on a development branch. A `ready` status means inspect the refreshed leases and select the appropriate lifecycle action; it MUST NOT itself trigger `--apply`.

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

The feature base resolves in this order: explicit `--base`, then
`worktree.feature_base`, then `worktree.production_branch`, then
`worktree.default_base`, then the remote default branch. `worktree.default_base`
keeps its existing meaning as the staging branch that source PRs are verified
against; set `worktree.feature_base` (or rely on `production_branch`) so that a
solo feature branch starts from production rather than from staging.

On `reuse`, MUST use the exact returned lease and MUST NOT create another worktree. On `preview`, inspect the returned branch, base, path, and ownership before applying it:

```sh
awf wt acquire --initiative <initiative> --purpose feature --repo-root <repo-root> --apply --json
```

If `acquire --apply` returns `ready`, MUST use or report the returned lease and
MUST NOT repeat `--apply`. The lease's checked-out branch is an ordinary
development branch: commit, push, and open pull requests on it normally.

## Recommended solo flow: main-based feature, staging PR, same-branch main PR

This is the default path for a single developer who validates on `staging`
and releases to `main` without a synthetic promotion branch. Configure the
repository once:

```toml
[worktree]
default_base = "staging"      # staging verification target for source PRs
production_branch = "main"
feature_base = "main"         # feature branches start from production

[promotion]
# Default: approved_or_self_merged. Set "approved" only when a reviewer exists.
source_review_policy = "approved_or_self_merged"
```

**Step 1 — branch.** Create the feature branch from `main` with `acquire`
above, or in your own worktree. Commit and push to it normally; no preflight or
preview per commit.

**Step 2 — staging PR.** Open a pull request from the feature branch to
`staging` and merge it once its checks pass. Under `approved_or_self_merged`
the author's own merge satisfies the review policy; MUST NOT request an
unavailable external reviewer. An explicit `approved` policy is enforced as
written. A repository without configured CI has an empty checks rollup, which
passes; MUST NOT demand that CI be added. The staging PR is an intermediate
step: MUST NOT merge it with `--delete-branch`, MUST NOT run `finish` against
it, and MUST NOT delete the local or remote feature branch, because the same
branch head becomes the production PR. For a managed feature lease, `link-pr`
with the merged staging PR is optional bookkeeping: it records
`record_staging_validation` (source PR, base SHA, head SHA), keeps the lease
`ACTIVE`, and leaves `target_pr` empty, so it never makes the lease cleanable.
Fixing the same feature, verifying it on staging again, and then promoting is
normal: each round is one more staging PR from the same branch, and the
production step below always names only the latest one.

**Step 3 — same-branch production PR.** After the required status preflight,
preview it:

```sh
awf wt status --repo-root <repo-root> --refresh --json
awf wt promote --source-pr <staging-pr> --to main --source-branch --repo-root <repo-root> --json
```

The preview returns exactly one `open_pull_request` action with
`promotion_mode: "source_branch"`, `source_branch`, `source_pr`, `tested_head`,
`source_head_sha`, `source_base_sha`, `reviewed_base_sha`, `target_branch`, and
`target_base_sha`, plus the ordered `source_prs`/`sources` validation chain when
earlier staging rounds of the same branch contribute to the head. Confirm that
`source_branch` is the feature branch, `tested_head` is the merged staging PR
head, every listed source is a staging PR of this branch, and the target is
the configured production branch, then apply:

```sh
awf wt promote --source-pr <staging-pr> --to main --source-branch --repo-root <repo-root> --apply --json
```

Apply opens exactly one pull request from the original remote feature branch at
that same verified head into the target through the existing GitHub client. It
creates no lease, worktree, synthetic commit, or push, and it never merges the
PR. The PR body carries `Validation-*` trailers for the whole chain. `ready`
and `reuse` return the same action with `target_pr` and `url`; an existing open
PR from that branch to the target is reused when it matches and is
`target_pr_mismatch` when it does not.

The source gates are unchanged: the source PR MUST be `MERGED` into the
configured staging branch, satisfy the review policy and checks, and not be a
synchronization PR; the target MUST be the configured production branch.
`--source-branch` accepts exactly one `--source-pr` and MUST NOT be combined
with `--exclude-path` or `--out-of-order`; any of those is
`invalid_source_branch_promotion`. The remote feature branch head MUST equal
`tested_head`, or the result is `source_branch_provenance_changed`. A branch
that carries staging commits absent from the target (for example a merge of
`staging` back into the feature) is `source_branch_contains_staging`. The
production candidate must start within the validated history and touch only
its reviewed paths; changes already on main may share those files. An invalid
candidate is `source_delta_mismatch`. No managed lease is required: proof comes from the
PR, the remote head, and the Git graph. When a managed feature lease has
recorded staging evidence, AWF additionally compares it, and evidence older
than a newer local commit is `staging_evidence_stale`. This path needs no
`prepare` or `verify.production.commands` configuration: there is no synthetic
result to verify locally, and the head was already verified on the staging PR.

**Step 4 — merge to main.** Merge the production PR only after the
repository's `main` branch protections and required checks pass on that exact
head. Approval is required only when branch policy requires it; a solo
repository MUST NOT invent an unavailable reviewer. If the remote feature branch
moves after the staging merge, the production PR head is no longer the verified
head: the preview reports `source_branch_provenance_changed`, and a managed
lease whose recorded staging evidence predates a new local commit reports
`staging_evidence_stale`. MUST NOT force-push the branch back to the old head
and MUST NOT switch to synthetic promotion to bypass the drift. Instead verify
the new head through another staging PR from the same branch, merged under the
same gates (then `link-pr` it if the lease keeps evidence), and replay the
preview naming only that latest staging PR. AWF walks the validation chain
itself: from the latest PR it finds the earlier merged staging PR of the same
branch whose head is exactly the latest reviewed merge-base, repeats until the
chain root is an ancestor of the production target, verifies merged state,
review, checks, graph, and paths for every predecessor, requires the remote head
to be the latest verified head, and exposes the chain in `source_prs`/`sources`
and the PR body. A predecessor that no staging PR of this branch reviewed is
`source_branch_contains_staging`; an ambiguous, reversed, or cyclic chain is
`source_validation_chain_invalid`; exhausting the bounded PR scan fails closed
as external `source_branch_unavailable`. Choosing the exact or cumulative
synthetic path in any of those cases is a user decision, never an automatic
switch.

**Step 5 — deleted remote branch.** If GitHub's automatic branch deletion
removed the remote feature branch after the staging merge, the preview stops
with `source_branch_unavailable`. AWF MUST NOT fall back to synthetic promotion
silently and MUST NOT force-push. When the local branch still points at the
verified `tested_head`, restore it with an ordinary non-force
`git push -u origin <feature-branch>` and replay the preview. If no local ref
retains that head, stop and report; choosing synthetic `promote` instead is a
new decision that belongs to the user.

**Step 6 — cleanup.** Only after the production PR is merged and deployment
health is proven (see Cleanup), record the production PR on the managed feature
lease with the `link-pr` procedure below, passing the production PR number.
That final link, not the staging link, records `target_pr` and `CLEANABLE`.
Then run `finish` with the production PR number. `finish` removes the lease
worktree and, for an `awf/`-prefixed managed branch, deletes its local and
remote branch, which is why it MUST NOT run against the intermediate staging
PR. An unmanaged worktree has no lease; its branch cleanup is the user's
ordinary Git work.

## Managed feature PR linkage

Use this when an active managed feature worktree's PR was created and merged
outside AWF. It has two outcomes. Linking the merged production PR (or, in a
staging-only repository without a configured production branch, the merged
staging PR) records `target_pr` and makes the lease cleanable. Linking a merged
staging PR while `worktree.default_base` and `worktree.production_branch` are
distinct records staging validation only. After the required status preflight,
preview the explicit link:

```sh
awf wt link-pr --lease <id> --pr <merged-pr> --json
```

The preview MUST identify the intended lease, PR, branch, exact head SHA, and
action kind (`record_staging_validation` or the cleanup link). Only then
explicitly apply:

```sh
awf wt link-pr --lease <id> --pr <merged-pr> --apply --json
```

`link-pr` accepts only a clean, managed `feature` lease that is `ACTIVE` with
no cleanup PR link, or the exact already-linked `CLEANABLE` lease for
idempotent reuse. The supplied PR MUST be merged and MUST exactly match the
lease repository, branch, and current registered/check-out worktree HEAD. The
recorded acquisition SHA may be older after normal feature commits. Apply
revalidates local Git after the GitHub lookup and replaces the recorded SHA with
the independently verified current PR/worktree SHA. For the production PR it
then atomically records `target_pr`, `CLEANABLE`, and `not_required`; for an
intermediate staging PR it records `source_pr`, `source_base_sha`, and
`source_head_sha`, keeps `ACTIVE`, and leaves `target_pr` empty. The same
linked PR at the same head returns `reuse`; any lease-state, repository, branch,
head, cleanliness, or merge mismatch is `blocked`, and a production link whose
recorded staging evidence predates a newer local commit is
`staging_evidence_stale`. A GitHub external failure is exit code `4`. MUST NOT
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

## Production-to-staging branch synchronization

Use `awf wt sync` after production receives content that is absent from the
configured staging branch. It accepts only the configured
`worktree.production_branch` as `--from` and `worktree.default_base` as `--to`.
It reconstructs the source-only delta since the live merge base on the latest
target; it MUST NOT merge either branch wholesale.

After the required status preflight, inspect and then apply:

```sh
awf wt sync --from main --to staging --repo-root <repo-root> --json
awf wt sync --from main --to staging --repo-root <repo-root> --apply --json
```

A `noop` result means staging already contains the production content and MUST
NOT create a worktree, branch, or PR. Apply pins both live branch SHAs, uses a
managed feature lease, preserves clean staging-only three-way merge results
(including Git modes), requires configured prepare and production verification,
then rechecks both remote SHAs before and after publication. It refuses an
existing sync PR, a pre-existing remote sync branch, and an incomplete open-PR
scan. An interrupted clean publication resumes the same verified lease; a true
source/target conflict, drift, dirty prepare, or failed verification is
`blocked` and preserves the managed worktree and branch.

Every sync commit and PR carries `AWF-No-Promote: true`, and every generated
branch uses the permanently reserved `awf/sync-<pair>-<source>/feature` shape.
`wt promote` and `wt release add` MUST reject either identity with
`source_pr_not_promotable`, avoiding a production↔staging loop. Merge the sync
PR only after repository checks and review policy pass; the command does not
bypass or invent those gates.

## Stale unpublished synchronization-conflict discard

Use `discard-sync` only for one AWF-owned `FEATURE` synchronization lease that
is `BLOCKED` by `sync_target_conflict`, has no target PR in any state, remote
branch, or cleanup reservation, and is no longer resumable because its recorded
source or target pin is stale. It is not general feature-lease disposal,
conflict resolution, or a way to remove a current-pinned sync lease.

After the required status preflight, inspect both exact preview actions:

```sh
awf wt status --repo-root <repo-root> --refresh --json
awf wt discard-sync --lease <id> --repo-root <repo-root> --json
```

Eligibility requires the configured production-to-staging initiative, reserved
branch, base, source/target pins, non-empty reviewed paths, one registered
non-symlink, non-bare, non-detached worktree, and matching worktree HEAD/local
branch target pin. Its unmerged state MUST comprise only Git's valid unmerged
status classes inside reviewed paths; every clean staged entry MUST exactly
match its source pin. Untracked, renamed, unstaged, unrelated, or
source-pin-mismatched changes stop the operation. Review `remove_worktree` and
`delete_local_branch`, then apply the same lease:

```sh
awf wt discard-sync --lease <id> --repo-root <repo-root> --apply --json
```

Apply locks and revalidates the repository, reserves cleanup, holds the branch,
rechecks the registered worktree root's non-symlink device/inode identity,
restores only the proven reviewed conflict paths to the target pin, then uses a
non-force worktree removal. A newly appearing untracked or out-of-scope change
therefore makes Git refuse removal rather than being deleted. After that
refusal, AWF reapplies the recorded source-only binary patch to the target
index and releases the reservation only when the original recorded conflict
paths are reconstructed exactly; the late file remains untouched. A path
identity mismatch or failed reconstruction retains `cleanup_reserved`
fail-closed. Successful cleanup completes the registry and compare-and-deletes
only the local branch. It NEVER deletes a remote branch or PR.

## Current synchronization-conflict recovery

Use `recover-sync` only for one AWF-owned, unpublished `BLOCKED`
`sync_target_conflict` lease whose configured production and staging pins are
still current and whose production verification commands are configured. It is
not a stale-lease retry or general feature recovery. Recovery supports only the
recorded `UU` conflict paths; every operator edit/unmerged path MUST remain a
subset of the reviewed source-only paths, and clean applied index entries MUST
match the source pin.

After the required status preflight, first inspect the allowed conflict paths:

```sh
awf wt status --repo-root <repo-root> --refresh --json
awf wt recover-sync --lease <id> --repo-root <repo-root> --json
```

Resolve only the reported files without direct staging, commits, pushes, or
registry changes, then apply the same lease:

```sh
awf wt recover-sync --lease <id> --repo-root <repo-root> --apply --json
```

Apply first restores every non-conflicted reviewed path whose immutable source
pin entry exists into both index and worktree, then stages only the recorded
conflict paths. A non-conflicted clean path deleted by the source pin is
unsupported: AWF blocks before index or worktree mutation and preserves the
worktree. If staging validation rejects markers or the exact delta, it restores
the prior conflict index and rematerializes those clean source-pin entries, so
the recorded `UU` conflicts plus the exact full reviewed delta remain available
for the next preview or retry. It inspects final stage-0 blobs directly for
conflict markers, requires the final changed-path set to exactly equal the
reviewed source-only delta, and creates a controlled synthetic sync commit with
target then source as its only parents.
It reruns prepare and production verification, then immediately revalidates the
exact commit head, parents, tree, paths, and marker-free blobs before an atomic
create-if-absent branch push and exact PR verification. Any drift, PR in any
state, remote branch, extra path, or parent mismatch is `blocked` and preserves
the worktree. If a process stops after the synthetic commit transition but
before publication completes, rerun `awf wt sync --from <production> --to
<staging> --apply --json` to resume that committed lease; do not create another
recovery commit.

## Production promotion

This is the synthetic promotion path: AWF reconstructs reviewed deltas on a
managed `PROMOTE` lease and publishes its own branch. Use it when the original
feature branch cannot itself become the production PR (multiple sources, path
exclusions, out-of-order shipping, or an unavailable source branch that the
user has explicitly decided not to restore). For a single staging PR whose
branch is still available, prefer the same-branch `--source-branch` flow above.
Choosing the synthetic path after a `source_branch_unavailable` result is a
user decision, never an automatic fallback.

A synthetic production promotion MUST contain only the ordered source PR
deltas, never the entire staging branch. `--source-pr` is repeatable and MUST
follow staging merge order. A source PR base MAY differ from the preceding PR
merge SHA; AWF reapplies the explicitly listed deltas to one branch based on the
latest target. Multi-source promotion MUST require every source merge SHA. Apply
MUST verify staging merge order; reversed input is `blocked` with
`source_pr_sequence_order`.

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
that leaves the worktree dirty is `blocked`.

`promotion.source_review_policy` defaults to `approved_or_self_merged`: a
source PR merged by its author satisfies the review policy; MUST NOT request an
unavailable external reviewer. An explicit `approved` policy is enforced as
written. The merged state, successful checks, staging base, prepare, and
production verification gates remain required for a synthetic promotion. Only
then explicitly create the managed promotion PR:

```sh
awf wt promote --source-pr <number> --to <branch> --repo-root <repo-root> --apply --json
```

If `promote --apply` returns `ready`, MUST use or report the returned lease and
MUST NOT repeat `--apply`. A blocked promotion is resumable only through the
CLI's verified prepare, verification, or publication recovery paths; MUST NOT
manually repair or recreate its lease.

## Empty exact promotion apply-failure discard

Use `discard-promotion` only for an AWF-owned `PROMOTE` lease whose exact
promotion apply failed before any promotion commit or publication. It is not
general feature-lease disposal, automatic cleanup, a retry fallback, or a way
to clear an out-of-order/manual-resolution promotion.

After the required status preflight, inspect the one exact lease:

```sh
awf wt status --repo-root <repo-root> --refresh --json
awf wt discard-promotion --lease <id> --repo-root <repo-root> --json
```

The preview is eligible only when the lease is `BLOCKED`, `EXACT`, has
`ResolutionState.NONE`, no target PR, no conflict/protected-index metadata, no
cleanup reservation, no remote branch, one clean registered non-bare,
non-detached worktree on its recorded branch, and its worktree HEAD and local
branch ref equal the recorded lease HEAD. Its last failure event MUST be
`promotion_blocked` with a `promotion_apply_failed:` summary. A present
`target_base_sha` MUST equal that HEAD; a legacy null target-base pin is eligible
only when its recorded HEAD is graph-proven to be an ancestor of the currently
resolved `base_ref`. Deployment state is irrelevant because the lease never
published.

Review both preview actions (`remove_worktree` and `delete_local_branch`) and
their `lease_id`, `path`, and `branch`, then explicitly apply the same lease:

```sh
awf wt discard-promotion --lease <id> --repo-root <repo-root> --apply --json
```

Apply takes the repository lock, revalidates every guard, reserves cleanup,
holds the branch, removes the worktree, completes the lease removal, then
compare-and-deletes only the local branch. It NEVER deletes a remote branch.
Any blocker, drift, reservation failure, or uncertain partial removal is a
stop condition: preserve or safely release the reservation as reported, and
MUST NOT use Git, SQLite, filesystem, or worktree-command bypasses.

## Archive-backed explicit abandonment, repack, and restore

Use `archive-discard` only after the user explicitly decides to abandon one
eligible lease. It does not claim that a PR merged or deployed, and it does not
replace `finish`, `discard-promotion`, or any of their evidence gates. A
blocked `finish` or promotion is not permission to use this command. The
default remains clean-only.

Before discard or repack, run the required non-destructive status preflight:

```sh
awf wt status --repo-root <repo-root> --refresh --json
```

Before discard preview, the user MUST create an absolute, user-controlled
private backup root. AWF accepts only an existing, operator-owned canonical
directory with mode `0700`, outside the repository, all managed worktrees, and
the AWF worktree cache:

```sh
# AWF never creates the backup root.
install -d -m 700 /absolute/private/awf-archives

awf wt archive-discard --lease <id> \
  --backup-root /absolute/private/awf-archives \
  --reason "work intentionally abandoned" \
  --repo-root <repo-root> --json
```

Preview MUST NOT create the backup root or an archive directory and MUST NOT
change the registry. Review the `create_archive` action's exact lease,
worktree path, branch, current HEAD, `preview_token`, deterministic
`backup_directory`, and full snapshot fingerprint, entry count, and byte
count. Apply repeats the same lease, backup root, reason, and token:

```sh
awf wt archive-discard --lease <id> \
  --backup-root /absolute/private/awf-archives \
  --reason "work intentionally abandoned" \
  --preview-token <token-from-preview> --apply \
  --repo-root <repo-root> --json
```

`--apply` without the matching SHA-256 token is blocked. Apply locks the
repository and rechecks lease identity/version, current HEAD, the complete
working-tree fingerprint, PR state, remote SHA, reason, and backup root. It
creates and verifies the durable archive before reserving cleanup, holding the
branch and HEAD, or removing the worktree without force. Any drift, token
mismatch, invalid backup root, failed archive verification, storage failure, or
removal failure preserves the original worktree and branch or retains the
verified archive and recoverable reservation as reported.

If worktree removal fails while the original worktree remains, AWF retains the
verified backup, safely releases the reservation, and requires a fresh preview.
If the worktree was removed but registry CAS completion fails, a preview without
a token reports `cleanup_reserved`. Retry the same `--apply` only with the
original preview token and identical lease, reason, and backup root. AWF fully
verifies the manifest before completing the reservation. A different token,
branch drift, or a missing or corrupt archive is a blocker. AWF never searches
for a manifest automatically or permits arbitrary manual registry recovery.

The default blocks dirty, untracked, conflicted, and manual-resolution state.
`--include-uncommitted` is a guarded, explicit opt-in only for an eligible
dirty ACTIVE AWF feature lease, an imported scratch lease, or a registered
BLOCKED manual/legacy retry with
proven ownership and worktree path. It never bypasses the root checkout,
protected ref, retained lease, open PR, canonical reserved synchronization
identity, active release, foreign-repository, ownership, or path-evidence
blockers. Do not use direct Git, filesystem, or registry cleanup to bypass a
blocked result.

`--exclude-ignored-path` accepts only the root `node_modules` directory. It
MUST be a normalized relative, present non-symlink directory with no
tracked descendants. AWF MUST verify ignored status and index only against the
specified repository in a neutral Git environment; global or XDG ignore files
and ambient `GIT_DIR` or `GIT_INDEX_FILE` never qualify a path. Use
`--exclude-ignored-path node_modules` only after the operator has approved
discarding its whole contents, including local changes. The policy is bound to
the preview token and manifest. An approved `node_modules` exclusion applies
both to the archive and to the private removal checkpoint. Every other ignored
file, including environment files, remains in the archive and checkpoint. A
rejected path is a blocker, not permission to remove it manually.

The archive can contain secrets because `worktree.tar` preserves all
non-excluded files and `history.bundle` preserves Git history. AWF rejects
symlinked ancestors, unsafe ownership or permissions, and unsafe locations. It
creates archive directories with mode `0700` and artifacts with mode `0600`.
The requesting operator may receive the local `backup_directory` and a
non-sensitive verification summary. Never expose archive contents, the full
manifest, artifact hashes, or secrets in public issue text, logs, or chat.

Use `archive-repack` only for one matching repository archive whose lease is
`REMOVED` and has no cleanup reservation. It is not a new archive path or a
cleanup bypass. Preview the exact exclusion policy:

```sh
awf wt status --repo-root <repo-root> --refresh --json
awf wt archive-repack \
  --archive /absolute/private/awf-archives/<archive> \
  --exclude-ignored-path node_modules \
  --repo-root <repo-root> --json
```

Review the `repack_archive_contents` action, archive directory, exclusion
policy, and token, then apply exactly that preview:

```sh
awf wt archive-repack \
  --archive /absolute/private/awf-archives/<archive> \
  --exclude-ignored-path node_modules \
  --preview-token <token-from-preview> --apply \
  --repo-root <repo-root> --json
```

Repack revalidates provenance and the old archive, creates and verifies a
sibling private replacement, then atomically publishes it. Source metadata,
Git history, and Git-state artifacts remain available in the derivative, whose
metadata records the source manifest hash and exclusion policy. The old archive
is retained until publication succeeds. If interrupted work reports
`repack_recovery_required`, preserve the backup and use only the reported AWF
recovery path; never delete or rename archives manually.

Use `archive-restore` to recover a trusted private archive. It requires neither
the original repository nor the worktree registry. Its backup parent and
destination parent must already be canonical private directories with mode
`0700`; the destination must be an absent absolute direct child. Existing
directories, files, and symlinks are never overwritten.

```sh
# Preview verifies the archive and destination parent/absence without writing.
awf wt archive-restore \
  --archive /absolute/private/awf-archives/<archive> \
  --destination /absolute/private/restores/restored-worktree --json

# Apply verifies again and restores only into the new destination.
awf wt archive-restore \
  --archive /absolute/private/awf-archives/<archive> \
  --destination /absolute/private/restores/restored-worktree \
  --apply --json
```

The default archive restores captured files, modes, symlinks, history reachable
from the captured HEAD, and, when present, raw Git index and supported
conflict/operation state. Worktree reflogs, worktree-only refs/configuration,
and `MERGE_AUTOSTASH` are not archived. A derivative intentionally
cannot restore its excluded ignored path. Restore never changes the source
archive. A partial failure affects only the new temporary destination and fails
closed; do not use manual extraction as a blocker workaround. JSON output
contains only safe command/action data, never the manifest, archived content,
artifact hashes, or secrets.

## Commit-only origin remote-branch discard

`archive-discard` always preserves remote refs. Use
`discard-remote-branch` only for an explicit, approved decision to delete one
`origin` branch after preserving its commit history. It never archives or
removes a worktree, local branch, local `refs/heads`, `HEAD`, index,
configuration, hooks, or working-tree file.

The branch MUST pass Git's `check-ref-format --branch` validation.
`--expected-sha` is required and MUST be the approved 40- or 64-hex remote
HEAD, not a ref to resolve later. `origin` MUST resolve to exactly one push URL
equal to its fetch URL; a separate or multiple push URL blocks the command.
Both branch discard commands require a GitHub `origin` for the open-PR
preflight, including local-only ref deletion. An absent or non-GitHub origin
blocks them; use a separately approved workflow instead of bypassing the
preflight.

Before preview, create an existing, canonical, operator-owned `0700` backup
root outside the repository, AWF cache, and every managed worktree:

```sh
install -d -m 700 /absolute/private/awf-remote-branch-backups

awf wt discard-remote-branch \
  --branch <branch> \
  --expected-sha <approved-40-or-64-hex-sha> \
  --backup-root /absolute/private/awf-remote-branch-backups \
  --reason "approved branch retirement" \
  --repo-root <repo-root> --json
```

Preview is read-only. Review the exact branch, observed remote SHA, private
backup directory, and token. Apply repeats all of them and supplies that exact
token:

```sh
awf wt discard-remote-branch \
  --branch <branch> \
  --expected-sha <approved-40-or-64-hex-sha> \
  --backup-root /absolute/private/awf-remote-branch-backups \
  --reason "approved branch retirement" \
  --preview-token <token-from-preview> --apply \
  --repo-root <repo-root> --json
```

Apply creates and independently verifies a detached commit-only bundle for the
approved commit's reachable Git history, then rechecks repository identity,
protected branches, live worktrees, matching leases and cleanup reservations,
active release state, open pull requests, and the exact remote SHA immediately
before CAS deletion. The bundle excludes a worktree snapshot, uncommitted
changes, index, local refs, reflogs, tags, and PR metadata. Protected/default/
integration branches and branch names beginning `release/`, `release-archive-`,
or `staging-archive-`; any live worktree, non-removed or retained lease,
reservation, active release, or open PR are blockers.

The remote CAS push uses `--no-verify` and does not invoke local pre-push
hooks; repository policy must therefore be enforced by remote protections
and the AWF preview blockers, not solely by that hook.

Before a delete attempt, AWF persists and fsyncs `attempt.json`. A confirmed
CAS rejection clears that attempt marker after the rejected push returns, so
the same intent can be retried if the remote again matches the approved SHA.
A definite pre-connection DNS or authentication failure also clears the marker
and permits a retry with the same token after restoring access. An ambiguous
transport failure or unobserved result keeps the marker and fails closed while
the remote ref exists. AWF writes the receipt only after observing the
remote ref absent. A receipt makes later preview or apply idempotent: AWF MUST
NOT delete again, and a recreated branch is reported untouched even if it has
the same SHA. A present remote with a pending attempt and no receipt is
`remote_delete_outcome_unknown` and fail-closed; MUST NOT retry with direct Git
deletion. A newly intended or recreated branch requires a new reason or
approved SHA, a new preview, and its new token.

This command has independent selected-branch evidence scope because it does not
mutate a checked-out worktree. It does not repair or waive global registry/Git
mismatch rules: inspect a reported mismatch with `awf wt doctor`. Unrelated
root cached HEAD or dirty state is neither changed nor used as evidence, while
every mismatch that affects the selected repository or branch remains a
blocker. Before any read or deletion, ambient `GIT_DIR`, `GIT_WORK_TREE`,
`GIT_COMMON_DIR`, `GIT_NAMESPACE`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`,
`GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_CEILING_DIRECTORIES`,
`GIT_DISCOVERY_ACROSS_FILESYSTEM`, and binding `GIT_CONFIG_*` overrides are
unsafe and block the command. Credential-helper and ordinary HTTPS
authentication settings remain available. JSON emits only safe status, branch,
remote SHA, token, backup path, and bundle byte count; it never emits a bundle
hash or manifest contents. MUST NOT use direct remote deletion, force, prune,
or filesystem/registry edits to bypass a blocker.

## Commit-only local-branch discard

Use `discard-local-branch` only for an explicit, approved decision to delete
one direct local `refs/heads/<branch>` ref. It is separate from
`archive-discard` and `discard-remote-branch`: `archive-discard` may remove
qualifying local branches but always preserves remote refs. The remote command
deletes only an `origin` ref. The local command
does not fetch, push, or modify a remote ref or `refs/remotes/*`. It also does
not create, remove, or alter a worktree, any worktree `HEAD`, the main `HEAD`,
index, branch configuration, hooks, or working-tree file.

The branch MUST pass Git's `check-ref-format --branch` validation.
`--expected-sha` is the required approved 40- or 64-hex SHA of the direct local
ref, not a symbolic alias or a name resolved later. Before preview, create an
existing, canonical, operator-owned `0700` backup root outside the repository,
AWF cache, and every managed worktree:

```sh
install -d -m 700 /absolute/private/awf-local-branch-backups

awf wt discard-local-branch \
  --branch <branch> \
  --expected-sha <approved-40-or-64-hex-sha> \
  --backup-root /absolute/private/awf-local-branch-backups \
  --reason "approved local branch retirement" \
  --repo-root <repo-root> --json
```

Preview is read-only. Review the exact branch, observed local SHA, private
backup directory, and local-command token. Apply repeats every argument and
uses that token:

```sh
awf wt discard-local-branch \
  --branch <branch> \
  --expected-sha <approved-40-or-64-hex-sha> \
  --backup-root /absolute/private/awf-local-branch-backups \
  --reason "approved local branch retirement" \
  --preview-token <local-token-from-preview> --apply \
  --repo-root <repo-root> --json
```

The local token cannot authorize `discard-remote-branch`, and a remote token
cannot authorize this command. Apply first creates and independently verifies
a private detached commit-only bundle containing history reachable from the
selected local branch's current HEAD. The bundle excludes worktree content,
uncommitted changes, index, local refs, reflogs, tags, and PR metadata.

The local deletion decision uses a stable intent digest separated from mutable
lease evidence. Its preview token remains bound to complete current evidence,
including lease state. A lease-only change for the same requested input cannot
create a new intent or bypass an existing attempt or receipt marker. A new
reason or approved SHA is an explicit new intent.


Before entering the Git deletion guard, AWF MUST recheck repository identity,
protected branches, current checkouts, matching leases and cleanup
reservations, active release state, open pull requests, the direct local SHA,
and the unsafe Git environment. The guard then prepares a no-deref branch
deletion and locks the HEAD or symref of each existing worktree. Under those
locks it revalidates the inventory, fsyncs the attempt, revalidates again, and
commits the CAS deletion. Protected/default/integration
branches and names beginning `release/`, `release-archive-`, or
`staging-archive-`; any checked-out worktree, non-removed or retained lease,
reservation, active release, or open PR are blockers.

Those locks cover the current worktree inventory, and AWF inventories and
revalidates again immediately before commit. They do not absolutely serialize a
raw Git worktree created outside AWF after that final check. MUST NOT overlap a
local discard with an external raw Git worktree creation targeting the selected
branch.


AWF fsyncs immutable `attempt.json` before deletion and writes the completion
receipt only after observing the local ref absent. A receipt MUST prevent
another deletion; a recreated branch is untouched even at the same SHA. A
present ref with an attempt but no receipt, including the approved SHA, is
`local_delete_outcome_unknown` and fails closed. MUST NOT retry through direct
Git, filesystem, or registry changes. If the local ref is absent and its backup
is valid, AWF can recover the receipt without deleting a ref. A newly intended
or recreated branch needs a new reason or approved SHA, a new preview, and a
new local token.

This command has selected-branch evidence scope. An unrelated root cached HEAD
or dirty state is unchanged and is not evidence, but every mismatch affecting
the selected repository or branch is a blocker. Ambient `GIT_DIR`,
`GIT_WORK_TREE`, `GIT_COMMON_DIR`, `GIT_NAMESPACE`, `GIT_INDEX_FILE`,
`GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`,
`GIT_CEILING_DIRECTORIES`, `GIT_DISCOVERY_ACROSS_FILESYSTEM`, and binding
`GIT_CONFIG_*` overrides block the command. JSON emits only safe status, branch,
local SHA, token, backup path, and bundle byte count. MUST NOT bypass a blocker
with direct ref deletion, filesystem edits, or registry edits.

## Cumulative managed release bridge

Use `awf wt release` when source PRs must accumulate over time before one
production pull request is published. It is not a staging-wide merge and does
not replace exact `wt promote`; it reconstructs only the persisted ordered
source deltas on one managed `PROMOTE` lease.

After the required status preflight, first inspect and then apply `open`:

```sh
awf wt release open --release <id> --to <branch> --repo-root <repo-root> --json
awf wt release open --release <id> --to <branch> --repo-root <repo-root> --apply --json
```

`open` creates or reuses the exact bridge from the latest target with no source
selected. On `reuse`, use the returned lease and branch; MUST NOT create a
second bridge or manually mutate the branch.

For each merged staging PR, inspect and apply one `add` in actual staging merge
order:

```sh
awf wt release add --release <id> --source-pr <number> --repo-root <repo-root> --json
awf wt release add --release <id> --source-pr <number> --repo-root <repo-root> --apply --json
```

Every source MUST pass the existing merged, review-policy, checks, and
configured staging-base gates. AWF pins immutable base, head, merge, and path
provenance; all multi-source pins require a merge SHA. A source cannot be
duplicated or reordered. `source_pr_sequence_order`,
`source_provenance_changed`, and `source_delta_mismatch` are stop conditions:
preserve the managed worktree and report the blocker. Unrelated staging commits
MUST NOT enter the bridge.

After all sources are present, inspect and apply `seal`:

```sh
awf wt release seal --release <id> --repo-root <repo-root> --json
awf wt release seal --release <id> --repo-root <repo-root> --apply --json
```

`seal` locks the source list. It reconstructs pinned deltas on the latest
target, requires the configured prepare and production-verification commands,
and blocks if prepare leaves the worktree dirty. After `SEALED`, `add` is
forbidden; MUST NOT add a source by changing Git, SQLite, or a branch manually.

Finally inspect and apply `publish`:

```sh
awf wt release publish --release <id> --repo-root <repo-root> --json
awf wt release publish --release <id> --repo-root <repo-root> --apply --json
```

Before publication, AWF rechecks every immutable source pin. If production
target drifted after sealing, AWF rebuilds the pinned deltas in the same managed
worktree and reruns prepare and production verification. It then pushes and
opens or reuses exactly one PR for the managed branch. Source drift, target PR
mismatch, verification failure, or a dirty worktree is `blocked`; preserve the
bridge and do not recreate, rebase, force-push, or manually publish it.

## Out-of-order production promotion

Use this opt-in path only when one or more reviewed staging PRs must reach
production without unrelated earlier staging changes. It is not a fallback from
exact promotion.

| Situation | Required workflow |
| --- | --- |
| A code may ship but must remain inactive | Preserve staging promotion order and gate A at runtime with a feature flag or equivalent. |
| A code must stay out of production; B applies cleanly | Use the ordered `--out-of-order` promotion below for B. |
| A code must stay out; B has a mechanical patch conflict | Resolve only in the managed promotion worktree, then replay preview/apply. |
| B requires A's API, schema, or behavior | Include A then B in the ordered source list; if A cannot ship, stop. |

`--out-of-order` accepts one or more repeated `--source-pr` values in staging
merge order and MUST NOT use `--exclude-path`. Every source PR must be merged
into the configured staging branch and still satisfy the configured source
review and checks policy. AWF atomically pins each source's ordinal, PR number,
base ref, base SHA, head SHA, merge SHA, and reviewed paths in the promotion
lease. The source pins, rather than scalar lease provenance fields, are
authoritative across preview, apply, retry, manual recovery, and publication.

Every source merge SHA must be in the configured staging history, every source
base must be an ancestor of its reviewed head, and each later source merge SHA
must descend from the preceding source merge SHA. Duplicate source PRs, missing
merge SHAs, reversed merge order, source drift, or a source outside staging
stop before publication. Renamed source paths are unsupported and stop with
`unsupported_out_of_order_rename`. The ordered list may contain a dependent
pair such as #527 followed by #530; a prerequisite outside that list is a stop
condition.

Preview the code-isolated synthetic production result. It exposes the ordered
`sources` pins plus `source_base_sha`, `source_head_sha`, `target_base_sha`, and
the reviewed-path union. AWF applies the ordered source patches into one
synthetic commit. The final delta must be a non-empty subset of the ordered
source reviewed-path union. Inspect those fields with the promotion mode and
verification actions before explicitly applying:

```sh
# Initial preview and apply; repeat --source-pr in staging merge order.
awf wt promote --source-pr <first> --source-pr <second> --to <branch> --out-of-order --repo-root <repo-root> --json
awf wt promote --source-pr <first> --source-pr <second> --to <branch> --out-of-order --repo-root <repo-root> --apply --json
# After an AWF-reported conflict, replay the same ordered preview and apply commands.
awf wt promote --source-pr <first> --source-pr <second> --to <branch> --out-of-order --repo-root <repo-root> --json
awf wt promote --source-pr <first> --source-pr <second> --to <branch> --out-of-order --repo-root <repo-root> --apply --json
```

When the replayed preview finds a pending conflict, it lists the work AWF
would perform. The action order is `resolve_out_of_order_conflict`,
`stage_paths`, `commit`, `verify_production`, `push_branch`, then
`open_pull_request`.

A failed three-way apply stops with `out_of_order_conflict`. AWF preserves an
unpublished managed worktree with the complete ordered source pins, target,
reviewed-path union, and conflicted-path provenance. Inside that managed
promotion worktree the operator may edit only the conflicted files returned by
AWF and MUST NOT use `git add`, `git commit`, `git reset`, `git cherry-pick`,
or `git push`; this guard belongs to the AWF-owned worktree, not to the
developer's own branches.

Operator's unstaged edits and unmerged paths must be a subset of
`conflicted_paths`. AWF clean-applied staged `protected_index_entries` may
remain outside `conflicted_paths`. Their mode+OID pin is exact across preview,
apply, and retry. Final indexed and committed paths must be a non-empty subset
of the ordered source reviewed-path union.

Direct `git add` tampering or chmod/file-type mode tampering returns
`promotion_resolution_scope_mismatch`. Any direct cherry-pick is forbidden for
production promotion. AWF reconstructs reviewed PR deltas only through
`awf wt promote`.

After editing, replay the same ordered preview command. It reports the blocked
lease, conflicted paths, and current changed paths. Replay the same command
with `--apply` only when every source pin and target provenance are unchanged.
An operator unstaged edit or unmerged path outside `conflicted_paths` returns
`promotion_resolution_scope_mismatch`. AWF stages the allowed conflict files;
an unmerged index entry that remains after staging returns
`promotion_resolution_unmerged`.

All conflict markers must be removed before apply. If a marker remains, AWF
does not publish and preserves the worktree. If any source base, head, merge,
reviewed path, order, or the target SHA changes, stop with
`promotion_provenance_changed`; preserve the worktree rather than transplanting
a resolution. Commit and PR trailers record each source PR/base/head/merge in
the exact input sequence.

The guard checks conflict markers only; trailing whitespace is not prohibited
by this policy. For a clean automatic apply, AWF rechecks every source pin and
the live target after verification before publish. A changed source or target
remains blocked and the managed worktree is preserved.

AWF stages, commits, verifies, pushes, and publishes the eligible resolution.
The synthetic production PR MUST pass successful checks on that exact PR before
merge. It requires approval only when the repository's branch policy requires
one; a solo repository MUST NOT invent an unavailable reviewer. Staging squash
commits are not production promotion inputs. A direct staging squash
cherry-pick is forbidden.

## Deployment verification

The repository MUST NOT configure deployment commands. The local operator MAY
map only the exact repository ID to an adapter under the exact
`~/.config/awf/adapters/` directory:

```toml
[worktree.deployment.adapters."<repository_id>"]
command = ["/home/operator/.config/awf/adapters/deployment-evidence-adapter"]
environment = ["DEPLOYMENT_REGION"] # optional inherited-name allowlist
max_age_seconds = 300
```

The config, every adapter-directory parent, and the executable MUST be
operator-owned and not group- or world-writable. The executable MUST be a
regular non-symlink file below that directory; repository fallback, alias,
profile selection, relative paths, and adapters outside it are forbidden.

AWF invokes the adapter with `shell=False`, neutral cwd, bounded pipes, and an
explicit minimal environment. It forwards only `HOME`, `USER`, `LOGNAME`,
`TMPDIR`, and `LANG` by default. `environment` permits only explicitly named
inherited values; `PATH`, `PYTHONPATH`, `DYLD_*`, `LD_*`, `GIT_*`, and cloud
credentials or overrides are not inherited unless explicitly allowlisted.

The adapter receives one `awf.deployment-evidence/v1` JSON request on stdin:
`protocol`, fresh `request_id`, `repository_id`, `pull_request_number`,
`source_head_sha`, exact merge-SHA `subject_revision`, and UTC `requested_at`.
It MUST emit one strict bounded JSON object with the same protocol, nonce,
repository, and subject revision; `healthy|superseded_healthy|pending|failed|unknown`
status; and RFC3339 UTC `observed_at`. `superseded_healthy` MUST include
`production_image_git_sha`: a lowercase 40- or 64-character Git OID different
from `subject_revision`. Other statuses MUST NOT include that field. Bounded
opaque `evidence_id` and `diagnostic_code` are optional.

`status --refresh` and `finish --apply` each obtain a fresh response. `finish`
MUST re-probe with a new nonce and compare the proven merge identity again
after both its probe and cleanup reservation. Fresh exact-bound `healthy`
permits cleanup. `superseded_healthy` permits cleanup only after AWF proves in
the current Git graph that
`merge_base(subject_revision, production_image_git_sha) == subject_revision`.
Equal revisions, unavailable objects, Git errors, and non-ancestors preserve
the worktree. `pending`, `unknown`, `failed`, changed merge identity, invalid
JSON, mismatch or replay, stale evidence, nonzero exit, timeout, or output
overflow also preserve it. AWF terminates the full adapter process group on
timeout or overflow even after the adapter leader exits, records received-at
time only with allowlisted structured evidence, and MUST NOT store raw stdout,
stderr, credentials, or provider-specific fields.

## Legacy out-of-order retry cleanup

Canonical AWF cache paths require a lowercase canonical UUID lease ID. AWF
accepts a legacy retry path only for a managed AWF `PROMOTE` lease in
`OUT_OF_ORDER` mode when the current repository ID, name, and resolved root
match the lease. It requires a valid target SHA, an initiative with the exact
`-retry-<target-sha>` suffix, the exact promotion branch, and the exact
recomputed `promotion-retry-<target-sha>-<sha256(initiative)[:16]>` path.
Any repository, branch, suffix, digest, or path mismatch preserves the
worktree.

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

## Safe ignored-path compaction

Use `compact` only to reclaim ignored dependency or cache data from a live
managed worktree; it does not remove a worktree, branch, lease, registry row,
or event. Preview before any apply:

```sh
awf wt compact --repo-root <repo-root> --path node_modules --older-than 7d --dry-run --json
awf wt compact --repo-root <repo-root> --path node_modules --older-than 7d --apply --json
```

Omit `--lease` only when every eligible lease in the exact repository may be
considered. Eligible leases are AWF-managed `PR_OPEN`, `DEPLOYING`, `DEPLOYED`,
or `CLEANABLE` leases older than the supplied threshold. Use `--lease <id>` to
inspect one lease. Each `--path` MUST be unique, normalized, repository
relative, present within the worktree, Git-ignored, and have no tracked
descendants; it and every ancestor MUST NOT be a symlink.

Before deletion, apply takes the repository's nonblocking lock and fully
revalidates every candidate: exact repository provenance, managed owner, clean
status, registered non-bare/non-detached branch, exact HEAD, no cleanup
reservation, age, and every requested path. Any preflight blocker stops the
entire apply before any path is deleted. Review every `remove_ignored_path`
action's `lease_id`, `worktree_path`, `path`, allocated `bytes`, and
`entry_count`. The result leaves lease/registry state and events, Git
HEAD/status, branch, and worktree registration unchanged. A filesystem failure
after deletion starts is `compact_remove_failed`; its actions contain only
paths that completed before the failure.

## Blocker response

For `blocked`, MUST report the result code, message, command, and deployment/PR evidence available. MUST preserve the worktree and branch. Resolve the reported condition through the managed lifecycle, then restart at preflight.

For `removed`, MUST report completion and take no further cleanup action for that lease.

## Forbidden fallbacks

These prohibitions apply to AWF-managed leases, AWF-owned synthetic branches, and the lifecycle actions this skill governs; they do not restrict ordinary commits or non-force pushes on a developer's own branch (see the scope boundary). MUST NOT use direct worktree creation, removal, pruning, direct Git or filesystem cleanup, or other unmanaged deletion of a managed lease, worktree, or AWF-owned branch. MUST NOT merge staging wholesale, use `git branch --merged` as cleanup proof, stash, reset, clean, or force-delete inside a managed synthetic worktree or against an AWF-owned branch, silently replace a blocked `--source-branch` promotion with a synthetic one, or bypass a CLI blocker. These actions are not a substitute for `awf wt` status, doctor, acquire, link-pr, promote, finish, or gc.

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
    "sync_preview": "awf wt sync --from main --to staging --repo-root <repo-root> --json",
    "sync_apply": "awf wt sync --from main --to staging --repo-root <repo-root> --apply --json",
    "discard_sync_preview": "awf wt discard-sync --lease <id> --repo-root <repo-root> --json",
    "discard_sync_apply": "awf wt discard-sync --lease <id> --repo-root <repo-root> --apply --json",
    "recover_sync_preview": "awf wt recover-sync --lease <id> --repo-root <repo-root> --json",
    "recover_sync_apply": "awf wt recover-sync --lease <id> --repo-root <repo-root> --apply --json",
    "promote_preview": "awf wt promote --source-pr <number> --to <branch> --repo-root <repo-root> --json",
    "promote_apply": "awf wt promote --source-pr <number> --to <branch> --repo-root <repo-root> --apply --json",
    "source_branch_promote_preview": "awf wt promote --source-pr <staging-pr> --to main --source-branch --repo-root <repo-root> --json",
    "source_branch_promote_apply": "awf wt promote --source-pr <staging-pr> --to main --source-branch --repo-root <repo-root> --apply --json",
    "release_open_preview": "awf wt release open --release <id> --to <branch> --repo-root <repo-root> --json",
    "release_open_apply": "awf wt release open --release <id> --to <branch> --repo-root <repo-root> --apply --json",
    "release_add_preview": "awf wt release add --release <id> --source-pr <number> --repo-root <repo-root> --json",
    "release_add_apply": "awf wt release add --release <id> --source-pr <number> --repo-root <repo-root> --apply --json",
    "release_seal_preview": "awf wt release seal --release <id> --repo-root <repo-root> --json",
    "release_seal_apply": "awf wt release seal --release <id> --repo-root <repo-root> --apply --json",
    "release_publish_preview": "awf wt release publish --release <id> --repo-root <repo-root> --json",
    "release_publish_apply": "awf wt release publish --release <id> --repo-root <repo-root> --apply --json",
    "out_of_order_promote_preview": "awf wt promote --source-pr <first> --source-pr <second> --to <branch> --out-of-order --repo-root <repo-root> --json",
    "out_of_order_promote_apply": "awf wt promote --source-pr <first> --source-pr <second> --to <branch> --out-of-order --repo-root <repo-root> --apply --json",
    "out_of_order_resolution_preview": "awf wt promote --source-pr <first> --source-pr <second> --to <branch> --out-of-order --repo-root <repo-root> --json",
    "out_of_order_resolution_apply": "awf wt promote --source-pr <first> --source-pr <second> --to <branch> --out-of-order --repo-root <repo-root> --apply --json",
    "discard_promotion_preview": "awf wt discard-promotion --lease <id> --repo-root <repo-root> --json",
    "discard_promotion_apply": "awf wt discard-promotion --lease <id> --repo-root <repo-root> --apply --json",
    "archive_discard_preview": "awf wt archive-discard --lease <id> --backup-root /absolute/private/awf-archives --reason \"work intentionally abandoned\" --repo-root <repo-root> --json",
    "archive_discard_apply": "awf wt archive-discard --lease <id> --backup-root /absolute/private/awf-archives --reason \"work intentionally abandoned\" --preview-token <token-from-preview> --apply --repo-root <repo-root> --json",
    "discard_remote_branch_preview": "awf wt discard-remote-branch --branch <branch> --expected-sha <approved-40-or-64-hex-sha> --backup-root /absolute/private/awf-remote-branch-backups --reason \"approved branch retirement\" --repo-root <repo-root> --json",
    "discard_remote_branch_apply": "awf wt discard-remote-branch --branch <branch> --expected-sha <approved-40-or-64-hex-sha> --backup-root /absolute/private/awf-remote-branch-backups --reason \"approved branch retirement\" --preview-token <token-from-preview> --apply --repo-root <repo-root> --json",
    "discard_local_branch_preview": "awf wt discard-local-branch --branch <branch> --expected-sha <approved-40-or-64-hex-sha> --backup-root /absolute/private/awf-local-branch-backups --reason \"approved local branch retirement\" --repo-root <repo-root> --json",
    "discard_local_branch_apply": "awf wt discard-local-branch --branch <branch> --expected-sha <approved-40-or-64-hex-sha> --backup-root /absolute/private/awf-local-branch-backups --reason \"approved local branch retirement\" --preview-token <local-token-from-preview> --apply --repo-root <repo-root> --json",
    "archive_repack_preview": "awf wt archive-repack --archive /absolute/private/awf-archives/<archive> --exclude-ignored-path node_modules --repo-root <repo-root> --json",
    "archive_repack_apply": "awf wt archive-repack --archive /absolute/private/awf-archives/<archive> --exclude-ignored-path node_modules --preview-token <token-from-preview> --apply --repo-root <repo-root> --json",
    "archive_restore_preview": "awf wt archive-restore --archive /absolute/private/awf-archives/<archive> --destination /absolute/private/restores/restored-worktree --json",
    "archive_restore_apply": "awf wt archive-restore --archive /absolute/private/awf-archives/<archive> --destination /absolute/private/restores/restored-worktree --apply --json",
    "finish_preview": "awf wt finish --repo-root <repo-root> --pr <merged-pr> --json",
    "finish_apply": "awf wt finish --repo-root <repo-root> --pr <merged-pr> --apply --json",
    "gc_preview": "awf wt gc --repo-root <repo-root> --merged --older-than 7d --dry-run --json",
    "gc_apply": "awf wt gc --repo-root <repo-root> --merged --older-than 7d --apply --json",
    "compact_preview": "awf wt compact --repo-root <repo-root> --path node_modules --older-than 7d --dry-run --json",
    "compact_apply": "awf wt compact --repo-root <repo-root> --path node_modules --older-than 7d --apply --json"
  },
  "safety": {
    "preflight": "required_non_destructive_status_refresh",
    "lease_reuse": "exact",
    "promotion_scope": "source_pr_delta_only",
    "ordinary_development": {
      "scope": "developer_owned_branches_and_managed_feature_lease_checkout",
      "git_operations": "add_commit_fetch_pull_nonforce_push_and_project_hooks_allowed",
      "per_commit_approval": "not_required",
      "preflight_before_commit": "not_required",
      "branch_name_release_or_feature": "not_an_awf_synthetic_object",
      "commit_permission": "separate_from_merge_and_deploy_permission",
      "unmanaged_worktrees": "registry_protection_does_not_forbid_ordinary_git",
      "data_loss_on_managed_objects": "evidence_and_explicit_request_required",
      "preview_then_apply": "same_turn_when_requested_and_preview_has_no_blocker_or_new_choice"
    },
    "immutable_index_commit_guard": {
      "scope": "awf_owned_promote_release_bridge_and_sync_worktrees_only",
      "forbidden_inside": ["git add", "git commit", "git reset", "git cherry-pick", "git stash", "git push"]
    },
    "feature_base": {
      "resolution_order": ["--base", "worktree.feature_base", "worktree.production_branch", "worktree.default_base", "remote_default"],
      "default_base_meaning": "staging_verification_target_unchanged"
    },
    "source_review_policy": {
      "default": "approved_or_self_merged",
      "explicit_approved": "enforced_as_written",
      "external_reviewer": "never_invented",
      "empty_checks_rollup": "passes_no_new_ci_required"
    },
    "source_branch_promotion": {
      "mode": "explicit_opt_in",
      "source_count": "exactly_one_latest_staging_pr",
      "repeated_staging": "normal_flow_one_more_staging_pr_per_round",
      "validation_chain": "predecessor_staging_pr_head_equals_latest_reviewed_merge_base_root_production_ancestor_every_link_merged_review_checks_graph_paths",
      "validation_chain_exposure": ["source_prs", "sources", "pr_body_validation_trailers"],
      "exclude_paths": "forbidden",
      "out_of_order": "forbidden",
      "publication": "one_pull_request_from_original_remote_feature_branch_at_tested_head",
      "artifacts": "no_lease_no_worktree_no_synthetic_commit_no_push_no_merge",
      "managed_lease": "optional_recorded_staging_evidence_compared_when_present",
      "local_verification": "prepare_and_verify_production_not_required",
      "target": "configured_production_branch_protections_and_checks_on_exact_head",
      "preview_action": "open_pull_request",
      "preview_fields": [
        "promotion_mode",
        "source_branch",
        "source_pr",
        "tested_head",
        "source_head_sha",
        "source_base_sha",
        "reviewed_base_sha",
        "target_branch",
        "target_base_sha",
        "source_prs",
        "sources"
      ],
      "ready_fields": ["target_pr", "url"],
      "staging_pr": "intermediate_no_delete_branch_no_finish",
      "staging_link_pr": "record_staging_validation_lease_stays_active",
      "cleanup_link_pr": "production_pr_only",
      "remote_branch_deleted": "restore_verified_head_with_nonforce_push_then_retry",
      "source_drift": "reverify_with_new_staging_pr_then_replay_naming_latest_pr",
      "synthetic_fallback": "explicit_user_decision_never_silent",
      "blocker_codes": [
        "invalid_source_branch_promotion",
        "source_branch_unavailable",
        "source_branch_provenance_changed",
        "source_branch_contains_staging",
        "source_validation_chain_invalid",
        "source_delta_mismatch",
        "staging_evidence_stale",
        "target_pr_mismatch"
      ]
    },
    "branch_sync": {
      "direction": "configured_production_to_staging_only",
      "scope": "source_only_delta_since_live_merge_base",
      "provenance": "pinned_source_target_reserved_branch_and_no_promote_marker",
      "remote_drift": "blocked_before_and_after_publish",
      "promotion_loop": "source_pr_not_promotable"
    },
    "release_bridge": {
      "source_pins": "ordered_immutable_base_head_merge_paths",
      "source_add_after_seal": "forbidden",
      "target_drift": "rebuild_same_managed_worktree_then_reverify",
      "publication": "one_managed_pull_request"
    },
    "deployment_health": "repository_rollout_evidence",
    "blocked_action": "preserve_worktree_report_code_message",
    "discard_promotion": {
      "scope": "one_awf_owned_blocked_empty_exact_promotion_apply_failure_only",
      "legacy_target_base": "null_requires_recorded_lease_head_to_be_ancestor_of_current_base",
      "preview_actions": ["remove_worktree", "delete_local_branch"],
      "apply": "lock_revalidate_reserve_hold_remove_complete_compare_delete_local",
      "remote_branch": "must_be_absent_and_never_deleted"
    },
    "archive_discard": {
      "scope": "explicit_user_abandonment_only_not_merged_deployed_or_finish_substitute",
      "default": "clean_only",
      "dirty_opt_in": "include_uncommitted_only_for_eligible_dirty_awf_feature_or_imported_scratch_or_registered_blocked_manual_legacy_retry_with_proven_ownership_and_path",
      "excluded_ignored_paths": "explicit_normalized_present_non_symlink_git_ignored_directory_without_tracked_descendants_policy_bound_to_token_and_manifest",
      "continued_blockers": "root_protected_ref_retained_open_pr_canonical_sync_active_release_foreign_repository_or_unproven_ownership_path",
      "preview": "read_only_create_archive_action_with_token_destination_and_full_snapshot",
      "apply": "matching_token_lock_revalidate_verified_private_backup_reserve_hold_nonforce_remove_complete_compare_delete_local",
      "backup": "absolute_private_outside_repo_managed_worktrees_and_cache_verified_bundle_tar_manifest",
      "remote_branch": "preserved_remote_sha_evidence_remote_presence_not_alone_blocker"
    },
    "discard_remote_branch": {
      "scope": "one_explicit_approved_origin_branch_remote_ref_only",
      "required_arguments": ["branch", "expected_sha", "backup_root", "reason"],
      "preview": "read_only_selected_branch_evidence_and_token",
      "apply": "matching_token_locked_revalidation_verified_detached_commit_bundle_then_remote_cas_delete_only",
      "backup": "existing_canonical_operator_owned_0700_private_root_full_reachable_commit_history_only",
      "blockers": "protected_default_integration_release_live_worktree_nonremoved_or_retained_lease_reservation_active_release_open_pr_unsafe_git_environment_or_sha_drift",
      "recreation_defense": "fsynced_attempt_before_delete_receipt_after_observed_absence_receipt_never_redeletes_recreated_branch_untouched",
      "unknown_delete_observation": "remote_delete_outcome_unknown_fail_closed",
      "local_state": "no_local_branch_worktree_head_index_config_or_working_tree_mutation",
      "global_mismatch": "doctor_inspect_not_waived_unrelated_root_cached_head_or_dirty_not_mutated_or_used_as_evidence",
      "output": "safe_status_branch_remote_sha_token_backup_path_bundle_bytes_only"
    },
    "discard_local_branch": {
      "scope": "one_explicit_approved_direct_local_refs_heads_ref_only",
      "required_arguments": ["branch", "expected_sha", "backup_root", "reason"],
      "intent": "stable_digest_separate_from_mutable_lease_evidence_preview_token_binds_full_evidence_lease_only_change_cannot_bypass_attempt_or_receipt_marker_new_reason_or_sha_explicit_new_intent",
      "preview": "read_only_selected_branch_evidence_and_local_scope_token",
      "apply": "matching_local_token_branch_and_all_worktree_head_symref_locks_reinventory_revalidate_verified_current_head_reachable_commit_bundle_then_local_cas_delete_only",
      "concurrency": "locks_current_inventory_worktree_heads_or_symrefs_and_revalidates_before_commit_external_raw_git_worktree_after_final_check_not_absolutely_serialized_must_not_overlap_selected_branch",
      "backup": "existing_canonical_operator_owned_0700_private_root_full_reachable_commit_history_only",
      "blockers": "protected_default_integration_release_any_checked_out_worktree_nonremoved_or_retained_lease_reservation_active_release_open_pr_unsafe_git_environment_or_sha_drift",
      "recreation_defense": "fsynced_attempt_before_delete_receipt_after_observed_absence_receipt_never_redeletes_recreated_branch_untouched",
      "unknown_delete_observation": "local_delete_outcome_unknown_fail_closed",
      "unchanged_state": "remote_refs_refs_remotes_all_worktrees_heads_index_branch_config_hooks_and_working_tree",
      "global_mismatch": "doctor_inspect_not_waived_unrelated_root_cached_head_or_dirty_not_mutated_or_used_as_evidence",
      "output": "safe_status_branch_local_sha_token_backup_path_bundle_bytes_only"
    },
    "archive_repack": {
      "scope": "one_removed_unreserved_matching_repository_archive_only",
      "preview": "read_only_verified_source_archive_and_policy_bound_token",
      "apply": "archive_specific_lock_revalidate_verified_sibling_create_atomic_publish",
      "failure": "preserve_source_archive_and_report_recovery_without_manual_deletion"
    },
    "archive_restore": {
      "scope": "verified_private_archive_to_absent_private_destination_without_source_repository_or_registry",
      "preview": "read_verified_archive_and_validate_absent_private_destination",
      "apply": "verify_again_restore_archive_only",
      "output": "no_manifest_contents_artifact_hashes_or_secrets"
    },
    "discard_sync": {
      "scope": "one_awf_owned_blocked_stale_unpublished_sync_target_conflict_only",
      "preview_actions": ["remove_worktree", "delete_local_branch"],
      "apply": "lock_revalidate_reserve_hold_normalize_nonforce_remove_rebuild_recorded_conflict_on_failure_complete_compare_delete_local",
      "remote_branch": "must_be_absent_and_never_deleted",
      "conflict_scope": "all_git_unmerged_classes_within_reviewed_paths_and_clean_staged_source_pin_entries"
    },
    "recover_sync": {
      "scope": "one_awf_owned_blocked_current_pin_unpublished_sync_target_conflict_only",
      "operator_scope": "recorded_uu_conflicted_paths_subset_of_reviewed_paths",
      "clean_index": "clean_applied_entries_match_source_pin",
      "commit": "controlled_two_parent_target_then_source_synthetic_sync_commit",
      "publication": "revalidate_drift_prepare_verify_exact_commit_atomic_create_pr"
    },
    "compact": {
      "scope": "ignored_untracked_paths_only",
      "eligible_states": ["PR_OPEN", "DEPLOYING", "DEPLOYED", "CLEANABLE"],
      "bulk": "exact_repository_eligible_leases_only",
      "apply": "nonblocking_lock_full_revalidation_before_any_deletion",
      "invariants": "lease_registry_events_git_head_status_branch_worktree_unchanged",
      "action_fields": ["lease_id", "worktree_path", "path", "bytes", "entry_count"]
    },
    "out_of_order": {
      "mode": "explicit_opt_in",
      "exact_mode": "default",
      "source_order": "one_or_more_unique_sources_in_staging_merge_order",
      "source_pins": "ordered_immutable_ordinal_pr_base_ref_base_head_merge_paths",
      "exclude_paths": "forbidden",
      "production_pr_review": "required",
      "production_pr_checks": "required",
      "direct_cherry_pick": "forbidden",
      "staging_squash_input": "forbidden",
      "conflict_resolution": "durable_source_ordinal_then_remaining_ordered_sources_before_single_synthetic_commit",
      "legacy_single_source_pins": "verified_live_scalar_provenance_and_exact_three_trailer_message_backfilled_on_apply_only",
      "dependency": "allowed_when_prerequisite_precedes_dependent_source",
      "rename": "unsupported",
      "initial_preview_fields": [
        "sources",
        "source_base_sha",
        "source_head_sha",
        "target_base_sha",
        "reviewed_paths"
      ],
      "resolution_preview_actions": [
        "resolve_out_of_order_conflict",
        "stage_paths",
        "commit",
        "verify_production",
        "push_branch",
        "open_pull_request"
      ],
      "operator_edit_scope": "unstaged_unmerged_subset_of_conflicted_paths",
      "final_indexed_delta": "non_empty_ordered_reviewed_path_union_subset",
      "conflict_marker_policy": "markers_only_trailing_whitespace_allowed",
      "live_target_recheck": "all_source_pins_and_target_after_verification_before_publish",
      "protected_index_entries": {
        "paths": "clean_applied_reviewed_paths_outside_conflicted_paths",
        "entry": "stage_zero_mode_blob_oid_or_null",
        "pin": "exact_preview_apply_retry",
        "tamper": "promotion_resolution_scope_mismatch"
      },
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
      "staging_apply": {
        "lease_state": "ACTIVE",
        "target_pr": null,
        "source_pr": "verified_staging_pr"
      },
      "production_apply": {
        "lease_state": "CLEANABLE",
        "deployment_state": "not_required",
        "target_pr": "verified_production_pr"
      },
      "one_stage_or_sync": "final_cleanup",
      "same_pr": "reuse",
      "different_staging_pr": "revalidate_and_replace_source_evidence",
      "different_final_pr": "blocked",
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
      "sync",
      "promote",
      "source_branch_promote",
      "release_open",
      "release_add",
      "release_seal",
      "release_publish",
      "out_of_order_promote",
      "out_of_order_resolution",
      "import",
      "adopt",
      "discard_promotion",
      "archive_discard",
      "discard_remote_branch",
      "discard_local_branch",
      "archive_repack",
      "archive_restore",
      "discard_sync",
      "recover_sync",
      "finish",
      "gc",
      "compact"
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
    ],
    "forbidden_fallback_scope": "awf_managed_leases_awf_owned_synthetic_branches_and_lifecycle_actions_only",
    "ordinary_git_on_development_branches": "not_restricted"
  },
  "decisions": {
    "reuse": "use_exact_lease",
    "preview": {
      "acquire": "review_then_apply_explicitly",
      "link_pr": "review_then_apply_explicitly",
      "sync": "review_then_apply_explicitly",
      "promote": "review_then_apply_explicitly",
      "source_branch_promote": "review_open_pull_request_action_then_apply_explicitly",
      "release_open": "review_then_apply_explicitly",
      "release_add": "review_then_apply_explicitly",
      "release_seal": "review_then_apply_explicitly",
      "release_publish": "review_then_apply_explicitly",
      "out_of_order_promote": "review_then_apply_explicitly",
      "out_of_order_resolution": "review_same_blocked_lease_then_apply_explicitly",
      "discard_promotion": "review_every_action_then_apply_explicitly",
      "archive_discard": "review_create_archive_action_then_apply_with_matching_token",
      "discard_remote_branch": "review_selected_branch_evidence_then_apply_with_matching_token",
      "discard_local_branch": "review_selected_local_branch_evidence_then_apply_with_matching_local_token",
      "archive_repack": "review_repack_archive_contents_then_apply_with_matching_token",
      "archive_restore": "verify_private_archive_then_apply_to_new_destination",
      "discard_sync": "review_every_action_then_apply_explicitly",
      "recover_sync": "review_allowed_conflict_paths_then_apply_explicitly",
      "finish": "review_blockers_then_apply",
      "gc": "review_blockers_then_apply",
      "compact": "review_every_action_then_apply_explicitly"
    },
    "ready": {
      "status": "inspect_select_lifecycle_action",
      "acquire_apply": "use_or_report_returned_lease",
      "link_pr_apply": "restart_status_preflight_then_finish",
      "link_pr_staging_apply": "lease_stays_active_continue_to_source_branch_promote",
      "sync_apply": "use_or_report_returned_lease",
      "promote_apply": "use_or_report_returned_lease",
      "source_branch_promote_apply": "report_returned_pull_request_no_lease",
      "release_open_apply": "use_or_report_returned_lease",
      "release_add_apply": "use_or_report_returned_lease",
      "release_seal_apply": "use_or_report_returned_lease",
      "release_publish_apply": "use_or_report_returned_lease",
      "out_of_order_promote_apply": "use_or_report_returned_lease",
      "out_of_order_resolution_apply": "use_or_report_returned_lease"
    },
    "removed": "report_completion",
    "blocked": "preserve_worktree_report_code_message"
  }
}
```
