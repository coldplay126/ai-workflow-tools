#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$HOME/.claude"

echo "=== ai-workflow-tools 설치 ==="
echo ""

# 1. Skills 심링크
echo "[1/3] Skills 설치 중..."
mkdir -p "$CLAUDE_DIR/skills"

SKILLS=(
  analysis
  wf-orchestrator phase-plan phase-review phase-approve phase-impl
  phase-verify phase-test phase-done
  wf-discovery wf-status wf-reset multi-agent
)

for skill in "${SKILLS[@]}"; do
  target="$CLAUDE_DIR/skills/$skill"
  source="$SCRIPT_DIR/claude/skills/$skill"

  if [ -L "$target" ]; then
    current=$(readlink "$target")
    if [ "$current" = "$source" ]; then
      echo "  ✓ $skill (이미 설치됨)"
      continue
    fi
    echo "  ↻ $skill (심링크 업데이트)"
    rm "$target"
  elif [ -d "$target" ]; then
    echo "  ⚠ $skill: 기존 디렉토리가 존재합니다."
    read -p "    교체하시겠습니까? (y/N) " -r
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
      echo "  → 건너뜀"
      continue
    fi
    rm -rf "$target"
  fi

  ln -sf "$source" "$target"
  echo "  ✓ $skill"
done

# 1b. Agents 심링크
echo ""
echo "[1b/3] Agents 설치 중..."
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

# 2. Commands → Skills 마이그레이션 안내
echo ""
echo "[2/3] Commands 확인 중..."
if [ -d "$CLAUDE_DIR/commands" ] && ls "$CLAUDE_DIR/commands"/wf*.md "$CLAUDE_DIR/commands"/analysis.md 2>/dev/null | head -1 > /dev/null 2>&1; then
  echo "  ⚠ ~/.claude/commands/ 에 기존 command 파일이 있습니다."
  echo "    Commands는 deprecated되었습니다. Skills로 마이그레이션되었으므로"
  echo "    기존 command 심링크를 삭제해도 됩니다:"
  echo "    rm ~/.claude/commands/wf*.md ~/.claude/commands/analysis.md"
else
  echo "  ✓ 마이그레이션 불필요 (기존 commands 없음)"
fi

# 3. 안내
echo ""
echo "[3/3] 추가 설정 안내"
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
echo "검증: Claude Code에서 /wf-status 또는 /analysis 를 실행해 보세요."
