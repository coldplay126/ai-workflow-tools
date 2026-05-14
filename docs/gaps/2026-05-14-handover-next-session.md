# 2026-05-14 multi-repo cycle 후속 — 다음 세션 핸드오버

> **상태**: §1.5 + 후속 P2 두 항목 한 세션에서 머지 (PR #117/#118/#119). 잔여 P1+는 §4.2 session 재사용 impl 하나뿐. spec은 사전 작성 완료.
> 이전 핸드오버: `docs/gaps/2026-05-13-handover-next-session.md` (§1.3 / §6 갱신됨)
> 본 문서는 다음 Claude Code 세션이 컨텍스트를 압축 복원하기 위한 인계자료.

---

## 1. 머지된 변경 (origin/main 기준)

```
5dc721c feat(awf): cross-repo expand-scope — sibling import graphs (#119)
b098c3f feat(awf): multi-repo scope-check followup — manifest validation + per-repo metrics (#118)
0b20390 feat(awf): multi-repo scope-check — sibling_repos manifest + @<name>/ prefix (#117)
```

신규 spec 두 건:
- `docs/specs/multi-repo-scope.md` — sibling_repos manifest + `@<name>/` prefix 규약 (PR #117)
- `docs/specs/cross-repo-expand-scope.md` — sibling repo의 import graph 활용 (PR #119)
- `docs/specs/session-reuse.md` — §4.2 impl 사전 설계 (impl 미진행, telemetry 누적 후 진입)

---

## 2. 테스트 상태

- `awf cli` **681/681** PASS (+31 신규: 11+8+12)
- `cmux-agent tests` 변경 없음 (이번 cycle은 cli만 변경)
- CI green on 모든 PR

---

## 3. 새 명령/기능 빠른 참조

### awf CLI

| 명령 | 변경 | PR |
|---|---|---|
| `awf wf scope-check` | sibling repo의 git diff 합산. `@<name>/` prefix 사용. repo-level error는 exit 2 | #117 |
| `awf wf expand-scope` | sibling repo의 import graph 자동 로드 (4단계 docs_root fallback). status=no_docs_root는 stderr 경고 + exit 0 | #119 |
| `awf ready --gate workflow-run` | manifest.json `sibling_repos` 사전 검증. malformed → block 결정 | #118 |
| `awf wiki compile` (scope-check 페이지) | 신규 "Multi-repo coverage" 섹션 — multi-repo run 비율 + sibling별 runs/violations | #118 |

### Manifest 스키마 확장

`.workflow/manifest.json`:

```jsonc
{
  "version": "1.0.0",
  "sibling_repos": [
    {
      "name": "api",                          // [A-Za-z0-9_-]+, 고유
      "path": "../sibling-api",               // cycle-root-relative, ".." 시작 필수
      "branch": "feature/x",                  // 선택, sibling repo git diff base
      "analysis_docs": "../docs/api"          // 선택, expand-scope용. 미지정 시 4-stage fallback
    }
  ]
}
```

### allowed-files.json 경로 규약

```jsonc
{
  "planned_files": [
    "src/main.ts",                  // cycle root repo
    "@api/src/handler.ts",          // sibling 'api'
    "@manager/lib/foo.ts"
  ]
}
```

unknown sibling prefix (`@typo/...`) → scope-check가 violation으로 surface (PR #117 §5).

---

## 4. §4.2 session 재사용 — impl 진입 전 체크리스트

`docs/specs/session-reuse.md`에 spec 사전 작성됨. impl 진입 전 다음 확인:

1. **Telemetry 누적**: `awf wf status` 의 telemetry 블록(§8.7-P1)에 1-2 cycle 데이터가 있는가?
   - 없으면: 실제 cycle 한두 번 돌린 뒤 ROI 평가
2. **Spec 재검토**: spec의 키 결정 사항이 여전히 유효한가?
   - `.workflow/session.json` 위치, session-id 갱신 트리거, prompt cache TTL 가정 등
3. **Prerequisite 패치**: spec §6에 명시된 사전 패치 항목 적용

자세한 진입 가이드는 spec §9 참조.

---

## 5. 다음 세션 시작 시 체크리스트

```bash
# 1. 환경 sanity
awf --help | head -3
cmux-agent --help | head -3

# 2. 새 명령 smoke (active workflow 없어도 동작)
awf wf scope-check --help                  # PR #117 멀티-레포 옵션 확인
awf wf expand-scope --help                 # PR #119 (--service는 cycle root만 적용)
awf ready --gate workflow-run --help       # PR #118 manifest 검증

# 3. multi-repo 실제 호출 (있다면)
awf wf scope-check --json | jq '.per_repo'
awf wf expand-scope --json --dry-run | jq '.per_repo'
```

---

## 6. 다음 cycle 시작 시 후보

### (A) §4.2 session 재사용 impl

사전 작업:
1. telemetry 1-2 cycle 누적 (지금 시점에서 0건)
2. `docs/specs/session-reuse.md` 재검토 + 필요 시 update
3. impl 진입 (spec §7 수락 기준 따라)

추정 규모: M (~반나절)

### (B) 실제 multi-repo cycle dogfooding

PR #117/#118/#119를 실제 multi-repo 시나리오에서 검증. BLIP cycle 같은 외부 작업에서 자연스럽게 발생할 가능성 — 별도 세션에서.

### (C) 새 운영 이슈 탐색

운영 중 알게 된 새 gap이 있으면 `docs/gaps/$(date +%Y-%m-%d)-<설명>.md` 형식으로 doc 작성 → §7 표준 절차.

---

## 7. 새 이슈 발생 시 표준 절차

`docs/gaps/2026-05-13-handover-next-session.md` §7과 동일.

---

## 8. 참조 문서

| 문서 | 내용 |
|---|---|
| `docs/gaps/2026-05-13-handover-next-session.md` | 이전 cycle (§1.1-§1.4 cycle 종료) |
| `docs/gaps/2026-05-13-blip-gem-cycle-operational-issues.md` | 운영 이슈 카탈로그 |
| `docs/specs/multi-repo-scope.md` | sibling_repos + `@<name>/` prefix 규약 (PR #117) |
| `docs/specs/cross-repo-expand-scope.md` | sibling import graph 활용 (PR #119) |
| `docs/specs/session-reuse.md` | §4.2 impl 사전 설계 |
| `cli/README.md` | awf 명령 상세 — scope-check/expand-scope 항목 갱신됨 |

---

## 9. 다음 세션에서 본 문서를 어떻게 활용할까

다음 세션 시작 시 한 줄 컨텍스트 복원:

> "`docs/gaps/2026-05-14-handover-next-session.md` 읽고 §5 sanity checklist 실행"

또는 구체적 작업 진입:

> "`docs/specs/session-reuse.md` 따라 §4.2 impl. telemetry 누적 데이터 먼저 확인"

---

**작성**: 2026-05-14, Claude Opus 4.7 (1M context)
**다음 갱신 시점**: §4.2 impl 진행 또는 새 cycle 진입 시
