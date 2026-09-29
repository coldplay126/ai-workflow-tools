#!/bin/sh
set -eu

if [ "$#" -lt 2 ]; then
  printf 'usage: %s SOURCE_SKILL_DIR SKILL_ROOT [SKILL_ROOT ...]\n' "${0##*/}" >&2
  exit 2
fi

source_input=$1
shift
if ! source_dir=$(CDPATH= cd "$source_input" 2>/dev/null && pwd -P); then
  printf 'error: source skill directory does not exist: %s\n' "$source_input" >&2
  exit 1
fi
skill_name=${source_dir##*/}

for skill_root in "$@"; do
  target=$skill_root/$skill_name
  if [ -L "$target" ]; then
    link_target=$(readlink "$target")
    if [ "$link_target" = "$source_dir" ]; then
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
