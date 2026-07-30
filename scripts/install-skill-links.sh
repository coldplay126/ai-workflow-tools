#!/bin/sh
set -eu

usage() {
  printf 'usage: %s SOURCE_SKILL_DIR SKILL_ROOT [SKILL_ROOT ...]\n' "${0##*/}" >&2
}

if [ "$#" -lt 2 ]; then
  usage
  exit 2
fi

source_input=$1
shift

if ! source_dir=$(CDPATH= cd "$source_input" 2>/dev/null && pwd -P); then
  printf 'error: source skill directory does not exist: %s\n' "$source_input" >&2
  exit 1
fi

source_skill=$source_dir/SKILL.md
if [ ! -f "$source_skill" ]; then
  printf 'error: source skill is missing SKILL.md: %s\n' "$source_dir" >&2
  exit 1
fi

skill_name=${source_dir##*/}

for skill_root in "$@"; do
  mkdir -p "$skill_root"
  target=$skill_root/$skill_name

  if [ -L "$target" ]; then
    if [ "$(readlink "$target")" = "$source_dir" ]; then
      printf 'unchanged: %s\n' "$target"
      continue
    fi
    rm "$target"
    printf 'updated: %s -> %s\n' "$target" "$source_dir"
  elif [ -e "$target" ]; then
    printf 'preserved: existing file or directory at %s\n' "$target" >&2
    continue
  else
    printf 'installed: %s -> %s\n' "$target" "$source_dir"
  fi

  ln -s "$source_dir" "$target"
done
