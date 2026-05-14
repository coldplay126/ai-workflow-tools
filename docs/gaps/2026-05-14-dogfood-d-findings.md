# 2026-05-14 Multi-repo dogfood [D] — findings

> **시나리오**: ai-workflow-tools(cycle root) + cmux-agent(sibling="cmux") 교차 등록
> **목적**: PR #117/#118/#119 (multi-repo scope-check + expand-scope + manifest 검증) end-to-end 검증
> **결과**: ✅ **PASS — all 3 commands behave per spec.** 단, 1건의 운영 가시성 gap 발견 (G-OPS-001).

---

## 1. 셋업

```jsonc
// .workflow/manifest.json
{
  "sibling_repos": [
    {"name": "cmux", "path": "../cmux-agent", "branch": "HEAD~5"}
  ]
}
```

```jsonc
// .workflow/artifacts/allowed-files.json
{
  "planned_files": [
    "cli/src/awf/core/state.py",                  // root
    "cli/src/awf/core/wf_scope.py",
    "cli/src/awf/commands/wf.py",
    "cli/tests/test_wf_scope.py",
    "docs/specs/multi-repo-scope.md",
    "@cmux/cmux_agent/application/broker.py",     // sibling
    "@cmux/cmux_agent/domain/models.py",
    "@cmux/tests/test_broker.py",
    "@typo-sibling/should/violate.py"             // intentional typo
  ]
}
```

base는 양쪽 모두 `HEAD~5` (= 실제 멀티-PR cycle 결과 17 + 16 = 33 diff files).

---

## 2. 검증 결과

### 2.1 `awf wf scope-check` (PR #117)

| 항목 | 기대 | 실제 | 결과 |
|---|---|---|---|
| header | `multi-repo: 1 root + 1 sibling` | 동일 | ✅ |
| per_repo JSON | 2 entries (root + cmux) | 2 entries | ✅ |
| changed_files 합산 | 17 + 16 = 33 | 33 | ✅ |
| `@cmux/` prefix routing | cmux git diff와 비교 | 매칭 OK (planned 3 hit) | ✅ |
| typo violation | `@typo-sibling/` → unknown sibling | "unknown sibling in planned_files: typo-sibling" | ✅ |
| 종료 코드 | 1 (violations 있음) | 1 | ✅ |

### 2.2 `awf wf expand-scope` (PR #119)

| 항목 | 기대 | 실제 | 결과 |
|---|---|---|---|
| stderr warning | sibling no_docs_root 안내 | "sibling 'cmux' has no docs_root — run `awf analyze`" | ✅ |
| per_repo JSON | 2 entries with docs_root_source | root="convention", cmux="none" | ✅ |
| root docs_root | `../analysis-docs` 자동 발견 | `/Users/steven/Documents/GitHub/analysis-docs` | ✅ |
| status code | exit 0 (no_docs_root은 경고만) | 0 | ✅ |

### 2.3 `awf ready --gate workflow-run` (PR #118)

| 시나리오 | 기대 | 실제 | 결과 |
|---|---|---|---|
| 정상 manifest | decision=allow, exit 0 | allow, 0 | ✅ |
| `name: "bad/name"` malformed | decision=block, exit 20 | block, 20 | ✅ |
| 에러 메시지 spec 링크 | `multi-repo-scope.md §3.1` | 포함 | ✅ |

### 2.4 `awf wiki compile` (PR #118 wiki 섹션)

```
## Multi-repo coverage
- Multi-repo runs: 3 of 6 (50.0%)
- Total repo-level errors: 0

| sibling | runs | total violations |
|---|---:|---:|
| `cmux` | 3 | 39 |
```

✅ Multi-repo coverage 섹션 정상 렌더, sibling 별 violation 합계 노출.

---

## 3. 발견된 gap

### G-OPS-001 — 글로벌 `awf` 설치본 stale 감지 부재 (NEW)

**증상**: `uv tool install` 으로 설치한 `/Users/steven/.local/bin/awf`는 source main이 머지된 PR #117/#118/#119 변경을 자동으로 흡수하지 않는다. 재설치 전까지 sibling_repos를 manifest에 선언해도 multi-repo 동작이 **silently 무효화**된다.

**재현**:
```bash
cd ai-workflow-tools && git pull   # main에 PR #117 머지본 도착
awf wf scope-check                  # 출력: `=== Scope Check (base: ...) ===` (single-repo header)
# manifest의 sibling_repos가 있어도 cycle root만 검사함
uv tool install --reinstall ./cli   # 재설치 후
awf wf scope-check                  # 출력: `=== Scope Check (multi-repo: 1 root + 1 sibling) ===`
```

**영향도**: 운영자가 다음 cycle에서 sibling_repos를 manifest에 추가했는데 `awf` 미재설치 상태면, scope-check가 sibling 변경을 **놓침에도 PASS를 반환**. G5 gate가 잘못 통과될 수 있음. 단, 이번 sandbox에서는 violation count가 25→0이 아니라 12→25로 *늘었기* 때문에 오탐 케이스는 다르게 surface됨 — 그러나 **누락 violation**(sibling에 실제 변경이 있는데 manifest 미반영) 시나리오는 위험.

**제안 대응**:
1. `awf doctor` 출력에 `installed_version vs source HEAD` 비교 추가 (cli/pyproject.toml의 version 또는 src/awf/__init__.py:__version__ vs `awf --version` 비교)
2. `awf wf scope-check` 시작 시 manifest에 sibling_repos는 있는데 코드 측에 `load_sibling_repos` 심볼이 없으면 stderr 경고 (현 코드 path에 도달하지 않아 inherent하게 어려움 → option 1이 더 현실적)
3. README/wiki에 "awf 재설치 권장 시점" 명시 — main 머지 후, `uv tool install --reinstall ./cli`

**우선순위**: P1 (다음 cycle에서 발생 가능, FAIL silently). 작업량 S (~30분).

---

## 4. 종합 평가

PR #117/#118/#119의 multi-repo 기능은 **spec 명세대로 정확히 동작**. dogfood에서 G-OPS-001 외에 새로운 spec/구현 결함은 발견되지 않음.

- `@<name>/` prefix routing ✓
- per_repo 구조 (scope-check, expand-scope 양쪽) ✓
- 에러 등급 분리 (config error vs no_docs_root) ✓
- gate workflow-run의 사전 manifest 검증 ✓
- wiki Multi-repo coverage 섹션 ✓

다음 multi-repo cycle에서 사용해도 무방하며, 운영자가 신경 쓸 점은 G-OPS-001 하나.

---

## 5. 후속 권장 작업

1. **G-OPS-001 해소** — `awf doctor` 또는 `awf --version`에 source 버전 비교 추가
2. **README 업데이트** — multi-repo 활성화 체크리스트 ("manifest에 sibling_repos 추가 + `uv tool install --reinstall ./cli`")
3. **다음 cycle** — §4.2 session 재사용 impl (handover §6A) 또는 G-OPS-001 fix

---

**작성**: 2026-05-14, Claude Opus 4.7 (1M context)
**참조**: `docs/gaps/2026-05-14-handover-next-session.md` §6(B) dogfood 진입
