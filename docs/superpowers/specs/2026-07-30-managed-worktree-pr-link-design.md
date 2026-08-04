# Managed Worktree PR Link Design

## 목적

`awf wt acquire`로 생성한 feature lease에 이미 병합된 GitHub PR을 명시적으로 연결한다. 현재 acquired lease는 `target_pr`를 기록할 공개 명령이 없어, 외부에서 PR을 생성·병합하면 `awf wt finish --pr`가 `lease_not_found`로 차단된다. 새 명령은 provenance를 fail-closed로 검증한 뒤 기존 `finish --pr` 정리 경로를 재사용한다.

## 범위

- `awf wt link-pr --lease <id> --pr <number> [--apply] [--json]` 추가
- AWF-managed `feature` lease만 지원
- `target_pr`가 없는 lease에 이미 병합된 PR만 연결
- preview는 무변경
- apply는 repository lock 안에서 재검증 후 단일 CAS transition
- 동일 lease·PR 재실행은 `reuse`
- 기존 `acquire`, `adopt`, `promote`, `finish`, `gc` 계약 유지
- CLI README와 `release-worktree-lifecycle` Skill에 feature PR 연결 절차 반영

범위 밖:

- branch 이름으로 PR 자동 검색
- OPEN 또는 closed-unmerged PR 연결
- imported lease 연결; 기존 `adopt --pr` 사용
- promotion lease 연결; `promote`가 자체적으로 target PR을 기록
- `finish` 안에서 암시적으로 registry를 수정
- raw SQLite 또는 Git 명령으로 registry/worktree 우회 정리

## 선택한 접근

### 명시적 `link-pr` 명령

PR 연결은 cleanup과 분리된 provenance 기록 작업이다. 별도 명령은 operator 의도를 명확하게 남기고, `adopt`의 imported → managed 책임과 `finish`의 proven-safe cleanup 책임을 유지한다.

### 제외한 접근

- `adopt --pr` 확장: imported adoption과 acquired lease linkage 의미가 섞인다.
- `finish --pr` 자동 복구: 제거 명령이 숨은 registry mutation과 PR 추론을 수행한다.
- registry 직접 수정: 공개 lifecycle 검증과 append-only event 기록을 우회한다.

## CLI 계약

```bash
awf wt link-pr --lease <lease-id> --pr <merged-pr-number> --json
awf wt link-pr --lease <lease-id> --pr <merged-pr-number> --apply --json
```

`--lease`는 비어 있지 않은 lease ID, `--pr`은 positive integer다. mutation은 `--apply`에서만 허용한다.

### Preview

1. 현재 repository identity로 lease를 read-only 조회한다.
2. lease owner, purpose, state, existing PR linkage를 검증한다.
3. Git worktree registration, path, branch, HEAD, clean status를 검증한다.
4. lease repository root에서 요청 PR을 조회한다.
5. PR number, completed state, head branch, head SHA를 현재 Git worktree와 대조한다.
6. registry, lock, Git worktree를 변경하지 않고 `decision=preview`와 구조화된 `link_pr` action을 반환한다.

### Apply

1. preview와 동일한 검증을 수행한다.
2. repository lock을 획득한다.
3. lease, cleanup reservation 부재, Git registration, current branch/HEAD/clean status, GitHub PR을 다시 조회한다.
4. 단일 registry CAS transition으로 다음을 기록한다.
   - `target_pr`를 요청한 검증 완료 PR 번호로 설정
   - `head_sha`를 검증된 현재 worktree/PR head로 설정
   - `state=CLEANABLE`
   - `deployment_state=NOT_REQUIRED`
   - event type `managed_lease_pr_linked`
   - event `pr_number`와 `observed_head_sha`
5. Git worktree, branch, files는 수정하지 않는다.
6. 이후 기존 `finish --pr` preview/apply가 cleanup을 수행한다.

## Lease 검증

모든 조건을 만족해야 한다.

- PR link가 없으면 `state=ACTIVE`, 동일 PR link가 이미 있으면 idempotent reuse를 위한 `state=CLEANABLE`
- lease repository ID가 현재 repository와 일치
- `managed=true`
- `owner_kind=awf`
- `purpose=feature`
- cleanup reservation이 없음
- `target_pr`가 없음, 또는 요청 PR과 동일함
- registry의 worktree path가 Git에 정확히 등록됨
- registered branch와 lease branch가 일치
- registered HEAD와 checkout HEAD가 일치
- worktree가 clean

다른 PR에 이미 연결된 lease는 `pr_link_mismatch`로 차단한다. 동일 PR에 이미 연결된 lease는 모든 provenance를 다시 검증한 뒤 mutation 없이 `decision=reuse`를 반환한다.

## PR provenance 검증

- 요청 PR 번호와 조회 결과 번호가 일치
- PR 상태가 `MERGED`, 또는 `CLOSED`이면서 merge commit이 존재
- PR `head_ref`가 lease branch와 일치
- PR `head_sha`가 registered/check-out worktree HEAD와 일치

Acquired feature lease의 recorded HEAD는 생성 시 base SHA이며 정상 개발 커밋 뒤에는 뒤처진다. 따라서 recorded HEAD와 PR HEAD의 차이는 blocker가 아니다. apply transition이 GitHub PR과 현재 worktree에서 독립적으로 일치가 확인된 HEAD를 새 `head_sha`로 기록한다. Squash merge도 PR의 원래 `head_sha`를 비교하므로 허용된다. merge commit SHA를 worktree HEAD와 비교하거나 branch 이름만으로 PR을 추론하지 않는다.

## 오류 계약

안전 blocker, exit 3:

- `unknown_lease`
- `repository_mismatch`
- `unmanaged_lease`
- `unsupported_purpose`
- `cleanup_reserved`
- `unsupported_state`
- `pr_link_mismatch`
- `pr_not_merged`
- `pr_branch_mismatch`
- `pr_head_mismatch`
- 기존 Git safety blocker: orphaned registration, branch/HEAD mismatch, dirty worktree

외부 의존성 오류, exit 4:

- GitHub 인증, 네트워크, timeout, malformed response

registry 충돌, exit 5:

- lock 안 재조회 후 CAS 실패
- transition 결과 재검증 실패

모든 실패는 lease, branch, worktree를 보존한다. 오류를 `lease_not_found`나 generic cleanup failure로 축소하지 않는다.

## 컴포넌트 변경

- `cli/src/awf/cli.py`: `link-pr` parser와 인자
- `cli/src/awf/commands/wt.py`: service 호출, JSON/exit 전달
- `cli/src/awf/worktrees/service.py`: preview/apply, provenance와 Git safety 검증
- 기존 `registry.transition(pr_number=...)` 재사용
- `cli/tests/test_worktree_service.py`: service 계약
- `cli/tests/test_worktree_commands.py`: CLI JSON, exit code, preview/apply
- `cli/README.md`: acquired feature PR lifecycle 예제
- `claude/skills/release-worktree-lifecycle/SKILL.md`: merge 전후 연결·정리 절차

## 테스트 전략

필수 RED/GREEN 계약:

1. preview는 registry version/event, lock path, Git worktree를 변경하지 않는다.
2. apply는 target PR, CLEANABLE, NOT_REQUIRED를 한 transition으로 기록한다.
3. 동일 PR 재실행은 `reuse`이고 version/event가 증가하지 않는다.
4. 다른 PR 재연결은 차단한다.
5. merged와 squash-merged PR을 허용한다.
6. OPEN, closed-unmerged PR을 차단한다.
7. PR number, branch, head SHA mismatch를 차단한다.
8. unmanaged/imported, promotion, removed, foreign-repository lease를 차단한다.
9. dirty, detached, orphaned worktree와 PR HEAD에 일치하지 않는 current HEAD를 차단한다.
10. apply는 lock 획득 후 PR/HEAD/lease 변경을 재검증한다.
11. GitHub 외부 실패는 JSON `status=error`, exit 4다.
12. 연결 후 기존 `finish --pr` preview/apply가 lease와 worktree를 제거한다.
13. 기존 `adopt --pr`, `promote`, `finish` 테스트가 그대로 통과한다.

## 실제 차단 해소 절차

구현과 검증이 끝난 새 CLI로 다음을 실행한다.

```text
wt link-pr --lease fd4d32c1-77d8-4381-bb5e-2be55a6a2c12 --pr 131 preview
→ wt link-pr ... --apply
→ wt finish --pr 131 preview
→ wt finish --pr 131 --apply
→ wt status --refresh
→ wt doctor
```

각 결과의 blocker가 비어 있을 때만 다음 단계로 진행한다. 현재 진단은 lease의 생성 시 recorded HEAD `ecee5f4fcf7a692b693fc121f8078cf09902ea17`와 실제 worktree/PR head `57898356083575698f67dca6d53868c1cf7bf402`가 다름을 확인했다. 이 차이는 정상 개발 커밋으로 발생했으며, `link-pr --apply`가 검증된 PR head를 `head_sha`와 `target_pr`에 함께 기록해야 한다.

## 완료 조건

- 새 명령의 focused/full test가 통과한다.
- PR #131 연결 결과가 `target_pr=131`, `CLEANABLE`, `NOT_REQUIRED`다.
- `finish` preview가 proven-safe removal action만 반환한다.
- `finish --apply` 후 lease는 removed 상태이고 worktree path는 존재하지 않는다.
- `wt doctor`에 해당 lease/path 관련 mismatch가 없다.
- 다른 사용자 worktree나 runtime Skill 링크는 변경하지 않는다.
