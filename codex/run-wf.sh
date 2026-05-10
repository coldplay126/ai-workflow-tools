#!/usr/bin/env bash

set -euo pipefail

ROOT="${PWD}"
TOOL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WF_DIR="${ROOT}/.workflow"
STATE_FILE="${WF_DIR}/state.json"
PROVIDER_FILE="${WF_DIR}/provider-config.json"

usage() {
  cat <<'EOF'
Usage:
  run-wf.sh status
  run-wf.sh dispatch
  run-wf.sh preflight [phase] [provider]
  run-wf.sh phase <name>
  run-wf.sh prompt [phase] [provider]
  run-wf.sh run-secondary [phase] [provider]
  run-wf.sh apply-secondary <phase> [result-file]

Notes:
  - This runner is intended for Codex host execution.
  - It reads .workflow state, builds a delegated prompt, and can invoke CLI providers.
  - Inline phase execution remains host-model specific.
EOF
}

require_file() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    echo "Missing required file: $path" >&2
    exit 1
  fi
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

run_awf() {
  if command_exists uv && [[ -f "${TOOL_ROOT}/cli/pyproject.toml" ]]; then
    PYTHONPATH="${TOOL_ROOT}/cli/src" uv run --project "${TOOL_ROOT}/cli" python -m awf.cli "$@"
  elif command_exists awf; then
    awf "$@"
  else
    echo "Missing awf CLI. Install awf or run from an ai-workflow-tools checkout with uv available." >&2
    return 127
  fi
}

run_awf_ready_gate() {
  local gate="$1"
  local allow_dry_run_only="${2:-false}"
  local tmp_file rc
  tmp_file="$(mktemp)"

  set +e
  run_awf ready --gate "$gate" --repo-root "$ROOT" --json > "$tmp_file"
  rc=$?
  set -e

  if [[ "$rc" -eq 0 ]]; then
    rm -f "$tmp_file"
    return 0
  fi
  if [[ "$rc" -eq 10 && "$allow_dry_run_only" == "true" ]]; then
    echo "ready_gate: dry_run_only (${gate})" >&2
    rm -f "$tmp_file"
    return 0
  fi

  cat "$tmp_file" >&2
  rm -f "$tmp_file"
  exit "$rc"
}

run_awf_wf_next_dry_run() {
  local phase="${1:-}"
  local provider="${2:-}"
  local print_json="${3:-false}"
  local tmp_file rc
  tmp_file="$(mktemp)"

  local args=(wf next --repo-root "$ROOT" --dry-run --output-format json)
  if [[ -n "$phase" ]]; then
    args+=(--phase "$phase")
  fi
  if [[ -n "$provider" ]]; then
    args+=(--provider "$provider")
  fi

  set +e
  run_awf "${args[@]}" > "$tmp_file"
  rc=$?
  set -e

  if [[ "$rc" -ne 0 ]]; then
    cat "$tmp_file" >&2
    rm -f "$tmp_file"
    exit "$rc"
  fi

  if ! node - "$tmp_file" "$phase" <<'NODE'
const fs = require("fs");
const file = process.argv[2];
const expectedPhase = process.argv[3] || "";
let data;
try {
  data = JSON.parse(fs.readFileSync(file, "utf8"));
} catch (error) {
  console.error(`Invalid awf wf next dry-run JSON: ${error.message}`);
  process.exit(2);
}

const missing = ["phase", "provider", "prompt"].filter((key) => {
  return typeof data[key] !== "string" || data[key].length === 0;
});

if (missing.length > 0) {
  console.error(`awf wf next dry-run JSON missing required fields: ${missing.join(", ")}`);
  process.exit(2);
}

if (expectedPhase && data.phase !== expectedPhase) {
  console.error(`awf wf next dry-run phase mismatch: expected ${expectedPhase}, got ${data.phase}`);
  process.exit(2);
}
NODE
  then
    cat "$tmp_file" >&2
    rm -f "$tmp_file"
    exit 2
  fi

  if [[ "$print_json" == "true" ]]; then
    cat "$tmp_file"
  fi
  rm -f "$tmp_file"
}

json_get() {
  local expr="$1"
  node -e '
    const fs = require("fs");
    const file = process.argv[1];
    const expr = process.argv[2];
    const data = JSON.parse(fs.readFileSync(file, "utf8"));
    const value = expr.split(".").filter(Boolean).reduce((acc, key) => acc?.[key], data);
    if (value === undefined || value === null) process.exit(2);
    if (typeof value === "object") {
      console.log(JSON.stringify(value));
    } else {
      console.log(String(value));
    }
  ' "$STATE_FILE" "$expr"
}

provider_get() {
  local expr="$1"
  node -e '
    const fs = require("fs");
    const file = process.argv[1];
    const expr = process.argv[2];
    const data = JSON.parse(fs.readFileSync(file, "utf8"));
    const value = expr.split(".").filter(Boolean).reduce((acc, key) => acc?.[key], data);
    if (value === undefined || value === null) process.exit(2);
    if (typeof value === "object") {
      console.log(JSON.stringify(value));
    } else {
      console.log(String(value));
    }
  ' "$PROVIDER_FILE" "$expr"
}

current_phase() {
  json_get currentPhase
}

resolve_phase() {
  local requested="${1:-}"
  if [[ -n "$requested" ]]; then
    echo "$requested"
  else
    current_phase
  fi
}

resolve_provider() {
  local phase="$1"
  local requested="${2:-}"
  if [[ -n "$requested" ]]; then
    echo "$requested"
    return
  fi

  provider_get "phase_routing.${phase}.secondary" 2>/dev/null || true
}

default_result_file() {
  local phase="$1"
  local provider="$2"
  echo "${WF_DIR}/tmp/result-${phase}-${provider//:/_}.json"
}

build_prompt_file() {
  local phase="$1"
  local provider="$2"
  require_file "$STATE_FILE"
  require_file "$PROVIDER_FILE"

  mkdir -p "${WF_DIR}/tmp"
  local prompt_file="${WF_DIR}/tmp/prompt-${phase}-${provider//:/_}.txt"

  node - "$ROOT" "$WF_DIR" "$STATE_FILE" "$PROVIDER_FILE" "$phase" "$provider" "$prompt_file" <<'NODE'
const fs = require("fs");
const path = require("path");

const [root, wfDir, stateFile, providerFile, phase, providerName, promptFile] = process.argv.slice(2);
const state = JSON.parse(fs.readFileSync(stateFile, "utf8"));
const providerConfig = JSON.parse(fs.readFileSync(providerFile, "utf8"));
const agentCardPath = path.join(wfDir, "agent-cards", `${phase}.json`);

if (!fs.existsSync(agentCardPath)) {
  console.error(`Missing agent card: ${agentCardPath}`);
  process.exit(1);
}

const agentCard = JSON.parse(fs.readFileSync(agentCardPath, "utf8"));
const provider = providerConfig.providers?.[providerName];
if (!provider) {
  console.error(`Unknown provider: ${providerName}`);
  process.exit(1);
}

function readIfExists(filePath) {
  return fs.existsSync(filePath) ? fs.readFileSync(filePath, "utf8") : null;
}

function resolveWorkflowPath(relPath) {
  if (!relPath) return null;
  return path.join(wfDir, relPath);
}

function artifactBlock(item, fileAccess) {
  const relPath = item.path;
  if (!relPath) return null;
  const fullPath = resolveWorkflowPath(relPath);
  if (!fs.existsSync(fullPath)) return null;

  if (fileAccess) {
    return `--- ${item.key} (${relPath}) ---\n[FILE_REF] ${relPath}`;
  }

  const content = fs.readFileSync(fullPath, "utf8");
  return `--- ${item.key} (${relPath}) ---\n${content}`;
}

const retries = state.phases?.[phase]?.retries ?? 0;
const retryMax = agentCard.retry?.max ?? 1;
const fileAccess = provider.file_access === true;
const requiredArtifacts = agentCard.input?.required_artifacts ?? [];
const optionalContext = agentCard.input?.optional_context ?? [];
const schema = {
  conclusion: "PASS|FAIL — <summary>",
  evidence: [{ id: "string", detail: "string" }],
  risks: [{ id: "string", severity: "HIGH|MEDIUM|LOW", detail: "string" }],
  action_items: [{ id: "string", action: "string" }],
  ...(agentCard.output?.structured_result ?? {})
};

const rulesFiles = ["AGENTS.md", "CLAUDE.md", "codex/AGENTS.md"]
  .map((name) => path.join(root, name))
  .filter((p) => fs.existsSync(p));

const rulesText = rulesFiles.length === 0
  ? "No project rule files found."
  : fileAccess
    ? rulesFiles.map((p) => `Read and follow: ${path.relative(root, p)}`).join("\n")
    : rulesFiles.map((p) => `--- ${path.basename(p)} ---\n${fs.readFileSync(p, "utf8")}`).join("\n\n");

const artifactParts = requiredArtifacts
  .map((item) => artifactBlock(item, fileAccess))
  .filter(Boolean)
  .join("\n\n");

const contextParts = optionalContext
  .map((item) => {
    if (!item.path) return item.description ? `--- ${item.key} ---\n${item.description}` : null;
    const fullPath = resolveWorkflowPath(item.path);
    if (!fs.existsSync(fullPath)) return null;
    if (fileAccess) {
      return `--- ${item.key} (${item.path}) ---\n[FILE_REF] ${item.path}`;
    }
    return `--- ${item.key} (${item.path}) ---\n${fs.readFileSync(fullPath, "utf8")}`;
  })
  .filter(Boolean)
  .join("\n\n");

const instructionByPhase = {
  review: [
    "Cross-validate spec, plan, and tasks for consistency and coverage.",
    "Focus on duplicate requirements, ambiguity, coverage gaps, inconsistencies, and domain conflicts.",
    "Return ONLY valid JSON matching the schema."
  ],
  verify: [
    "Verify implementation scope and spec compliance.",
    "Check allowed-files boundaries, missing requirements, code quality, and architecture issues.",
    "Return ONLY valid JSON matching the schema."
  ]
};

const instruction = instructionByPhase[phase] ?? [
  agentCard.description,
  "Use the provided workflow artifacts as the single source of truth.",
  "Return ONLY valid JSON matching the schema."
];

const lines = [
  "=== META ===",
  `Project: ${state.repo}`,
  `Branch: ${state.branch}`,
  `Phase: ${phase} (attempt ${retries + 1}/${retryMax})`,
  `Workflow ID: ${state.id}`,
  "",
  "=== ROLE ===",
  `You are a ${phase} agent for the ${state.repo} project.`,
  `Your role: ${agentCard.description}`,
  "",
  "=== RULES ===",
  rulesText,
  "",
  "=== INSTRUCTION ===",
  ...instruction.map((line) => `- ${line}`),
  "",
  "=== OUTPUT FORMAT ===",
  "CRITICAL: You MUST respond with ONLY a valid JSON object.",
  "No markdown fences, no explanation text, no preamble, no trailing text.",
  'The response must start with { and end with }.',
  "",
  "Required schema:",
  JSON.stringify(schema, null, 2),
  "",
  "=== ARTIFACTS ===",
  artifactParts || "(none)",
];

if (contextParts) {
  lines.push("", "=== CONTEXT ===", contextParts);
}

lines.push("", "=== END ===", "");
fs.writeFileSync(promptFile, lines.join("\n"));
console.log(promptFile);
NODE
}

run_cli_provider() {
  local provider="$1"
  local prompt_file="$2"
  local output_file="$3"

  local provider_json command_template
  provider_json="$(provider_get "providers.${provider}")"
  command_template="$(provider_get "providers.${provider}.command")"

  if [[ -z "$command_template" ]]; then
    echo "Provider does not define a CLI command: $provider" >&2
    exit 1
  fi

  local budget
  budget="$(provider_get "providers.${provider}.budget_usd" 2>/dev/null || true)"
  command_template="${command_template//\{budget\}/${budget:-0.50}}"

  if [[ "$command_template" != claude* ]]; then
    echo "Unsupported CLI provider command: $command_template" >&2
    exit 1
  fi

  if ! command_exists claude; then
    echo "Missing required CLI: claude" >&2
    exit 1
  fi

  local prompt_text
  prompt_text="$(cat "$prompt_file")"
  claude_output="$(CLAUDE_PROMPT="$prompt_text" bash -lc "$command_template \"\$CLAUDE_PROMPT\"")"
  printf '%s\n' "$claude_output" > "$output_file"

  node - "$output_file" <<'NODE'
const fs = require("fs");
const file = process.argv[2];
const raw = fs.readFileSync(file, "utf8").trim();
let parsed;
try {
  parsed = JSON.parse(raw);
} catch (_) {
  process.exit(0);
}

if (parsed && parsed.is_error) {
  const message = parsed.result || parsed.error || "Unknown Claude CLI error";
  console.error(`Claude CLI returned an error: ${message}`);
  process.exit(9);
}
NODE

  echo "$output_file"
}

status_cmd() {
  run_awf_ready_gate workflow-run true
  require_file "$STATE_FILE"
  echo "repo: $(json_get repo)"
  echo "phase: $(json_get currentPhase)"
  if [[ -f "$PROVIDER_FILE" ]]; then
    echo "provider-config: present"
  else
    echo "provider-config: missing"
  fi
}

dispatch_cmd() {
  run_awf_ready_gate workflow-run true
  require_file "$STATE_FILE"
  require_file "$PROVIDER_FILE"

  local phase mode secondary fallback
  phase="$(json_get currentPhase)"
  mode="$(provider_get "phase_routing.${phase}.mode" || true)"
  secondary="$(provider_get "phase_routing.${phase}.secondary" || true)"
  fallback="$(provider_get "fallback_chain" || true)"
  run_awf_wf_next_dry_run "$phase" "" false

  echo "dispatch"
  echo "  phase: ${phase}"
  echo "  mode: ${mode:-inline}"
  echo "  secondary: ${secondary:-none}"
  echo "  fallback_chain: ${fallback:-[]}"
  echo
  echo "Next step:"
  echo "  Build a phase prompt from .workflow artifacts and execute with Codex as host."
  echo "  Example: ./codex/run-wf.sh prompt ${phase} ${secondary:-claude:sonnet}"
}

preflight_cmd() {
  run_awf_ready_gate workflow-run true
  require_file "$STATE_FILE"
  require_file "$PROVIDER_FILE"

  local phase provider
  phase="$(resolve_phase "${1:-}")"
  provider="$(resolve_provider "$phase" "${2:-}")"
  run_awf_wf_next_dry_run "$phase" "$provider" true
}

phase_cmd() {
  run_awf_ready_gate workflow-run true
  local requested_phase="${1:-}"
  if [[ -z "$requested_phase" ]]; then
    echo "Missing phase name" >&2
    exit 1
  fi

  require_file "$PROVIDER_FILE"
  local mode secondary
  mode="$(provider_get "phase_routing.${requested_phase}.mode" || true)"
  secondary="$(provider_get "phase_routing.${requested_phase}.secondary" || true)"
  run_awf_wf_next_dry_run "$requested_phase" "" false

  echo "phase: ${requested_phase}"
  echo "mode: ${mode:-inline}"
  echo "secondary: ${secondary:-none}"
  echo "artifacts: ${WF_DIR}/artifacts"
  echo "agent-card: ${WF_DIR}/agent-cards/${requested_phase}.json"
}

prompt_cmd() {
  run_awf_ready_gate workflow-run true
  local phase provider prompt_file
  phase="$(resolve_phase "${1:-}")"
  provider="$(resolve_provider "$phase" "${2:-}")"

  if [[ -z "$provider" ]]; then
    echo "No provider resolved for phase: $phase" >&2
    exit 1
  fi

  run_awf_wf_next_dry_run "$phase" "$provider" false
  prompt_file="$(build_prompt_file "$phase" "$provider")"
  echo "prompt: $prompt_file"
}

run_secondary_cmd() {
  run_awf_ready_gate workflow-run false
  local phase provider prompt_file output_file provider_type
  phase="$(resolve_phase "${1:-}")"
  provider="$(resolve_provider "$phase" "${2:-}")"

  if [[ -z "$provider" ]]; then
    echo "No provider resolved for phase: $phase" >&2
    exit 1
  fi

  provider_type="$(provider_get "providers.${provider}.type")"
  run_awf_wf_next_dry_run "$phase" "$provider" false
  prompt_file="$(build_prompt_file "$phase" "$provider")"
  mkdir -p "${WF_DIR}/tmp"
  output_file="${WF_DIR}/tmp/result-${phase}-${provider//:/_}.json"

  case "$provider_type" in
    cli)
      run_cli_provider "$provider" "$prompt_file" "$output_file" >/dev/null
      ;;
    *)
      echo "Provider type is not executable by this runner: ${provider_type}" >&2
      echo "prompt available at: $prompt_file" >&2
      exit 1
      ;;
  esac

  echo "prompt: $prompt_file"
  echo "result: $output_file"
}

apply_secondary_cmd() {
  run_awf_ready_gate workflow-run false
  local phase result_file provider
  phase="$(resolve_phase "${1:-}")"
  provider="$(resolve_provider "$phase" "")"
  result_file="${2:-$(default_result_file "$phase" "${provider:-claude:sonnet}")}"

  require_file "$result_file"
  run_awf_wf_next_dry_run "$phase" "$provider" false
  mkdir -p "${WF_DIR}/tmp"

  node - "$ROOT" "$WF_DIR" "$phase" "$result_file" <<'NODE'
const fs = require("fs");
const path = require("path");

const [root, wfDir, phase, resultFile] = process.argv.slice(2);
const outputPathByPhase = {
  review: path.join(wfDir, "artifacts", "review-report.md"),
  verify: path.join(wfDir, "artifacts", "verification-report.md")
};

function loadJsonFromPossiblyWrappedText(filePath) {
  const raw = fs.readFileSync(filePath, "utf8").trim();
  try {
    return JSON.parse(raw);
  } catch (_) {
    const start = raw.indexOf("{");
    const end = raw.lastIndexOf("}");
    if (start === -1 || end === -1 || end <= start) {
      throw new Error(`Unable to locate JSON object in ${filePath}`);
    }
    return JSON.parse(raw.slice(start, end + 1));
  }
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function renderReview(data) {
  const findings = asArray(data.findings);
  const coverage = data.coverage || {};
  const critical = findings.filter((f) => f.severity === "CRITICAL").length;
  const high = findings.filter((f) => f.severity === "HIGH").length;
  const medium = findings.filter((f) => f.severity === "MEDIUM").length;
  const low = findings.filter((f) => f.severity === "LOW").length;
  const pass = critical === 0 && Number(coverage.percentage || 0) >= 80;

  const lines = [
    "# Review Report",
    "",
    "## Summary",
    `- Conclusion: ${data.conclusion || (pass ? "PASS" : "FAIL")}`,
    `- Coverage: ${coverage.percentage ?? "N/A"}%`,
    `- CRITICAL: ${critical} | HIGH: ${high} | MEDIUM: ${medium} | LOW: ${low}`,
    `- Gate G2: ${pass ? "PASS" : "FAIL"}`,
    "",
    "## Findings"
  ];

  if (findings.length === 0) {
    lines.push("", "No findings reported.");
  } else {
    lines.push("", "| ID | Category | Severity | Location | Summary | Recommendation |", "|----|----------|----------|----------|---------|----------------|");
    for (const finding of findings) {
      lines.push(`| ${finding.id || "-"} | ${finding.category || "-"} | ${finding.severity || "-"} | ${(finding.locations || []).join("<br>") || "-"} | ${finding.summary || "-"} | ${finding.recommendation || "-"} |`);
    }
  }

  lines.push(
    "",
    "## Metrics",
    `- Total Requirements: ${coverage.total_requirements ?? "N/A"}`,
    `- Mapped Requirements: ${coverage.mapped_requirements ?? "N/A"}`,
    `- Coverage %: ${coverage.percentage ?? "N/A"}`,
    `- Critical Issues: ${critical}`,
    "",
    "## Evidence",
    asArray(data.evidence).map((e) => `- ${e.id || "evidence"}: ${e.detail || JSON.stringify(e)}`).join("\n") || "- None",
    "",
    "## Risks",
    asArray(data.risks).map((r) => `- ${r.id || "risk"} [${r.severity || "N/A"}]: ${r.detail || JSON.stringify(r)}`).join("\n") || "- None",
    "",
    "## Action Items",
    asArray(data.action_items).map((a) => `- ${a.id || "action"}: ${a.action || JSON.stringify(a)}`).join("\n") || "- None",
    ""
  );

  return { markdown: lines.join("\n"), pass };
}

function renderVerify(data) {
  const scope = data.scope || {};
  const compliance = data.compliance || {};
  const quality = data.quality || {};
  const pass =
    Number(scope.violations || 0) === 0 &&
    Number(compliance.fail || 0) === 0 &&
    Number(compliance.percentage || 0) >= 90 &&
    Number(quality.critical || 0) === 0;

  const lines = [
    "# Verification Report",
    "",
    "## Summary",
    `- Conclusion: ${data.conclusion || (pass ? "PASS" : "FAIL")}`,
    `- Scope: ${scope.changed_files ?? "N/A"} changed / ${scope.planned_files ?? "N/A"} planned, ${scope.violations ?? "N/A"} violations`,
    `- Compliance: ${compliance.pass ?? "N/A"}/${compliance.total_requirements ?? "N/A"} (${compliance.percentage ?? "N/A"}%)`,
    `- Quality: critical=${quality.critical ?? "N/A"}, high=${quality.high ?? "N/A"}, medium=${quality.medium ?? "N/A"}, low=${quality.low ?? "N/A"}`,
    `- Gate G5: ${pass ? "PASS" : "FAIL"}`,
    "",
    "## Scope",
    `- Changed files: ${scope.changed_files ?? "N/A"}`,
    `- Planned files: ${scope.planned_files ?? "N/A"}`,
    `- Violations: ${scope.violations ?? "N/A"}`,
    `- Violation files: ${(scope.violation_files || []).join(", ") || "None"}`,
    "",
    "## Compliance",
    `- Total requirements: ${compliance.total_requirements ?? "N/A"}`,
    `- Pass: ${compliance.pass ?? "N/A"}`,
    `- Warn: ${compliance.warn ?? "N/A"}`,
    `- Fail: ${compliance.fail ?? "N/A"}`,
    `- Failed requirements: ${(compliance.failed_requirements || []).join(", ") || "None"}`,
    "",
    "## Quality Issues"
  ];

  const issues = asArray(quality.issues);
  if (issues.length === 0) {
    lines.push("", "No quality issues reported.");
  } else {
    lines.push("", "| ID | Severity | File | Summary |", "|----|----------|------|---------|");
    for (const issue of issues) {
      lines.push(`| ${issue.id || "-"} | ${issue.severity || "-"} | ${issue.file || "-"} | ${issue.summary || "-"} |`);
    }
  }

  lines.push(
    "",
    "## Evidence",
    asArray(data.evidence).map((e) => `- ${e.id || "evidence"}: ${e.detail || JSON.stringify(e)}`).join("\n") || "- None",
    "",
    "## Risks",
    asArray(data.risks).map((r) => `- ${r.id || "risk"} [${r.severity || "N/A"}]: ${r.detail || JSON.stringify(r)}`).join("\n") || "- None",
    "",
    "## Action Items",
    asArray(data.action_items).map((a) => `- ${a.id || "action"}: ${a.action || JSON.stringify(a)}`).join("\n") || "- None",
    ""
  );

  return { markdown: lines.join("\n"), pass };
}

const parsed = loadJsonFromPossiblyWrappedText(resultFile);
let rendered;
if (phase === "review") {
  rendered = renderReview(parsed);
} else if (phase === "verify") {
  rendered = renderVerify(parsed);
} else {
  throw new Error(`apply-secondary is currently supported for review/verify only: ${phase}`);
}

const outputPath = outputPathByPhase[phase];
fs.writeFileSync(outputPath, rendered.markdown);

const summary = {
  phase,
  result_file: resultFile,
  artifact: outputPath,
  gate: phase === "review" ? "G2" : "G5",
  gate_pass: rendered.pass
};

console.log(JSON.stringify(summary, null, 2));
NODE
}

cmd="${1:-}"
case "$cmd" in
  status)
    status_cmd
    ;;
  dispatch)
    dispatch_cmd
    ;;
  preflight)
    shift
    preflight_cmd "${1:-}" "${2:-}"
    ;;
  phase)
    shift
    phase_cmd "${1:-}"
    ;;
  prompt)
    shift
    prompt_cmd "${1:-}" "${2:-}"
    ;;
  run-secondary)
    shift
    run_secondary_cmd "${1:-}" "${2:-}"
    ;;
  apply-secondary)
    shift
    apply_secondary_cmd "${1:-}" "${2:-}"
    ;;
  *)
    usage
    exit 1
    ;;
esac
