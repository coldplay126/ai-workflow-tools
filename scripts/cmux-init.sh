#!/bin/bash
# cmux-agent 프로젝트 템플릿 적용
# 사용법: cmux-init.sh <template> [target-dir]
#   template: feature, bugfix, review
#   target-dir: 대상 프로젝트 (기본: 현재 디렉토리)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATES_DIR="$SCRIPT_DIR/../templates/cmux"

TEMPLATE="${1:-}"
TARGET="${2:-.}"

if [ -z "$TEMPLATE" ]; then
  echo "사용법: cmux-init.sh <template> [target-dir]"
  echo ""
  echo "템플릿:"
  for d in "$TEMPLATES_DIR"/*/; do
    name=$(basename "$d")
    workers=$(grep -o '"worker-[^"]*"' "$d/cmux-agent.json" 2>/dev/null | tr '\n' ' ' || echo "?")
    echo "  $name  — $workers"
  done
  exit 1
fi

SOURCE="$TEMPLATES_DIR/$TEMPLATE"
if [ ! -d "$SOURCE" ]; then
  echo "error: 템플릿 '$TEMPLATE'이 없습니다: $SOURCE"
  exit 1
fi

TARGET=$(cd "$TARGET" && pwd)
CUSTOM_DIR="$TARGET/.agent-custom"

# cmux-agent.json
if [ -f "$TARGET/cmux-agent.json" ]; then
  echo "  ⚠ cmux-agent.json 이미 존재 — 덮어쓰기"
fi
cp "$SOURCE/cmux-agent.json" "$TARGET/cmux-agent.json"
echo "  ✓ cmux-agent.json"

# .agent-custom/
mkdir -p "$CUSTOM_DIR"

# 템플릿 재적용 시 이전에 생성한 프로토콜 파일이 남지 않도록 정리한다.
find "$CUSTOM_DIR" -maxdepth 1 -type f \( -name 'ORCHESTRATOR*.md' -o -name 'WORKER-*.md' \) -delete

# 공통 orchestrator/worker 프로토콜을 복사한다.
cp "$TEMPLATES_DIR/ORCHESTRATOR-COMMON.md" "$CUSTOM_DIR/"
cp "$TEMPLATES_DIR/workers/WORKER-COMMON.md" "$CUSTOM_DIR/"

# 템플릿별 orchestrator 프로토콜을 복사한다.
for f in "$SOURCE/.agent-custom"/*.md; do
  cp "$f" "$CUSTOM_DIR/"
done

# cmux-agent.json에 정의된 worker별 프로토콜을 복사한다.
while IFS= read -r worker; do
  phase="${worker#worker-}"
  worker_file="WORKER-$(printf '%s' "$phase" | tr '[:lower:]' '[:upper:]').md"
  cp "$TEMPLATES_DIR/workers/$worker_file" "$CUSTOM_DIR/$worker_file"
done < <(grep -o '"worker-[^"]*"' "$SOURCE/cmux-agent.json" | tr -d '"' | sort -u)

custom_count=$(find "$CUSTOM_DIR" -maxdepth 1 -type f -name '*.md' | wc -l | tr -d ' ')
echo "  ✓ .agent-custom/ (${custom_count}개 파일)"

echo ""
echo "적용 완료: $TEMPLATE → $TARGET"
echo "실행: cd $TARGET && cmux-agent"
