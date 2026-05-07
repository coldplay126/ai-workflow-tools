#!/bin/bash
# cmux-agent 시작 후 커스텀 프로토콜을 .agent/에 복원하는 스크립트
# 사용법: cmux-apply-protocols.sh [target-dir]

set -euo pipefail

PROJECT_ROOT="${1:-.}"
PROJECT_ROOT="$(cd "$PROJECT_ROOT" && pwd)"
SOURCE="$PROJECT_ROOT/.agent-custom"
TARGET="$PROJECT_ROOT/.agent"

if [ ! -d "$SOURCE" ]; then
  echo "error: $SOURCE 디렉토리가 없습니다."
  exit 1
fi

mkdir -p "$TARGET"

for f in "$SOURCE"/*.md; do
  name=$(basename "$f")
  cp "$f" "$TARGET/$name"
  echo "  ✓ $name"
done

echo "프로토콜 복원 완료: $PROJECT_ROOT"
