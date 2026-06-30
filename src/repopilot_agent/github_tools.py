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
    GitHubComment,
    GitHubIssue,
    GitHubPullRequest,
    GitHubPullRequestFile,
    GitHubRepositoryRef,
    GitHubReview,
    GitHubReviewComment,
    GitHubSnapshot,
)

GITHUB_API_BASE = "https://api.github.com"
HTTPS_REMOTE_PATTERN = re.compile(r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")
SSH_REMOTE_PATTERN = re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$")
PREVIEW_LIMIT = 800
COMMENT_LIMIT = 5
FILE_LIMIT = 12


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
                body_preview=_preview(item.get("body") or ""),
                comments=_fetch_issue_comments(client, repository, item["number"]),
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
                body_preview=_preview(item.get("body") or ""),
                comments=_fetch_issue_comments(client, repository, number),
                files=_fetch_pull_request_files(client, repository, number),
                review_comments=_fetch_review_comments(client, repository, number),
                reviews=_fetch_reviews(client, repository, number),
                checks=_fetch_checks(client, repository, head_sha),
            )
        )
    return pull_requests


def _fetch_issue_comments(
    client: GitHubClient,
    repository: GitHubRepositoryRef,
    number: int,
) -> list[GitHubComment]:
    raw_comments = client.get_json(
        f"/repos/{repository.owner}/{repository.repo}/issues/{number}/comments",
        {"per_page": COMMENT_LIMIT},
    )
    comments: list[GitHubComment] = []
    for item in raw_comments[-COMMENT_LIMIT:]:
        body = item.get("body") or ""
        comments.append(
            GitHubComment(
                author=item.get("user", {}).get("login", "unknown"),
                created_at=item.get("created_at", ""),
                updated_at=item.get("updated_at", ""),
                body_preview=_preview(body),
                html_url=item.get("html_url", ""),
            )
        )
    return comments


def _fetch_pull_request_files(
    client: GitHubClient,
    repository: GitHubRepositoryRef,
    pull_number: int,
) -> list[GitHubPullRequestFile]:
    raw_files = client.get_json(
        f"/repos/{repository.owner}/{repository.repo}/pulls/{pull_number}/files",
        {"per_page": FILE_LIMIT},
    )
    files: list[GitHubPullRequestFile] = []
    for item in raw_files[:FILE_LIMIT]:
        files.append(
            GitHubPullRequestFile(
                filename=item.get("filename", ""),
                status=item.get("status", "unknown"),
                additions=int(item.get("additions") or 0),
                deletions=int(item.get("deletions") or 0),
                changes=int(item.get("changes") or 0),
                patch_preview=_preview(item.get("patch") or ""),
                raw_url=item.get("raw_url"),
                blob_url=item.get("blob_url"),
            )
        )
    return files


def _fetch_review_comments(
    client: GitHubClient,
    repository: GitHubRepositoryRef,
    pull_number: int,
) -> list[GitHubReviewComment]:
    raw_comments = client.get_json(
        f"/repos/{repository.owner}/{repository.repo}/pulls/{pull_number}/comments",
        {"per_page": COMMENT_LIMIT},
    )
    comments: list[GitHubReviewComment] = []
    for item in raw_comments[-COMMENT_LIMIT:]:
        comments.append(
            GitHubReviewComment(
                reviewer=item.get("user", {}).get("login", "unknown"),
                path=item.get("path", ""),
                line=item.get("line"),
                side=item.get("side"),
                body_preview=_preview(item.get("body") or ""),
                html_url=item.get("html_url", ""),
            )
        )
    return comments


def _fetch_reviews(
    client: GitHubClient,
    repository: GitHubRepositoryRef,
    pull_number: int,
) -> list[GitHubReview]:
    raw_reviews = client.get_json(f"/repos/{repository.owner}/{repository.repo}/pulls/{pull_number}/reviews")
    reviews: list[GitHubReview] = []
    review_comments = _fetch_review_comments(client, repository, pull_number)
    for item in raw_reviews[-5:]:
        body = item.get("body") or ""
        reviews.append(
            GitHubReview(
                reviewer=item.get("user", {}).get("login", "unknown"),
                state=item.get("state", "UNKNOWN"),
                submitted_at=item.get("submitted_at"),
                body_preview=_preview(body),
                html_url=item.get("html_url", ""),
                comments=[
                    comment
                    for comment in review_comments
                    if not item.get("html_url") or comment.html_url.startswith(item.get("html_url", ""))
                ][:COMMENT_LIMIT],
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
                started_at=item.get("started_at"),
                completed_at=item.get("completed_at"),
                output_title=item.get("output", {}).get("title"),
                output_summary_preview=_preview(item.get("output", {}).get("summary") or ""),
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
                output_summary_preview=_preview(item.get("description") or ""),
            )
        )
    return checks


def _preview(value: str, limit: int = PREVIEW_LIMIT) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."
