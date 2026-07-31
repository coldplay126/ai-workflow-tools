# Imported Worktree PR Link Design

## 목적

기존 Git worktree를 `awf wt import`로 등록하고 `awf wt adopt`할 때 이미 병합된 GitHub PR을 명시적으로 연결할 수 있게 한다. 연결된 lease는 기존 `status --refresh`와 `finish --pr` 안전 경로를 재사용한다. branch 기반 자동 추론이나 unmanaged worktree 직접 삭제는 허용하지 않는다.

## 범위

- `awf wt adopt --lease <id> --pr <number> [--apply] --json` 추가
- 기존 `adopt` without `--pr` 동작 유지
- 병합된 PR만 연결
- preview 무변경, apply 시 lock 안에서 재검증 후 단일 CAS transition
- 동일 lease·PR 재실행은 `reuse`
- CLI README와 `release-worktree-lifecycle` Skill 갱신

범위 밖:

- branch 이름으로 PR 자동 검색
- OPEN 또는 closed-unmerged PR 연결
- imported lease의 purpose 변경
- `finish --lease` 추가
- deployment orchestration 변경

## 접근 방식

### 선택: `adopt --pr` 명시 연결

unmanaged → managed 경계를 유지하면서 PR provenance를 lease에 저장한다. 기존 registry의 `target_pr`, `transition(pr_number=...)`, GitHub adapter, `status --refresh`, `finish --pr`를 재사용한다.

### 제외한 접근

- `finish --lease --pr`: adopt 경계를 우회한다.
- branch 기반 PR 추론: 동일 branch 재사용, fork, 복수 PR 때문에 모호하다.

## CLI 계약

```bash
awf wt adopt --lease <lease-id> --pr <merged-pr-number> --json
awf wt adopt --lease <lease-id> --pr <merged-pr-number> --apply --json
```

`--pr`은 positive integer 선택 옵션이다. 생략하면 기존 scratch adopt 계약을 유지한다.

### Preview

1. lease를 read-only로 조회한다.
2. 기존 adoption 검증을 수행한다: imported owner, unmanaged 상태, repository identity, worktree registration, branch, HEAD, clean status.
3. lease repository root에서 GitHub PR을 조회한다.
4. PR provenance를 검증한다.
5. registry, lock file, Git worktree를 변경하지 않고 `decision=preview`와 구조화된 `link_pr` action을 반환한다.

### Apply

1. preview와 동일한 사전 검증을 수행한다.
2. repository lock을 획득한다.
3. lease, Git registration, worktree HEAD/clean status, GitHub PR을 모두 다시 조회한다.
4. registry CAS transition 한 번으로 `managed=true`, `target_pr=<number>`를 기록한다.
5. state와 recorded HEAD는 유지한다.
6. 다음 `status --refresh`가 병합된 non-promotion lease를 `CLEANABLE/not_required`로 전이한다.

## PR provenance 검증

PR 연결은 다음 조건을 모두 만족해야 한다.

- 요청 PR 번호가 조회 결과 번호와 일치
- PR 상태가 `MERGED`, 또는 `CLOSED`이면서 merge commit이 존재
- PR `head_ref`가 imported lease branch와 일치
- PR `head_sha`가 imported lease recorded HEAD와 일치
- registered worktree HEAD와 현재 checkout HEAD도 recorded HEAD와 일치
- worktree가 clean

Squash merge는 merge commit이 아니라 PR의 원래 `head_sha`를 lease HEAD와 비교하므로 지원한다. PR base branch나 merge commit이 lease HEAD의 ancestor라고 추론하지 않는다.

## 상태와 이벤트

성공 apply:

- `managed=true`
- `target_pr=<number>`
- `state` 유지
- `deployment_state` 유지
- event type: `imported_lease_pr_linked`
- event에 `pr_number`, `observed_head_sha` 기록

동일 lease가 이미 동일 PR에 managed 상태로 연결돼 있으면 재검증 후 mutation 없이 `decision=reuse`를 반환한다. 다른 PR에 연결된 managed lease는 `pr_link_mismatch`로 차단한다. 기존 `adopt` without `--pr`의 already-adopted 동작은 유지한다.

## 오류 계약

안전 blocker, exit 3:

- `pr_not_merged`
- `pr_branch_mismatch`
- `pr_head_mismatch`
- `pr_link_mismatch`
- 기존 `dirty_worktree`, `head_mismatch`, `branch_mismatch`, `repository_mismatch`, `orphaned_lease`

외부 의존성 오류, exit 4:

- GitHub 인증, 네트워크, timeout, malformed response

registry/local Git 충돌, exit 5:

- CAS 실패
- registry와 local Git registration 불일치가 command boundary까지 예외로 전파되는 경우

모든 실패는 lease와 worktree를 보존한다. GitHub 오류를 safety blocker로 축소하지 않는다.

## 컴포넌트 변경

- `cli/src/awf/cli.py`: adopt parser에 `--pr` 추가
- `cli/src/awf/commands/wt.py`: positive integer 전달, PR 연결 시 `GhClient` 주입, exit contract 유지
- `cli/src/awf/worktrees/service.py`: adopt PR 검증·재검증·idempotency
- 기존 `registry.transition(pr_number=...)` 재사용
- `cli/tests/test_worktree_service.py`: service behavior
- `cli/tests/test_worktree_commands.py`: CLI JSON/exit code
- `cli/README.md`: legacy import/adopt/refresh/finish 예제
- `claude/skills/release-worktree-lifecycle/SKILL.md`: imported worktree PR-link 절차
- 필요 시 semantic audit 갱신

## 테스트 전략

필수 RED/GREEN 계약:

1. preview는 registry version/event, lock path, Git worktree를 변경하지 않는다.
2. apply는 managed와 target PR을 하나의 transition으로 기록한다.
3. 동일 PR 재실행은 `reuse`이고 version/event가 증가하지 않는다.
4. 다른 PR 재연결은 차단한다.
5. merged PR과 squash-merged PR을 허용한다.
6. OPEN, closed-unmerged PR을 차단한다.
7. PR number, branch, head SHA mismatch를 차단한다.
8. dirty, detached, orphaned, changed HEAD를 기존 규칙대로 차단한다.
9. apply의 lock 획득 후 PR/HEAD 변경을 재검증한다.
10. GitHub 외부 실패는 JSON `status=error`, exit 4다.
11. adopt 후 `status --refresh`가 CLEANABLE로 전이하고 `finish --pr` 기존 경로가 동작한다.
12. 기존 adopt without `--pr` 테스트가 그대로 통과한다.

## 운영 완료 흐름

```text
status --refresh
→ import --dry-run
→ import --apply
→ adopt --lease <id> --pr <merged-pr> preview
→ adopt --lease <id> --pr <merged-pr> --apply
→ status --refresh
→ finish --pr <merged-pr> preview
→ finish --pr <merged-pr> --apply
```

각 JSON의 blocker가 비어 있을 때만 다음 단계로 진행한다. stable merged-main 설치 source로 CLI와 Skill 링크를 이전하기 전에는 현재 release worktree를 제거하지 않는다.
