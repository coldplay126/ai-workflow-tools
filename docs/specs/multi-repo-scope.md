# Multi-Repo Scope Check Spec

> **상태**: Draft (2026-05-13)
> **트리거**: `docs/gaps/2026-05-13-handover-next-session.md` §6.A
> **관련 원칙**: Constitution C5 (변경 범위 통제), C7 (재현 가능한 검증)
> **선행 PR**: #107~#115 (single-repo scope-check 완성)

---

## 1. 배경과 문제

BLIP Gem cycle처럼 한 cycle이 **여러 sibling repo를 동시에 수정**하는 패턴이 빈번하다 (예: `blip-market-api` + `blip-market-manager` + `blip-docs`). 현재 `awf wf scope-check`는 cwd 하나의 `git diff`만 검사하므로:

- sibling repo의 변경은 G5 gate가 보지 못함 → 다른 repo에서 scope 이탈 발생해도 PASS
- 운영자가 각 repo에서 수동으로 `awf wf scope-check`를 돌려야 함 → 누락 위험
- `.workflow/artifacts/allowed-files.json`이 어느 repo의 파일을 가리키는지 모호 (지금은 cwd 기준)

**목표**: 단일 cycle 루트에서 `awf wf scope-check`를 한 번 실행하면 **선언된 모든 sibling repo의 git diff를 합산**하여 검사.

---

## 2. 비-목표

- 자동 sibling 탐지 (예: 동일 부모 디렉터리 스캔). 명시적 선언만 신뢰한다.
- 원격 repo (다른 머신) 지원. 로컬 파일시스템 sibling만.
- sibling repo의 branch 자동 동기화. 운영자 책임.
- cross-repo 의존성 그래프 (allowed-files 자동 expansion). 차후 별도 spec.

---

## 3. 데이터 모델

### 3.1 manifest.json 스키마 확장

`cli/src/awf/core/state.py:DEFAULT_MANIFEST`에 다음 필드 추가:

```jsonc
{
  // ...existing fields...
  "sibling_repos": [
    {
      "name": "blip-market-api",       // allowed-files 경로 prefix에 사용
      "path": "../blip-market-api",    // cycle 루트(.workflow 있는 곳) 기준 상대경로
      "branch": "feat/blip-gem-phase1" // git diff base에서 사용
    }
  ]
}
```

**제약**:
- `name`은 고유. 빈 문자열 금지. 영문/숫자/하이픈/언더스코어만.
- `path`는 cycle 루트 기준 상대. `..`로만 시작해야 함 (sibling 보장).
- `branch`는 선택. 미지정 시 sibling repo의 현재 HEAD가 속한 default branch(main/master/staging) fallback.
- `sibling_repos` 자체가 없거나 빈 배열이면 → **현재 single-repo 동작과 동일** (백워드 호환).

### 3.2 allowed-files.json 경로 규약

기존 `planned_files`/`expanded_files`의 경로는 **암묵적으로 cycle 루트(=cwd) 기준**이었다. multi-repo에서는 모호하므로 다음 규약을 따른다:

```jsonc
{
  "planned_files": [
    "src/main.ts",                          // cycle 루트 repo의 파일 (기존)
    "@blip-market-api/src/handlers/foo.ts", // sibling 'blip-market-api' 의 파일
    "@blip-market-manager/lib/bar.ts"
  ],
  "expanded_files": [
    "@blip-market-api/src/handlers/foo.test.ts"
  ]
}
```

**규약**:
- `@<sibling-name>/` prefix가 있으면 해당 sibling repo의 파일.
- prefix가 없으면 cycle 루트 repo의 파일 (기존 동작).
- `@`로 시작하지만 `<sibling-name>`이 manifest에 없으면 → **scope-check가 violation으로 보고** (typo 방지).

**대안 검토**: 절대경로 사용 — 거부. 머신 의존적이고 cycle artifact가 다른 머신에서 재현 불가.

### 3.3 ScopeCheckResult 확장

`cli/src/awf/core/wf_scope.py:ScopeCheckResult`에 per-repo 분류 추가:

```python
@dataclass(frozen=True)
class ScopeCheckResult:
    # existing
    base_branch: str
    planned_set: tuple[str, ...]
    # ...
    # new
    per_repo: tuple[RepoScopeResult, ...]  # cycle root + each sibling

@dataclass(frozen=True)
class RepoScopeResult:
    name: str                    # "" for cycle root, or sibling.name
    path: str                    # relative path to repo root from cycle root
    base_branch: str
    changed_files: tuple[str, ...]
    classifications: tuple[FileClassification, ...]
    violations: tuple[FileClassification, ...]
    error: str | None            # "missing_repo" | "git_diff_failed" | "branch_unknown" | None
```

상위 `ScopeCheckResult.violations`는 모든 repo 합산. `classifications`는 prefix 부착 경로로 통합.

---

## 4. 동작 명세

### 4.1 `awf wf scope-check` 흐름

```
1. load .workflow/manifest.json
2. load .workflow/artifacts/allowed-files.json
3. partition planned_files/expanded_files by sibling prefix:
   - "" (no prefix) → cycle root
   - "@<name>/" → sibling[name]
4. for each repo (cycle root + siblings):
   a. resolve repo path (cycle_root or cycle_root / sibling.path)
   b. resolve base branch (--base-branch > sibling.branch > state.baseBranch > main/master/staging)
   c. run `git -C <repo> diff --name-only <base>...HEAD`
   d. classify changed files vs that repo's allowed set
5. aggregate: prefix sibling file paths with "@<name>/" in output
6. emit unified ScopeCheckResult
```

### 4.2 종료 코드

- `0`: 모든 repo violation 0건
- `1`: 1개 이상 violation
- `2`: 설정 오류 (manifest 파싱 실패, sibling repo 미존재 등). 별도 stderr 메시지.

### 4.3 JSON 출력 변경

`--json` 출력에 `per_repo` 배열 추가:

```jsonc
{
  "base_branch": "main",                  // cycle root's base (legacy)
  "planned_set": [...],                   // all repos, prefixed
  "expanded_set": [...],
  "changed_files": [...],
  "violations": [...],
  "classifications": [...],
  "per_repo": [
    {
      "name": "",
      "path": ".",
      "base_branch": "main",
      "changed_files": ["src/main.ts"],
      "violations": [],
      "error": null
    },
    {
      "name": "blip-market-api",
      "path": "../blip-market-api",
      "base_branch": "feat/blip-gem-phase1",
      "changed_files": ["src/handlers/foo.ts"],
      "violations": [],
      "error": null
    }
  ]
}
```

기존 필드는 **유지** — 단일 repo 호출자의 호환을 위해.

### 4.4 사람용 출력

```
=== Scope Check (multi-repo: 1 root + 2 siblings) ===
[root @ main]
  planned: 3, expanded: 1, changed: 2
  ✓ planned    src/main.ts  (in planned_files)
  + expanded   src/main.test.ts  (test_of:src/main.ts)

[blip-market-api @ feat/blip-gem-phase1]
  planned: 5, expanded: 2, changed: 4
  ✓ planned    @blip-market-api/src/handlers/foo.ts
  ✗ violation  @blip-market-api/src/util/leak.ts  (not in planned_files or expanded_files)

[blip-market-manager @ feat/blip-gem-phase1]
  ERROR: missing_repo — path ../blip-market-manager does not exist

verdict: 4 planned, 1 expanded, 1 violation(s), 1 repo error(s)
```

---

## 5. 엣지케이스 / 실패 모드

| 케이스 | 처리 |
|---|---|
| `sibling_repos` 비어있거나 누락 | 기존 single-repo 동작. 회귀 없음 |
| sibling.path가 존재하지 않음 | `RepoScopeResult.error = "missing_repo"`, exit code 2. **violation으로 count 안 함** (운영 실수와 scope violation 구분) |
| sibling.path가 git repo가 아님 | `error = "not_git_repo"`, exit code 2 |
| sibling.branch 미지정 + default branch 추정 실패 | `error = "branch_unknown"`, exit code 2 |
| `@unknown/` prefix가 allowed-files에 있음 | violation으로 classify, `reason = "unknown sibling: unknown"`. exit 1 |
| sibling repo의 HEAD가 base branch와 동일 (변경 없음) | changed_files = []. violation 0. 정상 |
| `.workflow/`로 시작하는 경로 (sibling repo에서) | 현재 single-repo 동작과 동일하게 제외 |
| sibling이 sub-repo (cycle 루트의 하위) | path가 `..`로 시작하지 않으므로 manifest 검증에서 거부 |

---

## 6. 마이그레이션

기존 cycle은 `sibling_repos`가 없어도 동작해야 한다 (백워드 호환). 신규 cycle은 `awf wf init`이 빈 배열을 manifest에 넣지 않는다 — 필요한 경우 운영자가 수동으로 추가.

**알려진 비호환**: 기존 allowed-files.json에 우연히 `@`로 시작하는 경로가 있으면 violation으로 classify된다. 검색 결과 현재 BLIP 관련 cycle에는 해당 없음. 신규 규약 도입 시 release note에 명시.

---

## 7. 수락 기준 (G6 tests)

### 7.1 단위 테스트 (`cli/tests/test_wf_scope.py`)

1. `test_scope_check_no_siblings_backward_compat` — sibling_repos 없으면 기존 출력과 동일
2. `test_scope_check_single_sibling_planned` — 1 sibling, planned 파일만 변경 → PASS
3. `test_scope_check_sibling_violation` — sibling에서 scope 외 파일 변경 → violation 1, exit 1
4. `test_scope_check_unknown_prefix_in_allowed_files` — `@unknown/` prefix → violation
5. `test_scope_check_missing_sibling_path` — path 존재 안 함 → exit 2, error 명시
6. `test_scope_check_sibling_branch_fallback` — branch 미지정 → default branch 사용
7. `test_scope_check_sibling_not_git_repo` → exit 2, error="not_git_repo"
8. `test_scope_check_aggregates_violations_across_repos` — 2 repo 각 1 violation → 합산 2

### 7.2 통합 시나리오

- BLIP cycle 시나리오 재현: cycle 루트(`ai-workflow-tools`) + 2 sibling. allowed-files에 prefix 사용. `awf wf scope-check`가 모든 repo를 한 번에 검사.

### 7.3 운영 검증

- `--json` 출력에 `per_repo` 배열 존재 + 기존 필드 유지
- README + cli/README 갱신
- handover 문서 §1.3에서 §1.5 항목 제거 (또는 "완료" marking)

---

## 8. 향후 작업 (이 spec의 범위 밖)

- **cross-repo dependency graph** — sibling 간 import 그래프로 expanded_files 자동 expansion. `awf wf expand-scope`의 multi-repo 확장.
- **sibling repo의 cycle 상태 동기화** — sibling에 별도 `.workflow/`가 있으면 어떻게 reconcile할지.
- **원격 repo 지원** — 다른 머신/CI 환경에서 cycle 재현.
- **자동 sibling 등록** — `package.json`의 workspaces나 `pnpm-workspace.yaml`을 읽어 manifest 자동 생성.

---

## 9. 참조

- 현재 구현: `cli/src/awf/core/wf_scope.py:check_scope_violations`
- manifest 기본값: `cli/src/awf/core/state.py:DEFAULT_MANIFEST`
- 기존 단위 테스트: `cli/tests/test_wf_scope.py`
- handover 문서: `docs/gaps/2026-05-13-handover-next-session.md` §6.A
- 운영 이슈 로그: `docs/gaps/2026-05-13-blip-gem-cycle-operational-issues.md` §1.5
