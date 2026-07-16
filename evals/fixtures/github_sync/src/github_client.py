"""Minimal GitHub metadata client used as retrieval context."""


def repository_endpoint(owner: str, repo: str) -> str:
    return f"https://api.github.com/repos/{owner}/{repo}"
