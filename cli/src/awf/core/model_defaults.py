from __future__ import annotations

# Concrete SDK defaults live here so model refreshes remain atomic. CLI-backed
# providers should prefer vendor role aliases or native automatic selection.
DEFAULT_CLAUDE_SDK_MODEL = "claude-sonnet-5"
DEFAULT_OPENAI_MODEL = "gpt-5.6"
