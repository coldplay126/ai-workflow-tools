"""Centralized language/extension registry.

Single source of truth for all language detection, file collection patterns,
and extension mapping across the analysis pipeline.
"""
from __future__ import annotations

# Extension → language name (canonical direction)
EXT_TO_LANGUAGE: dict[str, str] = {
    # TypeScript / JavaScript
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    # Python
    ".py": "python",
    ".pyi": "python",
    # JVM
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    # Systems
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    # Web / PHP
    ".php": "php",
    # Infrastructure
    ".tf": "terraform",
    ".hcl": "terraform",
    # Data / Config
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    # Query / Schema
    ".sql": "sql",
    ".xml": "xml",
    ".graphql": "graphql",
    ".gql": "graphql",
    ".proto": "protobuf",
    ".prisma": "prisma",
    # Documentation
    ".md": "markdown",
    ".mdx": "markdown",
    # Shell
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    # Swift / Dart
    ".swift": "swift",
    ".dart": "dart",
    # Ruby
    ".rb": "ruby",
    ".erb": "ruby",
}

# Language → source file glob patterns (for scanning/collection)
LANGUAGE_TO_GLOBS: dict[str, list[str]] = {
    "typescript": ["**/*.ts", "**/*.tsx"],
    "javascript": ["**/*.js", "**/*.jsx", "**/*.mjs", "**/*.cjs"],
    "python": ["**/*.py"],
    "java": ["**/*.java"],
    "kotlin": ["**/*.kt", "**/*.kts"],
    "scala": ["**/*.scala"],
    "go": ["**/*.go"],
    "rust": ["**/*.rs"],
    "c": ["**/*.c", "**/*.h"],
    "cpp": ["**/*.cpp", "**/*.hpp"],
    "php": ["**/*.php"],
    "terraform": ["**/*.tf", "**/*.hcl"],
    "yaml": ["**/*.yaml", "**/*.yml"],
    "sql": ["**/*.sql"],
    "xml": ["**/*.xml"],
    "graphql": ["**/*.graphql", "**/*.gql"],
    "protobuf": ["**/*.proto"],
    "swift": ["**/*.swift"],
    "dart": ["**/*.dart"],
    "ruby": ["**/*.rb"],
    "shell": ["**/*.sh"],
}

# Default globs for domain file collection (analysis)
# Covers source code + schema/query files that contain domain logic
DEFAULT_SOURCE_GLOBS: list[str] = [
    # Source code
    "**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx",
    "**/*.py",
    "**/*.java", "**/*.kt",
    "**/*.go",
    "**/*.rs",
    "**/*.php",
    "**/*.swift", "**/*.dart",
    "**/*.rb",
    "**/*.scala",
    # Schema / query definitions
    "**/*.sql",
    "**/*.xml",      # MyBatis/iBatis mapper
    "**/*.graphql", "**/*.gql",
    "**/*.proto",
    "**/*.prisma",   # Prisma ORM schema
]


def detect_language(path_or_suffix: str) -> str:
    """Detect language from file path or extension suffix.

    Accepts either a full path ('src/foo.ts') or a suffix ('.ts').
    Returns language name or the raw suffix stripped of dot.
    """
    from pathlib import Path
    suffix = Path(path_or_suffix).suffix.lower() if "." in path_or_suffix and not path_or_suffix.startswith(".") else path_or_suffix.lower()
    return EXT_TO_LANGUAGE.get(suffix, suffix.lstrip(".") or "unknown")
