# AWF AI Workflow 실환경 검증 체크리스트

이 문서는 `Claude Code + Codex` 운영을 기준으로 `awf-cli` 실환경 검증을 수행할 때 따라가는 체크리스트입니다.

목표:
- Claude Code `/analysis` 경로가 정상 동작하는지 확인한다.
- `codex` review/verify 경로가 prompt, result, gate/state 갱신까지 이어지는지 확인한다.
- 실패 시 어디를 봐야 하는지 빠르게 판단한다.

현재 운영 가정:
- **Analysis**: Claude Code `/analysis`가 primary, `awf analyze --dry-run`이 보조 준비 도구
- **WF pipeline**: Claude Code `/wf`가 primary, `awf wf next --dry-run`이 보조 (Codex review/verify는 `awf wf next --provider codex` 경로 사용 가능)
- `awf analyze --provider claude-code`는 small domain에서 먼저 검증
- large domain 또는 Stage 3 자동 승격 케이스는 timeout 가능성이 있으므로 `/analysis` 권장

관련 문서:
- [awf-cli Phase 1](../../cli/README.md)
- [두 세션 운영 실습](./02-two-session-workflow.md)
- [awf-cli 아키텍처](../architecture/awf-cli-architecture.md)

## 1. 사전 조건

레포 루트:

```bash
cd ~/Documents/GitHub/ai-workflow-tools
```

필수 조건:
- `uv` 사용 가능
- `codex` CLI 설치 및 인증 완료
- `claude` CLI를 사용할 경우 `claude --print` 실행 가능한 로그인 상태

선택 확인:

```bash
uv run --project cli --no-editable awf config show --repo-root .
uv run --project cli --no-editable awf doctor --repo-root . --json --ci
```

`awf doctor --json --ci` 판정:
- exit code `0`: 현재 default provider가 installed/configured 기준으로 준비됨
- exit code `1`: default provider readiness 미달
- `--probe`를 같이 쓰면 subprocess provider는 lightweight probe까지 포함해 판정
- `pi_readiness`: Pi command/path/version, provider auth env 존재 여부,
  opt-in dispatch surface, Anthropic Extra Usage 과금 주의를 provider 호출
  없이 확인하는 참고 진단

## 2. 검증 A: Claude Code `/analysis`

실행:

```text
/analysis sample-api quest-challenge
```

성공 시 확인할 것:
- `.ai-context/.analysis-state.json` 생성 또는 갱신
- `.ai-context/.tmp/domain-bundle.xml` 생성
- 필수 산출물 4종 생성
  - `api-spec.json`
  - `data-model.md`
  - `domain-overview.md`
  - `external-integration.md`

상태 체크:
- `layers.bundle.status = completed`
- `layers.analyze.stage1.status = completed`
- `layers.analyze.stage2.status = completed`
- `layers.output.status = completed`
- Stage 3 자동 승격 케이스 (`related_domains` 존재 또는 `stage3_force: true`):
  - Claude Code / Claude 계열 provider에서는 `layers.analyze.stage3.status = completed`
  - fixture 등 비실행 provider에서는 `layers.analyze.stage3.status = scaffold`
  - 승격 조건을 만족하지 않으면 `stage3.status = skipped`

실패 시 우선 확인:
- `.analysis-state.json`의 `retryCount`
- `.ai-context/.tmp/` 아래 stage 결과/번들이 남았는가

## 3. 검증 B: `awf analyze` small-domain 확인

실행:

```bash
uv run --project cli --no-editable awf analyze sample-api health --repo-root . --provider claude-code --yolo
```

확인 포인트:
- `provider_running`
- `still_running`
- `provider_completed`
- `state_file`
- `output_status`

판정:
- small domain + standard mode에서 완료되면 CLI analyze 운영 경로 유지 가능
- 여기서도 timeout이면 `claude-code --print` 경로는 준비 도구/실험 경로로 더 낮춰야 함

## 4. 검증 C: `codex` review auto-apply

실행:

```bash
uv run --project cli --no-editable awf wf next --repo-root . --phase review --provider codex --auto-apply
```

성공 시 확인할 것:
- `.workflow/tmp/prompt-review-codex.txt`
- `.workflow/tmp/result-review-codex.txt`
- `.workflow/artifacts/review-report.md`
- `.workflow/state.json` 갱신

상태 체크:
- `phases.review.status`가 `completed` 또는 gate 결과 반영 상태인지
- `gates.G2.status`가 채워졌는지
- `history`에 review 실행 흔적이 남았는지

실패 시 우선 확인:
- `codex` CLI 인증/실행 오류
- result 파일에 JSON이 아닌 wrapper text만 남았는지
- `review-report.md`가 비어 있거나 findings 파싱이 실패했는지

## 5. 검증 D: `codex` verify auto-apply

실행:

```bash
uv run --project cli --no-editable awf wf next --repo-root . --phase verify --provider codex --auto-apply
```

성공 시 확인할 것:
- `.workflow/tmp/prompt-verify-codex.txt`
- `.workflow/tmp/result-verify-codex.txt`
- `.workflow/artifacts/verification-report.md`
- `.workflow/state.json`의 verify/gate 반영

상태 체크:
- `gates.G5.status`
- scope/compliance 관련 항목이 report에 반영됐는지

## 6. 재실행 시 체크

`analyze` 재실행:
- 완료 상태면 provider를 다시 호출하지 않고 skip 하는가
- bundle 설정이 바뀌면 `domain-bundle.xml`을 무효화하고 재생성하는가

`wf next` 재실행:
- 같은 phase가 `in_progress`면 경고가 출력되는가
- 이전 result 파일이 남아 있어도 새 실행 흔적이 구분되는가

## 7. 실패 분류

인증 실패:
- `codex` 로그인/실행 문제
- Claude Code 세션/도구 실행 문제

계약 실패:
- provider 출력 형식 불일치
- gate condition 미지원
- required output 4종 누락

권한 실패:
- `permissions.disabled_tools`
- `tool:file.read` 등 tool-level 차단

## 8. 완료 기준

실환경 검증이 끝났다고 보려면 최소 이 2개가 통과해야 합니다.

1. Claude Code `/analysis`
2. `codex review --auto-apply`

추가로 `verify --auto-apply`까지 통과하면 운영 준비 상태로 봐도 됩니다.

`awf analyze --provider claude-code`는 small domain 검증까지 통과하면 보조 운영 경로로 본다.

## 9. 선택 검증: `claude-sdk`

`ANTHROPIC_API_KEY`를 쓰는 환경이면 아래도 별도 검증할 수 있습니다.

```bash
uv run --project cli --no-editable awf analyze sample-api quest-challenge --repo-root . --provider claude-sdk --yolo
```

이 경로는 현재 기본 운영 경로가 아니라 optional 실험 경로입니다.

## 10. 선택 검증: Pi dispatch

Pi는 기본 dispatch surface가 아닙니다. `surface_preference=pi`는 실제 Pi
설치와 provider 인증이 있는 머신에서만 검증합니다.

```bash
python3 cli/tests/run_pi_field_smoke.py --json
```

전역 설치 없이 npm으로 임시 실행하려면:

```bash
python3 cli/tests/run_pi_field_smoke.py --npm-exec --json
```

이 검증은 fake binary test가 아니라 실제 Pi print-mode provider 호출을
수행합니다. 실패 시 `reason`과 `diagnosis.next_action`을 확인합니다.
`missing_provider_auth`는 provider 인증 미구성, `provider_quota_exhausted`
와 `billing_context: "anthropic_extra_usage"`는 Claude Extra Usage 한도
부족을 뜻합니다. 자세한 판정 기준은 [Pi Field Validation](./pi-field-validation.md)을 참고합니다.
