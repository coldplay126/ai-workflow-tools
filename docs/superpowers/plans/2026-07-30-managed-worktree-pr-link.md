# Managed Worktree PR Link Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit, fail-closed command that links an already-merged PR to an acquired feature lease, records the verified developed HEAD, and unblocks the existing safe cleanup path.

**Architecture:** Add `WorktreeService.link_pr()` as a provenance transition separate from imported adoption and cleanup. The command resolves the lease first, validates the current registered clean worktree against the exact merged GitHub PR, and atomically records `target_pr`, verified `head_sha`, `CLEANABLE`, and `NOT_REQUIRED`; `finish --pr` remains the only remover.

**Tech Stack:** Python 3.11+, argparse, SQLite CAS transitions, Git worktrees, GitHub CLI adapter, pytest.

---

## File map

- `cli/src/awf/worktrees/service.py`: managed feature lease PR-link validation, preview/apply, idempotency.
- `cli/src/awf/commands/wt.py`: lease-root command construction and result/exit mapping.
- `cli/src/awf/cli.py`: `wt link-pr` parser surface.
- `cli/tests/test_worktree_service.py`: service state, provenance, safety, race, and cleanup integration contracts.
- `cli/tests/test_worktree_commands.py`: argparse and JSON envelope contracts.
- `cli/README.md`: acquired feature lifecycle command reference.
- `claude/skills/release-worktree-lifecycle/SKILL.md`: operator procedure and blocker rules.
- `docs/superpowers/specs/2026-07-30-managed-worktree-pr-link-design.md`: approved source specification; no behavioral edits unless implementation exposes a contradiction.

### Task 1: Lock the service contract with failing tests

**Files:**
- Modify: `cli/tests/test_worktree_service.py`
- Test: `cli/tests/test_worktree_service.py`

- [ ] **Step 1: Add a fixture for an acquired lease whose developed HEAD differs from its recorded acquisition HEAD**

Add this helper near `matching_adoption_pr()`:

```python
def developed_managed_feature(
    harness: Harness, *, number: int = 131
) -> tuple[Lease, str]:
    acquired = harness.acquire("managed-pr-link")
    assert acquired.lease is not None
    lease = acquired.lease
    (lease.worktree_path / "feature.txt").write_text("feature\n", encoding="utf-8")
    git_command(lease.worktree_path, "add", "feature.txt")
    git_command(lease.worktree_path, "commit", "-q", "-m", "feature")
    current_head = harness.git.head_sha(lease.worktree_path)
    assert current_head != lease.head_sha
    harness.github.prs[number] = replace(
        merged_pr(number=number, head_sha=current_head),
        head_ref=lease.branch,
    )
    return lease, current_head
```

- [ ] **Step 2: Add preview and atomic apply tests**

Add tests that assert the complete observable contract:

```python
def test_link_pr_preview_validates_developed_head_without_mutation(
    harness: Harness,
) -> None:
    lease, current_head = developed_managed_feature(harness)
    before = harness.registry.get_lease(lease.id)
    before_events = harness.registry.list_events(lease.id)

    result = harness.service.link_pr(lease.id, pr_number=131, apply=False)

    assert result.decision == "preview"
    assert result.actions == ({
        "kind": "link_pr",
        "lease_id": lease.id,
        "path": str(lease.worktree_path),
        "pr_number": 131,
        "head_sha": current_head,
    },)
    assert harness.registry.get_lease(lease.id) == before
    assert harness.registry.list_events(lease.id) == before_events


def test_link_pr_records_verified_head_and_cleanup_state_atomically(
    harness: Harness,
) -> None:
    lease, current_head = developed_managed_feature(harness)

    result = harness.service.link_pr(lease.id, pr_number=131, apply=True)

    assert result.decision == "ready"
    assert result.lease is not None
    assert result.lease.target_pr == 131
    assert result.lease.head_sha == current_head
    assert result.lease.state is LeaseState.CLEANABLE
    assert result.lease.deployment_state is DeploymentState.NOT_REQUIRED
    event = harness.registry.list_events(lease.id)[-1]
    assert event.event_type == "managed_lease_pr_linked"
    assert event.observed_head_sha == current_head
    assert event.pr_number == 131
```

- [ ] **Step 3: Add idempotency and failure-matrix tests**

Cover these cases with parameterized tests and assert lease/event equality before and after every rejection:

```text
@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("open", "pr_not_merged"),
        ("closed_unmerged", "pr_not_merged"),
        ("number", "pr_number_mismatch"),
        ("branch", "pr_branch_mismatch"),
        ("head", "pr_head_mismatch"),
        ("dirty", "dirty_worktree"),
    ),
)
def test_link_pr_rejects_unproven_links_without_mutation(
    harness: Harness, mutation: str, expected_code: str
) -> None:
    lease, current_head = developed_managed_feature(harness)
    matching = harness.github.prs[131]
    if mutation == "open":
        harness.github.prs[131] = replace(
            matching, state="OPEN", merge_commit_sha=None
        )
    elif mutation == "closed_unmerged":
        harness.github.prs[131] = replace(
            matching, state="CLOSED", merge_commit_sha=None
        )
    elif mutation == "number":
        harness.github.prs[131] = replace(matching, number=132)
    elif mutation == "branch":
        harness.github.prs[131] = replace(matching, head_ref="other-branch")
    elif mutation == "head":
        harness.github.prs[131] = replace(matching, head_sha="0" * 40)
    elif mutation == "dirty":
        (lease.worktree_path / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    before = harness.registry.get_lease(lease.id)
    before_events = harness.registry.list_events(lease.id)

    result = harness.service.link_pr(lease.id, pr_number=131, apply=True)

    assert result.status == "blocked"
    assert result.blockers[0]["code"] == expected_code
    assert harness.registry.get_lease(lease.id) == before
    assert harness.registry.list_events(lease.id) == before_events
    assert harness.git.head_sha(lease.worktree_path) == current_head

The body must construct the exact fixture for each mutation rather than testing source text. Add separate tests for:

- same linked PR returns `decision="reuse"` without version/event growth;
- another PR returns `pr_link_mismatch`;
- GitHub `ExternalServiceError` returns `command="wt.link-pr"`, exit `4`, and no mutation;
- a CAS race or cleanup reservation returns exit `5`/`cleanup_reserved` without partial transition;
- after successful link, `finish(pr_number=131, apply=False)` returns only the existing proven-safe removal action.

- [ ] **Step 4: Run the new tests and prove RED**

Run:

```bash
uv run --project cli pytest cli/tests/test_worktree_service.py -q -k 'link_pr'
```

Expected: failures because `WorktreeService.link_pr` does not exist. No pre-existing test may fail before implementation.

- [ ] **Step 5: Commit the RED service contract**

```bash
git add cli/tests/test_worktree_service.py
git commit -m "test: define managed lease PR link contract"
```

### Task 2: Implement the service transition

**Files:**
- Modify: `cli/src/awf/worktrees/service.py`
- Test: `cli/tests/test_worktree_service.py`

- [ ] **Step 1: Add the public service method**

Add `link_pr()` beside `finish()` so lifecycle mutations remain grouped:

```python
def link_pr(
    self, lease_id: str, *, pr_number: int, apply: bool = False
) -> CommandResult:
    lease = self.registry.get_lease_read_only(lease_id)
    if lease is None:
        return self._managed_link_blocked(
            "unknown_lease", f"lease {lease_id} does not exist"
        )
    validated = self._validate_managed_pr_link(lease, pr_number)
    if isinstance(validated, CommandResult):
        return validated
    pull_request, current_head = validated
    if lease.target_pr == pr_number:
        return CommandResult.ok("wt.link-pr", decision="reuse", lease=lease)
    if not apply:
        return CommandResult.ok(
            "wt.link-pr",
            decision="preview",
            lease=lease,
            actions=({
                "kind": "link_pr",
                "lease_id": lease.id,
                "path": str(lease.worktree_path),
                "pr_number": pull_request.number,
                "head_sha": current_head,
            },),
        )
    # Acquire repository_lock, reload the lease, rerun all validation, then CAS.
```

Do not accept a repository path from the caller. The lease registry row determines the Git/GitHub root.

- [ ] **Step 2: Add fail-closed lease and worktree validation**

Implement private helpers with these exact responsibilities:

```text
def _managed_link_lease_blocker(self, lease: Lease) -> CommandResult | None:
    # repository ID must equal self.git.repository_id()
    # lease.managed must be true and owner_kind must be "awf"
    # purpose must be Purpose.FEATURE
    # target_pr=None requires ACTIVE; exact same target_pr requires CLEANABLE
    # REMOVED, BLOCKED, and every other state are rejected
    # no cleanup reservation may exist


def _managed_link_worktree(
    self, lease: Lease
) -> tuple[GitWorktree, str] | CommandResult:
    # exact registered path must exist once
    # registered branch must equal lease.branch and not be detached
    # registered HEAD must equal GitClient.head_sha(lease.worktree_path)
    # status_porcelain must be empty
    # recorded lease.head_sha is intentionally not compared: it is acquisition HEAD
```

Use existing blocker names where semantics match. Return `unsupported_purpose` for scratch/promotion and `unmanaged_lease` for imported/unmanaged rows.

- [ ] **Step 3: Add exact merged-PR provenance validation**

```text
def _validate_managed_pr_link(
    self, lease: Lease, pr_number: int
) -> tuple[PullRequest, str] | CommandResult:
    # positive integer, excluding bool
    # validate lease/worktree first
    # call GhClient.view_pr(pr_number)
    # exact number match
    # _is_completed_pr(pull_request)
    # pull_request.head_ref == lease.branch
    # pull_request.head_sha == verified current worktree HEAD
```

Map GitHub adapter failures to `CommandResult.external_error()` with command `wt.link-pr`, code `github_link_failed`, and exit `4`. Do not convert provider failures to safety blockers.

- [ ] **Step 4: Perform one atomic transition under the repository lock**

After lock acquisition, reload and repeat lease, worktree, and PR validation. Then call:

```python
linked = self.registry.transition(
    current.id,
    LeaseState.CLEANABLE,
    expected_version=current.version,
    event_type="managed_lease_pr_linked",
    summary=f"managed feature lease linked to pull request #{pull_request.number}",
    observed_head_sha=current_head,
    pr_number=pull_request.number,
    head_sha=current_head,
    deployment_state=DeploymentState.NOT_REQUIRED,
)
```

Immediately reload and verify version, `target_pr`, `head_sha`, state, and deployment state. Return exit `5` `registry_conflict` if the CAS or postcondition check fails. Never modify Git files, branch, or worktree registration.

- [ ] **Step 5: Run focused service tests and prove GREEN**

```bash
uv run --project cli pytest cli/tests/test_worktree_service.py -q -k 'link_pr or finish'
```

Expected: all selected tests pass; existing finish contracts remain green.

- [ ] **Step 6: Commit the service implementation**

```bash
git add cli/src/awf/worktrees/service.py
git commit -m "feat: link managed feature leases to merged PRs"
```

### Task 3: Expose the command through argparse and JSON handlers

**Files:**
- Modify: `cli/src/awf/cli.py`
- Modify: `cli/src/awf/commands/wt.py`
- Modify: `cli/tests/test_worktree_commands.py`
- Test: `cli/tests/test_worktree_commands.py`

- [ ] **Step 1: Add failing CLI tests**

Create a helper that acquires a feature lease, commits `feature.txt`, and monkeypatches `GhClient` to return a merged `PullRequest` whose `head_ref` and `head_sha` equal the actual branch/current HEAD. Add tests for:

```python
rc, stdout, stderr = capture_main([
    "wt", "link-pr", "--lease", lease.id, "--pr", "131", "--json"
])
assert rc == 0
assert stderr == ""
payload = json.loads(stdout)
assert payload["command"] == "wt.link-pr"
assert payload["decision"] == "preview"
assert payload["actions"][0]["head_sha"] == current_head
```

Then apply and repeat:

```python
["wt", "link-pr", "--lease", lease.id, "--pr", "131", "--apply", "--json"]
```

Assert `target_pr`, verified `head_sha`, `cleanable`, and `not_required`. Add argparse rejection tests for `0`, `-1`, and `not-a-number`, plus unknown lease JSON blocker and GitHub external error exit `4`.

- [ ] **Step 2: Run CLI tests and prove RED**

```bash
uv run --project cli pytest cli/tests/test_worktree_commands.py -q -k 'link_pr'
```

Expected: argparse rejects the unknown `link-pr` subcommand.

- [ ] **Step 3: Add parser wiring**

Import `run_wt_link_pr` in `cli/src/awf/cli.py` and add the parser between `promote` and `finish`:

```python
wt_link_pr_parser = wt_subparsers.add_parser(
    "link-pr",
    help="Link an exact merged pull request to a managed feature lease.",
)
wt_link_pr_parser.add_argument("--lease", required=True, help="Managed feature lease id.")
wt_link_pr_parser.add_argument(
    "--pr", required=True, type=_positive_int,
    help="Already-merged pull request to link.",
)
wt_link_pr_parser.add_argument(
    "--apply", action="store_true",
    help="Record the verified pull request link instead of previewing it.",
)
wt_link_pr_parser.add_argument(
    "--json", action="store_true", help="Print a versioned JSON result."
)
wt_link_pr_parser.set_defaults(handler=run_wt_link_pr)
```

- [ ] **Step 4: Add the lease-root handler**

Implement `run_wt_link_pr()` beside `run_wt_adopt()` in `cli/src/awf/commands/wt.py`. It must:

1. open `WorktreeRegistry(state_db_path())`;
2. read `args.lease` without requiring `--repo-root`;
3. return structured `unknown_lease` if absent;
4. construct `GitClient(lease.repository_root)` and `GhClient(lease.repository_root)`;
5. call `service.link_pr(args.lease, pr_number=args.pr, apply=args.apply)`;
6. map config/filesystem/Git/SQLite errors exactly like `run_wt_adopt()` but use command `wt.link-pr`;
7. emit one JSON envelope and preserve exit codes.

- [ ] **Step 5: Run CLI and service tests**

```bash
uv run --project cli pytest cli/tests/test_worktree_commands.py cli/tests/test_worktree_service.py -q -k 'link_pr or adopt or finish'
```

Expected: all selected tests pass; imported adoption and cleanup contracts remain unchanged.

- [ ] **Step 6: Commit the CLI surface**

```bash
git add cli/src/awf/cli.py cli/src/awf/commands/wt.py cli/tests/test_worktree_commands.py
git commit -m "feat: expose managed lease PR linking"
```

### Task 4: Document, regress, and resolve the live blocker

**Files:**
- Modify: `cli/README.md`
- Modify: `claude/skills/release-worktree-lifecycle/SKILL.md`
- Test: `cli/tests/test_skill_contract_matrix.py`
- Test: `cli/tests/test_docs_semantic_audit.py`
- Runtime lease: `fd4d32c1-77d8-4381-bb5e-2be55a6a2c12`

- [ ] **Step 1: Update operator documentation**

Add the CLI summary entry:

```text
awf wt link-pr --lease <id> --pr <merged-pr> [--apply] [--json]: acquired feature lease의 clean current HEAD와 exact merged PR head/branch를 검증해 target_pr와 developed head를 원자적으로 기록한다.
```

In the Skill's Feature worktree section, add the post-merge preview/apply sequence:

```sh
awf wt link-pr --lease <id> --pr <merged-pr> --json
awf wt link-pr --lease <id> --pr <merged-pr> --apply --json
awf wt status --repo-root <repo-root> --refresh --json
awf wt finish --repo-root <repo-root> --pr <merged-pr> --json
awf wt finish --repo-root <repo-root> --pr <merged-pr> --apply --json
```

State explicitly: link only an exact already-merged PR; current worktree and PR branch/head must match; recorded acquisition HEAD may be older and is atomically replaced only after verification; stop on every blocker/external error.

- [ ] **Step 2: Run documentation and lifecycle regression tests**

```bash
uv run --project cli pytest \
  cli/tests/test_skill_contract_matrix.py \
  cli/tests/test_docs_semantic_audit.py \
  cli/tests/test_worktree_service.py \
  cli/tests/test_worktree_commands.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run the complete CLI suite**

```bash
uv run --project cli pytest cli/tests -q
```

Expected: zero failures. Record pass/skip/deselected counts exactly from output.

- [ ] **Step 4: Commit documentation**

```bash
git add cli/README.md claude/skills/release-worktree-lifecycle/SKILL.md
git commit -m "docs: add managed feature PR link procedure"
```

- [ ] **Step 5: Run the live command preview against PR #131**

From `/Users/steven/Documents/GitHub/ai-workflow-tools`, using the new CLI source:

```bash
uv run --project /Users/steven/.cache/awf/worktrees/ai-workflow-tools/4d28b0c4-7f26-49a5-a2a1-7f5101ed04ac/cli awf wt link-pr \
  --lease fd4d32c1-77d8-4381-bb5e-2be55a6a2c12 \
  --pr 131 --json
```

Expected: `decision=preview`, no blockers, action head SHA `57898356083575698f67dca6d53868c1cf7bf402`. Confirm PR #131 is merged and its head branch is `awf/awf-skill-validation/feature`.

- [ ] **Step 6: Apply the exact verified link**

```bash
uv run --project /Users/steven/.cache/awf/worktrees/ai-workflow-tools/4d28b0c4-7f26-49a5-a2a1-7f5101ed04ac/cli awf wt link-pr \
  --lease fd4d32c1-77d8-4381-bb5e-2be55a6a2c12 \
  --pr 131 --apply --json
```

Expected: `decision=ready`, `target_pr=131`, `head_sha=57898356083575698f67dca6d53868c1cf7bf402`, `state=cleanable`, `deployment_state=not_required`.

- [ ] **Step 7: Restart at required preflight, then preview cleanup**

```bash
uv run --project /Users/steven/.cache/awf/worktrees/ai-workflow-tools/4d28b0c4-7f26-49a5-a2a1-7f5101ed04ac/cli awf wt status \
  --repo-root /Users/steven/Documents/GitHub/ai-workflow-tools \
  --refresh --json
uv run --project /Users/steven/.cache/awf/worktrees/ai-workflow-tools/4d28b0c4-7f26-49a5-a2a1-7f5101ed04ac/cli awf wt finish \
  --repo-root /Users/steven/Documents/GitHub/ai-workflow-tools \
  --pr 131 --json
```

Expected: the linked lease remains `cleanable`; finish returns only the proven-safe worktree removal action and no blockers.

- [ ] **Step 8: Apply cleanup and verify the blocker is gone**

```bash
uv run --project /Users/steven/.cache/awf/worktrees/ai-workflow-tools/4d28b0c4-7f26-49a5-a2a1-7f5101ed04ac/cli awf wt finish \
  --repo-root /Users/steven/Documents/GitHub/ai-workflow-tools \
  --pr 131 --apply --json
uv run --project /Users/steven/.cache/awf/worktrees/ai-workflow-tools/4d28b0c4-7f26-49a5-a2a1-7f5101ed04ac/cli awf wt doctor \
  --repo-root /Users/steven/Documents/GitHub/ai-workflow-tools --json
```

Expected: finish `decision=removed`; path `/Users/steven/.cache/awf/worktrees/ai-workflow-tools/fd4d32c1-77d8-4381-bb5e-2be55a6a2c12` no longer exists; doctor has no action for lease/path `fd4d32c1…`. Do not alter unrelated doctor findings or user runtime Skill links.

- [ ] **Step 9: Record final branch state and integration boundary**

Confirm the implementation worktree is clean and report its commit IDs. Do not create or merge a new PR without the user's explicit integration instruction; resolving PR #131's cleanup blocker does not imply approval to merge the new CLI feature.
