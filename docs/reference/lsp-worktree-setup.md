# Worktree-local LSP setup reference

`awf lsp`는 하나의 Git repository와 연결된 worktree가 같은 로컬 LSP profile을
안전하게 사용하도록 준비하는 CLI입니다. 이 문서는 profile이나 OMP 설정 파일을
직접 편집하는 방법이 아닙니다. 실제 로컬 설정은 Git에 커밋하지 않습니다.

## 1. CLI 계약

공개 명령은 다음 세 개입니다.

```bash
awf lsp setup --repo-root <repo> --json
awf lsp setup --apply --repo-root <repo> --json
awf lsp status --repo-root <repo> --json
awf lsp materialize --repo-root <repo> --json
```

`setup`은 기본적으로 preview입니다. preview는 파일, symlink, user profile, user
OMP LSP 설정을 변경하지 않습니다. 먼저 JSON을 읽고, blocker와 warning을 판단한
뒤에만 사용자가 명시적으로 `--apply`를 요청할 수 있습니다.

`setup --json` 응답은 다음 top-level field를 제공합니다.

| Field | 의미 |
|---|---|
| `schema_version` | 응답을 해석하는 CLI 계약 버전 |
| `command` | 실행한 LSP 명령 |
| `decision` | 명령별 상태. `setup`: `preview|applied|partial|blocked`; `status`: `configured|incomplete|not_configured|blocked`; `materialize`: `materialized|partial|blocked` |
| `languages` | 이 repository에서 감지한 언어 ID의 고정 순서 목록 |
| `servers` | `{name, language, binary, command, resolved_command, available}` server 목록. `resolved_command`는 repo-local bin 또는 `PATH`에서 실제 선택한 실행 파일 |
| `actions` | preview 또는 apply가 수행하거나 수행한 작업 |
| `blockers` | apply를 중단해야 하는 안전 조건 |
| `warnings` | 사용자가 처리할 수 있으나 설정을 지우지는 않는 조건 |

`decision=preview`는 아직 아무것도 바꾸지 않았다는 뜻입니다. `decision=blocked`이면
`--apply`를 실행하지 말고 `blockers`의 항목을 해결하거나 설정 소유자에게 넘깁니다.
`decision=applied`는 명시적 apply가 끝났다는 뜻이며, `decision=materialized`는 현재
worktree에서 shared profile을 materialize했다는 뜻입니다. `status`의
`configured`, `incomplete`, `not_configured`는 각각 완전한 설정, 일부 누락, profile
미생성을 나타냅니다.

`decision=partial`은 일부 local action이 적용된 뒤 후속 action이 실패했다는 뜻입니다.
CLI는 non-zero로 종료하며 AWF prepare success marker를 만들지 않습니다. `actions`에서
`applied|partial|failed|planned` 상태를 확인하고 blocker를 해결한 뒤 같은 명령을
재실행합니다. 직접 rollback하거나 config를 손으로 합치지 않습니다.
단, blocker가 `recovery backup`을 지목하면 아래 fail-closed 절차가 우선합니다.

server binary가 없으면 `servers[].available`과 `warnings`가 그 사실을 알려 줍니다.
해당 warning은 `code`, `server`, `binary`, `message`, `suggestion`으로 필요한 조치를
표시합니다. 이는 설치 가능한 도구를 알리는 warning이지 기존 user setting을 지우거나
대체하라는 신호가 아닙니다. AWF는 binary를 설치하지 않습니다.

## 2. 권장 순서

다음은 한 repository의 안전한 순서입니다.

```bash
awf lsp setup --repo-root <repo> --json
# JSON의 blockers와 warnings를 검토한 뒤, 사용자가 명시적으로 승인한 경우에만 실행
awf lsp setup --apply --repo-root <repo> --json
awf lsp status --repo-root <repo> --json
awf lsp materialize --repo-root <repo> --json
```

AI는 preview만으로 apply를 추정하거나, 경고를 숨기거나, 이 순서를 대신 결정해서는
안 됩니다. `--apply`와 `materialize`는 각각 사용자의 명시적 요청 뒤에 CLI로만
실행합니다. `status`는 현재 profile, 감지된 언어, server 가용성, pending 상태를
확인할 때 사용합니다.

## 3. 언어 감지와 mixed repository

한 repository에 다음 언어가 함께 있어도 `setup`은 하나의 profile을 구성할 수
있습니다.

| 지원 언어 | `languages`의 대상 |
|---|---|
| Python | Python 프로젝트 |
| TypeScript / JavaScript | TypeScript 또는 JavaScript 프로젝트 |
| PHP | PHP 프로젝트 |
| Go | Go 프로젝트 |
| Rust | Rust 프로젝트 |
| Java / Kotlin | Java 또는 Kotlin 프로젝트 |
| Vue | Vue single-file component 프로젝트 |

언어 목록을 하나로 고르거나, AI가 server 설정을 수동으로 합치지 않습니다. preview의
`languages`와 `servers`가 실제 선택을 보여 줍니다. 필요한 binary가 일부 없더라도
다른 언어의 기존 설정은 유지하고, 필요한 binary만 warning으로 보고합니다.

## 4. local-only profile과 worktree 공유

apply는 user profile과 user OMP LSP 설정을 merge합니다. 기존 user-owned setting을
삭제하지 않습니다. CLI가 관리하는 profile-linked symlink, `.awf/worktree.toml`,
그리고 생성한 language-local config는 `.git/info/exclude`로 ignore합니다. 실제
profile 값과 local config는 commit 대상이 아닙니다.

Python `src` layout이 하나 이상 감지되면 root `pyrightconfig.json`에 각 project의
`executionEnvironments`와 `extraPaths`를 생성합니다. 기존 tracked config는
repository-owned로 유지하고, 기존 untracked config의 내용이 다르면
`local_config_preserved` warning과 `actions[].status=preserved`로 보고하며 덮어쓰지
않습니다.

linked worktree는 Git common directory를 공유합니다. 따라서 CLI가 구성한
`.git/info/exclude`도 common directory에서 공유되고, main checkout과 linked
worktree는 같은 profile identity를 사용합니다. 다른 worktree에서 profile을 쓸 때는
그 worktree에서 다음을 명시적으로 실행합니다.

```bash
awf lsp materialize --repo-root <repo> --json
```

이 동작은 각 worktree에 repository-local link와 생성된 language-local config를
materialize하지만 profile identity는 Git common directory 기준으로 유지합니다.
`$HOME` 아래 user config, `<profile>` 값, 또는 `<repo>`의 실제 경로를 문서나 Git에
복사하지 마세요.

## 5. AWF managed prepare와 OMP isolation

apply는 ignored `.awf/worktree.toml`에 AWF가 관리하는 prepare 구성을 준비합니다.
이 prepare는 worktree-local LSP link를 필요한 때에 materialize하는 CLI 경로입니다.
수동 shell recipe, 직접 `ln -s`, 또는 config 파일 복사는 사용하지 않습니다.

OMP isolated Impl의 기본 안전 설정은 다음 의미를 가집니다.

| 설정 | 의미 |
|---|---|
| `task.isolation.mode=auto` | host가 지원하는 isolation mode를 선택 |
| `task.isolation.apply=false` | worker가 repository에 직접 apply하지 않음 |
| `task.isolation.merge=patch` | worker 결과는 parent가 검토할 patch로 전달 |

LSP profile 설정은 parent orchestration과 별개입니다. parent만 workflow state,
gate, scope hash, approval, patch 적용을 소유합니다. OMP isolated worker가 없거나
실행할 수 없는 경우에도 이 설정이 worker의 직접 쓰기를 허용하지 않습니다. parent는
기존 workflow fallback을 사용하고, LSP 상태는 `awf lsp status`로 확인합니다.

## 6. Fail-closed와 custom prepare fallback

다음 조건은 CLI가 기존 설정을 덮어쓰지 않고 `blockers`로 보고합니다.

- `.omp/lsp.json` 또는 `.awf/worktree.toml`처럼 AWF가 소유해야 하는 local 파일이
  이미 Git에 tracked된 경우
- profile-linked symlink의 대상이 안전하지 않거나 symlink attack이 의심되는 경우
- user 또는 local config 형식이 malformed인 경우
- 기존 custom prepare command가 AWF managed prepare와 호환되지 않는 경우
- 기존 local config의 안전한 교체에 native atomic exchange가 없는 platform인 경우
  (`atomic_exchange_unsupported`)

기존 manual prepare recipe는 `awf lsp setup --apply` 자동 설정으로 대체합니다.
custom prepare가 반드시 필요한 repository에서는 그것을 advanced fallback으로
취급합니다. 먼저 preview와 blocker를 보관하고, 해당 command의 소유자가 호환되는
구성을 결정할 때까지 apply하지 마세요. AI는 custom command를 덮어쓰거나 두 prepare
명령을 임의로 연결해서는 안 됩니다.

기존 파일 교체 직후 concurrent mutation이 감지되면 CLI는 target을 더 바꾸지 않고
이전 파일을 같은 directory의 random `.tmp` recovery backup으로 보존한 채
`partial|blocked`로 종료합니다. blocker가 지목한 backup과 target을 모두 보존하고
소유자가 내용을 판정할 때까지 삭제, rollback, 재실행하지 않습니다.

## 7. OMP 호환성과 schema version

OMP 18.0.6에서 top-level main/managed worktree LSP는 검증되었습니다. 현재 field
environment의 task child tool inventory는 `isolated=true|false` 모두 LSP device를
노출하지 않았습니다. 따라서 version만으로 child LSP를 보장하지 않습니다. child가
LSP unavailable을 반환하면 parent가 같은 worktree/file을 진단하고 결과를 child
검증에 사용합니다. child에 shell type-check나 별도 MCP를 LSP처럼 실행시키지 않습니다.

```bash
omp update --check
omp update
```

JSON 소비자는 `schema_version`을 먼저 확인해야 합니다. 같은 repository와 linked
worktree에서 사용하는 AWF CLI가 이 schema version을 지원하지 않으면 CLI를
업데이트한 뒤 preview를 다시 실행합니다. schema를 수동으로 바꾸거나 새 client의
profile을 오래된 client에 복사하지 않습니다.

현재 설치된 CLI와 설정 preview는 다음으로 확인합니다.

```bash
awf --version
awf lsp setup --repo-root <repo> --json
```
