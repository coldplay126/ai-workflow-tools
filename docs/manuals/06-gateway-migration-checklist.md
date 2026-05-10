# Gateway Migration Checklist

## 목적

현재 `awf-cli`의 gateway/orchestrator 경로가 실제로 보이는지 빠르게 확인한다.

기준:

- `analyze`: stage/worker/event summary
- `wf`: phase/stage/gate/event summary
- `status`: `eventSync` 기반 조회

## 사전 조건

```bash
cd ~/Documents/GitHub/ai-workflow-tools
```

fixture 기반 확인이므로 실 provider 인증은 필수가 아니다.

## 1. 핵심 fixture 회귀

```bash
python3 cli/tests/run_router_fixture.py
python3 cli/tests/run_fixture_flow.py
python3 cli/tests/run_gateway_state_sync_fixture.py
python3 cli/tests/run_wf_status_fixture.py
python3 cli/tests/run_analyze_status_fixture.py
```

기대:

- 전부 exit code `0`

## 2. workflow status 확인

```bash
uv run --project cli awf wf status --repo-root .
```

확인 포인트:

- `event_summary:`
- `event_phases:`
- `event_tasks:`
- `event_gates:`
- `event_artifacts:`

최근 실행 이력이 있으면 추가로:

- `event_stages:`

## 3. analyze status 확인

```bash
uv run --project cli awf analyze sample-api quest-challenge --repo-root . --status
uv run --project cli awf analyze sample-api quest-challenge --repo-root . --status --json
```

확인 포인트:

- `output_status:`
- `analyze_stages:`
- 실행된 상태라면 `event_summary:`
- 실행 전 pending 상태라면 `event_summary:`가 없어도 정상

## 4. 자연어 라우팅 확인

```bash
uv run --project cli awf "quest challenge 분석 상태 보여줘"
uv run --project cli awf "review 실행"
uv run --project cli awf "provider 상태 probe 확인해줘"
```

확인 포인트:

- analyze status 라우팅
- workflow 실행 라우팅
- doctor probe 라우팅

## 5. workflow 실행 visibility 확인

fixture provider 기준:

```bash
python3 cli/tests/run_fixture_flow.py
```

출력에서 확인할 것:

- `phase_started: review`
- `stage_started: prepare`
- `stage_started: execute`
- `stage_started: apply`
- `gate_evaluated: G2 PASS`
- `phase_completed: review`
- `event_summary:`

## 6. analyze 실행 visibility 확인

small domain 기준:

```bash
uv run --project cli awf analyze sample-api health --repo-root . --provider claude-code --yolo
```

확인 포인트:

- `stage_started: stage1`
- `stage_completed: stage1`
- `stage_started: stage2`
- `provider_execution_mode:` 또는 `provider_running:`
- 완료 후 `event_summary:`

주의:

- large domain은 prompt 크기와 provider 특성 때문에 시간이 길어질 수 있다
- 운영 기본 경로는 여전히 Claude Code `/analysis`
- `awf analyze`는 small domain + standard mode에 더 적합

## 7. 현재 판정

이 체크리스트가 통과하면 다음을 확인한 것이다.

- gateway event pipeline 정상
- `eventSync` state sync 정상
- `wf`/`analyze` 상태 조회 정상
- 자연어 라우팅 정상
- 실사용 경로 기준 gateway migration 기능 완성 수준 유지
