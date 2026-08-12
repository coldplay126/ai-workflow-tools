#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
OMP_AGENT_DIR="${OMP_AGENT_DIR:-$HOME/.omp/agent/agents}"
AGENTS_SKILLS_DIR="${AGENTS_SKILLS_DIR:-$HOME/.agents/skills}"
OMP_SKILLS_DIR="${OMP_SKILLS_DIR:-$HOME/.omp/agent/skills}"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv가 필요합니다: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi


echo "=== ai-workflow-tools 설치 ==="
echo ""

# 1. Skills 심링크
echo "[1/4] AWF CLI 설치 중..."
uv tool install --force --editable "$SCRIPT_DIR/cli"
AWF_BIN="$(uv tool dir --bin)/awf"
if [ ! -x "$AWF_BIN" ]; then
  echo "error: awf 실행 파일을 찾을 수 없습니다: $AWF_BIN" >&2
  exit 1
fi
echo "  ✓ $AWF_BIN"
if ! command -v awf >/dev/null 2>&1; then
  echo "  ! PATH 추가 필요: export PATH=\"$(uv tool dir --bin):\$PATH\""
fi

echo ""
echo "[2/4] Claude Skills 설치 중..."

SKILLS=(
  analysis
  multi-agent
  phase-approve
  phase-done
  phase-impl
  phase-plan
  phase-review
  phase-test
  phase-verify
  release-worktree-lifecycle
  wf
  wf-discovery
  wf-orchestrator
  wf-reset
  wf-status
)

runtime_names=(claude agent-skills omp)
runtime_roots=("$CLAUDE_DIR/skills" "$AGENTS_SKILLS_DIR" "$OMP_SKILLS_DIR")
install_blocked=0
for skill in "${SKILLS[@]}"; do
  skill_source="$SCRIPT_DIR/claude/skills/$skill"
  if [ "$skill" = "release-worktree-lifecycle" ]; then
    skill_source="$SCRIPT_DIR/cli/src/awf/resources/release-worktree-lifecycle"
  fi
  for index in "${!runtime_names[@]}"; do
    runtime="${runtime_names[$index]}"
    root="${runtime_roots[$index]}"
    if "$SCRIPT_DIR/scripts/install-skill-links.sh" \
      "$skill_source" \
      "$root"; then
      :
    else
      status=$?
      if [ "$status" -ne 3 ]; then
        exit "$status"
      fi
      printf 'runtime=%s skill=%s path=%s\n' "$runtime" "$skill" "$root/$skill" >&2
      install_blocked=1
    fi
  done
done

if [ "$install_blocked" -ne 0 ]; then
  printf 'AWF Skill installation is BLOCKED; inspect AWF_SKILL_INSTALL_RESULT lines above.\n' >&2
  exit 3
fi

# 1b. Agents 심링크
echo ""
echo "[3a/4] Claude Agents 설치 중..."
mkdir -p "$CLAUDE_DIR/agents"

for agent_file in "$SCRIPT_DIR/claude/agents"/*.md; do
  [ -f "$agent_file" ] || continue
  agent_name=$(basename "$agent_file")
  target="$CLAUDE_DIR/agents/$agent_name"

  if [ -L "$target" ]; then
    current=$(readlink "$target")
    if [ "$current" = "$agent_file" ]; then
      echo "  ✓ $agent_name (이미 설치됨)"
      continue
    fi
    echo "  ↻ $agent_name (심링크 업데이트)"
    rm "$target"
  elif [ -f "$target" ]; then
    echo "  ⚠ $agent_name: 기존 파일이 존재합니다. 교체합니다."
    rm "$target"
  fi

  ln -sf "$agent_file" "$target"
  echo "  ✓ $agent_name"
done

# 1c. OMP task agents 설치
echo ""
echo "[3b/4] OMP Agents 설치 중..."
"$AWF_BIN" agents sync-omp --repo-root "$SCRIPT_DIR" --force >/dev/null
mkdir -p "$OMP_AGENT_DIR"

for agent_file in "$SCRIPT_DIR/.omp/agents"/*.md; do
  [ -f "$agent_file" ] || continue
  agent_name=$(basename "$agent_file")
  target="$OMP_AGENT_DIR/$agent_name"

  if [ -L "$target" ]; then
    current=$(readlink "$target")
    if [ "$current" = "$agent_file" ]; then
      echo "  ✓ $agent_name (이미 설치됨)"
      continue
    fi
    rm "$target"
  elif [ -f "$target" ]; then
    echo "  ⚠ $agent_name: 기존 파일을 보존하고 건너뜁니다."
    continue
  fi

  ln -sf "$agent_file" "$target"
  echo "  ✓ $agent_name"
done

# 2. Commands → Skills 마이그레이션 안내
echo ""
echo "[4/4] 기존 Commands 확인 중..."
legacy_command_found=0
if [ -d "$CLAUDE_DIR/commands" ]; then
  for legacy_command in "$CLAUDE_DIR"/commands/wf*.md "$CLAUDE_DIR"/commands/analysis.md; do
    if [ -e "$legacy_command" ] || [ -L "$legacy_command" ]; then
      legacy_command_found=1
      break
    fi
  done
fi

if [ "$legacy_command_found" -eq 1 ]; then
  echo "  ⚠ ~/.claude/commands/ 에 기존 AWF command 파일이 있습니다."
  echo "    Commands는 deprecated되었습니다. Skills로 마이그레이션되었으므로"
  echo "    기존 command 심링크를 삭제해도 됩니다:"
  echo "    rm ~/.claude/commands/wf*.md ~/.claude/commands/analysis.md"
else
  echo "  ✓ 마이그레이션 불필요 (기존 AWF commands 없음)"
fi

# 3. 안내
echo ""
echo "추가 설정 안내"
echo ""
echo "  ── CLAUDE.md 섹션 추가 (선택) ──"
echo "  아래 파일의 내용을 ~/.claude/CLAUDE.md에 추가하세요:"
echo "    → $SCRIPT_DIR/snippets/claude-md-multi-agent.md"
echo "    → $SCRIPT_DIR/snippets/claude-md-wf-pipeline.md"
echo ""
echo "  ── Codex MCP 설치 (선택) ──"
echo "  WF Dual Mode를 사용하려면:"
echo "    claude mcp add --scope user codex -- codex mcp-server"
echo ""
echo "=== 설치 완료 ==="
echo ""
echo "검증: $AWF_BIN --help"
echo "시작: 프로젝트에서 $AWF_BIN ready --repo-root ."
