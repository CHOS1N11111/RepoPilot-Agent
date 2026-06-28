"""GitHub repository awareness through the GitHub REST API."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .git_tools import inspect_repository
from .models import (
    GitHubCheck,
    GitHubIssue,
    GitHubPullRequest,
    GitHubRepositoryRef,
    GitHubReview,
    GitHubSnapshot,
)

GITHUB_API_BASE = "https://api.github.com"
HTTPS_REMOTE_PATTERN = re.compile(r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")
SSH_REMOTE_PATTERN = re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$")


class GitHubClient:
    def __init__(self, token: str | None = None, api_base: str = GITHUB_API_BASE) -> None:
        self.token = token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
        self.api_base = api_base.rstrip("/")

    def get_json(self, path: str, query: dict[str, Any] | None = None) -> Any:
        url = self._build_url(path, query)
        request = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API request failed with HTTP {exc.code}: {message}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GitHub API request failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError("GitHub API request timed out.") from exc

    def _build_url(self, path: str, query: dict[str, Any] | None = None) -> str:
        encoded_query = urllib.parse.urlencode(query or {})
        url = f"{self.api_base}{path}"
        if encoded_query:
            url = f"{url}?{encoded_query}"
        return url

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "RepoPilot-Agent",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers


def inspect_github_repository(
    repo_path: str | Path,
    limit: int = 5,
    client: GitHubClient | None = None,
) -> GitHubSnapshot:
    repository = resolve_github_repository(repo_path)
    if repository is None:
        return GitHubSnapshot(
            repository=None,
            issues=[],
            pull_requests=[],
            unavailable_reason="No GitHub remote was found for this repository.",
        )

    active_client = client or GitHubClient()
    try:
        issues = _fetch_issues(active_client, repository, limit)
        pull_requests = _fetch_pull_requests(active_client, repository, limit)
    except RuntimeError as exc:
        return GitHubSnapshot(
            repository=repository,
            issues=[],
            pull_requests=[],
            unavailable_reason=str(exc),
        )
    return GitHubSnapshot(repository=repository, issues=issues, pull_requests=pull_requests)


def resolve_github_repository(repo_path: str | Path) -> GitHubRepositoryRef | None:
    state = inspect_repository(repo_path)
    for remote in state.remotes:
        if remote.kind != "fetch":
            continue
        parsed = parse_github_remote(remote.url)
        if parsed is not None:
            owner, repo = parsed
            return GitHubRepositoryRef(
                owner=owner,
                repo=repo,
                html_url=f"https://github.com/{owner}/{repo}",
            )
    return None


def parse_github_remote(remote_url: str) -> tuple[str, str] | None:
    for pattern in (HTTPS_REMOTE_PATTERN, SSH_REMOTE_PATTERN):
        match = pattern.match(remote_url)
        if match:
            return match.group("owner"), match.group("repo")
    return None


def _fetch_issues(client: GitHubClient, repository: GitHubRepositoryRef, limit: int) -> list[GitHubIssue]:
    raw_issues = client.get_json(
        f"/repos/{repository.owner}/{repository.repo}/issues",
        {"state": "open", "per_page": limit},
    )
    issues: list[GitHubIssue] = []
    for item in raw_issues:
        if "pull_request" in item:
            continue
        issues.append(
            GitHubIssue(
                number=item["number"],
                title=item["title"],
                state=item["state"],
                author=item.get("user", {}).get("login", "unknown"),
                labels=[label.get("name", "") for label in item.get("labels", []) if label.get("name")],
                updated_at=item.get("updated_at", ""),
                html_url=item.get("html_url", ""),
            )
        )
    return issues


def _fetch_pull_requests(
    client: GitHubClient,
    repository: GitHubRepositoryRef,
    limit: int,
) -> list[GitHubPullRequest]:
    raw_prs = client.get_json(
        f"/repos/{repository.owner}/{repository.repo}/pulls",
        {"state": "open", "per_page": limit},
    )
    pull_requests: list[GitHubPullRequest] = []
    for item in raw_prs:
        number = item["number"]
        head_sha = item.get("head", {}).get("sha", "")
        pull_requests.append(
            GitHubPullRequest(
                number=number,
                title=item["title"],
                state=item["state"],
                author=item.get("user", {}).get("login", "unknown"),
                source_branch=item.get("head", {}).get("ref", ""),
                target_branch=item.get("base", {}).get("ref", ""),
                head_sha=head_sha,
                updated_at=item.get("updated_at", ""),
                html_url=item.get("html_url", ""),
                reviews=_fetch_reviews(client, repository, number),
                checks=_fetch_checks(client, repository, head_sha),
            )
        )
    return pull_requests


def _fetch_reviews(
    client: GitHubClient,
    repository: GitHubRepositoryRef,
    pull_number: int,
) -> list[GitHubReview]:
    raw_reviews = client.get_json(f"/repos/{repository.owner}/{repository.repo}/pulls/{pull_number}/reviews")
    reviews: list[GitHubReview] = []
    for item in raw_reviews[-5:]:
        body = item.get("body") or ""
        reviews.append(
            GitHubReview(
                reviewer=item.get("user", {}).get("login", "unknown"),
                state=item.get("state", "UNKNOWN"),
                submitted_at=item.get("submitted_at"),
                body_preview=body[:160],
                html_url=item.get("html_url", ""),
            )
        )
    return reviews


def _fetch_checks(client: GitHubClient, repository: GitHubRepositoryRef, head_sha: str) -> list[GitHubCheck]:
    if not head_sha:
        return []

    checks: list[GitHubCheck] = []
    check_runs = client.get_json(f"/repos/{repository.owner}/{repository.repo}/commits/{head_sha}/check-runs")
    for item in check_runs.get("check_runs", []):
        checks.append(
            GitHubCheck(
                name=item.get("name", "unknown"),
                status=item.get("status", "unknown"),
                conclusion=item.get("conclusion"),
                html_url=item.get("html_url"),
            )
        )

    combined_status = client.get_json(f"/repos/{repository.owner}/{repository.repo}/commits/{head_sha}/status")
    for item in combined_status.get("statuses", []):
        checks.append(
            GitHubCheck(
                name=item.get("context", "status"),
                status=item.get("state", "unknown"),
                conclusion=item.get("state"),
                html_url=item.get("target_url"),
            )
        )
    return checks
