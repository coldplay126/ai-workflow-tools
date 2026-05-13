from __future__ import annotations

from pathlib import Path
from typing import Any

from awf.core.config import load_awf_config, resolve_runtime_paths
from awf.core.readiness import collect_doctor_report
from awf.core.scanner import PYTHON_PROJECT_MARKERS, scan_repo, scan_result_to_dict
from awf.core.skills import discover_skills


_PROJECT_MARKERS = (
    "package.json",
    *PYTHON_PROJECT_MARKERS,
    "go.mod",
    "Cargo.toml",
    "composer.json",
)


AUTOMATION_LEVELS = {
    0: "inspect only",
    1: "prompt/artifact generation",
    2: "provider execution",
    3: "workflow file edits",
    4: "PR/merge automation",
}


READY_GATES = ("inspect", "analysis", "workflow-init", "workflow-run", "operations")


GATE_EXIT_CODES = {
    "allow": 0,
    "dry_run_only": 10,
    "block": 20,
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


def _is_gitignored_by_pattern(repo_root: Path, path: str) -> bool:
    gitignore = repo_root / ".gitignore"
    if not gitignore.is_file():
        return False
    candidates = {path, f"{path}/", f"/{path}", f"/{path}/"}
    try:
        lines = gitignore.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if line in candidates:
            return True
    return False


def _workflow_status(repo_root: Path) -> dict[str, Any]:
    workflow_dir = repo_root / ".workflow"
    state_path = workflow_dir / "state.json"
    manifest_path = workflow_dir / "manifest.json"
    gitignored = _is_gitignored_by_pattern(repo_root, ".workflow")
    warning = None
    if gitignored:
        warning = ".workflow/ is ignored by .gitignore; workflow state is local-only"

    # Validate `sibling_repos` in manifest.json so operator typos surface
    # at `awf ready` time instead of `awf wf scope-check` time
    # (see docs/specs/multi-repo-scope.md §3.1, PR #117 follow-up).
    manifest_status = "missing"
    manifest_error: str | None = None
    sibling_count = 0
    if manifest_path.is_file():
        try:
            from awf.core.wf_scope import load_sibling_repos
            siblings = load_sibling_repos(repo_root)
            sibling_count = len(siblings)
            manifest_status = "ok"
        except ValueError as exc:
            manifest_status = "invalid"
            manifest_error = str(exc)
        except Exception as exc:  # pragma: no cover - defensive
            manifest_status = "invalid"
            manifest_error = f"unexpected error: {exc}"

    return {
        "status": "ready" if state_path.is_file() else "not_started",
        "workflow_dir_exists": workflow_dir.is_dir(),
        "state_exists": state_path.is_file(),
        "state_path": str(state_path),
        "gitignored": gitignored,
        "warning": warning,
        "manifest_status": manifest_status,
        "manifest_error": manifest_error,
        "sibling_repo_count": sibling_count,
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


def _pi_smoke_command(pi_readiness: dict[str, Any]) -> str:
    command = str(
        pi_readiness.get("field_smoke_command")
        or "python3 cli/tests/run_pi_field_smoke.py --json"
    )
    if "--write-result" not in command:
        command = f"{command} --write-result"
    return command


def _pi_field_smoke_recommendation(
    report: dict[str, Any],
) -> dict[str, str] | None:
    pi_readiness = report.get("doctor", {}).get("pi_readiness", {}) or {}
    dispatch = report.get("doctor", {}).get("dispatch", {}) or {}
    preference = dispatch.get("surface_preference", {}) or {}
    surface_preference = str(preference.get("surface_preference") or "auto")
    last_smoke = pi_readiness.get("last_field_smoke", {}) or {}
    if last_smoke.get("status") == "missing":
        if surface_preference != "pi":
            return None
        return {
            "command": _pi_smoke_command(pi_readiness),
            "why": (
                "capture Pi field evidence before opting into "
                "dispatch.surface_preference=pi"
            ),
        }
    if last_smoke.get("status") == "invalid":
        return {
            "command": _pi_smoke_command(pi_readiness),
            "why": "refresh the invalid Pi field smoke evidence artifact",
        }
    if last_smoke.get("status") != "found":
        return None

    smoke_command = _pi_smoke_command(pi_readiness)
    reason = str(last_smoke.get("reason") or "")
    if last_smoke.get("stale"):
        return {
            "command": smoke_command,
            "why": (
                "last Pi field smoke is stale; refresh before relying on "
                "Pi dispatch"
            ),
        }

    reason_guidance = {
        "pi_not_found": (
            "Pi was not found in the latest field smoke; install Pi or retry "
            "with npm before opting into Pi dispatch"
        ),
        "missing_provider_auth": (
            "latest Pi field smoke lacks provider auth; log in through Pi or "
            "set a provider API key before using Pi dispatch"
        ),
        "provider_quota_exhausted": (
            "latest Pi field smoke hit Claude Extra Usage limits; keep Pi "
            "dispatch opt-in disabled until quota or credentials are fixed"
        ),
        "provider_auth_failed": (
            "latest Pi field smoke failed provider auth; refresh the Pi login "
            "or provider API key before using Pi dispatch"
        ),
        "provider_rate_limited": (
            "latest Pi field smoke was rate-limited; rerun after the provider "
            "limit resets before using Pi dispatch"
        ),
        "provider_contract_parse_error": (
            "latest Pi field smoke did not satisfy the JSON contract; keep "
            "Pi dispatch opt-in disabled until the contract is stable"
        ),
    }
    if reason in reason_guidance:
        return {
            "command": smoke_command,
            "why": reason_guidance[reason],
        }
    return None


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
    pi_recommendation = _pi_field_smoke_recommendation(report)
    if pi_recommendation:
        recommendations.append(pi_recommendation)
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


def _gate_payload(
    *,
    gate: str,
    decision: str,
    reason: str,
    required_capabilities: list[str],
    recommended_next: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "name": gate,
        "decision": decision,
        "exit_code": GATE_EXIT_CODES[decision],
        "reason": reason,
        "required_capabilities": required_capabilities,
        "recommended_next": recommended_next,
    }


def _first_recommendation(report: dict[str, Any], command_prefix: str | None = None) -> list[dict[str, str]]:
    items = report.get("recommended_next", []) or []
    if command_prefix is None:
        return list(items[:1])
    for item in items:
        if str(item.get("command", "")).startswith(command_prefix):
            return [item]
    return list(items[:1])


def evaluate_ready_gate(report: dict[str, Any], gate: str) -> dict[str, Any]:
    """Return a deterministic allow/block decision for automation entrypoints."""
    if gate not in READY_GATES:
        raise ValueError(f"unsupported ready gate: {gate}")

    provider_status = str(report["provider"]["status"])
    scan_status = str(report["scan"]["status"])
    skills_status = str(report["skills"]["status"])
    workflow_status = str(report["workflow"]["status"])
    manifest_status = str(report["workflow"].get("manifest_status") or "missing")
    manifest_error = report["workflow"].get("manifest_error")
    operations_status = str(report["operations"]["status"])

    if gate == "inspect":
        return _gate_payload(
            gate=gate,
            decision="allow",
            reason="repo root resolved; read-only diagnostics are available",
            required_capabilities=["inspect"],
            recommended_next=_first_recommendation(report),
        )

    if gate == "analysis":
        if scan_status != "ready":
            return _gate_payload(
                gate=gate,
                decision="block",
                reason="analysis unit discovery is not deterministic yet",
                required_capabilities=["analysis_dry_run", "analysis_run"],
                recommended_next=_first_recommendation(report, "awf scan"),
            )
        if provider_status != "blocked":
            reason = (
                "analysis units and default provider are ready"
                if provider_status == "ready"
                else f"analysis units are ready; default provider is {provider_status}"
            )
            return _gate_payload(
                gate=gate,
                decision="allow",
                reason=reason,
                required_capabilities=["analysis_run"],
                recommended_next=_first_recommendation(report, "awf analyze"),
            )
        return _gate_payload(
            gate=gate,
            decision="dry_run_only",
            reason=f"default provider is {provider_status}; provider-backed analysis is blocked",
            required_capabilities=["analysis_dry_run"],
            recommended_next=_first_recommendation(report, "awf analyze"),
        )

    if gate == "workflow-init":
        if skills_status != "ready":
            return _gate_payload(
                gate=gate,
                decision="block",
                reason="required workflow skills are missing",
                required_capabilities=["workflow"],
                recommended_next=[{
                    "command": "awf skills list --repo-root .",
                    "why": "inspect installed workflow skills before initializing .workflow",
                }],
            )
        return _gate_payload(
            gate=gate,
            decision="allow",
            reason="workflow skills are available for initialization",
            required_capabilities=["workflow"],
            recommended_next=[{
                "command": 'awf wf init "small feature" --repo-root .',
                "why": "create deterministic .workflow state before phase execution",
            }],
        )

    if gate == "workflow-run":
        if workflow_status != "ready":
            return _gate_payload(
                gate=gate,
                decision="block",
                reason=".workflow/state.json is required before workflow execution",
                required_capabilities=["workflow"],
                recommended_next=[{
                    "command": 'awf wf init "small feature" --repo-root .',
                    "why": "initialize workflow state before running phases",
                }],
            )
        if manifest_status == "invalid":
            return _gate_payload(
                gate=gate,
                decision="block",
                reason=f"manifest.json sibling_repos invalid: {manifest_error}",
                required_capabilities=["workflow"],
                recommended_next=[{
                    "command": "edit .workflow/manifest.json",
                    "why": "fix sibling_repos schema (docs/specs/multi-repo-scope.md §3.1)",
                }],
            )
        if skills_status != "ready":
            return _gate_payload(
                gate=gate,
                decision="block",
                reason="required workflow skills are missing",
                required_capabilities=["workflow"],
                recommended_next=[{
                    "command": "awf skills list --repo-root .",
                    "why": "inspect installed workflow skills before running phases",
                }],
            )
        if provider_status == "blocked":
            return _gate_payload(
                gate=gate,
                decision="dry_run_only",
                reason=f"default provider is {provider_status}; delegated workflow execution is blocked",
                required_capabilities=["workflow"],
                recommended_next=[{
                    "command": "awf wf next --repo-root . --dry-run",
                    "why": "prepare the next phase prompt without provider execution",
                }],
            )
        reason = (
            "workflow state, skills, and default provider are ready"
            if provider_status == "ready"
            else f"workflow state and skills are ready; default provider is {provider_status}"
        )
        return _gate_payload(
            gate=gate,
            decision="allow",
            reason=reason,
            required_capabilities=["workflow"],
            recommended_next=[{
                "command": "awf wf next --repo-root .",
                "why": "run the next gated workflow phase",
            }],
        )

    if operations_status != "ready":
        return _gate_payload(
            gate=gate,
            decision="block",
            reason=".awf-operations profile is not initialized",
            required_capabilities=["operations_wiki"],
            recommended_next=[{
                "command": "awf wiki init --repo-root .",
                "why": "initialize local operations history before recording decisions",
            }],
        )
    return _gate_payload(
        gate=gate,
        decision="allow",
        reason="operations profile is initialized",
        required_capabilities=["operations_wiki"],
        recommended_next=[{
            "command": "awf wiki compile --repo-root .",
            "why": "refresh deterministic operations wiki pages",
        }],
    )


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
            "runners": doctor.get("runners", []),
            "pi_readiness": doctor.get("pi_readiness", {}),
            "dispatch": doctor.get("dispatch", {}),
            "mcp": doctor.get("mcp", {}),
        },
    }
    capabilities = _build_capabilities(report)
    report["capabilities"] = capabilities
    report["automation_level"] = _automation_level(capabilities)
    report["recommended_next"] = _recommended_next(report)
    return report
