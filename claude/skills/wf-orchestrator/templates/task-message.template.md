# Task Message Template

오케스트레이터가 외부 워커에게 보내는 self-contained task message 구성 가이드.

## 메시지 구조

```json
{
  "id": "msg_{phase}_{timestamp}",
  "task_id": "{state.id}",
  "phase": "{currentPhase}",
  "context_id": "wf_{state.id}",

  "parts": [
    { "type": "meta", "project": "...", "branch": "...", "phase": "...", "attempt": "..." },
    { "type": "role", "text": "..." },
    { "type": "rules", "content|paths": "..." },
    { "type": "instruction", "text": "...", "output_schema": {} },
    { "type": "artifact|file_ref", "key": "...", "content|path": "..." },
    { "type": "context", "key": "...", "content": "..." }
  ],

  "execution_hints": {
    "sandbox": "read-only|workspace-write",
    "cwd": "{project_root}",
    "timeout_seconds": 300,
    "budget_usd": 0.50
  }
}
```

## Part Types

### meta
프로젝트 메타 정보. 모든 task message 최상단에 배치.
- `project`: state.repo
- `branch`: state.branch
- `phase`: currentPhase
- `attempt`: `{retries + 1}/{max_retries}`
- `workflow_id`: state.id

### role
워커의 역할 정의. agent card의 description 기반.
- `text`: `You are a {phase} agent for the {project} project. Your role: {agent_card.description}`

### rules
프로젝트 규칙 (AGENTS.md, CLAUDE.md). manifest.json의 `context_providers` 기반.
- `file_access: true` 워커 → 파일 경로만 전달 (토큰 절약)
- `file_access: false` 워커 → 파일 내용 전문 임베드

### instruction
워커에게 무엇을 해야 하는지 알려주는 지시문.
- `text`: 자연어 지시 (Phase별 agent card의 description + skills 기반)
- `output_schema`: agent card의 `output.structured_result` 스키마 (워커가 이 형식으로 JSON 응답)

### artifact
파일 전문을 임베드. `file_access: false`인 워커 (Claude `--bare`)용.
- `key`: agent card의 `input.required_artifacts[].key`
- `content`: 파일 전체 내용
- `content_type`: `text/markdown` 또는 `application/json`

### file_ref
파일 경로만 전달. `file_access: true`인 워커 (Codex MCP, cwd 접근 가능)용.
- `key`: agent card의 `input.required_artifacts[].key`
- `path`: `.workflow/` 기준 상대 경로

### context
추가 컨텍스트 (프로젝트 규칙, git diff 등).
- `key`: 컨텍스트 식별자
- `content`: 텍스트 내용

## 워커 파일 접근 능력에 따른 Part 선택

```
agent card의 input.required_artifacts를 순회:
  IF provider.file_access == true:
    → file_ref part (경로만)
  ELSE:
    IF artifact.embed_for_stateless == true:
      → artifact part (전문 임베드)
    ELSE:
      → skip (optional이면 생략)

Rules 임베딩:
  IF provider.file_access == true:
    → "Read and follow the rules in: ./AGENTS.md, ./CLAUDE.md"
  ELSE:
    → AGENTS.md + CLAUDE.md 전문 임베드
```

## Phase별 Instruction 예시

### review (Phase 2)
```
You are a code review agent. Cross-validate the following artifacts for:
1. Duplicate requirements (similar FR-NNN IDs)
2. Ambiguous requirements (unmeasurable adjectives, unresolved placeholders)
3. Coverage gaps (requirements without mapped tasks)
4. Inconsistencies (terminology mismatches, file path conflicts)
5. Domain conflicts (against existing codebase patterns)

Return JSON matching the output_schema.
```

### verify (Phase 5)
```
You are a verification agent. Verify the implementation against the spec:
1. Scope check: compare git diff files against allowed-files.json
2. Spec compliance: for each FR-NNN, confirm implementation exists
3. Code quality: check for error handling, security issues, convention violations

Return JSON matching the output_schema.
```

### plan pre-validate (Phase 1 — generate_then_validate 전용)
```
You are a plan validation agent. Your job is NOT to generate plans,
but to review generated artifacts for completeness and consistency.

Review the following spec, plan, and tasks against the original concept.
Check for:
1. Coverage gaps (concept requirements without FR-NNN)
2. Scope creep (changes affecting shared infrastructure not acknowledged)
3. Ambiguous acceptance criteria (unmeasurable adjectives, unresolved placeholders)
4. Environment coverage (all stated environments have scenarios)
5. File path validity (do referenced files exist in the project?)

Return JSON with 4-Block structure + findings.
```

## 프롬프트 조합 규칙

1. agent card 읽기
2. **META part 생성** (project, branch, phase, attempt, workflow_id)
3. **ROLE part 생성** (agent_card.description 기반)
4. **RULES part 생성** (manifest.json context_providers → AGENTS.md/CLAUDE.md)
5. instruction part 생성 (description + skills → 자연어 지시)
6. **OUTPUT FORMAT 강화** (structured_result 스키마 + JSON-only 강제 문구)
7. required_artifacts 순회 → artifact 또는 file_ref part 추가
8. optional_context 순회 → context part 추가 (파일 존재 시)
9. execution_hints 설정 (provider-config.json에서)
10. 전체를 하나의 프롬프트 문자열로 flatten (워커 CLI가 JSON을 직접 파싱하지 않으므로)

## Flatten된 프롬프트 (실제 워커에게 전달되는 형태)

```
=== META ===
Project: sample-api
Branch: feature/my-feature
Phase: review (attempt 1/2)
Workflow ID: 2026-03-24-feature-x

=== ROLE ===
You are a review agent for the sample-api project.
Your role: Cross-validates spec/plan/tasks and performs domain review.

=== RULES ===
Follow these project rules strictly:

[AGENTS.md content or path]
[CLAUDE.md content or path]

=== INSTRUCTION ===
You are a code review agent. Cross-validate the following artifacts for:
1. Duplicate requirements...
2. Ambiguous requirements...
...

=== OUTPUT FORMAT ===
CRITICAL: You MUST respond with ONLY a valid JSON object.
No markdown fences, no explanation text, no preamble, no trailing text.
The response must start with { and end with }.

Your response MUST follow the 4-Block structure (결론/근거/리스크/실행안):
- "conclusion": Final verdict (PASS/FAIL + summary)
- "evidence": Supporting data for the conclusion
- "risks": Side effects, edge cases, or concerns
- "action_items": Recommended next steps

Schema:
{
  "conclusion": "PASS|FAIL — <summary>",
  "evidence": [{"id": "...", "detail": "..."}],
  "risks": [{"id": "...", "severity": "HIGH|MEDIUM|LOW", "detail": "..."}],
  "action_items": [{"id": "...", "action": "..."}],
  "findings": [...],
  "coverage": { "total_requirements": N, "mapped_requirements": N, ... }
}

=== ARTIFACTS ===

--- spec.md ---
<spec.md 전문>

--- plan.md ---
<plan.md 전문>

--- tasks.md ---
<tasks.md 전문>

=== END ===
```

## Format Correction Prompt (재시도용)

워커가 유효한 JSON을 반환하지 않았을 때 사용하는 교정 프롬프트.
오케스트레이터의 Step B3에서 파싱 실패 시 **1회** 재시도에 사용.

```
Your previous response could not be parsed as valid JSON.

IMPORTANT: Respond ONLY with a valid JSON object matching this exact schema.
No markdown fences (```), no explanation, no text before or after the JSON.
The response must start with { and end with }.

Required schema:
{output_schema}

Your previous response (first 500 chars for reference):
{truncated_response}
```

**재시도 방식**:
- Codex MCP: `mcp__codex__codex-reply(threadId, FORMAT_CORRECTION_PROMPT)` — 스레드 유지로 컨텍스트 보존
- Claude CLI: 새 호출 `claude --print --bare ... "FORMAT_CORRECTION_PROMPT"` — stateless이므로 스키마 + 이전 응답 포함 필수
- 재시도 성공 시: `provider_status: "format_retry"`로 기록
- 재시도 실패 시: fallback_chain 다음 프로바이더로 이동
