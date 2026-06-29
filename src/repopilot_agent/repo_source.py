"""Repository source resolution for local paths and GitHub URLs."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse


GITHUB_SSH_PATTERN = re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$")
GITHUB_SLUG_PATTERN = re.compile(r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?$")
SAFE_SEGMENT_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class RepositorySource:
    source: str
    input: str
    local_path: str
    github_url: str | None = None
    owner: str | None = None
    repo: str | None = None
    cached: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class GitHubRepositoryInput:
    owner: str
    repo: str
    clone_url: str
    html_url: str


def resolve_repository_reference(
    repo: str | Path | None = None,
    repo_source: str = "auto",
    github_url: str | None = None,
    cache_root: str | Path | None = None,
    clone_if_missing: bool = True,
) -> RepositorySource:
    source = (repo_source or "auto").strip().lower()
    repo_value = str(repo or ".").strip() or "."
    github_value = str(github_url or "").strip()

    if source == "auto":
        candidate = github_value or repo_value
        if github_value:
            source = "github" if parse_github_repository_input(candidate) else "local"
        elif Path(repo_value).expanduser().exists():
            source = "local"
        else:
            source = "github" if parse_github_repository_input(candidate) else "local"
    if source == "local":
        local_path = Path(repo_value).expanduser().resolve()
        return RepositorySource(
            source="local",
            input=repo_value,
            local_path=str(local_path),
            cached=True,
            message="Using local repository path.",
        )
    if source != "github":
        raise ValueError(f"Unsupported repository source: {repo_source}")

    github_input = github_value or repo_value
    parsed = parse_github_repository_input(github_input)
    if parsed is None:
        raise ValueError("GitHub repository source must be a github.com URL, SSH remote, or owner/repo slug.")

    target = github_cache_path(parsed.owner, parsed.repo, cache_root)
    existed = target.exists()
    if not existed:
        if not clone_if_missing:
            raise FileNotFoundError(f"GitHub repository has not been cloned yet: {parsed.html_url}")
        target.parent.mkdir(parents=True, exist_ok=True)
        _run_git_command(["clone", "--depth", "1", parsed.clone_url, str(target)])
        message = f"Cloned {parsed.html_url} into the local RepoPilot cache."
    else:
        _ensure_cached_repository(target)
        message = f"Using cached clone for {parsed.html_url}."

    return RepositorySource(
        source="github",
        input=github_input,
        local_path=str(target.resolve()),
        github_url=parsed.html_url,
        owner=parsed.owner,
        repo=parsed.repo,
        cached=existed,
        message=message,
    )


def parse_github_repository_input(value: str | Path | None) -> GitHubRepositoryInput | None:
    text = str(value or "").strip()
    if not text:
        return None

    ssh_match = GITHUB_SSH_PATTERN.match(text)
    if ssh_match:
        owner = ssh_match.group("owner")
        repo = _clean_repo_name(ssh_match.group("repo"))
        if _valid_owner_repo(owner, repo):
            return GitHubRepositoryInput(
                owner=owner,
                repo=repo,
                clone_url=text if text.endswith(".git") else f"git@github.com:{owner}/{repo}.git",
                html_url=f"https://github.com/{owner}/{repo}",
            )
        return None

    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"} and parsed.netloc.lower() == "github.com":
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            return None
        owner = parts[0]
        repo = _clean_repo_name(parts[1])
        if _valid_owner_repo(owner, repo):
            return GitHubRepositoryInput(
                owner=owner,
                repo=repo,
                clone_url=f"https://github.com/{owner}/{repo}.git",
                html_url=f"https://github.com/{owner}/{repo}",
            )
        return None

    slug_match = GITHUB_SLUG_PATTERN.match(text)
    if slug_match:
        owner = slug_match.group("owner")
        repo = _clean_repo_name(slug_match.group("repo"))
        if _valid_owner_repo(owner, repo):
            return GitHubRepositoryInput(
                owner=owner,
                repo=repo,
                clone_url=f"https://github.com/{owner}/{repo}.git",
                html_url=f"https://github.com/{owner}/{repo}",
            )
    return None


def github_cache_path(owner: str, repo: str, cache_root: str | Path | None = None) -> Path:
    root = Path(cache_root or os.getenv("REPOPILOT_REPO_CACHE") or Path.cwd() / ".repopilot" / "repos")
    return root.expanduser().resolve() / _safe_segment(owner) / _safe_segment(repo)


def _clean_repo_name(repo: str) -> str:
    return repo.removesuffix(".git")


def _valid_owner_repo(owner: str, repo: str) -> bool:
    return bool(owner and repo and GITHUB_SLUG_PATTERN.match(f"{owner}/{repo}"))


def _safe_segment(value: str) -> str:
    cleaned = SAFE_SEGMENT_PATTERN.sub("_", value).strip("._")
    if not cleaned:
        raise ValueError("GitHub owner and repository names must contain safe path characters.")
    return cleaned


def _ensure_cached_repository(path: Path) -> None:
    if not (path / ".git").exists():
        raise RuntimeError(f"Cached repository path is not a Git repository: {path}")


def _run_git_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], text=True, capture_output=True)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"Git command failed: {' '.join(args)}"
        raise RuntimeError(message)
    return result
