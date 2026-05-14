# Cycle Session Reuse Spec

> **상태**: Draft (2026-05-14) — impl 대기. telemetry 1-2 cycle 누적 후 ROI 확인 시 진입
> **트리거**: `docs/gaps/2026-05-13-blip-gem-cycle-operational-issues.md` §4.2
> **관련 원칙**: Constitution C8 (재현성), C9 (운영 비용 가시화)
> **관련 PR**: §8.7-P1 telemetry (PR #114)

---

## 1. 배경과 문제

`awf wf next`로 worker(claude code subprocess 또는 Codex MCP)를 spawn할 때 매 phase마다 새 `--session-id`가 생성된다. 결과:

- **context 재빌드**: 매 verify phase가 spec.md/plan.md/tasks.md/impl-log.md 전체를 prompt에 다시 포함 (~217KB)
- **token 누적**: phase 6단계 × 2회 verify ≈ 12회 풀 context = 약 2.6MB input
- **시간 누적**: verify 1회당 15-21분 (context 평가 + LLM 응답)
- **Anthropic prompt cache 미활용**: 5분 TTL이라 phase 간 간격이 길어지면 cache miss

`§8.7-P1 telemetry`(PR #114)로 phase별 input_tokens가 누적되기 시작 → 실제 ROI 측정 가능. 누적 데이터 1-2 cycle 후 impl 진입.

---

## 2. 비-목표

- claude code 외부의 session 재사용 (Codex의 turn 보존은 codex-cli-runtime 책임)
- cross-cycle session 재사용 (cycle 종료 시 session도 종료)
- broker worker(persistent cmux-agent tab) — 이미 자체 session 유지 중. 본 spec은 단발 spawn 케이스에 한정
- prompt cache 정책 변경 (Anthropic 제공 기능 활용만)

---

## 3. 데이터 모델

### 3.1 `.workflow/session.json` 신규 파일

```jsonc
{
  "version": "1.0.0",
  "cycle_id": "20260514-multi-repo-followup",   // state.json의 cycle id와 동일
  "providers": {
    "claude": {
      "session_id": "0190f3a4-1234-7abc-9def-...",   // UUIDv7 권장
      "created_at": "2026-05-14T10:00:00+09:00",
      "last_used_at": "2026-05-14T12:30:15+09:00",
      "phase_invocations": [
        {"phase": "plan",   "ts": "...", "input_tokens": 12000},
        {"phase": "review", "ts": "...", "input_tokens": 8500},
        {"phase": "verify", "ts": "...", "input_tokens": 5200}
      ]
    },
    "codex": {
      "session_id": null,    // Codex는 turn 기반이라 별도 정책
      "rollout_path": ".../sessions/2026/05/14/rollout-....jsonl"
    }
  }
}
```

- `cycle_id`는 `.workflow/state.json` 의 cycle id 또는 concept slug 사용
- `providers` 키는 확장 가능. 현재 spec은 `claude`만 다룸
- `phase_invocations`는 가시화용 — 비대해지면 최근 N개만 보존(예: 20)

### 3.2 lifecycle

| 시점 | 동작 |
|---|---|
| `awf wf init` | session.json 생성 안 함 (lazy) |
| 첫 `awf wf next` (claude worker spawn) | session.json 신규 생성, UUID 발급 |
| 후속 `awf wf next` | session.json 읽어 `--session-id <persisted>` 전달 |
| `awf wf reset` / `awf wf reset --new-concept` | session.json 폐기 (새 cycle = 새 session) |
| `awf wf decide abort` | session.json 그대로 보존 (재개 가능) |
| `awf wf done` (cycle 종료) | session.json 그대로 보존 (디버깅/감사) |
| `awf wf pr` 머지 후 정리 | session.json은 .gitignore (machine-local) |

---

## 4. 동작 명세

### 4.1 `awf wf next` 흐름 변경

```
1. resolve current phase + provider chain
2. for each provider in chain:
   a. if provider == "claude":
      i. load_or_create_session(repo_root, "claude")
      ii. build prompt as before
      iii. spawn worker with `claude --session-id <persisted> ...`
      iv. on success: record_phase_invocation(session_id, phase, input_tokens)
   b. else (codex/inline/cmux-agent):
      existing behavior (no session.json write)
3. continue existing flow
```

### 4.2 session-id 갱신 트리거 (재발급 조건)

다음 경우 session.json을 폐기하고 새 UUID 발급:

1. `awf wf reset` (state도 함께 초기화)
2. `awf wf next --new-session` (운영자 명시) — 새 옵션
3. `last_used_at` 으로부터 24시간 경과 (prompt cache는 이미 cold)
4. cycle_id 가 state.json과 불일치 (stale session.json 감지)

### 4.3 prompt cache TTL 가정

- Anthropic prompt cache는 ~5분 TTL (운영 환경 변경 가능 — spec impl 시 재확인)
- session-id 재사용해도 cache hit는 5분 내 호출에만 적용
- 따라서 session 재사용의 주된 이득은:
  - (a) 5분 내 연속 phase의 cache hit
  - (b) provider-side prompt history 압축 (provider가 지원할 경우)
  - (c) 비용/시간 가시화 — phase_invocations로 누적 추적

### 4.4 phase prompt 변경 없음

session 재사용은 prompt **빌드 방식을 바꾸지 않는다**. 즉:

- 매 phase가 여전히 full context를 prompt에 포함
- claude code가 session-id를 받고 prompt cache 매칭을 시도
- cache hit 시 provider가 자동으로 압축 (운영자 개입 없음)

대안(prompt 자체를 줄이기)은 별도 spec — 본 spec은 session-id 보존만 다룬다.

---

## 5. CLI 인터페이스

### 5.1 새 옵션 — `awf wf next --new-session`

```bash
awf wf next --new-session    # 기존 session.json 폐기 후 새 UUID
```

### 5.2 새 명령 — `awf wf session [show|reset|prune]`

```bash
awf wf session show        # session.json 내용 표시
awf wf session reset       # session.json 폐기
awf wf session prune       # phase_invocations 배열을 최근 20개로 트리밍
```

### 5.3 `awf wf status` 출력 확장

```
session:
  claude:
    session_id: 0190f3a4-...
    created_at: 2026-05-14T10:00:00+09:00
    last_used_at: 2026-05-14T12:30:15+09:00
    phase_invocations: 3 (plan, review, verify)
    total_input_tokens: 25700  # telemetry와 cross-check
```

`session.json` 없으면 이 섹션 미출력.

---

## 6. 사전 patch 필요 항목 (impl 진입 전)

- [ ] `cli/src/awf/core/state.py` — `.workflow/session.json` load/save helper
- [ ] `cli/src/awf/commands/wf.py` — `next` flow에 session-id injection
- [ ] worker spawn 경로(provider별) — claude provider에 `--session-id` 옵션 전달 확인
- [ ] `.gitignore` 템플릿 — `.workflow/session.json` 추가 (machine-local)
- [ ] telemetry hook — `record_phase_telemetry` 호출 시점에 `record_phase_invocation`도 호출

---

## 7. 수락 기준 (G6 tests)

### 7.1 단위 테스트

1. `test_session_load_or_create_lazy_init` — `awf wf next` 첫 호출 시 session.json 생성
2. `test_session_reused_across_next_calls` — 2회 호출 시 동일 UUID
3. `test_session_invalidated_by_wf_reset` — reset 후 새 UUID
4. `test_session_invalidated_by_new_session_flag` — `--new-session` 시 새 UUID
5. `test_session_invalidated_after_24h` — `last_used_at` 24h 이전이면 새 UUID
6. `test_session_invalidated_on_cycle_id_mismatch` — cycle_id 변경 감지
7. `test_session_phase_invocations_appended` — telemetry 기록 시 invocations 누적
8. `test_session_prune_keeps_recent_n` — `awf wf session prune` 동작

### 7.2 통합 시나리오

- 동일 cycle에서 3 phase 연속 호출 → session.json의 phase_invocations 3건
- 실제 worker spawn 명령에 `--session-id <UUID>` 포함 확인 (cli.py argv 검증)

### 7.3 ROI 측정 기준

impl 후 1-2 cycle 추가 실행하여:

- `awf wf status` telemetry의 `total_input_tokens` 가 session 재사용 전후로 감소했는가?
- phase 평균 시간(특히 verify) 단축 측정
- ROI 미미하면(<5% 절감) feature flag 뒤로 숨기고 default off

---

## 8. 엣지케이스 / 실패 모드

| 케이스 | 처리 |
|---|---|
| session.json 손상 (invalid JSON) | 새 UUID 발급 + 손상본 백업(`.workflow/session.json.bak`) |
| state.json의 cycle_id 변경 (concept replan) | session 자동 폐기 |
| provider가 `--session-id` 미지원 (구버전) | session.json 미사용. silent fallback + 1회 경고 |
| 동일 cycle에서 다른 provider 사용 | provider별 분리 entry. 충돌 없음 |
| 24h 이상 cycle (장기 작업) | last_used_at 갱신 → 24h cap 의미는 cold start 방지용 |
| `.workflow/session.json` 을 .gitignore에 추가 안 함 | 사용자 PR에 session.json 포함 → 다른 머신에서 stale. impl 시 .gitignore 템플릿 갱신 필수 |

---

## 9. 진입 가이드 (next session)

impl 시작 시:

1. **prerequisite 확인**:
   - `awf wf status` telemetry에 `total_input_tokens` 1-2 cycle 누적되어 있는가?
   - 없으면 실제 cycle 한두 번 돌린 뒤 재진입
2. **spec 재검토**:
   - §4.3 prompt cache TTL은 Anthropic 운영 변경 가능성 — 최신 doc 재확인
   - §4.2 트리거 정책(특히 24h cap)이 합리적인가?
3. **사전 patch 적용** (§6 체크리스트)
4. **impl 후 ROI 측정** (§7.3) — 미미하면 default off
5. **PR 생성** — `feat(awf): cycle session reuse (§4.2 impl)`

---

## 10. 참조

- 운영 이슈 원본: `docs/gaps/2026-05-13-blip-gem-cycle-operational-issues.md` §4.2
- telemetry 인프라: PR #114 (§8.7-P1)
- handover: `docs/gaps/2026-05-14-handover-next-session.md` §4
- chat session 구현 참고: `cli/src/awf/core/chat_session.py` (Phase 5 chat은 별도 트랙)
