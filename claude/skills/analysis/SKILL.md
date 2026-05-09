---
name: analysis
version: 2.0.0
description: "소스코드 분석 파이프라인. .ai-context 문서를 생성하고 resume/incremental/deep 분석을 지원."
type: analysis

# LLM 중립 메타데이터
capabilities:
  - file_read
  - file_write
  - shell_exec
  - code_analysis

conditions:
  trigger: "소스코드 분석, .ai-context 문서 생성, 프로젝트 문서화가 필요할 때"
  skip: "일반 코드 리뷰, 워크플로우 파이프라인 진행 중, PR 리뷰"

# CLI 실행 매핑
cli:
  command: "awf analyze {service} {unit}"
  args:
    service: { required: true, description: "서비스명 (config에 없으면 auto-discovery)" }
    unit: { required: true, description: "분석 단위명 (도메인, 모듈, 컴포넌트 등)" }
    deep: { flag: true, description: "심층 분석 (크로스서비스 + Stage 3)" }
    mode: { choices: ["solo", "precise", "cross", "critical"], description: "멀티에이전트 모드" }
    all: { flag: true, description: "서비스 내 전체 단위 순차 분석" }
    check: { flag: true, description: "소스 변경 감지 (drift detection). 분석 실행 없이 stale 단위 탐지" }
    catalog: { flag: true, description: "서비스 전체 분석 현황 (config + .ai-context join)" }

# 리소스 (manifest.json 기반 spec-as-truth)
resources:
  modes:
    document: "modes/document.json"
    review: "modes/review.json"
    investigate: "modes/investigate.json"
  prompts:
    stage1: "prompts/stage1-file.md"
    stage2: "prompts/stage2.md"
    stage3: "prompts/stage3.md"
    mode_overlays:
      precise: "prompts/mode-precise.md"
      cross: "prompts/mode-cross.md"
---

# /analysis — 소스코드 분석 파이프라인

4계층 파이프라인으로 소스코드를 분석하여 `.ai-context/` 문서를 자동 생성합니다.
기본 모드는 빠른 분석, `--deep` 모드는 크로스서비스 분석과 심층 검증을 포함합니다.
모든 프로젝트 구조(TypeScript, PHP, Python, Go, Terraform, K8s 등)를 지원합니다.

**상세 파이프라인 지침은 [reference.md](reference.md)를 참조하세요.**

## 사용법

```
# Claude Code에서
/analysis {service} {unit} [--deep]

# awf-cli에서
awf analyze {service} {unit} [--deep] [--mode cross] [--all]
awf analyze {service} --check        # drift detection
awf analyze {service} --catalog      # 전체 현황
```

- **service**: 서비스명 (config에 없으면 auto-discovery로 자동 탐색)
- **unit**: 분석 단위명 (프로젝트 구조에 따라 도메인, 모듈, 컴포넌트 등)
- **--deep**: 크로스서비스 분석 + Stage 3 포함
- **--mode**: 멀티에이전트 교차 검증 (cross = codex + sonnet 병렬)
- **--all**: 서비스 내 전체 단위 순차 분석
- **--check**: 소스 파일 해시 비교로 stale 단위 탐지 (분석 실행 없음)
- **--catalog**: 전체 분석 현황 출력 (config의 단위 정의 + .ai-context 상태 join)

## Deterministic Preflight

분석 실행 전 반드시 repo root에서 다음 gate를 실행합니다:

```bash
awf ready --gate analysis --repo-root . --json
```

- exit code `0` (`decision: "allow"`)일 때만 provider-backed 분석을 실행합니다.
- exit code `10` (`decision: "dry_run_only"`)이면 provider 호출 없이 `awf analyze ... --dry-run`까지만 실행합니다.
- 그 외 non-zero는 분석을 중단하고 `gate.recommended_next`의 명령만 제안합니다.

`awf analyze`도 provider-backed 실행 전 같은 gate를 내부에서 다시 확인합니다. 상위 wrapper가 이미 같은 판정을 수행한 경우에만 `--no-ready-gate`를 사용합니다.

## 분석 파이프라인

| Stage | 역할 | Provider |
|-------|------|----------|
| Stage 1 | 파일별 XML 번들 분석 (import context 포함) | 저비용 (codex) |
| Stage 2 | 단위 합성 (.ai-context 4개 파일 생성) | 중간 (sonnet) |
| Stage 3 | 프로젝트 정제 (cross-service, deep만) | 고비용 (opus) |

Stage별 provider는 `analysis-pipeline.json`의 `stage_routing.{scale}`에서 결정됩니다.

## 핵심 규칙

1. **Resume 최우선**: `.analysis-state.json`에서 미완료 단계부터 재개
2. **Incremental**: 소스 파일 해시가 변경되면 변경 파일만 Stage 1 재분석, 이전 결과 merge
3. **기존 문서 보존**: Stage 2 프롬프트에 기존 `.ai-context` 결과가 주입되고, 변경 없는 부분은 유지 지시
4. **상태 업데이트**: 각 Stage 완료 시 반드시 상태 파일 업데이트
5. **언어 중립 수집**: `include_patterns`로 설정하거나 프로젝트 언어 자동 감지
6. **빈 입력 차단**: 수집 파일 0건이면 Stage 2 진입 안 함
7. **XML 번들**: Stage 1에서 파일별 개별 XML, domain-bundle에도 import context 포함
   - `role="target"` + Stage 1 역할/요약이 `summary` 속성으로 annotate
   - `role="context" mode="signatures"`: unit 외부 import 파일의 시그니처만 포함
   - structure 섹션에 파일 역할(`controller`, `service` 등) 표시
8. **프롬프트 외부화**: `prompts/*.md` 파일에서 로드 — 코드 수정 없이 프롬프트 변경 가능
9. **Mode Contract**: `modes/*.json`에서 mode별 output files, writers, judge config 로드 (spec-as-truth)
10. **Manifest**: `manifest.json`에 카테고리 선언 → `spec_loader.load_skill_resource()`로 런타임 로드
