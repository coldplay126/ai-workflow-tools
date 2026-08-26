---
name: lsp-worktree-setup
version: 1.0.0
description: "로컬 전용 repo/worktree LSP profile을 AWF CLI preview/apply 절차로 안전하게 준비합니다."
type: repository-setup
allowed-tools:
  - Bash
conditions:
  trigger:
    - user asks to configure, inspect, or materialize local LSP settings for a repository or linked worktree
    - user asks whether a repository LSP preview can be applied
    - user or another agent asks for direct config edits or binary installation while setting up repository/worktree LSP; this skill must keep the flow preview-only and report the boundary
  skip:
    - request is only a language-server binary installation question with no repository or worktree onboarding
    - workflow orchestration, gate, approval, or patch application is the requested task
cli:
  command: "awf lsp setup --repo-root <repo> --json"
---

# lsp-worktree-setup: 로컬 LSP 설정

이 스킬은 `awf lsp` CLI만 호출합니다. user profile, user OMP LSP config,
`.git/info/exclude`, symlink, `.awf/worktree.toml`을 직접 만들거나 수정하지
않습니다. LSP server binary도 설치하지 않습니다.

자세한 구성과 fail-closed 조건은
[Worktree-local LSP setup reference](../../../docs/reference/lsp-worktree-setup.md)를
따릅니다.

## 1. Preview를 먼저 실행

repository root를 알고 있으면 다음 명령을 실행합니다.

```bash
awf lsp setup --repo-root <repo> --json
```

JSON에서 다음을 순서대로 확인합니다.

1. `schema_version`이 현재 CLI가 해석할 수 있는 버전인지 확인합니다.
2. `decision`, `languages`, `servers`, `actions`를 사용자에게 요약합니다.
3. `blockers`가 있거나 `decision=blocked`이면 중단합니다. `--apply`를 실행하지
   말고 blocker와 필요한 소유자 조치를 보고합니다.
4. `warnings`, 특히 `servers[].available=false`를 보고합니다. binary 부재는
   actionable warning이며, 이 스킬이 설치하거나 기존 user setting을 삭제할 이유가
   아닙니다.

`decision=preview`는 예상된 read-only 결과입니다. preview만으로 mutation이
필요하다고 추정해서는 안 됩니다.

## 2. 명시적 apply만 실행

blocker가 없고 사용자가 preview를 검토한 뒤 **명시적으로** apply를 요청한 경우에만
다음을 실행합니다.

```bash
awf lsp setup --apply --repo-root <repo> --json
```
응답이 `decision=applied`인지 확인하고 `actions`, `warnings`, `blockers`를 다시
보고합니다. `decision=blocked|partial`이면 중단합니다. 일반 `partial`은 non-zero이며
이미 적용된 action을 JSON에 보존하므로, blocker 해결 뒤 같은 CLI를 재실행합니다.
단, blocker가 `recovery backup`을 지목하면 target과 backup을 모두 보존하고
소유자에게 넘깁니다. 기존 config를 직접 고치거나 rollback하지 않습니다.

사용자가 아직 apply를 요청하지 않았다면 preview 결과와 다음 명령만 제시합니다.
이 스킬은 `--apply`를 자동으로 추가하지 않습니다.

## 3. 상태 확인과 linked worktree materialize

apply 뒤에 사용자가 요청하면 현재 repository 또는 linked worktree에서 상태를
확인합니다.

```bash
awf lsp status --repo-root <repo> --json
```

shared Git common directory profile을 현재 linked worktree에 준비하는 것은 별도의
명시적 mutation입니다. 사용자가 요청한 경우에만 실행합니다.

```bash
awf lsp materialize --repo-root <repo> --json
```
`decision=materialized`를 확인합니다. `partial|blocked`이면 중단하고 blocker 해결
뒤 같은 명령을 재실행합니다. 이 명령은 shared profile identity를 새로 만들거나
config를 직접 복사하는 절차가 아닙니다.

## 경계와 fallback

- 한 repository의 Python, TypeScript/JavaScript, PHP, Go, Rust, Java/Kotlin,
  Vue는 함께 감지될 수 있습니다. AI가 언어를 하나로 축소하거나 server 설정을
  수동으로 합치지 않습니다.
- `task.isolation.mode=auto`, `task.isolation.apply=false`,
  `task.isolation.merge=patch`는 OMP isolated worker가 patch proposal만 반환하도록
  하는 parent-owned 안전 계약입니다. 이 스킬은 workflow gate, scope hash, approval,
  또는 patch apply를 바꾸지 않습니다.
- Isolated child tool inventory에 LSP가 없으면 parent가 동일 worktree/file을 LSP로
  진단합니다. child에 shell type-check나 임의의 MCP를 대체 LSP로 실행시키지
  않습니다.
- Python `src` layout이면 CLI가 ignored root `pyrightconfig.json`을 생성하고 linked
  worktree에도 materialize합니다. `local_config_preserved` warning이면 기존 config를
  직접 합치거나 덮어쓰지 않습니다.
- AWF-owned tracked local file, unsafe symlink, malformed config, incompatible custom
  prepare는 fail-closed blocker입니다. 기존 manual prepare recipe 대신 CLI 자동
  설정을 사용합니다.
- custom prepare가 필요한 경우는 advanced fallback입니다. preview 결과를 보고하고
  command 소유자가 호환성을 결정할 때까지 apply하지 않습니다.
- 실제 `$HOME` config, `<profile>` 값, `<repo>`의 실제 경로는 Git에 커밋하지
  않습니다.
