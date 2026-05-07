"""Token usage tracking and cost estimation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Cost per 1K tokens (USD) — approximate, based on public pricing (2026-04)
COST_PER_1K_TOKENS: dict[str, dict[str, float]] = {
    # Anthropic (claude.ai/pricing)
    "claude-code": {"input": 0.003, "output": 0.015},      # Sonnet 4 (Claude Code default)
    "claude:opus": {"input": 0.015, "output": 0.075},       # Opus 4
    "claude:sonnet": {"input": 0.003, "output": 0.015},     # Sonnet 4
    "claude:haiku": {"input": 0.0008, "output": 0.004},     # Haiku 3.5
    "claude-sdk": {"input": 0.003, "output": 0.015},        # Sonnet (SDK default)
    # OpenAI
    "codex": {"input": 0.003, "output": 0.012},             # Codex (o3-mini class)
    "openai": {"input": 0.002, "output": 0.008},            # GPT-4.1-mini
    # Default fallback
    "_default": {"input": 0.003, "output": 0.015},
}


@dataclass
class UsageSummary:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    breakdown: list[dict[str, Any]] | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def format_text(self) -> str:
        parts = [
            f"token_usage: input={self.input_tokens:,} output={self.output_tokens:,} total={self.total_tokens:,}",
        ]
        if self.cost_usd > 0:
            parts.append(f"cost_estimate: ~${self.cost_usd:.4f}")
        if self.breakdown:
            details = []
            for b in self.breakdown:
                provider = b.get("provider", "?")
                role = b.get("role", "")
                cost = b.get("cost", 0)
                inp = b.get("input_tokens", 0)
                out = b.get("output_tokens", 0)
                label = f"{provider}" + (f"/{role}" if role else "")
                details.append(f"{label}=${cost:.4f}(in={inp:,},out={out:,})")
            parts.append(f"cost_breakdown: {' + '.join(details)}")
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "breakdown": self.breakdown or [],
        }


def estimate_cost(provider_name: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost for given token counts."""
    rates = COST_PER_1K_TOKENS.get(provider_name, COST_PER_1K_TOKENS["_default"])
    return (input_tokens / 1000 * rates["input"]) + (output_tokens / 1000 * rates["output"])


def summarize_usage(entries: list[dict[str, Any]]) -> UsageSummary:
    """Summarize token usage from multiple agent/provider runs.

    Each entry: {"provider": str, "role": str, "input_tokens": int, "output_tokens": int}
    """
    total_input = 0
    total_output = 0
    total_cost = 0.0
    breakdown = []

    for entry in entries:
        provider = entry.get("provider", "_default")
        inp = int(entry.get("input_tokens", 0) or 0)
        out = int(entry.get("output_tokens", 0) or 0)
        cost = estimate_cost(provider, inp, out)

        total_input += inp
        total_output += out
        total_cost += cost

        breakdown.append({
            "provider": provider,
            "role": entry.get("role", ""),
            "input_tokens": inp,
            "output_tokens": out,
            "cost": round(cost, 6),
        })

    return UsageSummary(
        input_tokens=total_input,
        output_tokens=total_output,
        cost_usd=round(total_cost, 6),
        breakdown=breakdown if len(breakdown) > 1 else None,
    )
