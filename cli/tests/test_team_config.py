from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.team_config import get_team_config, validate_provider_config


def _team(name: str, roles: list[dict], *, execution: str = "sequential", **extra: object) -> dict:
    return {
        "name": name,
        "roles": roles,
        "execution": execution,
        "max_turns": 1,
        "timeout_sec": 60,
        **extra,
    }


def test_plan_research_and_three_review_lenses_are_opt_in_team_roles() -> None:
    config = {
        "version": "3.0.0",
        "phase_routing": {
            "plan": {
                "pattern": "team",
                "team": _team(
                    "plan-baseline",
                    [
                        {
                            "id": "baseline_research",
                            "protocol": "analyzer",
                            "provider": "codex",
                            "baseline_research": True,
                        }
                    ],
                ),
            },
            "review": {
                "pattern": "team",
                "team": _team(
                    "review-three-lenses",
                    [
                        {
                            "id": "review_requirements",
                            "protocol": "cross_artifact_reviewer",
                            "provider": "codex",
                            "review_lens": "requirements",
                        },
                        {
                            "id": "review_architecture",
                            "protocol": "cross_artifact_reviewer",
                            "provider": "codex",
                            "review_lens": "architecture",
                        },
                        {
                            "id": "review_risk",
                            "protocol": "cross_artifact_reviewer",
                            "provider": "codex",
                            "review_lens": "risk",
                        },
                    ],
                    execution="parallel",
                ),
            },
        },
    }

    assert validate_provider_config(config) == []
    assert get_team_config(config, "plan")["roles"][0]["baseline_research"] is True
    assert [role["review_lens"] for role in get_team_config(config, "review")["roles"]] == [
        "requirements",
        "architecture",
        "risk",
    ]


def test_baseline_research_and_review_lenses_fail_closed_when_they_write() -> None:
    config = {
        "version": "3.0.0",
        "phase_routing": {
            "plan": {
                "pattern": "team",
                "team": _team(
                    "invalid-baseline",
                    [
                        {
                            "id": "baseline_research",
                            "baseline_research": True,
                            "write_scope": [".workflow/artifacts/spec.md"],
                        }
                    ],
                ),
            },
            "review": {
                "pattern": "team",
                "team": _team(
                    "invalid-lens",
                    [
                        {
                            "id": "review_requirements",
                            "review_lens": "requirements",
                            "write_scope": [".workflow/artifacts/review-report.md"],
                        }
                    ],
                ),
            },
        },
    }

    assert validate_provider_config(config) == [
        "phase_routing.plan.team.roles[0].baseline_research: must not declare write_scope",
        "phase_routing.review.team.roles[0].review_lens: must not declare write_scope",
    ]


def test_isolated_omp_impl_role_requires_task_selector_and_write_scope() -> None:
    config = {
        "version": "3.0.0",
        "phase_routing": {
            "impl": {
                "pattern": "team",
                "team": _team(
                    "isolated-impl",
                    [
                        {
                            "id": "implement-one",
                            "provider": "claude-code",
                            "isolated_omp": True,
                            "task_selector": "T042",
                            "write_scope": ["cli/src/awf/core/team_config.py"],
                        },
                        {
                            "id": "implement-two",
                            "provider": "claude-code",
                            "isolated_omp": True,
                            "task_selector": "parallel",
                            "write_scope": ["cli/src/awf/core/team_runner.py"],
                        },
                    ],
                    execution="parallel",
                    on_write_scope_overlap="fail",
                ),
            }
        },
    }

    assert validate_provider_config(config) == []

    config["phase_routing"]["impl"]["team"]["roles"][0]["task_selector"] = "all"
    assert validate_provider_config(config) == [
        "phase_routing.impl.team.roles[0].task_selector: isolated_omp requires 'parallel' or an explicit T-number"
    ]


def test_legacy_team_config_remains_valid_without_opt_in_fields() -> None:
    config = {
        "version": "2.3.0",
        "phase_routing": {"review": {"mode": "dual"}},
    }

    assert validate_provider_config(config) == []
