#!/bin/sh
set -eu

if [ "$#" -lt 2 ]; then
  printf 'usage: %s SOURCE_SKILL_DIR SKILL_ROOT [SKILL_ROOT ...]\n' "${0##*/}" >&2
  exit 2
fi

source_input=$1
shift
skill_name=${source_input##*/}
if ! source_dir=$(CDPATH= cd "$source_input" 2>/dev/null && pwd -P); then
  if source_parent=$(CDPATH= cd "$(dirname "$source_input")" 2>/dev/null && pwd -P); then
    source_dir=$source_parent/$skill_name
  else
    repo_root=$(CDPATH= cd "$(dirname "$0")/.." && pwd -P)
    if [ "$source_input" != "$repo_root/claude/skills/$skill_name" ]; then
      printf 'warning: source skill directory does not exist; skipping: %s\n' "$source_input" >&2
      exit 0
    fi
    source_dir=$repo_root/claude/skills/$skill_name
  fi
  printf 'warning: source skill directory does not exist; checking owned link to %s\n' "$source_dir" >&2
else
  skill_name=${source_dir##*/}
fi

for skill_root in "$@"; do
  target=$skill_root/$skill_name
  if [ -L "$target" ]; then
    link_target=$(readlink "$target")
    owned=0
    if [ "$link_target" = "$source_dir" ]; then
      owned=1
    elif resolved=$(CDPATH= cd -P "$target" 2>/dev/null && pwd -P) &&
      [ "$resolved" = "$source_dir" ]; then
      owned=1
    fi
    if [ "$owned" -eq 1 ]; then
      rm "$target"
      printf 'removed: %s\n' "$target"
    else
      printf 'kept: %s (foreign link -> %s)\n' "$target" "$link_target"
    fi
  elif [ -e "$target" ]; then
    printf 'kept: %s (user_owned)\n' "$target"
  else
    printf 'absent: %s\n' "$target"
  fi
done
