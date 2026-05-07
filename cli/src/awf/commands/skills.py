from __future__ import annotations

import argparse
import json
import sys

from awf.core.skills import discover_skills, skill_search_paths


def run_skills_list(args: argparse.Namespace) -> int:
    try:
        search_paths = skill_search_paths(args.repo_root)
        skills = discover_skills(args.repo_root)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = {
        "search_paths": [str(path) for path in search_paths],
        "skills": [
            {
                "name": skill.name,
                "description": skill.description,
                "path": str(skill.path),
                "source_dir": str(skill.source_dir),
            }
            for skill in skills
        ],
    }

    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    lines = ["skill_search_paths:"]
    for path in payload["search_paths"]:
        lines.append(f"  - {path}")
    lines.append("skills:")
    if not skills:
        lines.append("  - (none found)")
    else:
        for skill in skills:
            description = f" — {skill.description}" if skill.description else ""
            lines.append(f"  - {skill.name}{description}")
            lines.append(f"    path: {skill.path}")
    print("\n".join(lines))
    return 0
