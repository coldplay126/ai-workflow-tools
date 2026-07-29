## Output Format

반드시 JSON으로 결과를 반환하세요:
{"conclusion":"PASS|FAIL","findings":[{"severity":"CRITICAL|HIGH|MAJOR|MEDIUM|LOW|INFO","category":"...","location":"...","description":"...","suggestion":"..."}],"evidence":[...],"risks":[...],"action_items":[...]}

Phase별 gate metric도 JSON 최상위에 포함하세요:
- review: `"coverage":{"total_requirements":int,"mapped_requirements":int,"percentage":number,"gaps":[string]}`
- verify: `"scope":{"violations":int},"compliance":{"fail":int,"percentage":number},"quality":{"critical":int}`

확인하지 못한 metric을 성공값으로 추정하지 마세요. 값이 불완전하면 `percentage: 0`과
구체적인 `risks` 항목을 반환하여 gate가 fail-closed 하도록 하세요.
