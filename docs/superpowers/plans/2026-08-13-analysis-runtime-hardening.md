# Analysis runtime hardening implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 분석 산출물의 세대 무결성, multi-agent 판정과 실행 제한, 장애 복구, 증분·provider 설정 일관성을 네 개의 독립 patch release로 강화한다.

**Architecture:** 기존 공개 CLI와 상태 schema를 유지한다. 각 단계는 현재 orchestration 경계(`commands/analyze.py`, `core/analysis_resume.py`, `core/agent_runner.py`, `core/multi_agent.py`)에서 실패 신호를 보존하고 기존 fallback을 재사용한다. 단계마다 RED→GREEN 회귀 테스트, 사용자·참조 문서, CHANGELOG, 버전 동기화를 완료한 뒤 별도 PR과 GitHub Release를 발행한다.

**Tech Stack:** Python 3.9+, pytest, uv, JSON state/artifact contracts, OMP native/print adapters, GitHub CLI, AWF managed worktree lifecycle.

---

## Release invariants

- Stage 1은 `v0.1.1`, Stage 2는 `v0.1.2`, Stage 3는 `v0.1.3`, Stage 4는 `v0.1.4`다.
- 각 단계는 직전 release가 병합된 `origin/main`에서 새 managed feature worktree를 확보한다.
- 버전 동기화 대상은 `cli/pyproject.toml`, `cli/src/awf/__init__.py`, `cli/uv.lock`, `cli/tests/test_docs_semantic_audit.py`다. `cmux-agent` 버전은 변경하지 않는다.
- 각 단계의 `CHANGELOG.md` 항목은 `[Unreleased]` 바로 아래에 최신 버전 우선으로 추가한다.
- PR checks와 로컬 전체 테스트가 통과한 뒤에만 merge한다. merge 뒤 `awf wt link-pr`과 `awf wt finish`를 사용한다.
- GitHub Release 이름은 `awf-cli vX.Y.Z`, tag는 `vX.Y.Z`, target은 `main`이다. 본문은 `Overview`, `Highlights`, `Reliability fixes`, tag-pinned CHANGELOG 링크 순서를 따른다.

## File map

- `cli/src/awf/core/analysis_resume.py`: Stage 2 결과 세대 검사와 Stage 3 실패 보존.
- `cli/src/awf/commands/analyze.py`: 이번 실행 출력 완전성, domain lock, Ctrl-C, hash 확정, JSON stdout, fanout provider 정책.
- `cli/src/awf/core/analysis_fanout.py`: 손상된 writer contract를 existing single-agent fallback으로 정규화.
- `cli/src/awf/core/agent_runner.py`: timeout 전달·판정과 `AgentResult` 방어.
- `cli/src/awf/runners/omp.py`: non-object worker result 정규화.
- `cli/src/awf/core/multi_agent.py`: 명시적 FAIL 판정과 실제 downgrade 실행.
- `cli/src/awf/core/judge.py`: location-aware high-severity finding signature.
- `cli/tests/test_analysis_stage3.py`, `test_analysis_outputs.py`: Stage 1 분석 회귀 계약.
- `cli/tests/test_agent_runner.py`, `test_multi_agent_judge_v2.py`, `test_omp_runtime.py`: Stage 2 실행·판정 계약.
- `cli/tests/test_analysis_spec.py`, `test_analyze_runtime_hardening.py`: Stage 3 fallback·동시성·중단 계약.
- `cli/tests/test_analyze_runtime.py`, `test_workflow_results_apply.py`: Stage 4 hash/stdout/provider/judge 계약.
- `cli/README.md`, `docs/reference/analysis-pipeline.md`, `docs/patterns/analysis-pipeline/*`: analysis 사용자·불변식 문서.
- `docs/reference/multi-agent.md`, `docs/patterns/multi-agent/*`: 판정·timeout·fallback 사용자 계약.

## Stage 1 — Analysis generation integrity (`v0.1.1`)

### Task 1: Reject stale or incomplete Stage 2 generations

**Files:**
- Modify: `cli/tests/test_analysis_stage3.py`
- Modify: `cli/tests/test_analysis_outputs.py`
- Modify: `cli/src/awf/core/analysis_resume.py`
- Modify: `cli/src/awf/commands/analyze.py`

- [ ] **Step 1: Write failing stale-reuse tests**

Add a resume test using the existing `_FakeContext` and `_make_analysis_state` helpers. Save a Stage 2 result artifact, save old hashes, pass changed `current_file_entries`, and assert:

```python
result = resolve_analysis_resume(context, current_file_entries=changed_entries)
assert result["reused_result"] is False
assert any("source" in message and "changed" in message for message in result["messages"])
```

Add the equivalent bundle-config invalidation assertion when the saved `configHash` differs from `compute_bundle_config_hash(context)`.

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest -q tests/test_analysis_stage3.py -k "reuse_rejected"
```

Expected: `reused_result` is currently true.

- [ ] **Step 3: Implement generation-aware reuse**

In `_resolve_analysis_resume_locked()`, make the saved Stage 2 result reusable only when source hashes and bundle configuration belong to the current generation:

```python
can_reuse_generation = not files_changed and not bundle_invalidated
reused_result = bool(
    can_reuse_generation
    and result_path
    and result_path.exists()
    and stage2_status in {"in_progress", "completed"}
    and stage1_status == "completed"
    and not output_files_present
)
```

When generation invalidation is detected, clear the stale result pointer through the existing cleanup helper and append a deterministic diagnostic message.

- [ ] **Step 4: Write failing current-attempt completeness test**

Use a temp analysis context with four old required files. Parse a new Stage 2 payload that omits `external-integration.md`, write the parsed files, then finalize with the current attempt’s missing set. Assert:

```python
assert finalized["layers"]["analyze"]["stage2"]["status"] == "failed"
assert finalized["layers"]["output"]["status"] == "failed"
assert "external-integration.md" in finalized["layers"]["output"]["errorMessage"]
```

- [ ] **Step 5: Verify RED**

Run:

```bash
uv run pytest -q tests/test_analysis_outputs.py -k "current_attempt"
```

Expected: old files cause the finalizer to report completed.

- [ ] **Step 6: Pass explicit output completeness to finalization**

Extend the finalizer with a backward-compatible keyword-only argument:

```python
def finalize_analysis_run(
    context,
    provider_name: str,
    returncode: int,
    error_message: str = "",
    *,
    missing_output_files: Sequence[str] = (),
) -> dict:
    ...
```

A non-empty set must force Stage 2/output failure even when old files exist. In `run_analyze()`, initialize the current attempt to the mode’s required output set, replace it with each selected Stage 2 payload’s `output_summary["missing_files"]`, and pass it to both the saved-result and normal finalization paths. Recompute it when cross synthesis selects the secondary payload.

- [ ] **Step 7: Run focused tests**

```bash
uv run pytest -q tests/test_analysis_stage3.py tests/test_analysis_outputs.py tests/test_analysis_resume_concurrency.py
```

Expected: all pass.

### Task 2: Preserve Stage 3 failure through finalization

**Files:**
- Modify: `cli/tests/test_analysis_stage3.py`
- Modify: `cli/src/awf/core/analysis_resume.py`

- [ ] **Step 1: Replace the obsolete cleanup expectation with failure preservation tests**

Assert that a failed Stage 3 run retains `errorMessage`, `reason`, retry count, and diagnostic artifact, and that Stage 2 success cannot change output to completed:

```python
finalized = finalize_analysis_run(context, "fixture", 0)
assert finalized["layers"]["analyze"]["stage3"]["status"] == "failed"
assert finalized["layers"]["output"]["status"] == "failed"
assert finalized["layers"]["analyze"]["stage3"]["errorMessage"] == "stage3 exploded"
```

- [ ] **Step 2: Verify RED**

```bash
uv run pytest -q tests/test_analysis_stage3.py -k "stage3_failed or finalization_preserves"
```

Expected: resume deletes the artifact or finalization reports completed.

- [ ] **Step 3: Implement fail-closed Stage 3 handling**

Do not unlink a failed Stage 3 diagnostic artifact during resume. Evaluate retry eligibility without erasing the last failure. In `_finalize_analysis_run_locked()`, check Stage 3 before the Stage 2/output success branch; a failed Stage 3 keeps output failed.

- [ ] **Step 4: Verify GREEN**

```bash
uv run pytest -q tests/test_analysis_stage3.py tests/test_analysis_outputs.py
```

Expected: all pass.

### Task 3: Document and release Stage 1

**Files:**
- Modify: `cli/README.md`
- Modify: `docs/reference/analysis-pipeline.md`
- Modify: `docs/patterns/analysis-pipeline/02-stages.md`
- Modify: `docs/patterns/analysis-pipeline/03-resume-optimization.md`
- Modify: `claude/skills/analysis/reference.md`
- Modify: `CHANGELOG.md`
- Modify: `cli/pyproject.toml`
- Modify: `cli/src/awf/__init__.py`
- Modify: `cli/uv.lock`
- Modify: `cli/tests/test_docs_semantic_audit.py`

- [ ] **Step 1: Update contracts**

Document these invariants once in the pattern docs and reference their concrete state/artifact fields in the reference docs:

```text
A Stage 2 payload is complete only when the current attempt supplies every required output.
Saved Stage 2 results are reusable only for the same source/config generation.
A failed required Stage 3 remains failed until a later Stage 3 attempt succeeds or is explicitly skipped by policy.
```

- [ ] **Step 2: Add `0.1.1` changelog and synchronize versions**

Use `### Fixed` and `### Documentation`. Update the four `awf-cli` version sites to `0.1.1`.

- [ ] **Step 3: Verify release candidate**

```bash
cd cli && uv lock && uv run --group dev pytest -q
cd ../cmux-agent && uv run pytest -q
cd .. && git diff --check
```

Expected: CLI and cmux suites pass; no whitespace errors.

- [ ] **Step 4: Commit, push, create/merge PR, tag, and release**

Use a managed feature PR. Wait for required checks, merge without bypassing blockers, link the merged PR to the lease, then create `v0.1.1` / `awf-cli v0.1.1` from merged `main`. Verify `awf --version` from main reports `0.1.1` before publishing the GitHub Release.

## Stage 2 — Multi-agent verdict and execution safety (`v0.1.2`)

### Task 4: Enforce verdict, timeout, and parsed-result contracts

**Files:**
- Create: `cli/tests/test_agent_runner.py`
- Modify: `cli/tests/test_multi_agent_judge_v2.py`
- Modify: `cli/tests/test_omp_runtime.py`
- Modify: `cli/src/awf/core/agent_runner.py`
- Modify: `cli/src/awf/core/multi_agent.py`
- Modify: `cli/src/awf/runners/omp.py`

- [ ] **Step 1: Write failing explicit-FAIL tests**

```python
agent = _result(conclusion="FAIL: tests did not pass", findings=[])
assert judge([agent])[0] == "FAIL"
```

Also assert a PASS/FAIL pair produces the Rule 3 disagreement result.

- [ ] **Step 2: Verify RED and implement normalized conclusion classification**

Run the focused test, then replace substring matching with a shared startswith classifier for `PASS`, `FAIL`, and unknown conclusions. Unknown structured conclusions remain fail-closed.

- [ ] **Step 3: Write failing timeout forwarding tests**

Use a provider that records `timeout_sec` and returns 124. Assert:

```python
assert provider.received_timeout == 17
assert result.timed_out is True
```

- [ ] **Step 4: Verify RED and implement timeout forwarding**

Pass `timeout_sec` to `provider.complete()`. Classify explicit provider timeout (`returncode == 124`) as timed out; preserve the existing streaming deadline behavior.

- [ ] **Step 5: Write failing OMP non-object tests**

Feed a native worker `result` containing a list or string and assert no `AgentResult` property raises. When JSON object output is required, assert `parse_error=True` and `parsed is None`.

- [ ] **Step 6: Verify RED and normalize at both boundaries**

Normalize non-dict native/print results before constructing `AgentResult`, and make `AgentResult` properties tolerate non-dict values defensively.

- [ ] **Step 7: Run focused tests**

```bash
uv run pytest -q tests/test_agent_runner.py tests/test_multi_agent_judge_v2.py tests/test_omp_runtime.py
```

Expected: all pass.

### Task 5: Document and release Stage 2

Update `cli/README.md`, `docs/reference/multi-agent.md`, `docs/patterns/multi-agent/02-judge-rules.md`, and `docs/patterns/multi-agent/03-provider-routing.md`. Record explicit FAIL precedence, timeout inheritance, and non-object OMP rejection. Add `0.1.2` to CHANGELOG, synchronize the four version sites, run both full suites, merge a separate managed PR, and publish `v0.1.2` / `awf-cli v0.1.2`.

## Stage 3 — Failure recovery and exclusive execution (`v0.1.3`)

### Task 6: Normalize fanout failures and propagate cancellation

**Files:**
- Modify: `cli/tests/test_analysis_spec.py`
- Create: `cli/tests/test_analyze_runtime_hardening.py`
- Modify: `cli/src/awf/core/analysis_fanout.py`
- Modify: `cli/src/awf/commands/analyze.py`

- [ ] **Step 1: Convert zero-writer test to the fallback contract**

```python
result, error, metadata = run_stage2_fanout(...)
assert result is None
assert error.startswith("fanout_unavailable:")
assert metadata["status"] == "fallback"
```

- [ ] **Step 2: Verify RED and return the existing tuple fallback**

Move writer validation into the protected path. Invalid/missing writer configuration returns `(None, diagnostic, metadata)` instead of raising. Do not hide provider execution failures that already have their own result contract.

- [ ] **Step 3: Write the `--all` interrupt test**

Stub three scanner units and make the first child return 130. Assert only one child call, no delay, and outer return code 130.

- [ ] **Step 4: Verify RED and stop immediately on 130**

Add an explicit `if rc == 130` branch before appending ordinary failures.

### Task 7: Reject concurrent mutation of the same domain

**Files:**
- Modify: `cli/tests/test_analyze_runtime_hardening.py`
- Modify: `cli/src/awf/commands/analyze.py`
- Reuse: `cli/src/awf/worktrees/locking.py`

- [ ] **Step 1: Write a cross-process lock test**

Hold `.analysis-run.lock` in a child process and invoke the mutating domain route in the parent. Assert provider execution is not reached and the command returns a deterministic non-success code with `analysis already running` on stderr.

- [ ] **Step 2: Verify RED**

Expected: both processes currently enter analysis.

- [ ] **Step 3: Add a narrow locked domain helper**

Keep `--status`, `--dry-run`, `--check`, `--catalog`, and `--cycles` read-only. After context resolution and dry-run output, delegate mutations through:

```python
try:
    with repository_lock(context.ai_context_dir / ".analysis-run.lock", blocking=False):
        return _run_analyze_domain_mutation(...)
except BlockingIOError:
    print("error: analysis already running for service/domain", file=sys.stderr)
    return 4
```

Extract, do not duplicate, the current mutation body. The context manager guarantees release on every return and exception.

- [ ] **Step 4: Verify GREEN**

Run the new hardening tests and the complete analysis test subset.

### Task 8: Document and release Stage 3

Update `cli/README.md`, `docs/reference/analysis-pipeline.md`, `docs/patterns/analysis-pipeline/02-stages.md`, and `docs/reference/multi-agent.md`. Document malformed fanout fallback, same-domain exclusive mutation, and `--all` rc 130 behavior. Add `0.1.3`, synchronize versions, run both full suites, merge a separate managed PR, and publish `v0.1.3` / `awf-cli v0.1.3`.

## Stage 4 — Incremental and provider-policy consistency (`v0.1.4`)

### Task 9: Commit hashes only after success and restore JSON stdout

**Files:**
- Create: `cli/tests/test_analyze_runtime.py`
- Modify: `cli/tests/run_analysis_fixture.py`
- Modify: `cli/src/awf/commands/analyze.py`

- [ ] **Step 1: Write failing hash-baseline test**

Run one successful analysis, change a source, force provider failure, and assert `.ai-context/.tmp/hashes.json` remains byte-for-byte equal to the successful baseline.

- [ ] **Step 2: Verify RED and move hash publication**

Compute drift at startup but call `save_hashes_file()` only after `finalized.layers.output.status == "completed"`. Keep the old baseline available to Stage 1 transitive invalidation.

- [ ] **Step 3: Write failing stdout restoration tests**

Directly call a JSON-mode early-failure route and assert `sys.stdout` is restored. At CLI level assert stdout contains one parseable JSON envelope and diagnostics remain on stderr.

- [ ] **Step 4: Verify RED and add a scoped redirect**

Use `contextlib.redirect_stdout(sys.stderr)` around the domain execution helper, print the final envelope explicitly to the original stream, and let the context manager restore stdout for every early return.

### Task 10: Make downgrade, finding comparison, and fanout permissions truthful

**Files:**
- Modify: `cli/tests/test_multi_agent_judge_v2.py`
- Modify: `cli/tests/test_workflow_results_apply.py`
- Modify: `cli/tests/test_analyze_runtime.py`
- Modify: `cli/src/awf/core/multi_agent.py`
- Modify: `cli/src/awf/core/judge.py`
- Modify: `cli/src/awf/commands/analyze.py`

- [ ] **Step 1: Write failing downgrade test**

Make both cross agents fail and a precise fallback pass. Assert the fallback provider is called, result mode matches the actual fallback, and the reason says `cross → precise` rather than `solo`.

- [ ] **Step 2: Implement one bounded fallback dispatch**

Consume `auto_downgrade()`’s returned target. Dispatch `_run_precise` or `_run_solo` once and preserve the actual target in provenance/reason. Do not recursively downgrade.

- [ ] **Step 3: Write failing finding-signature test**

Give providers HIGH findings with the same severity/category/description but different locations. Assert `high_severity_findings_mismatch`.

- [ ] **Step 4: Canonicalize `location` and `locations`**

Include normalized location(s) and `description` (falling back to `summary`) in `_finding_signature()`; sort multi-location values deterministically.

- [ ] **Step 5: Write failing fanout permission test**

Capture every provider produced by the fanout factory under `--yolo`; assert each receives `bypassPermissions`.

- [ ] **Step 6: Apply permission policy in the factory**

Create a local factory that calls `registry.get(provider_name)`, applies `_apply_provider_permission_mode(..., yolo=...)`, and returns the configured provider.

### Task 11: Preserve streaming provider options

**Files:**
- Modify: `cli/tests/test_agent_runner.py`
- Modify: `cli/src/awf/core/agent_runner.py`
- Modify as needed: provider command builders under `cli/src/awf/providers/`

- [ ] **Step 1: Write executable-capture tests**

For Claude Code, assert streaming argv preserves `--effort`, JSON schema, and add-dir flags. For Codex, assert reasoning effort/output schema are preserved and the prompt is sent through stdin rather than argv.

- [ ] **Step 2: Verify RED**

Expected: current streaming argv contains only `command + flags` and prompt argv.

- [ ] **Step 3: Share provider spawn specifications**

Extract the smallest provider-owned command/stdin builder used by both `complete()` and streaming execution. Do not introduce a second command grammar. Keep temporary output-file parsing and progress callbacks unchanged.

- [ ] **Step 4: Verify GREEN**

Run agent runner, provider conformance, dispatch, and multi-agent tests.

### Task 12: Document and release Stage 4

Update `README.md` Korean/English JSON contract paragraphs, `cli/README.md`, `docs/reference/analysis-pipeline.md`, `docs/patterns/analysis-pipeline/03-resume-optimization.md`, `docs/reference/multi-agent.md`, and `docs/patterns/multi-agent/03-provider-routing.md`. Document success-only hash baselines, one-envelope JSON stdout, actual downgrade target, location-aware comparison, permission inheritance, and streaming option parity. Add `0.1.4`, synchronize versions, run both full suites, merge a separate managed PR, and publish `v0.1.4` / `awf-cli v0.1.4`.

## Final verification

- [ ] From refreshed `main`, verify tags `v0.1.1` through `v0.1.4` point to merged main commits and all four GitHub Releases are non-draft/non-prerelease.
- [ ] Run `uv run --project cli awf --version` and require `awf 0.1.4`.
- [ ] Run the CLI and cmux-agent full suites once more from merged main.
- [ ] Run `awf wt status --refresh --json`, finish each linked merged feature lease only after required deployment/release evidence is present, and leave unrelated leases untouched.
