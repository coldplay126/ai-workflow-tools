# Cross-Repo Expand-Scope Spec

> **상태**: Draft (2026-05-14)
> **트리거**: `docs/specs/multi-repo-scope.md` §8 (향후 작업)
> **관련 원칙**: Constitution C5 (변경 범위 통제), C6 (감사 가능한 결정)
> **선행 PR**: #117 (multi-repo scope-check), #118 (manifest 검증 + per-repo metrics)

---

## 1. 배경과 문제

`awf wf scope-check`는 PR #117으로 sibling repo의 `git diff`까지 합산하지만, `awf wf expand-scope`는 여전히 cycle 루트 repo 하나의 `analysis-docs`에서만 import graph를 로드한다. 그 결과:

- sibling repo에서 planned된 파일의 dependent/import 확장이 이루어지지 않음 → G5 false positive 재발 (sibling repo의 ripple 효과)
- 운영자가 `@<name>/...` prefix를 expanded_files에 수동으로 추가해야 함 → 누락 위험

**목표**: cycle 루트에서 `awf wf expand-scope`를 한 번 실행하면 **각 sibling repo의 import graph도 로드**해서 expansion을 합산.

---

## 2. 비-목표

- cross-repo import 분석 (sibling A의 파일이 sibling B의 파일에 의존하는 그래프). sibling 각각 내부의 그래프만 활용.
- 자동 sibling analysis-docs 탐지 (예: 디렉터리 스캔). 명시 선언 + 명시적 convention만 신뢰.
- 원격 sibling 지원. 로컬 파일시스템만.
- 새 analysis-docs 위치 표준 강제. 기존 cycle은 그대로 작동해야 함.

---

## 3. 데이터 모델

### 3.1 manifest.json 스키마 확장 (선택적)

PR #117에서 정의한 `sibling_repos[]`에 **선택 필드** `analysis_docs` 추가:

```jsonc
{
  "sibling_repos": [
    {
      "name": "api",
      "path": "../sibling-api",
      "branch": "feature",
      "analysis_docs": "../analysis-docs/api"  // 선택; 미지정 시 fallback 적용
    }
  ]
}
```

- `analysis_docs`는 cycle 루트(`.workflow` 있는 곳) 기준 상대경로. 절대경로 금지 (재현성).
- 기존 `name/path/branch` 필드는 변경 없음 → PR #117과 호환.

### 3.2 Sibling analysis-docs 해석 순서

각 sibling repo의 `docs_root`는 다음 순서로 결정:

1. **manifest `sibling.analysis_docs`** (있으면) — `repo_root / sibling.analysis_docs`로 해석
2. **sibling repo의 `.awf.toml`** — `resolve_runtime_paths(sibling_path)["analysis_docs"]`로 해석 (sibling repo가 자체 설정을 가진 경우)
3. **convention fallback** — `(sibling_path).parent / "analysis-docs"` (현재 cycle 루트 convention과 동일)
4. **위 어느 것도 존재하지 않으면** — sibling의 expansion은 skip, coverage="no_docs_root"로 표시

**근거**: (1)은 manifest로 명시 → 가장 신뢰. (2)는 sibling repo가 분석 워크플로우를 직접 운영하는 경우. (3)은 mono-parent에 analysis-docs를 공유하는 default. (4)는 분석되지 않은 sibling을 silent하게 무시하지 않기 위한 안전망.

### 3.3 prefix 규약 (PR #117 유지)

planned/expanded files의 `@<name>/<path>` prefix 규약은 PR #117과 동일하게 사용. expand-scope는:

- planned_files를 `@<name>/` prefix로 partition
- 각 repo의 docs_root에서 expand_allowed_files 실행
- expanded_files도 prefix를 부여하여 저장

---

## 4. 동작 명세

### 4.1 `awf wf expand-scope` 흐름

```
1. load .workflow/manifest.json → sibling_repos (PR #117 helper 재사용)
2. load .workflow/artifacts/allowed-files.json
3. partition planned_files by sibling prefix:
   - ""        → cycle root
   - "@<name>/" → sibling[name]
4. resolve each repo's docs_root (§3.2 순서)
5. for each repo with planned files:
   a. run `expand_allowed_files(planned, docs_root, direction, depth)`
   b. collect ExpansionResult
6. aggregate:
   - prefix each entry's path with @<name>/ when sibling
   - merge coverage maps with prefixed keys
7. write back to allowed-files.json (apply_expansion_to_payload)
   - expanded_files entries use prefixed paths
   - graph_expansion audit trail per-repo broken down
8. emit per-repo summary in CLI output
```

### 4.2 종료 코드

- `0`: 정상 (added 0건 포함)
- `2`: 설정 오류 (manifest 파싱 실패, allowed-files.json 누락, sibling repo 미존재 등)

repo가 일부만 분석 가능한 경우(no_docs_root)는 exit 0 + 경고 메시지.

### 4.3 JSON 출력 변경

```jsonc
{
  "planned": ["src/main.ts", "@api/src/handler.ts"],
  "added": ["@api/src/handler.spec.ts"],
  "entries": [
    {"path": "@api/src/handler.spec.ts", "reason": "dependent_of:@api/src/handler.ts"}
  ],
  "coverage": {
    "src/main.ts": "found_in:.../root/analysis-docs/svc/main",
    "@api/src/handler.ts": "found_in:.../analysis-docs/api/svc/handler"
  },
  "direction": "dependents",
  "depth": 1,
  "per_repo": [
    {"name": "", "docs_root": ".../analysis-docs", "planned_in_repo": 1, "added_in_repo": 0, "status": "ok"},
    {"name": "api", "docs_root": "../analysis-docs/api", "planned_in_repo": 1, "added_in_repo": 1, "status": "ok"},
    {"name": "manager", "docs_root": null, "planned_in_repo": 1, "added_in_repo": 0, "status": "no_docs_root"}
  ]
}
```

기존 top-level 필드(`planned`/`added`/`entries`/`coverage`)는 prefix 포함된 합산 결과 → 기존 호출자도 호환.

### 4.4 사람용 출력

```
planned: 3 file(s)
added:   1 file(s) (direction=dependents, depth=1)
  + @api/src/handler.spec.ts  (dependent_of:@api/src/handler.ts)

per-repo:
  [root]            planned=1  added=0  docs_root=.../analysis-docs  status=ok
  [api]             planned=1  added=1  docs_root=../analysis-docs/api  status=ok
  [manager]         planned=1  added=0  docs_root=(none)  status=no_docs_root

note: 1 repo(s) have no docs_root — run `awf analyze` for those repos to improve coverage
  · manager
```

### 4.5 graph_expansion audit trail

`apply_expansion_to_payload`가 생성하는 `graph_expansion` 객체에 per-repo 분기 추가:

```jsonc
"graph_expansion": {
  "direction": "dependents",
  "depth": 1,
  "added_count": 1,
  "entries": [
    {"path": "@api/src/handler.spec.ts", "reason": "dependent_of:@api/src/handler.ts"}
  ],
  "coverage": { ... },
  "per_repo": [
    {"name": "", "added": 0, "status": "ok"},
    {"name": "api", "added": 1, "status": "ok"},
    {"name": "manager", "added": 0, "status": "no_docs_root"}
  ]
}
```

기존 `direction/depth/added_count/entries/coverage` 필드 유지 → scope-check audit trail은 호환.

---

## 5. API 설계

### 5.1 새 함수 — `expand_allowed_files_multi_repo`

```python
def expand_allowed_files_multi_repo(
    repo_root: Path,
    planned_files: list[str],
    *,
    direction: str = "dependents",
    depth: int | None = DEFAULT_DEPTH,
    runtime_only: bool = False,
) -> MultiRepoExpansionResult:
    """
    Partition planned_files by sibling prefix, resolve each repo's docs_root
    per §3.2, and aggregate per-repo ExpansionResult into a single object.

    Returns:
        MultiRepoExpansionResult — has the same shape as ExpansionResult
        (planned/added/entries/coverage/direction/depth) plus a per_repo
        breakdown.
    """
```

기존 `expand_allowed_files`는 단일 repo 함수로 유지 — 하위호환 + 단위 테스트 격리.

### 5.2 새 dataclass — `RepoExpansionResult`

```python
@dataclass(frozen=True)
class RepoExpansionResult:
    name: str
    docs_root: str | None         # absolute resolved path, or None when missing
    status: str                   # "ok" | "no_docs_root" | "no_repo"
    planned_in_repo: int
    added_in_repo: int
    inner: ExpansionResult | None # None when status != "ok"

@dataclass(frozen=True)
class MultiRepoExpansionResult:
    planned: tuple[str, ...]      # all prefixed planned paths
    added: tuple[str, ...]        # all prefixed added paths
    entries: tuple[ExpansionEntry, ...]  # prefixed
    coverage: dict[str, str]
    direction: str
    depth: int | None
    per_repo: tuple[RepoExpansionResult, ...]
```

### 5.3 docs_root 해석 헬퍼

```python
def resolve_sibling_docs_root(
    cycle_root: Path,
    sibling: SiblingRepo,
) -> tuple[Path | None, str]:
    """Return (docs_root | None, status).

    status ∈ {"ok_manifest", "ok_awf_toml", "ok_convention", "no_docs_root"}.
    None when no candidate exists or all candidates are missing.
    """
```

---

## 6. 엣지케이스 / 실패 모드

| 케이스 | 처리 |
|---|---|
| `sibling_repos` 없음 | 기존 single-repo expand-scope와 동일 (회귀 없음) |
| sibling repo path 미존재 | `RepoExpansionResult.status = "no_repo"`. 해당 sibling planned files는 expansion skip. 경고 stderr |
| sibling.analysis_docs 미존재 | `status = "no_docs_root"`. expansion skip + 경고 |
| sibling.analysis_docs 미선언 + sibling/.awf.toml 없음 + convention fallback 없음 | `status = "no_docs_root"` |
| `@unknown/` prefix가 planned_files에 | scope-check가 이미 violation으로 surface (PR #117). expand-scope는 silently skip + 경고 한 줄 (`unknown sibling: <name>`) |
| 두 repo가 같은 파일 경로(예: `src/main.ts`)를 가질 때 | prefix가 다르므로 별개 entry. 동일 prefix 내부에서만 dedup |
| sibling의 docs_root에 다른 sibling을 가리키는 import 엣지가 있음 | 비-목표(§2) — 무시. import graph는 repo 내부만 신뢰 |
| sibling planned_files만 있고 cycle 루트 planned_files 없음 | cycle 루트 ExpansionResult는 빈 결과, per_repo의 root entry는 planned_in_repo=0 |
| 사용자가 `--service`로 서비스 제약 | cycle 루트 docs_root에만 적용. sibling docs_root에는 적용하지 않음 (제약 의도가 cycle 루트 기준) |
| `awf wf expand-scope`를 cycle 루트에서 실행했는데 sibling planned 파일만 있음 | 정상. 루트 entry는 빈 결과, sibling만 expand |

---

## 7. 마이그레이션

- 기존 cycle의 manifest는 `sibling_repos` 미선언 또는 `analysis_docs` 필드 없음 → 모두 backward compat.
- 기존 allowed-files.json의 expanded_files는 prefix 없는 경로 → 그대로 cycle 루트 파일로 해석.
- PR #117 도입 후 신규 cycle은 expand-scope가 자동으로 prefix를 부여 → 시간이 지나면 자연스럽게 일관성 확보.

---

## 8. 수락 기준 (G6 tests)

### 8.1 단위 테스트

1. `test_expand_multi_repo_no_siblings_backward_compat` — sibling_repos 없으면 단일 repo와 동일 출력
2. `test_expand_multi_repo_sibling_with_manifest_docs_root` — manifest 명시 docs_root 사용
3. `test_expand_multi_repo_sibling_falls_back_to_awf_toml` — sibling repo의 .awf.toml docs_root
4. `test_expand_multi_repo_sibling_falls_back_to_convention` — `<sibling_path>.parent / "analysis-docs"`
5. `test_expand_multi_repo_sibling_no_docs_root` — 모든 후보 실패 → status="no_docs_root", expansion skip, exit 0
6. `test_expand_multi_repo_sibling_missing_path` — sibling repo 디렉터리 없음 → status="no_repo"
7. `test_expand_multi_repo_aggregates_entries_with_prefix` — entries의 path가 sibling repo는 prefix 부착
8. `test_expand_multi_repo_unknown_prefix_silently_skipped` — `@unknown/` prefix는 경고 후 skip
9. `test_resolve_sibling_docs_root_priority_order` — manifest > .awf.toml > convention 우선순위
10. `test_apply_expansion_to_payload_includes_per_repo_audit` — graph_expansion에 per_repo 배열

### 8.2 CLI 통합

- `awf wf expand-scope --json` 출력에 `per_repo` 배열 + 기존 필드 유지
- 사람용 출력에 per-repo block
- `--service`는 cycle 루트에만 적용됨을 명시

### 8.3 운영 검증

- PR #117의 multi-repo scope-check와 함께 dogfooding 가능
- 기존 expand-scope 호출자 회귀 없음 (cli/tests/test_wf_scope.py 21개 expand 테스트 통과)

---

## 9. 향후 작업 (이 spec의 범위 밖)

- **cross-repo dependency** — sibling A의 파일이 sibling B의 파일을 import하는 엣지. 현재는 repo 내부 그래프만 신뢰
- **sibling-별 `--service` 필터** — sibling repo마다 services 제약을 다르게 적용
- **자동 sibling docs_root 추정** — 디렉토리 스캔으로 `<sibling>-analysis-docs` 같은 패턴 자동 인식
- **`awf wf init`에서 sibling.analysis_docs 자동 detect** — convention path 존재 시 manifest에 자동 기록

---

## 10. 참조

- PR #117: `docs/specs/multi-repo-scope.md`, `cli/src/awf/core/wf_scope.py:check_scope_violations`
- PR #118: manifest validation in `cli/src/awf/core/ready.py:_workflow_status`
- 현재 expand 구현: `cli/src/awf/core/wf_scope.py:expand_allowed_files`
- CLI runner: `cli/src/awf/commands/wf.py:run_wf_expand_scope`
- analysis_docs 해석: `cli/src/awf/core/config.py`
