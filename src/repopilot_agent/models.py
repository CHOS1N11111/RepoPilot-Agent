"""Core data models for the local RepoPilot workflow."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RepoFile:
    path: Path
    relative_path: str
    size_bytes: int
    language: str
    content: str


@dataclass(frozen=True)
class SearchHit:
    path: str
    score: int
    reasons: list[str]
    preview: str


@dataclass(frozen=True)
class PlanStep:
    order: int
    title: str
    detail: str
    status: str = "pending"


@dataclass(frozen=True)
class PlanMetadata:
    source: str
    model: str | None = None
    fallback_used: bool = False
    error: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    command: str
    allowed: bool
    exit_code: int | None
    stdout: str
    stderr: str


@dataclass(frozen=True)
class FileChangeProposal:
    path: str
    change_type: str
    rationale: str
    suggested_actions: list[str]
    confidence: str


@dataclass(frozen=True)
class RiskNote:
    level: str
    message: str
    mitigation: str


@dataclass(frozen=True)
class PatchProposal:
    objective: str
    files: list[FileChangeProposal]
    risks: list[RiskNote]
    validation_suggestions: list[str]
    ready_for_patch: bool


@dataclass(frozen=True)
class GitRemote:
    name: str
    url: str
    kind: str


@dataclass(frozen=True)
class GitCommit:
    short_hash: str
    subject: str
    author: str
    date: str


@dataclass(frozen=True)
class GitFileChange:
    path: str
    index_status: str
    working_tree_status: str
    description: str


@dataclass(frozen=True)
class GitRepositoryState:
    repo_path: str
    branch: str
    upstream: str | None
    ahead: int
    behind: int
    remotes: list[GitRemote]
    latest_commit: GitCommit | None
    changes: list[GitFileChange]
    diff_stat: str
    staged_diff_stat: str

    @property
    def clean(self) -> bool:
        return not self.changes


@dataclass(frozen=True)
class PullRequestDraft:
    title: str
    body: str


@dataclass(frozen=True)
class GitWorkflowSummary:
    state: GitRepositoryState
    suggested_commit_message: str
    change_summary: list[str]
    validation_notes: list[str]
    pull_request: PullRequestDraft

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GitHubRepositoryRef:
    owner: str
    repo: str
    html_url: str


@dataclass(frozen=True)
class GitHubIssue:
    number: int
    title: str
    state: str
    author: str
    labels: list[str]
    updated_at: str
    html_url: str


@dataclass(frozen=True)
class GitHubReview:
    reviewer: str
    state: str
    submitted_at: str | None
    body_preview: str
    html_url: str


@dataclass(frozen=True)
class GitHubCheck:
    name: str
    status: str
    conclusion: str | None
    html_url: str | None


@dataclass(frozen=True)
class GitHubPullRequest:
    number: int
    title: str
    state: str
    author: str
    source_branch: str
    target_branch: str
    head_sha: str
    updated_at: str
    html_url: str
    reviews: list[GitHubReview]
    checks: list[GitHubCheck]


@dataclass(frozen=True)
class GitHubSnapshot:
    repository: GitHubRepositoryRef | None
    issues: list[GitHubIssue]
    pull_requests: list[GitHubPullRequest]
    unavailable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkflowReport:
    task: str
    repo_path: str
    files_scanned: int
    relevant_files: list[SearchHit] = field(default_factory=list)
    plan: list[PlanStep] = field(default_factory=list)
    plan_metadata: PlanMetadata = field(default_factory=lambda: PlanMetadata(source="rules"))
    patch_proposal: PatchProposal | None = None
    validation: list[ValidationResult] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
