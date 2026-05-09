from __future__ import annotations

from pathlib import Path
from typing import Any

from awf.core.config import load_awf_config, resolve_runtime_paths
from awf.core.readiness import collect_doctor_report
from awf.core.scanner import scan_repo, scan_result_to_dict
from awf.core.skills import discover_skills


_PROJECT_MARKERS = ("pyproject.toml", "package.json", "go.mod", "Cargo.toml", "composer.json")


AUTOMATION_LEVELS = {
    0: "inspect only",
    1: "prompt/artifact generation",
    2: "provider execution",
    3: "workflow file edits",
    4: "PR/merge automation",
}


def _detect_subprojects(repo_root: Path) -> list[dict[str, str]]:
    subprojects: list[dict[str, str]] = []
    try:
        children = sorted(p for p in repo_root.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return subprojects
    for child in children:
        for marker in _PROJECT_MARKERS:
            if (child / marker).is_file():
                subprojects.append({
                    "name": child.name,
                    "path": str(child.relative_to(repo_root)),
                    "marker": marker,
                })
                break
    return subprojects


def _provider_status(doctor: dict[str, Any]) -> dict[str, Any]:
    default_provider = str(doctor.get("default_provider") or "")
    providers = doctor.get("providers", []) or []
    entry = next((p for p in providers if str(p.get("provider")) == default_provider), None)
    if entry is None:
        return {
            "status": "blocked",
            "provider": default_provider,
            "reason": "default provider is not present in readiness report",
        }

    installed = entry.get("installed", {}) or {}
    configured = entry.get("configured", {}) or {}
    installed_status = str(installed.get("status") or "fail")
    configured_status = str(configured.get("status") or "fail")
    if installed_status != "ok":
        return {
            "status": "blocked",
            "provider": default_provider,
            "reason": str(installed.get("detail") or "provider command/package unavailable"),
            "installed": installed,
            "configured": configured,
        }
    if configured_status == "ok":
        return {
            "status": "ready",
            "provider": default_provider,
            "reason": "default provider installed and configured",
            "installed": installed,
            "configured": configured,
        }
    if configured_status == "skip":
        return {
            "status": "caution",
            "provider": default_provider,
            "reason": str(configured.get("detail") or "provider auth not verified"),
            "installed": installed,
            "configured": configured,
        }
    return {
        "status": "blocked",
        "provider": default_provider,
        "reason": str(configured.get("detail") or "provider is not configured"),
        "installed": installed,
        "configured": configured,
    }


def _skill_status(skills: list[Any]) -> dict[str, Any]:
    names = sorted(str(skill.name) for skill in skills)
    required_for_wf = {
        "wf-orchestrator",
        "wf-status",
        "phase-plan",
        "phase-review",
        "phase-impl",
        "phase-verify",
        "phase-test",
        "phase-done",
    }
    missing = sorted(required_for_wf - set(names))
    return {
        "status": "ready" if not missing else "caution",
        "count": len(names),
        "names": names,
        "missing_workflow_skills": missing,
    }


def _scan_status(scan: dict[str, Any], *, repo_root: Path) -> dict[str, Any]:
    units = scan.get("units", []) or []
    language = str(scan.get("language") or "unknown")
    framework = str(scan.get("framework") or "unknown")
    subprojects = _detect_subprojects(repo_root)
    if units:
        return {
            "status": "ready",
            "service": scan.get("service"),
            "language": language,
            "framework": framework,
            "unit_count": len(units),
            "unit_pattern": scan.get("unit_pattern", ""),
            "sample_units": units[:5],
            "subprojects": subprojects,
        }
    if subprojects:
        return {
            "status": "caution",
            "service": scan.get("service"),
            "language": language,
            "framework": framework,
            "unit_count": 0,
            "unit_pattern": "",
            "sample_units": [],
            "subprojects": subprojects,
            "reason": "root looks like a workspace; scan a detected subproject",
        }
    if language != "unknown":
        return {
            "status": "caution",
            "service": scan.get("service"),
            "language": language,
            "framework": framework,
            "unit_count": 0,
            "unit_pattern": "",
            "sample_units": [],
            "subprojects": subprojects,
            "reason": "project language detected, but no analysis units found by heuristic scan",
        }
    return {
        "status": "blocked",
        "service": scan.get("service"),
        "language": language,
        "framework": framework,
        "unit_count": 0,
        "unit_pattern": "",
        "sample_units": [],
        "subprojects": subprojects,
        "reason": "project language and analysis units were not detected",
    }


def _config_status(paths: dict[str, str]) -> dict[str, Any]:
    project_config = Path(paths["project_config"])
    user_config = Path(paths["user_config"])
    return {
        "status": "ready" if project_config.is_file() else "caution",
        "project_config_exists": project_config.is_file(),
        "user_config_exists": user_config.is_file(),
        "project_config": str(project_config),
        "user_config": str(user_config),
    }


def _workflow_status(repo_root: Path) -> dict[str, Any]:
    workflow_dir = repo_root / ".workflow"
    state_path = workflow_dir / "state.json"
    return {
        "status": "ready" if state_path.is_file() else "not_started",
        "workflow_dir_exists": workflow_dir.is_dir(),
        "state_exists": state_path.is_file(),
        "state_path": str(state_path),
    }


def _operations_status(repo_root: Path) -> dict[str, Any]:
    root = repo_root / ".awf-operations"
    profile = root / ".profile"
    return {
        "status": "ready" if profile.is_file() else "not_started",
        "root_exists": root.is_dir(),
        "profile_exists": profile.is_file(),
        "root": str(root),
    }


def _capability(name: str, level: int, status: str, reason: str, command: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "level": level,
        "level_label": AUTOMATION_LEVELS[level],
        "status": status,
        "reason": reason,
    }
    if command:
        payload["command"] = command
    return payload


def _build_capabilities(report: dict[str, Any]) -> list[dict[str, Any]]:
    repo_root = report["paths"]["repo_root"]
    service = report["scan"].get("service") or Path(repo_root).name
    sample_units = report["scan"].get("sample_units") or []
    sample_unit = sample_units[0]["name"] if sample_units else "<unit>"
    provider_status = report["provider"]["status"]
    scan_status = report["scan"]["status"]
    skill_status = report["skills"]["status"]
    workflow_started = report["workflow"]["status"] == "ready"
    analysis_dry_run_status = (
        "ready" if sample_units else ("caution" if scan_status == "caution" else "blocked")
    )
    analysis_run_status = (
        provider_status
        if sample_units
        else ("caution" if scan_status == "caution" and provider_status != "blocked" else "blocked")
    )

    return [
        _capability(
            "inspect",
            0,
            "ready",
            "repo root resolved; read-only diagnostics are available",
            "awf ready --repo-root .",
        ),
        _capability(
            "analysis_dry_run",
            1,
            analysis_dry_run_status,
            "generates prompt/bundle preview without provider execution",
            f"awf analyze {service} {sample_unit} --repo-root . --dry-run",
        ),
        _capability(
            "analysis_run",
            2,
            analysis_run_status,
            "runs the configured provider and writes .ai-context outputs",
            f"awf analyze {service} {sample_unit} --repo-root .",
        ),
        _capability(
            "workflow",
            3,
            "ready" if skill_status == "ready" and workflow_started else "caution",
            "creates .workflow state and runs gated feature phases",
            'awf wf init "small feature" --repo-root .',
        ),
        _capability(
            "operations_wiki",
            1,
            "ready",
            "records local operational decisions and telemetry",
            "awf wiki init --repo-root .",
        ),
    ]


def _automation_level(capabilities: list[dict[str, Any]]) -> dict[str, Any]:
    ready_levels = [
        int(cap["level"])
        for cap in capabilities
        if str(cap.get("status")) == "ready"
    ]
    caution_levels = [
        int(cap["level"])
        for cap in capabilities
        if str(cap.get("status")) in {"ready", "caution"}
    ]
    safe_level = max(ready_levels) if ready_levels else 0
    max_possible = max(caution_levels) if caution_levels else safe_level
    return {
        "safe_level": safe_level,
        "safe_label": AUTOMATION_LEVELS[safe_level],
        "max_possible_level": max_possible,
        "max_possible_label": AUTOMATION_LEVELS[max_possible],
    }


def _recommended_next(report: dict[str, Any]) -> list[dict[str, str]]:
    recommendations: list[dict[str, str]] = []
    paths = report["paths"]
    repo_root = paths["repo_root"]
    service = report["scan"].get("service") or Path(repo_root).name
    sample_units = report["scan"].get("sample_units") or []
    sample_unit = sample_units[0]["name"] if sample_units else "<unit>"

    if not report["config"]["project_config_exists"]:
        recommendations.append({
            "command": "awf init --repo-root .",
            "why": "create explicit project config before running provider-backed automation",
        })
    if report["provider"]["status"] != "ready":
        recommendations.append({
            "command": "awf doctor --repo-root . --probe",
            "why": report["provider"]["reason"],
        })
    if report["scan"]["status"] == "ready":
        recommendations.append({
            "command": f"awf analyze {service} {sample_unit} --repo-root . --dry-run",
            "why": "inspect the first analysis prompt/artifacts before provider execution",
        })
    elif report["scan"].get("subprojects"):
        subproject = report["scan"]["subprojects"][0]
        recommendations.append({
            "command": f"awf scan {subproject['path']} --no-ai",
            "why": report["scan"].get("reason", "scan detected subproject"),
        })
    else:
        recommendations.append({
            "command": "awf scan . --no-ai",
            "why": report["scan"].get("reason", "confirm analysis unit discovery"),
        })
    if report["operations"]["status"] != "ready":
        recommendations.append({
            "command": "awf wiki init --repo-root .",
            "why": "initialize local operations history before decisions accumulate",
        })
    return recommendations[:4]


def collect_ready_report(repo_root: str | None = None, *, probe: bool = False) -> dict[str, Any]:
    config = load_awf_config(repo_root)
    paths = resolve_runtime_paths(repo_root)
    resolved_root = Path(paths["repo_root"])
    doctor = collect_doctor_report(config, str(resolved_root), probe=probe)
    skills = discover_skills(str(resolved_root))
    scan = scan_result_to_dict(scan_repo(resolved_root, use_ai=False))

    report: dict[str, Any] = {
        "repo_root": str(resolved_root),
        "paths": paths,
        "probe_enabled": probe,
        "config": _config_status(paths),
        "provider": _provider_status(doctor),
        "skills": _skill_status(skills),
        "scan": _scan_status(scan, repo_root=resolved_root),
        "workflow": _workflow_status(resolved_root),
        "operations": _operations_status(resolved_root),
        "doctor": {
            "default_provider": doctor.get("default_provider"),
            "provider_fallback": doctor.get("provider_fallback", []),
            "dispatch": doctor.get("dispatch", {}),
            "mcp": doctor.get("mcp", {}),
        },
    }
    capabilities = _build_capabilities(report)
    report["capabilities"] = capabilities
    report["automation_level"] = _automation_level(capabilities)
    report["recommended_next"] = _recommended_next(report)
    return report
