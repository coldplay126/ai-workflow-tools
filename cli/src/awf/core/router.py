from __future__ import annotations

from difflib import get_close_matches
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re


@dataclass
class RouteDecision:
    target: str
    reason: str
    argv: list[str] | None = None
    requires_confirmation: bool = False
    risk_level: str = "low"


_ANALYZE_PATTERNS = [
    re.compile(r"^(?P<service>[a-z0-9-]+)\s+(?P<domain>[a-z0-9-]+)\s+(analyze|analysis|분석해줘|분석)$"),
    re.compile(r"^(analyze|analysis|분석)\s+(?P<service>[a-z0-9-]+)\s+(?P<domain>[a-z0-9-]+)$"),
]

_ANALYZE_EXECUTE_PATTERNS = [
    re.compile(r"^(?P<service>[a-z0-9-]+)\s+(?P<domain>[a-z0-9-]+)\s+(분석 실행|분석 실행해줘|analyze run|analysis run)$"),
    re.compile(r"^(analyze run|analysis run|분석 실행)\s+(?P<service>[a-z0-9-]+)\s+(?P<domain>[a-z0-9-]+)$"),
]

_DEFAULT_ANALYZE_SERVICE = "sample-api"
_SERVICE_ALIASES = {
    "sample-api": "sample-api",
    "sample api": "sample-api",
    "sample-api": "sample-api",
    "sample api": "sample-api",
}
_DOMAIN_ALIASES = {
    "quest-challenge": "quest-challenge",
    "quest challenge": "quest-challenge",
    "health": "health",
}
_HIGH_RISK_TOKENS = [
    "delete",
    "drop",
    "destroy",
    "reset",
    "배포",
    "운영",
    "production",
    "migrate",
    "migration",
    "삭제",
    "approve",
    "apply",
    "auto-apply",
    "release",
]


def _resolve_docs_root(repo_root: str | None = None) -> Path:
    env_root = os.environ.get("AWF_DOCS_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    root = Path(repo_root).expanduser().resolve() if repo_root else Path.cwd().resolve()
    sibling = root.parent / "analysis-docs"
    if sibling.exists():
        return sibling.resolve()
    return (Path.home() / "Documents" / "GitHub" / "analysis-docs").resolve()


def _load_analysis_catalog(repo_root: str | None = None) -> tuple[set[str], dict[str, list[str]]]:
    config_path = _resolve_docs_root(repo_root) / "_templates" / "analysis-config.json"
    if not config_path.exists():
        return set(), {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return set(), {}
    services = {str(item) for item in payload.get("service_map", {}).keys()}
    domain_services: dict[str, list[str]] = {}
    for domain_name, definition in payload.get("domain_definitions", {}).items():
        domain_services[str(domain_name)] = sorted(str(item) for item in definition.get("directories", {}).keys())
    return services, domain_services


def _alias_variants(canonical: str) -> set[str]:
    variants = {canonical, canonical.replace("-", " ")}
    if canonical.startswith("awf-"):
        variants.add(canonical.replace("awf-", "awf ", 1).replace("-", " "))
    return {item.strip() for item in variants if item.strip()}


def _known_service_aliases(repo_root: str | None = None) -> dict[str, str]:
    aliases = dict(_SERVICE_ALIASES)
    services, _ = _load_analysis_catalog(repo_root)
    for canonical in services:
        for alias in _alias_variants(canonical):
            aliases.setdefault(alias, canonical)
    return aliases


def _known_domain_aliases(repo_root: str | None = None) -> dict[str, str]:
    aliases = dict(_DOMAIN_ALIASES)
    _, domain_services = _load_analysis_catalog(repo_root)
    for canonical in domain_services:
        for alias in _alias_variants(canonical):
            aliases.setdefault(alias, canonical)
    return aliases


def _risk_level_for_text(normalized: str, *, actual_run: bool = False) -> str:
    if any(token in normalized for token in _HIGH_RISK_TOKENS):
        return "high"
    if actual_run:
        return "medium"
    return "low"


def _contains_execution_intent(normalized: str) -> bool:
    return any(token in normalized for token in ["실행", "run"])


def _contains_status_intent(normalized: str) -> bool:
    return any(token in normalized for token in ["status", "상태", "진행", "진행상황"])


def _contains_fuzzy_token(normalized: str, keywords: list[str], *, cutoff: float = 0.8) -> bool:
    words = normalized.split()
    for word in words:
        if get_close_matches(word, keywords, n=1, cutoff=cutoff):
            return True
    return False


def _contains_analysis_intent(normalized: str) -> bool:
    return any(token in normalized for token in ["분석", "analyze", "analysis"]) or _contains_fuzzy_token(
        normalized,
        ["analyze", "analysis"],
    )


def _phrase_candidates(normalized: str, *, max_words: int = 3) -> list[str]:
    words = normalized.split()
    candidates: list[str] = []
    for size in range(1, min(max_words, len(words)) + 1):
        for start in range(0, len(words) - size + 1):
            candidates.append(" ".join(words[start : start + size]))
    return candidates


def _fuzzy_alias_target(normalized: str, aliases: dict[str, str], *, cutoff: float = 0.84) -> str | None:
    candidates = _phrase_candidates(normalized)
    for candidate in candidates:
        match = get_close_matches(candidate, list(aliases.keys()), n=1, cutoff=cutoff)
        if match:
            return aliases[match[0]]
    return None


def _infer_alias_target(normalized: str, aliases: dict[str, str]) -> str | None:
    for alias, canonical in aliases.items():
        if alias in normalized:
            return canonical
    return _fuzzy_alias_target(normalized, aliases)


def _resolve_service_for_domain(domain: str, repo_root: str | None = None) -> str:
    services, domain_services = _load_analysis_catalog(repo_root)
    candidates = domain_services.get(domain, [])
    if len(candidates) == 1:
        return candidates[0]
    if _DEFAULT_ANALYZE_SERVICE in services or not services:
        return _DEFAULT_ANALYZE_SERVICE
    return sorted(services)[0]


def _is_probable_service(token: str, repo_root: str | None = None) -> bool:
    return token.startswith("awf-") or token in set(_known_service_aliases(repo_root).values())


def _infer_analyze_route(normalized: str, repo_root: str | None = None) -> RouteDecision | None:
    if not _contains_analysis_intent(normalized):
        return None

    explicit_service = _infer_alias_target(normalized, _known_service_aliases(repo_root))
    explicit_domain = _infer_alias_target(normalized, _known_domain_aliases(repo_root))
    if explicit_domain is None:
        return None

    service = explicit_service or _resolve_service_for_domain(explicit_domain, repo_root)
    if _contains_status_intent(normalized) and not _contains_execution_intent(normalized):
        return RouteDecision(
            target="analyze_status",
            reason="matched inferred analyze status intent",
            argv=["analyze", service, explicit_domain, "--status"],
        )
    if _contains_execution_intent(normalized):
        return RouteDecision(
            target="analyze_run",
            reason="matched inferred analyze execution intent",
            argv=["analyze", service, explicit_domain],
            requires_confirmation=True,
            risk_level=_risk_level_for_text(normalized, actual_run=True),
        )
    return RouteDecision(
        target="analyze_dry_run",
        reason="matched inferred analyze intent",
        argv=["analyze", service, explicit_domain, "--dry-run"],
    )


def route_natural_language(text: str, repo_root: str | None = None) -> RouteDecision:
    normalized = " ".join(text.strip().lower().split())

    if any(
        token in normalized
        for token in [
            "doctor",
            "readiness",
            "provider 상태",
            "provider readiness",
            "환경 진단",
            "설정 진단",
            "인증 상태",
            "provider 확인",
        ]
    ):
        if any(token in normalized for token in ["probe", "연결 확인", "connectivity", "reachable", "버전 확인"]):
            return RouteDecision(target="doctor_probe", reason="matched doctor probe intent", argv=["doctor", "--probe"])
        return RouteDecision(target="doctor", reason="matched doctor intent", argv=["doctor"])
    if any(token in normalized for token in ["workflow status", "wf status", "워크플로우 상태"]):
        return RouteDecision(target="wf_status", reason="matched workflow status intent", argv=["wf", "status"])
    if any(token in normalized for token in ["config show", "설정 보여", "config 확인", "구성 보여"]):
        return RouteDecision(target="config_show", reason="matched config inspection intent", argv=["config", "show"])
    if any(token in normalized for token in ["session list", "sessions list", "chat sessions", "세션 목록", "대화 목록"]):
        return RouteDecision(target="chat_list_sessions", reason="matched chat session listing intent", argv=["chat", "--list-sessions"])
    if any(token in normalized for token in ["compact latest session", "compact latest", "최근 세션 압축", "최근 대화 압축", "최근 대화 요약"]):
        return RouteDecision(
            target="chat_compact_latest",
            reason="matched latest chat compaction intent",
            argv=["chat", "--compact-latest"],
            requires_confirmation=True,
            risk_level="medium",
        )
    if any(token in normalized for token in ["latest session", "show latest session", "최근 세션", "최근 대화 보여", "마지막 대화 보여"]):
        return RouteDecision(target="chat_show_latest", reason="matched latest chat session inspection intent", argv=["chat", "--show-latest"])
    if any(token in normalized for token in ["skills list", "skill list", "스킬 목록", "skills 보여"]):
        return RouteDecision(target="skills_list", reason="matched skills listing intent", argv=["skills", "list"])
    if any(token in normalized for token in ["mcp list", "mcp 목록", "mcp 보여", "mcp servers"]):
        return RouteDecision(target="mcp_list", reason="matched mcp listing intent", argv=["mcp", "list"])
    if any(token in normalized for token in ["review 해줘", "review 보여줘", "review 실행", "review dry run"]):
        if any(token in normalized for token in ["review 실행", "review run"]):
            return RouteDecision(
                target="wf_review_run",
                reason="matched workflow review execution intent",
                argv=["wf", "next", "--phase", "review"],
                requires_confirmation=True,
                risk_level=_risk_level_for_text(normalized, actual_run=True),
            )
        return RouteDecision(
            target="wf_review_dry_run",
            reason="matched workflow review intent",
            argv=["wf", "next", "--phase", "review", "--dry-run"],
        )
    if any(token in normalized for token in ["verify 해줘", "verify 보여줘", "verify 실행", "verify dry run"]):
        if any(token in normalized for token in ["verify 실행", "verify run"]):
            return RouteDecision(
                target="wf_verify_run",
                reason="matched workflow verify execution intent",
                argv=["wf", "next", "--phase", "verify"],
                requires_confirmation=True,
                risk_level=_risk_level_for_text(normalized, actual_run=True),
            )
        return RouteDecision(
            target="wf_verify_dry_run",
            reason="matched workflow verify intent",
            argv=["wf", "next", "--phase", "verify", "--dry-run"],
        )

    for pattern in _ANALYZE_EXECUTE_PATTERNS:
        match = pattern.match(normalized)
        if match:
            service = match.group("service")
            domain = match.group("domain")
            if not _is_probable_service(service, repo_root):
                continue
            return RouteDecision(
                target="analyze_run",
                reason="matched explicit analyze execution intent",
                argv=["analyze", service, domain],
                requires_confirmation=True,
                risk_level=_risk_level_for_text(normalized, actual_run=True),
            )

    for pattern in _ANALYZE_PATTERNS:
        match = pattern.match(normalized)
        if match:
            service = match.group("service")
            domain = match.group("domain")
            if not _is_probable_service(service, repo_root):
                continue
            return RouteDecision(
                target="analyze_dry_run",
                reason="matched explicit analyze intent",
                argv=["analyze", service, domain, "--dry-run"],
            )

    inferred = _infer_analyze_route(normalized, repo_root)
    if inferred is not None:
        return inferred

    return RouteDecision(target="chat", reason="defaulted to chat session mode", argv=["chat", "--message", text.strip()])
