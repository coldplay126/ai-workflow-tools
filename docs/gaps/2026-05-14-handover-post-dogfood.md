# 2026-05-14 dogfood [D] + G-OPS-001 fix 후속 — 다음 세션 핸드오버

> **상태**: 같은 날 후반 세션. `2026-05-14-handover-next-session.md` §6(B) 실행 → §6(C) G-OPS-001 fix까지 한 세션에서 마감.
> 이전 핸드오버: `docs/gaps/2026-05-14-handover-next-session.md` (사전 시점)
> 본 문서는 다음 Claude Code 세션 컨텍스트 복원용.

---

## 1. 머지된 변경 (origin/main 기준)

```
e5a69d0 docs(cli-readme): mention `install_freshness` in `awf doctor` field list (#121 followup) (#122)
7ba9493 fix(doctor): detect stale `awf` install vs source (G-OPS-001) (#121)
3b5c095 docs: 2026-05-14 cycle handover + session-reuse spec (§4.2) (#120)   ← 이전 세션
```

신규 산출물:
- `cli/src/awf/core/version_check.py` — install drift 감지 (4 status: in_sync / stale / editable / no_source_found)
- `cli/tests/test_version_check.py` — 12 unit tests
- `docs/gaps/2026-05-14-dogfood-d-findings.md` — dogfood 보고서 (G-OPS-001 발견 + 해소 경로)

---

## 2. 테스트 상태

- `awf cli` **693/693** PASS (681 → 693, +12 신규)
- 전체 PR 별 CI green
- 글로벌 `awf` 재설치 완료 → `awf doctor` → `install_freshness: in_sync (102 files)`

---

## 3. dogfood [D] 검증 결과 요약

ai-workflow-tools (cycle root) + cmux-agent (sibling="cmux") 교차 등록으로 PR #117/#118/#119 검증:

| 명령 | 결과 |
|---|---|
| `awf wf scope-check` | multi-repo 헤더, per_repo=2, changed=33, `@cmux/` routing, `@typo-sibling/` violation 감지 ✓ |
| `awf wf expand-scope --dry-run` | per_repo=2, status=no_docs_root (cmux 측), stderr 경고 + exit 0 ✓ |
| `awf ready --gate workflow-run` | 정상 manifest→allow/0, malformed→block/exit 20 ✓ |
| `awf wiki compile` | "Multi-repo coverage" 섹션 정상 렌더 ✓ |

자세한 데이터는 `docs/gaps/2026-05-14-dogfood-d-findings.md` 참조.

---

## 4. G-OPS-001 fix 핵심 동작 (PR #121)

```
awf doctor                              # post-reinstall
install_freshness: in_sync (102 files)

# cli/ 머지 후 재설치 안 한 상태
awf doctor
⚠ install_freshness: STALE
  installed `awf` (102 files, hash 17a6d7625479) differs from source at ... — new behavior in `cli/` may be silently disabled until you reinstall
  fix: uv tool install --reinstall /Users/steven/Documents/GitHub/ai-workflow-tools/cli
```

JSON 필드: `awf doctor --json | jq .install_freshness` — status / installed_hash / source_hash / file_count / reinstall_command.

**의존성 변경 없음** — `hashlib`, `pathlib`만 사용. 신규 import 없음.

---

## 5. 운영 변경점 — `cli/` 머지 후 절차

이제 `cli/` 변경이 main에 머지되면 다음을 반드시 수행:

```bash
cd /Users/steven/Documents/GitHub/ai-workflow-tools
git pull
uv tool install --reinstall ./cli
awf doctor | grep install_freshness   # → in_sync (N files) 확인
```

`awf doctor` 가 STALE을 보여주면 새 기능(예: 다음 multi-repo manifest 필드)이 silently 무시될 수 있음. STALE은 hard fail이 아니라 ⚠ 경고이므로 운영자가 무시할 수 있다 — 그러나 시 다음 manifest 필드 추가/변경 머지 후 곧바로 인지 가능해짐.

---

## 6. 다음 세션 시작 시 sanity checklist

```bash
# 1. 환경
awf --help | head -3
awf doctor | grep install_freshness   # in_sync 확인

# 2. 만약 install_freshness=stale 이면 즉시
uv tool install --reinstall /Users/steven/Documents/GitHub/ai-workflow-tools/cli

# 3. multi-repo 명령 그대로 사용 가능
awf wf scope-check --help
awf wf expand-scope --help
awf ready --gate workflow-run --help
```

---

## 7. 다음 cycle 후보

### (A) §4.2 session 재사용 impl  *(handover §6A 그대로)*

- 사전 작업: telemetry 1-2 cycle 누적 (현재 **0건** — 진행 전 ROI 평가 불가)
- spec: `docs/specs/session-reuse.md`
- 진입 가이드: spec §9
- 추정 규모: M (~반나절)

### (B) 실제 multi-repo cycle dogfooding (real-world)

오늘 dogfood [D]는 합성 시나리오(HEAD~5 base). 실제 feature 브랜치 환경에서 base resolution이 다르게 동작하는지는 자연 발생 시 검증. BLIP 등 외부 cycle 진입 시.

### (C) 새 운영 이슈 탐색

`docs/gaps/$(date +%Y-%m-%d)-<설명>.md` 형식.

### (D) install_freshness 후속 개선 (low priority)

- `cli/` 외에 `.json schema`, `.toml` 등 비-Python 리소스 추가 시 해시 비교 확장
- CI에서 STALE detected → fail 옵션 (`awf doctor --ci` 에 통합)
- `pyproject.toml` 버전 번호 bump 정책 정의

---

## 8. 새 이슈 발생 시 표준 절차

`docs/gaps/2026-05-13-handover-next-session.md` §7과 동일.

---

## 9. 참조 문서

| 문서 | 내용 |
|---|---|
| `docs/gaps/2026-05-14-handover-next-session.md` | 사전 핸드오버 (§5 sanity + §6 후보) |
| `docs/gaps/2026-05-14-dogfood-d-findings.md` | dogfood [D] 보고서 + G-OPS-001 발견 |
| `docs/gaps/2026-05-13-handover-next-session.md` | 이전 cycle (§1.1-§1.4) |
| `docs/specs/multi-repo-scope.md` | sibling_repos + `@<name>/` prefix 규약 |
| `docs/specs/cross-repo-expand-scope.md` | sibling import graph 활용 |
| `docs/specs/session-reuse.md` | §4.2 impl 사전 설계 |
| `cli/README.md` | `awf doctor` install_freshness 필드 안내 (PR #122) |

---

## 10. 다음 세션에서 본 문서를 어떻게 활용할까

한 줄 컨텍스트 복원:

> "`docs/gaps/2026-05-14-handover-post-dogfood.md` 읽고 §6 sanity checklist 실행"

또는 구체 작업:

> "`docs/specs/session-reuse.md` 따라 §4.2 impl. telemetry 누적 데이터 먼저 확인"

---

**작성**: 2026-05-14 후반, Claude Opus 4.7 (1M context)
**다음 갱신 시점**: §7 후보 중 하나 진입 시
