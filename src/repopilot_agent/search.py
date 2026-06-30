"""Lightweight repository search for local workflow runs."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import PurePosixPath

from .models import RepoFile, SearchHit

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]{3,}")
SYMBOL_PATTERN = re.compile(
    r"^\s*(?:async\s+def|def|class|function|const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)

ALIASES = {
    "api": {"endpoint", "route", "request", "response", "server"},
    "auth": {"authenticate", "authentication", "authorize", "authorization", "login", "token"},
    "browser": {"web", "ui", "frontend"},
    "ci": {"check", "checks", "workflow", "actions", "status"},
    "github": {"issue", "issues", "pull", "request", "pr", "review", "checks"},
    "history": {"memory", "sqlite", "run", "runs"},
    "llm": {"model", "prompt", "prompts", "trace", "traces", "context"},
    "memory": {"history", "sqlite", "runs"},
    "pr": {"pull", "request", "review"},
    "test": {"tests", "testing", "validation"},
    "ui": {"web", "frontend", "static", "html", "css", "javascript"},
    "validation": {"test", "tests", "validator", "check"},
    "web": {"ui", "frontend", "static", "html", "css", "javascript"},
}

PATH_INTENTS = {
    "web_ui": {
        "triggers": {"web", "ui", "frontend", "browser", "page", "html", "css", "javascript"},
        "path_terms": {"web", "static", "html", "css", "js", "app.js", "index.html"},
    },
    "github": {
        "triggers": {"github", "issue", "issues", "pull", "request", "pr", "review", "ci", "checks"},
        "path_terms": {"github", "git_tools", "git_summary", "repo_source"},
    },
    "memory": {
        "triggers": {"memory", "history", "sqlite", "runs", "trace", "traces"},
        "path_terms": {"memory", "history", "sqlite", "web_sessions"},
    },
    "llm": {
        "triggers": {"llm", "model", "prompt", "prompts", "context", "trace", "traces"},
        "path_terms": {"llm", "prompt", "schema", "tracing", "context_builder"},
    },
    "validation": {
        "triggers": {"validation", "validate", "test", "tests", "lint"},
        "path_terms": {"validator", "validation", "test", "tests"},
    },
}


def search_files(task: str, files: list[RepoFile], limit: int = 8) -> list[SearchHit]:
    query_terms = _tokenize(task)
    if not query_terms:
        return []

    task_terms = set(query_terms)
    candidates: dict[str, SearchHit] = {}
    for repo_file in files:
        path_lower = repo_file.relative_path.lower()
        content_lower = repo_file.content.lower()
        symbols = _extract_symbols(repo_file.content)
        score = 0
        reasons: list[str] = []

        for term, weight in query_terms.items():
            path_count = path_lower.count(term)
            content_count = content_lower.count(term)
            symbol_count = sum(1 for symbol in symbols if term in symbol)
            if path_count:
                score += path_count * weight * 5
                reasons.append(f"path matches '{term}'")
            if content_count:
                score += min(content_count, 5) * weight
                reasons.append(f"content matches '{term}'")
            if symbol_count:
                score += symbol_count * weight * 8
                reasons.append(f"symbol matches '{term}'")

        intent_score, intent_reasons = _score_path_intent(task_terms, path_lower)
        if intent_score:
            score += intent_score
            reasons.extend(intent_reasons)

        if score > 0 and _looks_important(repo_file.relative_path):
            score += 2
            reasons.append("important project file")

        if score > 0:
            candidates[repo_file.relative_path] = SearchHit(
                path=repo_file.relative_path,
                score=score,
                reasons=sorted(set(reasons)),
                preview=_preview(repo_file.content, query_terms),
            )

    _add_paired_files(candidates, files, query_terms)
    return sorted(candidates.values(), key=lambda hit: (-hit.score, hit.path))[:limit]


def _tokenize(text: str) -> Counter[str]:
    ignored = {
        "and",
        "bug",
        "change",
        "code",
        "fix",
        "for",
        "from",
        "implement",
        "issue",
        "request",
        "task",
        "the",
        "this",
        "with",
    }
    tokens = [token.lower() for token in TOKEN_PATTERN.findall(text)]
    weighted_terms: Counter[str] = Counter()
    for token in tokens:
        if token in ignored:
            continue
        weighted_terms[token] += 3
        for variant in _variants(token):
            if variant not in ignored:
                weighted_terms[variant] += 1
        for alias in ALIASES.get(token, set()):
            if alias not in ignored:
                weighted_terms[alias] += 1
    return weighted_terms


def _looks_important(path: str) -> bool:
    lower = path.lower()
    return lower in {"readme.md", "pyproject.toml", "package.json"} or lower.startswith(
        ("src/", "app/", "lib/")
    )


def _preview(content: str, query_terms: Counter[str]) -> str:
    lines = content.splitlines()
    lowered_terms = set(query_terms)
    blocks: list[str] = []
    used_ranges: list[range] = []
    for index, line in enumerate(lines):
        lower_line = line.lower()
        if not any(term in lower_line for term in lowered_terms):
            continue
        start = max(0, index - 1)
        end = min(len(lines), index + 2)
        current_range = range(start, end)
        if any(_ranges_overlap(current_range, used) for used in used_ranges):
            continue
        used_ranges.append(current_range)
        blocks.append("\n".join(lines[start:end]).strip())
        if len(blocks) >= 3:
            break
    if blocks:
        return "\n\n...\n\n".join(block for block in blocks if block)
    return "\n".join(lines[:3]).strip()


def _variants(token: str) -> set[str]:
    variants = {token}
    if token.endswith("ies") and len(token) > 4:
        variants.add(token[:-3] + "y")
    if token.endswith("es") and len(token) > 4:
        variants.add(token[:-2])
    if token.endswith("s") and len(token) > 3:
        variants.add(token[:-1])
    if token.endswith("ing") and len(token) > 5:
        variants.add(token[:-3])
        variants.add(token[:-3] + "e")
    if token.endswith("er") and len(token) > 4:
        variants.add(token[:-2])
        variants.add(token[:-1])
    if token.endswith("or") and len(token) > 4:
        variants.add(token[:-2])
    if token.endswith("ion") and len(token) > 5:
        variants.add(token[:-3])
    return {variant for variant in variants if len(variant) >= 3}


def _extract_symbols(content: str) -> set[str]:
    symbols: set[str] = set()
    for match in SYMBOL_PATTERN.finditer(content):
        symbol = match.group(1).lower()
        symbols.add(symbol)
        symbols.update(part for part in re.split(r"[_\W]+", symbol) if len(part) >= 3)
    return symbols


def _score_path_intent(task_terms: set[str], path_lower: str) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    for intent, config in PATH_INTENTS.items():
        triggers = config["triggers"]
        if not task_terms.intersection(triggers):
            continue
        path_terms = config["path_terms"]
        if any(term in path_lower for term in path_terms):
            score += 6
            reasons.append(f"path intent matches {intent}")
    return score, reasons


def _add_paired_files(
    candidates: dict[str, SearchHit],
    files: list[RepoFile],
    query_terms: Counter[str],
) -> None:
    by_path = {repo_file.relative_path: repo_file for repo_file in files}
    existing_paths = list(candidates)
    for path in existing_paths:
        for paired_path in _candidate_pair_paths(path, by_path):
            paired_reason = f"paired with {path}"
            if paired_path in candidates:
                existing_hit = candidates[paired_path]
                if paired_reason not in existing_hit.reasons:
                    candidates[paired_path] = SearchHit(
                        path=existing_hit.path,
                        score=existing_hit.score + max(1, candidates[path].score // 4),
                        reasons=sorted([*existing_hit.reasons, paired_reason]),
                        preview=existing_hit.preview,
                    )
                continue
            repo_file = by_path.get(paired_path)
            if repo_file is None:
                continue
            candidates[paired_path] = SearchHit(
                path=paired_path,
                score=max(1, candidates[path].score // 2),
                reasons=[f"paired with {path}"],
                preview=_preview(repo_file.content, query_terms),
            )


def _candidate_pair_paths(path: str, by_path: dict[str, RepoFile]) -> list[str]:
    normalized = path.replace("\\", "/")
    pure_path = PurePosixPath(normalized)
    name = pure_path.name
    stem = pure_path.stem
    suffix = pure_path.suffix
    candidates: list[str] = []

    if _is_test_path(normalized):
        base_stem = stem[5:] if stem.startswith("test_") else stem.removesuffix("_test")
        candidates.extend(
            [
                f"src/{base_stem}{suffix}",
                f"app/{base_stem}{suffix}",
                f"lib/{base_stem}{suffix}",
                f"{base_stem}{suffix}",
            ]
        )
    else:
        candidates.extend(
            [
                f"tests/test_{stem}{suffix}",
                f"test/test_{stem}{suffix}",
                f"tests/{stem}_test{suffix}",
                f"{pure_path.parent}/test_{name}",
            ]
        )

    return [candidate for candidate in candidates if candidate in by_path and candidate != normalized]


def _is_test_path(path: str) -> bool:
    lower = path.lower()
    return lower.startswith(("tests/", "test/")) or "/tests/" in lower or lower.endswith(("_test.py", ".test.js"))


def _ranges_overlap(first: range, second: range) -> bool:
    return first.start < second.stop and second.start < first.stop
