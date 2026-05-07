"""Import extraction and context file resolution."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# Per-language import resolution config
_LANG_RESOLVE: dict[str, dict] = {
    "typescript": {
        "extensions": [".ts", ".tsx", ".js", ".jsx"],
        "index_files": ["index.ts", "index.tsx", "index.js"],
        "root_prefixes": ["src/", ""],
    },
    "javascript": {
        "extensions": [".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"],
        "index_files": ["index.js", "index.ts"],
        "root_prefixes": ["src/", ""],
    },
    "python": {
        "extensions": [".py"],
        "index_files": ["__init__.py"],
        "root_prefixes": ["src/", ""],
    },
    "rust": {
        "extensions": [".rs"],
        "index_files": ["mod.rs", "lib.rs"],
        "root_prefixes": ["src/", ""],
    },
    "go": {
        "extensions": [".go"],
        "index_files": [],  # Go package = directory (handled specially)
        "root_prefixes": [""],
        "dir_is_package": True,  # directory itself is the import target
    },
    "java": {
        "extensions": [".java"],
        "index_files": [],
        "root_prefixes": ["src/main/java/", "src/", ""],
    },
    "kotlin": {
        "extensions": [".kt", ".kts"],
        "index_files": [],
        "root_prefixes": ["src/main/kotlin/", "src/", ""],
    },
    "php": {
        "extensions": [".php"],
        "index_files": [],
        "root_prefixes": ["app/", "src/", ""],
    },
}


def extract_imports_with_kinds(content: str, language: str) -> list[tuple[str, str]]:
    """Like `extract_imports` but tags each path with an edge kind.

    Returns list of (import_path, kind) where kind is one of:
    - "import"     — runtime value/symbol import
    - "type-only"  — TypeScript `import type` (erased at compile time)
    - "reexport"   — TypeScript `export ... from`

    Other languages currently return only "import" since type-only semantics
    do not apply or require AST-level analysis.
    """
    results: list[tuple[str, str]] = []

    if language in ("typescript", "javascript"):
        # `import type { ... } from '...'`  → type-only
        for m in re.finditer(r"""import\s+type\s+[^;]*?\s+from\s+['"]([^'"]+)['"]""", content):
            path = m.group(1)
            if path and not path.startswith("@") and not path.startswith("node_modules"):
                results.append((path, "type-only"))
        # `export ... from '...'` → reexport (treat as runtime; barrel re-exports propagate)
        for m in re.finditer(r"""export\s+[^;]*?\s+from\s+['"]([^'"]+)['"]""", content):
            path = m.group(1)
            if path and not path.startswith("@") and not path.startswith("node_modules"):
                results.append((path, "reexport"))
        # Plain runtime imports: `import ... from '...'` (excluding the type form above)
        for m in re.finditer(r"""import\s+(?!type\s)[^;]*?\s+from\s+['"]([^'"]+)['"]""", content):
            path = m.group(1)
            if path and not path.startswith("@") and not path.startswith("node_modules"):
                results.append((path, "import"))
        # CommonJS: require('...')
        for m in re.finditer(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""", content):
            path = m.group(1)
            if path and not path.startswith("@") and not path.startswith("node_modules"):
                results.append((path, "import"))
        # Dedup while keeping the strongest kind: type-only < import == reexport
        # If the same path appears as both type-only and import, prefer "import".
        merged: dict[str, str] = {}
        kind_rank = {"type-only": 0, "reexport": 1, "import": 2}
        for path, kind in results:
            if path not in merged or kind_rank[kind] > kind_rank[merged[path]]:
                merged[path] = kind
        return list(merged.items())

    # Default: every path from extract_imports is a runtime import.
    return [(p, "import") for p in extract_imports(content, language)]


def extract_imports(content: str, language: str) -> list[str]:
    """Extract import/require paths from file content."""
    paths: list[str] = []

    if language in ("typescript", "javascript"):
        # import { X } from './path'
        # import X from '../path'
        # require('./path')
        # export * from './path'  (re-exports from barrel files)
        # export { X } from './path'
        for m in re.finditer(r"""(?:(?:import|export)\s+.*?\s+from\s+['"]([^'"]+)['"]|require\s*\(\s*['"]([^'"]+)['"]\s*\))""", content):
            path = m.group(1) or m.group(2)
            if path and not path.startswith("@") and not path.startswith("node_modules"):
                paths.append(path)

    elif language == "python":
        # from module.path import X, Y
        # from .relative import X  →  also track imported names for relative packages
        # import module.path
        # import a, b
        for m in re.finditer(
            r"from\s+(\.{0,3}[\w.]*)\s+import\s+([\w][\w\s,]*)|import\s+([\w.][\w.,\s]*)",
            content,
        ):
            from_module = m.group(1)
            imported_names = m.group(2)
            bare_imports = m.group(3)
            if from_module is not None:
                # from X import Y — always include the module itself
                if from_module not in ("", "."):
                    paths.append(from_module)
                # For relative package imports (from . import user, from .. import models),
                # each imported name is a sub-module to resolve
                if from_module.rstrip(".") == "" or from_module == ".":
                    # Pure-dot prefix: names are sibling modules
                    dots = from_module if from_module else "."
                    if imported_names:
                        for name in imported_names.split(","):
                            name = name.strip()
                            if name and name != "*":
                                paths.append(dots + name)
                elif from_module.startswith(".") and imported_names:
                    # from .pkg import X — imported names may be sub-modules
                    for name in imported_names.split(","):
                        name = name.strip()
                        if name and name != "*":
                            paths.append(from_module + "." + name)
            elif bare_imports:
                # import a, b, c.d
                for part in bare_imports.split(","):
                    part = part.strip()
                    if part:
                        paths.append(part)

    elif language == "php":
        # use App\Http\Controllers\...
        for m in re.finditer(r"use\s+([\w\\]+)", content):
            paths.append(m.group(1))

    elif language == "go":
        # import "package/path"
        for m in re.finditer(r'import\s+(?:\w+\s+)?"([^"]+)"', content):
            paths.append(m.group(1))
        # import block
        for block in re.finditer(r'import\s*\((.*?)\)', content, re.DOTALL):
            for m in re.finditer(r'"([^"]+)"', block.group(1)):
                paths.append(m.group(1))

    elif language in ("terraform", "tf"):
        # module "name" { source = "./modules/vpc" }
        for m in re.finditer(r'source\s*=\s*"([^"]+)"', content):
            paths.append(m.group(1))

    elif language in ("java", "kotlin"):
        # import com.example.package.ClassName
        for m in re.finditer(r'import\s+([\w.]+)', content):
            paths.append(m.group(1))

    elif language == "rust":
        # use crate::module::item
        # use super::module
        for m in re.finditer(r'use\s+([\w:]+)', content):
            paths.append(m.group(1))

    return paths


def resolve_import_to_file(
    import_path: str,
    source_file: Path,
    repo_root: Path,
    language: str,
) -> Optional[Path]:
    """Resolve an import path to an actual file on disk."""
    # Language-specific pre-processing
    if language == "php":
        # App\Http\Controllers\Api\V2\Auth → app/Http/Controllers/Api/V2/Auth
        import_path = import_path.replace("\\", "/")
        if import_path.startswith("App/"):
            import_path = "app/" + import_path[4:]

    elif language == "python":
        if import_path.startswith("."):
            # Relative import: .user → ./user, ..models → ../models
            dots = len(import_path) - len(import_path.lstrip("."))
            rest = import_path[dots:]
            relative_prefix = "./" if dots == 1 else "../" * (dots - 1)
            import_path = relative_prefix + rest.replace(".", "/")
        else:
            # Absolute import: app.models.user → app/models/user
            import_path = import_path.replace(".", "/")

    elif language in ("terraform", "tf"):
        if import_path.startswith("./") or import_path.startswith("../"):
            candidate = (source_file.parent / import_path).resolve()
            if candidate.is_dir():
                main = candidate / "main.tf"
                if main.exists():
                    return main
                tf_files = list(candidate.glob("*.tf"))
                if tf_files:
                    return tf_files[0]
        return None

    elif language in ("java", "kotlin"):
        # com.example.package.ClassName → com/example/package/ClassName
        import_path = import_path.replace(".", "/")

    elif language == "rust":
        # crate::models::user::User → try models/user (strip symbol at end)
        import_path = import_path.replace("::", "/")
        if import_path.startswith("crate/"):
            import_path = import_path[6:]
        elif import_path.startswith("super/"):
            import_path = "../" + import_path[6:]
        # Rust: last segment may be a symbol (type/function), not a module
        # Try progressively shorter paths until a file matches
        _rust_candidates = [import_path]
        parts = import_path.split("/")
        if len(parts) > 1:
            _rust_candidates.append("/".join(parts[:-1]))  # drop last (symbol)
        import_path = _rust_candidates[0]  # will try all in resolve loop

    # Get language config (fall back to generic)
    config = _LANG_RESOLVE.get(language, {})
    extensions = config.get("extensions", [])
    index_files = config.get("index_files", [])
    root_prefixes = config.get("root_prefixes", ["src/", ""])

    if not extensions:
        return None  # unsupported language

    # For languages where imports may include symbol names (Rust, Go),
    # generate candidate paths by progressively dropping trailing segments
    candidates = [import_path]
    if language == "rust":
        parts = import_path.split("/")
        for i in range(len(parts) - 1, 0, -1):
            candidates.append("/".join(parts[:i]))
    elif language == "go":
        # Go: strip module prefix from go.mod to get repo-relative path
        go_mod = repo_root / "go.mod"
        if go_mod.exists():
            for line in go_mod.read_text().splitlines():
                if line.startswith("module "):
                    mod_path = line.split(None, 1)[1].strip()
                    if import_path.startswith(mod_path + "/"):
                        stripped = import_path[len(mod_path) + 1:]
                        candidates = [stripped]
                    break
        if candidates == [import_path]:
            # Fallback: try progressively shorter suffix paths
            parts = import_path.split("/")
            for i in range(len(parts)):
                sub = "/".join(parts[i:])
                if sub not in candidates:
                    candidates.append(sub)

    dir_is_package = config.get("dir_is_package", False)

    for candidate_path in candidates:
        is_relative = candidate_path.startswith(".") or candidate_path.startswith("/")

        if not is_relative:
            for prefix in root_prefixes:
                for ext in extensions:
                    candidate = repo_root / prefix / (candidate_path + ext)
                    if candidate.exists():
                        return candidate
                candidate_dir = repo_root / prefix / candidate_path
                if candidate_dir.is_dir():
                    # For languages where directory = package (Go), return first source file
                    if dir_is_package:
                        for f in sorted(candidate_dir.iterdir()):
                            if f.is_file() and f.suffix in {ext for ext in extensions}:
                                return f
                    for idx in index_files:
                        candidate = candidate_dir / idx
                        if candidate.exists():
                            return candidate
        else:
            base = source_file.parent / candidate_path
            for ext in extensions:
                candidate = Path(str(base) + ext)
                if candidate.exists():
                    return candidate
            if base.is_dir():
                if dir_is_package:
                    for f in sorted(base.iterdir()):
                        if f.is_file() and f.suffix in {ext for ext in extensions}:
                            return f
                for idx in index_files:
                    candidate = base / idx
                    if candidate.exists():
                        return candidate

    return None


def _extract_used_names_from_import(content: str, import_path: str) -> list[str]:
    """Extract specific named imports from an import statement.

    e.g. `import { UserAttendance } from '../../common/entities'` → ['UserAttendance']
    Returns empty list for wildcard imports (import * as X).
    """
    names: list[str] = []
    # Match: import { Name1, Name2 } from 'import_path'
    escaped = re.escape(import_path)
    for m in re.finditer(rf"import\s*\{{([^}}]+)\}}\s*from\s*['\"]" + escaped + r"['\"]", content):
        for name in m.group(1).split(","):
            name = name.strip()
            if " as " in name:
                name = name.split(" as ")[0].strip()
            if name:
                names.append(name)
    return names


def collect_context_files(
    target_path: Path,
    repo_root: Path,
    language: str,
    content: str,
    max_context_chars: int = 8000,
) -> list[Path]:
    """Collect context files (imports) for a target file.

    Budget-based: includes all resolvable imports until total signature
    chars exceed max_context_chars (~1K tokens).

    For barrel files (index.ts with re-exports), follows one level of
    re-exports to include the actual definition files.
    """
    imports = extract_imports(content, language)
    context: list[Path] = []
    seen: set[str] = set()
    total_chars = 0

    for imp in imports:
        resolved = resolve_import_to_file(imp, target_path, repo_root, language)
        if resolved and str(resolved) not in seen and resolved != target_path:
            seen.add(str(resolved))
            try:
                ctx_content = resolved.read_text(encoding="utf-8", errors="replace")
                sig = extract_signatures(ctx_content, language)
                sig_chars = len(sig)
            except Exception:
                sig_chars = 200
                ctx_content = ""
            if total_chars + sig_chars > max_context_chars:
                break
            total_chars += sig_chars
            context.append(resolved)

            # Follow re-exports from barrel files (e.g. index.ts with export * from)
            # Only include re-exported files that are actually used by the target
            is_barrel = resolved.name.startswith("index.") or resolved.name == "__init__.py" or resolved.name == "mod.rs"
            if is_barrel and ctx_content:
                # Find which names are imported from this barrel
                _used_names = _extract_used_names_from_import(content, imp)
                reexports = extract_imports(ctx_content, language)
                for re_imp in reexports:
                    # If we know specific names, only follow matching re-exports
                    if _used_names:
                        re_basename = re_imp.rsplit("/", 1)[-1].replace("-", "").replace(".", "").lower()
                        if not any(n.lower() in re_basename for n in _used_names):
                            continue
                    re_resolved = resolve_import_to_file(re_imp, resolved, repo_root, language)
                    if re_resolved and str(re_resolved) not in seen and re_resolved != target_path:
                        seen.add(str(re_resolved))
                        try:
                            re_content = re_resolved.read_text(encoding="utf-8", errors="replace")
                            re_sig = extract_signatures(re_content, language)
                            re_chars = len(re_sig)
                        except Exception:
                            re_chars = 200
                        if total_chars + re_chars > max_context_chars:
                            break
                        total_chars += re_chars
                        context.append(re_resolved)

    return context


def extract_signatures(content: str, language: str) -> str:
    """Extract only export/class/function/interface signatures from file.

    Returns a compact representation suitable for context in XML bundles.
    """
    lines = content.splitlines()
    signatures: list[str] = []

    if language in ("typescript", "javascript"):
        for line in lines:
            stripped = line.strip()
            # Top-level exports and class/interface declarations
            if any(stripped.startswith(kw) for kw in [
                "export class ", "export default class ", "export interface ",
                "export type ", "export enum ", "export function ",
                "export default function ", "export const ", "export * ",
            ]):
                signatures.append(stripped[:150])
            # ORM/framework decorators that define schema
            elif stripped.startswith("@") and any(kw in stripped for kw in [
                "Entity", "Column", "Table", "Index", "PrimaryColumn",
                "PrimaryGeneratedColumn", "ManyToOne", "OneToMany",
                "ManyToMany", "JoinColumn", "JoinTable",
            ]):
                signatures.append(stripped[:150])

    elif language == "php":
        for line in lines:
            stripped = line.strip()
            if any(stripped.startswith(kw) for kw in [
                "class ", "interface ", "trait ", "abstract class",
                "public function", "protected function", "private function",
                "public static", "namespace ",
            ]):
                signatures.append(stripped[:200])
            # ORM/model schema: $table, $fillable, $casts, etc.
            elif any(kw in stripped for kw in [
                "protected $table", "protected $fillable", "protected $casts",
                "protected $hidden", "protected $primaryKey",
            ]):
                signatures.append(stripped[:200])

    elif language == "python":
        for line in lines:
            stripped = line.strip()
            if any(stripped.startswith(kw) for kw in [
                "class ", "def ", "async def ", "@",
            ]):
                signatures.append(stripped[:200])
            # SQLAlchemy/Django ORM schema definitions
            elif any(kw in stripped for kw in [
                "__tablename__", "__table_args__",
                "= Column(", "= relationship(",
                "= models.CharField", "= models.IntegerField",
                "= models.ForeignKey", "= models.Model",
            ]):
                signatures.append(stripped[:200])

    elif language == "go":
        for line in lines:
            stripped = line.strip()
            if any(stripped.startswith(kw) for kw in [
                "func ", "type ", "interface ",
            ]):
                signatures.append(stripped[:200])

    elif language in ("terraform", "tf"):
        for line in lines:
            stripped = line.strip()
            if any(stripped.startswith(kw) for kw in [
                "resource ", "data ", "variable ", "output ",
                "module ", "locals ", "provider ",
            ]):
                signatures.append(stripped[:200])

    elif language in ("yaml", "yml"):
        # K8s manifests: kind, metadata.name, spec highlights
        for line in lines:
            stripped = line.strip()
            if any(stripped.startswith(kw) for kw in [
                "kind:", "apiVersion:", "metadata:", "  name:",
                "spec:", "  replicas:", "  selector:", "  ports:",
                "  rules:", "  host:", "  path:",
            ]):
                signatures.append(stripped[:200])

    elif language == "java":
        for line in lines:
            stripped = line.strip()
            if any(stripped.startswith(kw) for kw in [
                "public class ", "public interface ", "public enum ",
                "public abstract ", "protected class ",
                "public static ", "public ", "@",
            ]):
                signatures.append(stripped[:200])

    elif language == "kotlin":
        for line in lines:
            stripped = line.strip()
            if any(stripped.startswith(kw) for kw in [
                "class ", "data class ", "object ", "interface ",
                "fun ", "val ", "var ", "sealed class ", "enum class ",
                "abstract class ", "@",
            ]):
                signatures.append(stripped[:200])

    elif language == "rust":
        for line in lines:
            stripped = line.strip()
            if any(stripped.startswith(kw) for kw in [
                "pub fn ", "pub struct ", "pub enum ", "pub trait ",
                "pub type ", "pub mod ", "pub const ",
                "fn ", "struct ", "enum ", "trait ", "impl ",
                "#[",  # derive macros, serde attributes, diesel table_name, etc.
            ]):
                signatures.append(stripped[:200])

    # Language-agnostic fallback
    if not signatures:
        for line in lines:
            stripped = line.strip()
            if any(kw in stripped for kw in ["export ", "module.exports", "pub ", "public "]):
                signatures.append(stripped[:150])
    return "\n".join(signatures) if signatures else "(no exports found)"
