"""Compact symbol and dependency map for repository-level agent context."""

from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath

from .models import RepoFile


TOKEN_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")
JS_SYMBOL_PATTERN = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?"
    r"(?:(class)\s+([A-Za-z_$][\w$]*)|(?:function)\s+([A-Za-z_$][\w$]*)\s*(\([^\n{]*\))|"
    r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(\([^\n=]*\)|[A-Za-z_$][\w$]*)\s*=>)",
    re.MULTILINE,
)
JS_IMPORT_PATTERN = re.compile(
    r"(?:from\s+|import\s*\(\s*|require\s*\(\s*)['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)
GENERIC_SYMBOL_PATTERNS = (
    re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*(class|interface|struct|enum|trait)\s+([A-Za-z_][\w]*)", re.MULTILINE),
    re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?(fn|func)\s+([A-Za-z_][\w]*)\s*(\([^\n{;]*\))", re.MULTILINE),
)


@dataclass(frozen=True)
class RepositorySymbol:
    name: str
    qualified_name: str
    kind: str
    line: int
    signature: str


@dataclass
class RepositoryMapEntry:
    path: str
    language: str
    symbols: list[RepositorySymbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    related_paths: list[str] = field(default_factory=list)
    parse_error: str | None = None


@dataclass(frozen=True)
class RepositoryMapMatch:
    path: str
    score: int
    reasons: list[str]
    symbols: list[str]
    related_paths: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RepositoryMap:
    entries: list[RepositoryMapEntry]

    @property
    def files_indexed(self) -> int:
        return len(self.entries)

    @property
    def symbol_count(self) -> int:
        return sum(len(entry.symbols) for entry in self.entries)

    @property
    def relation_count(self) -> int:
        return sum(len(entry.related_paths) for entry in self.entries)

    @property
    def parse_error_count(self) -> int:
        return sum(1 for entry in self.entries if entry.parse_error)

    def to_summary(self, task: str = "", seed_paths: list[str] | None = None, limit: int = 8) -> dict:
        languages = Counter(entry.language for entry in self.entries)
        matches = rank_repository_map(task, self, seed_paths=seed_paths, limit=limit)
        return {
            "files_indexed": self.files_indexed,
            "symbols_indexed": self.symbol_count,
            "relations_indexed": self.relation_count,
            "parse_errors": self.parse_error_count,
            "languages": dict(sorted(languages.items())),
            "relevant_entries": [match.to_dict() for match in matches],
        }


def build_repository_map(files: list[RepoFile]) -> RepositoryMap:
    entries = [_build_entry(repo_file) for repo_file in files]
    by_path = {entry.path: entry for entry in entries}
    alias_map = _build_module_alias_map(entries)

    for entry in entries:
        related: list[str] = []
        for imported in entry.imports:
            resolved = _resolve_import(imported, entry.path, alias_map)
            if resolved and resolved != entry.path and resolved not in related:
                related.append(resolved)
        for paired in _test_source_pairs(entry.path, by_path):
            if paired not in related:
                related.append(paired)
        entry.related_paths = sorted(related)

    for entry in entries:
        for related_path in list(entry.related_paths):
            related_entry = by_path.get(related_path)
            if related_entry is not None and entry.path not in related_entry.related_paths:
                related_entry.related_paths.append(entry.path)
                related_entry.related_paths.sort()
    return RepositoryMap(entries=entries)


def rank_repository_map(
    task: str,
    repository_map: RepositoryMap,
    *,
    seed_paths: list[str] | None = None,
    limit: int = 8,
) -> list[RepositoryMapMatch]:
    terms = {token.lower() for token in TOKEN_PATTERN.findall(task)}
    seeds = {path.replace("\\", "/") for path in seed_paths or []}
    seed_neighbors = {
        related
        for entry in repository_map.entries
        if entry.path in seeds
        for related in entry.related_paths
    }
    matches: list[RepositoryMapMatch] = []

    for entry in repository_map.entries:
        score = 0
        reasons: list[str] = []
        path_lower = entry.path.lower()
        symbol_names = [symbol.qualified_name for symbol in entry.symbols]
        symbol_text = " ".join(symbol_names).lower()
        import_text = " ".join(entry.imports).lower()
        for term in terms:
            if term in path_lower:
                score += 8
                reasons.append(f"path matches '{term}'")
            if term in symbol_text:
                score += 12
                reasons.append(f"symbol matches '{term}'")
            if term in import_text:
                score += 4
                reasons.append(f"import matches '{term}'")
        if entry.path in seeds:
            score += 14
            reasons.append("selected repository context")
        if entry.path in seed_neighbors:
            score += 9
            reasons.append("dependency of selected context")
        if score <= 0:
            continue
        matches.append(
            RepositoryMapMatch(
                path=entry.path,
                score=score,
                reasons=sorted(set(reasons)),
                symbols=symbol_names[:12],
                related_paths=entry.related_paths[:8],
            )
        )
    matches.sort(key=lambda match: (-match.score, match.path))
    return matches[: max(0, limit)]


def render_repository_map(
    repository_map: RepositoryMap,
    task: str,
    *,
    seed_paths: list[str] | None = None,
    max_files: int = 10,
    max_chars: int = 3_000,
) -> str:
    matches = rank_repository_map(task, repository_map, seed_paths=seed_paths, limit=max_files)
    by_path = {entry.path: entry for entry in repository_map.entries}
    if matches:
        paths = [match.path for match in matches]
    else:
        paths = [entry.path for entry in repository_map.entries if entry.symbols][:max_files]

    blocks: list[str] = []
    for path in paths:
        entry = by_path[path]
        symbol_lines = [
            f"  {symbol.kind} {symbol.signature} (line {symbol.line})"
            for symbol in entry.symbols[:12]
        ]
        block = "\n".join(
            [
                f"{entry.path} [{entry.language}]",
                *(symbol_lines or ["  no indexed symbols"]),
                f"  imports: {', '.join(entry.imports[:8]) or 'none'}",
                f"  related: {', '.join(entry.related_paths[:8]) or 'none'}",
            ]
        )
        candidate = "\n\n".join([*blocks, block])
        if len(candidate) > max_chars:
            if not blocks:
                marker = "\n[...repository map truncated...]"
                if max_chars <= len(marker):
                    return marker[:max_chars]
                return block[: max_chars - len(marker)] + marker
            break
        blocks.append(block)
    if not blocks:
        return "No symbol-level repository map entries matched the task."[:max_chars]
    return "\n\n".join(blocks)


def _build_entry(repo_file: RepoFile) -> RepositoryMapEntry:
    if repo_file.language == "Python":
        return _build_python_entry(repo_file)
    if repo_file.language in {"JavaScript", "TypeScript"}:
        return _build_javascript_entry(repo_file)
    return _build_generic_entry(repo_file)


def _build_python_entry(repo_file: RepoFile) -> RepositoryMapEntry:
    entry = RepositoryMapEntry(path=repo_file.relative_path, language=repo_file.language)
    try:
        tree = ast.parse(repo_file.content)
    except SyntaxError as exc:
        entry.parse_error = f"line {exc.lineno or 0}: {exc.msg}"
        return entry

    visitor = _PythonMapVisitor()
    visitor.visit(tree)
    entry.symbols = visitor.symbols
    entry.imports = sorted(set(visitor.imports))
    return entry


class _PythonMapVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.symbols: list[RepositorySymbol] = []
        self.imports: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified = ".".join([*self.scope, node.name])
        self.symbols.append(RepositorySymbol(node.name, qualified, "class", node.lineno, node.name))
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, "method" if self.scope else "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, "method" if self.scope else "async_function")

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.extend(alias.name for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        prefix = "." * node.level
        self.imports.append(prefix + module)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, kind: str) -> None:
        qualified = ".".join([*self.scope, node.name])
        try:
            arguments = ast.unparse(node.args)
        except Exception:
            arguments = "..."
        self.symbols.append(
            RepositorySymbol(node.name, qualified, kind, node.lineno, f"{node.name}({arguments})")
        )
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()


def _build_javascript_entry(repo_file: RepoFile) -> RepositoryMapEntry:
    symbols: list[RepositorySymbol] = []
    for match in JS_SYMBOL_PATTERN.finditer(repo_file.content):
        if match.group(1):
            name = match.group(2)
            kind = "class"
            signature = name
        elif match.group(3):
            name = match.group(3)
            kind = "function"
            signature = f"{name}{match.group(4) or '()'}"
        else:
            name = match.group(5)
            kind = "function"
            signature = f"{name}{match.group(6) or '()'}"
        symbols.append(
            RepositorySymbol(name, name, kind, _line_number(repo_file.content, match.start()), signature)
        )
    imports = sorted(set(JS_IMPORT_PATTERN.findall(repo_file.content)))
    return RepositoryMapEntry(
        path=repo_file.relative_path,
        language=repo_file.language,
        symbols=symbols,
        imports=imports,
    )


def _build_generic_entry(repo_file: RepoFile) -> RepositoryMapEntry:
    symbols: list[RepositorySymbol] = []
    for pattern in GENERIC_SYMBOL_PATTERNS:
        for match in pattern.finditer(repo_file.content):
            kind = match.group(1)
            name = match.group(2)
            suffix = match.group(3) if match.lastindex and match.lastindex >= 3 else ""
            symbols.append(
                RepositorySymbol(
                    name=name,
                    qualified_name=name,
                    kind=kind,
                    line=_line_number(repo_file.content, match.start()),
                    signature=f"{name}{suffix or ''}",
                )
            )
    symbols.sort(key=lambda symbol: (symbol.line, symbol.name))
    return RepositoryMapEntry(path=repo_file.relative_path, language=repo_file.language, symbols=symbols)


def _build_module_alias_map(entries: list[RepositoryMapEntry]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for entry in entries:
        pure = PurePosixPath(entry.path)
        without_suffix = pure.with_suffix("").as_posix()
        candidates = {entry.path, without_suffix, without_suffix.replace("/", "."), pure.stem}
        for prefix in ("src/", "app/", "lib/"):
            if without_suffix.startswith(prefix):
                stripped = without_suffix[len(prefix) :]
                candidates.update({stripped, stripped.replace("/", ".")})
        if without_suffix.endswith("/__init__"):
            package = without_suffix.removesuffix("/__init__")
            candidates.update({package, package.replace("/", ".")})
        for candidate in candidates:
            aliases.setdefault(candidate, entry.path)
    return aliases


def _resolve_import(import_name: str, source_path: str, aliases: dict[str, str]) -> str | None:
    clean = import_name.strip()
    if not clean:
        return None
    if clean.startswith(("./", "../")):
        parts = list(PurePosixPath(source_path).parent.parts)
        for part in clean.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(part)
        relative = "/".join(parts)
        without_suffix = str(PurePosixPath(relative).with_suffix(""))
        for candidate in (relative, without_suffix, without_suffix.replace("/", ".")):
            if candidate in aliases:
                return aliases[candidate]
        return None
    if clean.startswith("."):
        level = len(clean) - len(clean.lstrip("."))
        module = clean.lstrip(".")
        parent_parts = list(PurePosixPath(source_path).parent.parts)
        if level > 1:
            parent_parts = parent_parts[: -(level - 1)] if len(parent_parts) >= level - 1 else []
        local = "/".join([*parent_parts, *module.split(".")]).strip("/")
        for candidate in (local, local.replace("/", "."), module):
            if candidate in aliases:
                return aliases[candidate]
        return None
    normalized = clean.replace("/", ".").removesuffix(".js").removesuffix(".ts")
    for candidate in (clean, normalized, normalized.lstrip("."), normalized.split(".")[-1]):
        if candidate in aliases:
            return aliases[candidate]
    return None


def _test_source_pairs(path: str, by_path: dict[str, RepositoryMapEntry]) -> list[str]:
    pure = PurePosixPath(path)
    stem = pure.stem
    suffix = pure.suffix
    candidates: list[str] = []
    lower = path.lower()
    is_test = lower.startswith(("tests/", "test/")) or "/tests/" in lower or stem.startswith("test_")
    if is_test:
        base = stem[5:] if stem.startswith("test_") else stem.removesuffix("_test").removesuffix(".test")
        candidates.extend([f"src/{base}{suffix}", f"app/{base}{suffix}", f"lib/{base}{suffix}", f"{base}{suffix}"])
    else:
        candidates.extend(
            [
                f"tests/test_{stem}{suffix}",
                f"tests/{stem}_test{suffix}",
                f"tests/{stem}.test{suffix}",
            ]
        )
    return [candidate for candidate in candidates if candidate in by_path and candidate != path]


def _line_number(content: str, offset: int) -> int:
    return content.count("\n", 0, offset) + 1
