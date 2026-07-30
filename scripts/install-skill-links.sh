#!/usr/bin/env bash
set -euo pipefail

SKILL_NAME="release-worktree-lifecycle"
DRY_RUN=false
JSON=false

usage() {
  printf 'usage: %s [--dry-run --json]\n' "${0##*/}" >&2
}

while (($#)); do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      ;;
    --json)
      JSON=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
  shift
done

if "$JSON" && ! "$DRY_RUN"; then
  printf 'error: --json requires --dry-run\n' >&2
  exit 2
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)"
CANONICAL_SKILL="$REPO_ROOT/claude/skills/$SKILL_NAME/SKILL.md"

if [[ ! -f "$CANONICAL_SKILL" || -L "$CANONICAL_SKILL" ]]; then
  printf 'error: canonical skill is missing or not a regular file: %s\n' "$CANONICAL_SKILL" >&2
  exit 1
fi

CANONICAL_SKILL="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$CANONICAL_SKILL")"
CLAUDE_ROOT="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
OMP_ROOT="${OMP_CONFIG_DIR:-${PI_CODING_AGENT_DIR:-$HOME/.omp/agent}}"
AGENTS_ROOT="${AGENTS_HOME:-${CODEX_HOME:-$HOME/.agents}}"

ROOTS=("$CLAUDE_ROOT/skills" "$OMP_ROOT/skills" "$AGENTS_ROOT/skills")
TARGETS=()
PLANNED_LINKS=()
NOOP_LINKS=()
CONFLICTS=()

relative_target() {
  python3 -c 'import os, sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' "$1" "$2"
}

resolved_target() {
  python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

for root in "${ROOTS[@]}"; do
  target="$root/$SKILL_NAME"
  TARGETS+=("$target")

  if [[ -L "$target" ]]; then
    link_value="$(readlink "$target")"
    if [[ "$link_value" != /* && -f "$target" && "$(resolved_target "$target")" == "$CANONICAL_SKILL" ]]; then
      NOOP_LINKS+=("$target")
    else
      CONFLICTS+=("$target")
    fi
  elif [[ -e "$target" ]]; then
    CONFLICTS+=("$target")
  else
    PLANNED_LINKS+=("$target")
  fi
done

if "$JSON"; then
  set +u
  python3 - "$CANONICAL_SKILL" "${PLANNED_LINKS[@]}" -- "${NOOP_LINKS[@]}" -- "${CONFLICTS[@]}" <<'PY'
import json
import sys

canonical = sys.argv[1]
sections = [[], [], []]
section = 0
for argument in sys.argv[2:]:
    if argument == "--":
        section += 1
    else:
        sections[section].append(argument)
print(json.dumps({
    "dry_run": True,
    "canonical_skill": canonical,
    "planned_links": sections[0],
    "unchanged_links": sections[1],
    "conflicts": sections[2],
}, sort_keys=True))
PY
  set -u
fi

if ((${#CONFLICTS[@]})); then
  for target in "${CONFLICTS[@]}"; do
    printf 'conflict: refusing to overwrite %s\n' "$target" >&2
  done
  exit 1
fi

if "$DRY_RUN"; then
  exit 0
fi

for index in "${!TARGETS[@]}"; do
  target="${TARGETS[$index]}"
  if [[ -L "$target" || -e "$target" ]]; then
    continue
  fi

  root="${ROOTS[$index]}"
  mkdir -p "$root"
  link_value="$(relative_target "$CANONICAL_SKILL" "$root")"
  ln -s "$link_value" "$target"

  if [[ ! -L "$target" || ! -f "$target" || "$(resolved_target "$target")" != "$CANONICAL_SKILL" ]]; then
    printf 'error: installed link did not resolve to the canonical skill: %s\n' "$target" >&2
    exit 1
  fi

  printf 'installed: %s -> %s\n' "$target" "$link_value"
done
