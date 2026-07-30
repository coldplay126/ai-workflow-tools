# 배포 Worktree 수명주기 CLI와 스킬 설계

## 배경

현재 작업 디렉터리에는 기능 구현, 테스트, 리뷰, 운영 승격을 위해 만든 Git worktree가 장기간 누적된다. 생성 주체가 사용자, Claude, OMP worker 등으로 분산되어 있고 PR merge, 배포 상태, worktree 정리가 연결되어 있지 않다. 특히 squash merge를 사용하는 저장소에서는 `git branch --merged`만으로 삭제 안전성을 판정할 수 없다.

해결책은 두 계층으로 나눈다.

- `awf wt`: worktree 생성, 재사용, 승격, 상태 추적, 안전한 정리의 단일 실행 엔진
- `release-worktree-lifecycle` 스킬: Claude, OMP, Codex 및 Agent Skills 호환 LLM이 배포 요청을 받았을 때 직접 Git 명령을 조합하지 않고 `awf wt`를 사용하도록 하는 절차 계약

스킬은 정책을 설명하고 CLI를 호출한다. 상태 판정과 삭제 안전성은 항상 CLI가 책임진다.

## 목표

- worktree를 agent나 세션이 아니라 `initiative + repository + purpose` 단위로 재사용한다.
- 기능 worktree와 일시적인 운영 승격 worktree를 명확히 구분한다.
- staging PR의 변경만 main/master에 승격하고 staging의 무관한 변경을 포함하지 않는다.
- PR, CI, 배포 상태와 worktree 수명주기를 연결한다.
- dirty, closed-unmerged, 사용자 소유, HEAD 불일치 worktree를 자동 삭제하지 않는다.
- 기존 worktree를 읽기 전용으로 inventory한 뒤 명시적으로 채택할 수 있게 한다.
- LLM이 안정적으로 소비할 수 있는 versioned JSON 출력을 제공한다.
- 하나의 스킬 원본을 Claude와 Agent Skills 호환 경로에 배포한다.

## 비목표

- GitHub Actions, Argo CD, Kubernetes 등 저장소별 배포 시스템을 새로운 범용 배포 엔진으로 대체하지 않는다.
- 모든 저장소의 브랜치 전략을 trunk-based development로 변경하지 않는다.
- `node_modules`나 virtualenv를 worktree 사이에서 직접 공유하지 않는다.
- dirty 또는 unmerged worktree를 자동 수정, stash, commit, force-delete하지 않는다.
- 등록되지 않은 사용자 worktree를 GC 대상으로 간주하지 않는다.
- 스킬만으로 안전성을 보장하지 않는다. 강제 조건은 CLI에서 검증한다.

## 선택한 구조

### 단일 실행 엔진

`ai-workflow-tools/cli`에 `awf wt` command group을 추가한다. 모든 mutation은 이 command group을 거친다. 스킬, 사용자, 다른 자동화는 동일한 CLI 계약을 사용한다.

### 다중 LLM 스킬

스킬 원본은 기존 저장소 관례에 맞춰 다음 위치에 둔다.

```text
claude/skills/release-worktree-lifecycle/SKILL.md
```

`setup.sh`가 같은 원본을 다음 두 사용자 경로에 심링크한다.

```text
~/.claude/skills/release-worktree-lifecycle
~/.agents/skills/release-worktree-lifecycle
```

Claude Code는 첫 번째 경로를 사용한다. OMP는 `agents` skill provider를 통해 두 번째 경로를 발견하고, Codex 및 Agent Skills 호환 도구도 두 번째 경로를 사용할 수 있다. 같은 파일을 복사하지 않으므로 provider별 문서가 drift하지 않는다.

기존 실제 디렉터리나 파일을 설치 과정에서 조용히 덮어쓰지 않는다. 올바른 심링크는 재사용하고, 다른 심링크는 명시적으로 갱신하며, 충돌하는 실제 디렉터리는 경고 후 보존한다.

## 관리 단위와 저장 위치

### Lease identity

worktree lease의 논리적 identity는 다음 tuple이다.

```text
(repository identity, initiative, purpose)
```

- `repository identity`: canonical remote URL과 원본 checkout realpath에서 계산한 안정적 ID
- `initiative`: 사람이 읽을 수 있는 작업 slug
- `purpose`: `feature`, `promote`, `scratch` 중 하나

동일 tuple의 활성 lease가 있으면 `acquire`는 새 worktree를 만들지 않고 기존 경로를 반환한다. 동시에 다른 branch가 반드시 필요한 경우에는 다른 purpose 또는 명시적 initiative를 사용한다.

### Worktree 경로

CLI가 생성하는 worktree는 기본적으로 다음 위치에 둔다.

```text
~/.cache/awf/worktrees/<repo-name>/<lease-id>/
```

경로는 `AWF_WORKTREE_CACHE_DIR`로 override할 수 있다. 사용자 저장소 루트에는 원본 checkout만 남긴다.

### Registry

기본 registry는 다음 SQLite 파일이다.

```text
~/.local/state/awf/worktrees.sqlite3
```

테스트와 격리 실행을 위해 `AWF_WORKTREE_STATE_DB`로 override할 수 있다. registry에는 secret, token, API key, 환경변수 값, command stdout 전문을 저장하지 않는다.

## Registry schema

### `worktree_leases`

- `id`: UUID text primary key
- `repository_id`: canonical repository identity
- `repository_name`
- `repository_root`: 원본 checkout realpath
- `worktree_path`: unique realpath
- `initiative`
- `purpose`: `feature | promote | scratch`
- `branch`
- `base_ref`
- `head_sha`
- `managed`: CLI가 삭제할 수 있는지 여부
- `owner_kind`: `awf | imported | user`
- `owner_id`: session/run/user label; optional
- `state`
- `source_pr`: promotion source PR number; optional
- `target_pr`: 이 worktree에서 생성한 PR number; optional
- `deployment_state`: `unknown | pending | healthy | failed | not_required`
- `retain`: 자동 정리 금지 flag
- `created_at`, `last_used_at`, `updated_at`, `removed_at`
- `version`: optimistic state transition version

활성 lease에 대해 `(repository_id, initiative, purpose)` unique constraint를 적용한다. `REMOVED` row는 감사 기록으로 남기되 활성 unique constraint에서 제외한다.

### `worktree_events`

append-only event table이다.

- `id`
- `lease_id`
- `event_type`
- `from_state`, `to_state`
- `observed_head_sha`
- `pr_number`
- `summary`: redacted bounded text
- `created_at`

GitHub 응답 전문, secret, command 환경변수는 저장하지 않는다.

### 동시성

동일 repository의 mutation은 state directory 아래 repository hash 기반 file lock으로 직렬화한다. SQLite transaction은 registry 변경에만 짧게 사용하며 Git, GitHub, 테스트 명령 실행 중에는 장기 write transaction을 유지하지 않는다. 외부 작업 후 lock을 보유한 상태에서 실제 Git 상태를 다시 읽고 compare-and-swap 방식으로 registry를 갱신한다.

## 상태 모델

정상 상태는 purpose에 따라 갈라진다.

```text
feature: ACTIVE -> PR_OPEN -> MERGED -> CLEANABLE -> REMOVED
promote: ACTIVE -> PR_OPEN -> MERGED -> DEPLOYING -> DEPLOYED -> CLEANABLE -> REMOVED
scratch: ACTIVE -> CLEANABLE -> REMOVED
```

예외 상태:

- `DIRTY`: tracked 또는 untracked 변경이 있음
- `CLOSED_UNMERGED`: 연결 PR이 merge되지 않고 닫힘
- `ORPHANED`: registry와 `git worktree list`가 불일치
- `BLOCKED`: HEAD, branch, PR, CI, 배포 조건 중 하나가 안전 조건을 충족하지 않음

`feature` purpose는 merge 후 배포 확인이 필요하지 않으며 `deployment_state=not_required`로 `CLEANABLE`에 갈 수 있다. `promote` purpose는 production deployment가 `healthy`로 확인되어야 `CLEANABLE`에 갈 수 있다. `scratch` purpose는 PR이 없을 수 있으므로 자동 GC하지 않고 명시적 `finish`와 clean 상태를 요구한다.

예외 상태는 데이터를 수정해 숨기지 않는다. 원인이 해소된 뒤 다음 `status --refresh`, `doctor`, 또는 `finish`가 정상 상태로 재평가한다.

## CLI 계약

모든 subcommand는 사람용 text 출력과 `--json`을 지원한다. mutation 명령은 기본 preview와 명시적 `--apply`를 구분한다. `acquire`만 새 worktree 생성을 핵심 동작으로 하므로 `--apply`를 요구하고, 기존 lease 조회만 발생한 경우에는 mutation 없이 성공한다.

### `awf wt acquire`

```bash
awf wt acquire \
  --repo-root /path/to/repo \
  --initiative reward-artist-widget \
  --purpose feature \
  --base staging \
  --apply \
  --json
```

동작:

1. repository identity와 원본 checkout을 검증한다.
2. exact active lease가 있으면 branch, path, Git registration을 검증하고 재사용한다.
3. lease가 없으면 base ref를 fetch하고 정확한 SHA를 고정한다.
4. branch가 명시되지 않으면 `awf/<initiative>/<purpose>`를 생성한다.
5. cache 경로에 worktree를 만들고 registry/event를 기록한다.
6. 충돌 branch, 이미 다른 worktree에 checkout된 branch, 불명확한 base는 fail-closed 처리한다.

`--base`가 없으면 `.awf/worktree.toml`의 `default_base`를 사용한다. 설정도 없으면 `origin/HEAD`를 사용하며 `staging`을 추측하지 않는다.

### `awf wt status`

```bash
awf wt status [--repo-root PATH] [--initiative SLUG] [--refresh] [--json]
```

기본 모드는 registry와 로컬 Git 상태만 읽는다. `--refresh`는 연결된 PR, checks, deployment check를 조회하고 상태 전이를 기록한다. 외부 조회 실패는 기존 상태를 성공으로 간주하지 않고 warning과 stale timestamp를 반환한다.

### `awf wt promote`

```bash
awf wt promote \
  --repo-root /path/to/repo \
  --source-pr 372 \
  --to main \
  --apply \
  --json
```

사전 조건:

- source PR은 GitHub에서 정확히 식별되어야 한다.
- source PR의 base, head SHA, commit 목록, merge 상태, checks를 수집한다.
- 기본 정책은 source PR이 source base에 `MERGED`되고 required checks가 성공한 경우만 허용한다.
- target branch는 최신 remote SHA로 고정한다.
- 동일 source PR과 target에 대한 활성 promotion lease가 있으면 재사용한다.

적용 순서:

1. target branch에서 `promote` purpose의 임시 worktree를 만든다.
2. source PR의 base SHA와 head SHA를 fetch하고 merge-base를 계산한다.
3. merge-base에서 head까지의 binary diff를 PR delta로 생성한다. merge 방식과 관계없이 branch나 merge commit 전체가 아니라 이 delta 하나만 승격 입력으로 사용한다.
4. target worktree에 PR delta를 index 포함 상태로 적용하고 단일 promotion commit을 만든다.
5. promotion commit의 changed path 집합이 GitHub source PR changed path 집합과 정확히 같은지 비교한다.
6. path가 추가·누락되거나 source SHA를 fetch할 수 없거나 delta가 clean하게 적용되지 않으면 중단한다.
7. 충돌 시 working tree를 보존하고 `BLOCKED`로 기록한다. 자동 conflict resolution은 하지 않는다.
8. `.awf/worktree.toml`의 production verify commands를 실행한다.
9. 검증이 모두 성공한 경우에만 target PR을 생성하거나 기존 exact PR을 연결한다.
10. PR body에 source PR, source base/head SHA, target base SHA, promotion commit SHA, lease ID를 기록한다.

staging branch 전체를 main/master에 merge하거나 cherry-pick하지 않는다.

### `awf wt finish`

```bash
awf wt finish --repo-root /path/to/repo --pr 375 --apply --json
```

연결 PR, HEAD, clean 상태, 배포 상태를 새로 확인한다. 다음 조건을 모두 만족할 때만 worktree를 제거하고 branch 정리를 수행한다.

- CLI가 관리하는 lease이고 `managed=true`
- `retain=false`
- `git status --porcelain`이 비어 있음
- PR이 `MERGED`
- worktree HEAD가 기록된 PR head SHA 또는 검증된 promotion SHA와 일치
- 같은 branch를 다른 worktree가 사용하지 않음
- `promote` purpose이면 production deployment check가 `healthy`
- local/remote branch 삭제 대상이 source-of-truth branch가 아님

조건 미충족 시 아무것도 삭제하지 않고 blockers를 반환한다. remote branch 삭제는 CLI가 생성했고 연결 PR이 merge된 branch에만 허용한다.

### `awf wt gc`

```bash
awf wt gc --merged --older-than 7d --dry-run --json
awf wt gc --merged --older-than 7d --apply --json
```

기본 동작은 dry-run이다. `--apply`에서도 각 lease에 `finish`와 동일한 안전 조건을 다시 적용한다. registry에 없는 worktree와 `managed=false` lease는 후보 목록에만 표시하고 제거하지 않는다.

### `awf wt import`와 `adopt`

```bash
awf wt import --root ~/Documents/GitHub --dry-run --json
awf wt import --root ~/Documents/GitHub --apply --json
awf wt adopt --lease <lease-id> --apply --json
```

`import`는 기존 worktree를 `owner_kind=imported`, `managed=false`로 등록한다. branch, dirty 상태, remote, 연결 PR을 관찰하지만 수정하지 않는다. `adopt`는 clean 상태, repository registration, branch ownership, PR 관계를 검증한 뒤에만 `managed=true`로 전환한다. closed-unmerged 또는 dirty lease는 채택할 수 없다.

### `awf wt doctor`

registry, `git worktree list --porcelain`, cache directory, branch checkout 관계를 교차 검증한다. orphan directory, missing registration, duplicate branch, stale lock, source checkout 오인식, 설치된 skill link 상태를 보고한다. 기본은 read-only이며 자동 repair하지 않는다.

## Repository configuration

저장소별 설정은 `.awf/worktree.toml`에 둔다.

```toml
[worktree]
default_base = "staging"
production_branch = "main"

[prepare]
inputs = ["package-lock.json", ".nvmrc"]
command = ["npm", "ci", "--cache", "~/.cache/awf/npm"]

[verify.production]
commands = [
  ["npm", "test", "--", "focused-release-test"],
  ["npm", "run", "build"],
]

[deployment]
status_command = ["argocd", "app", "wait", "my-service", "--health", "--sync"]
```

명령은 shell string이 아니라 argv 배열로 저장한다. AWF는 placeholder interpolation이나 `shell=True`를 사용하지 않는다. `~` 경로 확장은 path 필드에만 허용한다.

prepare cache key는 다음 값의 SHA-256이다.

- prepare inputs의 내용
- runtime version
- package manager version
- prepare command argv

같은 cache key면 download cache를 재사용할 수 있지만 worktree-local dependency directory는 새로 준비한다.

production promotion에 verify command가 없으면 CLI는 target PR 생성 전에 `BLOCKED`를 반환한다. promotion cleanup에 deployment status command가 없거나 결과를 확인할 수 없으면 worktree를 보존한다.

## JSON 출력

모든 `--json` 결과는 다음 envelope를 사용한다.

```json
{
  "schema_version": 1,
  "command": "wt.promote",
  "status": "ok",
  "decision": "ready",
  "lease": {},
  "actions": [],
  "blockers": [],
  "warnings": [],
  "observed_at": "2026-07-30T00:00:00Z"
}
```

- `status`: `ok | blocked | error`
- `decision`: `reuse | preview | ready | removed | no_op | blocked`
- `actions`: 실행했거나 preview한 구조화된 action
- `blockers`: mutation을 막은 안전 조건
- `warnings`: 성공을 무효화하지 않지만 사용자가 알아야 할 상태

stdout에는 JSON 한 개만 출력한다. progress와 진단은 stderr로 보낸다. path, branch, PR, SHA는 구조화 필드로 제공해 LLM이 prose를 파싱하지 않게 한다.

Exit code:

- `0`: 성공, preview, reuse, no-op
- `2`: CLI usage 또는 config schema 오류
- `3`: 안전 조건에 의해 blocked
- `4`: GitHub, Git remote, deployment checker 등 외부 의존성 실패
- `5`: registry와 실제 Git 상태 충돌

## 스킬 계약

`SKILL.md`의 description은 다음 요청을 명시적으로 포함한다.

- deploy, production release, 운영 배포
- staging에서 main/master로 promotion
- release PR 생성 또는 merge
- 배포 worktree 생성, 재사용, 정리
- merged branch/worktree cleanup

스킬이 로드되면 agent는 다음 순서를 따른다.

1. 변경 대상 repository와 initiative를 식별한다.
2. `awf wt status --repo-root <repo-root> --refresh --json`으로 기존 lease와 blocker를 확인한다.
3. 구현 worktree가 필요하면 `acquire`; production 승격이면 `promote`를 사용한다.
4. JSON의 `status`, `decision`, `blockers`를 검사한다. blocked/error를 우회하지 않는다.
5. 기존 저장소 CI와 배포 절차를 실행하고 실제 rollout을 검증한다.
6. 검증 후 `finish`를 호출한다.
7. `finish`가 보존한 worktree를 직접 `git worktree remove`, `rm -rf`, branch force-delete로 제거하지 않는다.

금지 사항:

- 관리 대상 배포에서 직접 `git worktree add/remove/prune` 실행
- staging 전체를 production branch에 merge
- `git branch --merged`만으로 squash-merged branch 삭제 판정
- dirty 변경 stash/commit/reset
- PR 또는 deployment 상태를 추측해 cleanup 진행
- user-owned 또는 imported-unmanaged worktree 삭제

CLI가 설치되지 않았거나 repository config가 부족하면 스킬은 정확한 준비 명령과 blocker를 보고한다. 수동 Git 명령으로 fallback하지 않는다.

## 오류 처리와 복구

- GitHub 인증 실패: 외부 의존성 오류로 중단하고 local state를 성공으로 전이하지 않는다.
- worktree 생성 중 실패: 생성된 path와 Git registration을 검사해 안전하게 rollback 가능한 부분만 되돌린다. dirty content가 생겼으면 보존하고 `ORPHANED` 또는 `BLOCKED`로 기록한다.
- promotion conflict: worktree와 conflict 정보를 보존하고 자동 해결하지 않는다.
- verify 실패: target PR을 생성하지 않고 lease를 `BLOCKED`로 유지한다.
- target PR 생성 후 registry 기록 실패: 다음 `doctor`가 branch/PR provenance로 복구 후보를 제시하지만 자동 채택하지 않는다.
- cleanup 중 branch 삭제 실패: worktree 제거 결과와 branch 결과를 각각 event로 기록하며, 이미 제거한 worktree를 다시 만든다고 가장하지 않는다.
- process interruption: repository lock은 process 종료 시 해제되고, 다음 mutation 전에 실제 Git 상태를 재검증한다.

mutation은 가능한 단계별 idempotent 동작으로 구현한다. 동일 명령 재실행은 기존 exact lease/PR을 재사용하고 중복 worktree나 PR을 만들지 않는다.

## 테스트 전략

### CLI TDD

각 observable contract를 failing test로 먼저 고정한다.

- exact lease 재사용과 중복 생성 방지
- repository별 mutation 직렬화
- base ref 고정과 generated branch naming
- source PR commit만 promotion하는 local Git fixture
- staging의 unrelated commit이 target diff에 포함되지 않음
- conflict 시 보존과 `BLOCKED` 전이
- verify command 실패 시 PR 생성 차단
- squash merge에서 GitHub PR 상태를 사용한 cleanup 판정
- dirty, untracked, HEAD mismatch, closed-unmerged, retain, user-owned 차단
- production deployment unknown/failed 상태에서 cleanup 차단
- import가 항상 `managed=false`이고 adopt가 명시적으로 필요함
- JSON envelope, stdout/stderr 분리, exit code
- interrupted operation 이후 doctor 진단과 idempotent retry

Git/GitHub 연동 테스트는 임시 local repositories와 deterministic fake GitHub adapter를 사용한다. 삭제 테스트는 임시 directory 밖을 가리킬 수 없게 guard한다.

### 스킬 TDD

스킬 작성 전에 다음 pressure scenario를 스킬 없이 agent에 제시해 baseline 실패를 기록한다.

1. 이미 동일 기능 worktree가 있는데 배포를 요청한다.
2. squash-merged PR의 branch와 dirty worktree가 섞여 있다.
3. staging에 다른 팀의 commit이 추가된 상태에서 production 승격을 요청한다.
4. source PR이 closed-unmerged인데 정리를 요청한다.
5. promotion PR은 merge됐지만 rollout 상태를 확인할 수 없다.

스킬 적용 후 agent가 `awf wt` JSON을 먼저 조회하고, existing lease를 재사용하며, unrelated staging change와 unsafe cleanup을 차단하는지 검증한다. 단순 문구 회상이 아니라 실제 command sequence와 stop condition을 평가한다.

### Installer와 smoke test

- 임시 HOME에서 `setup.sh` 또는 분리된 installer helper가 두 skill path를 같은 원본에 연결한다.
- 기존 실제 directory 충돌을 덮어쓰지 않는다.
- OMP skill discovery에서 `release-worktree-lifecycle` metadata와 `skill://release-worktree-lifecycle` 본문을 읽을 수 있다.
- 임시 Git repository에서 `acquire -> promote preview -> blocked verify -> configured verify -> finish blocked until deployment healthy -> remove` 흐름을 실제 실행한다.

## Rollout

1. SQLite registry, read-only `status`, `doctor`, JSON envelope를 먼저 배포한다.
2. `acquire`와 local Git integration을 활성화한다.
3. `import --dry-run`으로 기존 worktree inventory를 수집한다.
4. `promote`를 preview-only로 운영해 기존 수동 production diff와 비교한다.
5. 검증된 저장소부터 `.awf/worktree.toml`을 추가하고 `--apply`를 허용한다.
6. `finish`와 `gc --apply`를 활성화한다.
7. 마지막으로 스킬을 Claude와 `~/.agents/skills`에 배포해 LLM 배포 요청의 기본 entrypoint로 전환한다.

초기 import 결과는 모두 unmanaged이므로 rollout만으로 기존 worktree가 삭제되지 않는다.

## 완료 기준

- `awf wt status/acquire/promote/finish/gc/import/adopt/doctor`가 text와 versioned JSON 출력을 제공한다.
- 동일 initiative/repository/purpose 요청이 하나의 active worktree를 재사용한다.
- promotion 결과에 source PR 밖의 staging 변경이 포함되지 않는다.
- dirty, closed-unmerged, HEAD mismatch, user-owned, deployment-unknown worktree가 실제로 보존된다.
- production rollout이 healthy일 때만 managed promotion worktree가 cleanable/removable 상태가 된다.
- 기존 51개 worktree를 `import --dry-run`으로 mutation 없이 inventory할 수 있다.
- 스킬이 Claude와 `~/.agents/skills`에 동일 원본으로 설치된다.
- OMP와 Agent Skills 호환 agent가 배포 요청에서 직접 Git worktree 명령 대신 `awf wt`를 사용한다.
- CLI integration tests, installer tests, skill pressure tests, 임시 repository smoke test가 모두 통과한다.
