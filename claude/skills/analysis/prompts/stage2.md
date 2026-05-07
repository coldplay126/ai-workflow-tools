Analyze the `{unit}` unit for service `{service}` and generate the required `.ai-context` outputs.

**모든 출력은 한국어로 작성하세요.** 코드 식별자(함수명, 변수명, 파일명 등)와 JSON 키는 영어 원문 그대로 유지합니다.

{existing_ai_context_section}

아래 Domain XML Bundle과 Stage 1 Memo를 기반으로 4개 파일을 생성하세요.
Stage 1 Memo의 File Analyses에 각 파일의 role, imports, summary가 이미 정리되어 있습니다.
이 정보를 신뢰하고 합성에 집중하세요.

Return exactly four sections using the following markers and no Markdown fences:

===FILE: api-spec.json===
===FILE: data-model.md===
===FILE: domain-overview.md===
===FILE: external-integration.md===

각 파일의 형식과 품질 기준은 아래를 따르세요.
