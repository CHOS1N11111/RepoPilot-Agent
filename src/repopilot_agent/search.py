"""Lightweight repository search for local workflow runs."""

from __future__ import annotations

import re
from collections import Counter

from .models import RepoFile, SearchHit

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]{3,}")


def search_files(task: str, files: list[RepoFile], limit: int = 8) -> list[SearchHit]:
    query_terms = _tokenize(task)
    if not query_terms:
        return []

    hits: list[SearchHit] = []
    for repo_file in files:
        path_lower = repo_file.relative_path.lower()
        content_lower = repo_file.content.lower()
        score = 0
        reasons: list[str] = []

        for term, weight in query_terms.items():
            path_count = path_lower.count(term)
            content_count = content_lower.count(term)
            if path_count:
                score += path_count * weight * 5
                reasons.append(f"path matches '{term}'")
            if content_count:
                score += min(content_count, 5) * weight
                reasons.append(f"content matches '{term}'")

        if score > 0 and _looks_important(repo_file.relative_path):
            score += 2
            reasons.append("important project file")

        if score > 0:
            hits.append(
                SearchHit(
                    path=repo_file.relative_path,
                    score=score,
                    reasons=sorted(set(reasons)),
                    preview=_preview(repo_file.content, query_terms),
                )
            )

    return sorted(hits, key=lambda hit: (-hit.score, hit.path))[:limit]


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
    return Counter(token for token in tokens if token not in ignored)


def _looks_important(path: str) -> bool:
    lower = path.lower()
    return lower in {"readme.md", "pyproject.toml", "package.json"} or lower.startswith(
        ("src/", "app/", "lib/")
    )


def _preview(content: str, query_terms: Counter[str]) -> str:
    lines = content.splitlines()
    lowered_terms = set(query_terms)
    for index, line in enumerate(lines):
        if any(term in line.lower() for term in lowered_terms):
            start = max(0, index - 1)
            end = min(len(lines), index + 2)
            return "\n".join(lines[start:end]).strip()
    return "\n".join(lines[:3]).strip()
