Analyze this file and return a JSON object.
Return ONLY valid JSON, no markdown fences, no explanation.

{xml_bundle}

Return:
{
  "path": "{path}",
  "role": "<router|controller|service|dao|entity|model|util|test|config|middleware|component|module|other>",
  "imports": ["<imported module/class names>"],
  "exports": ["<exported function/class names>"],
  "summary": "<1-2 sentence description>",
  "dependencies": ["<relative paths of dependencies>"],
  "complexity": "<low|medium|high>"
}
