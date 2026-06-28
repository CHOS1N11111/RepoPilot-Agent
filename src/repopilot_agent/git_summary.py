"""Git workflow summaries and PR draft generation."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .git_tools import inspect_repository
from .models import GitFileChange, GitWorkflowSummary, PullRequestDraft


def build_git_workflow_summary(
    repo_path: str | Path,
    validation_notes: list[str] | None = None,
) -> GitWorkflowSummary:
    state = inspect_repository(repo_path)
    notes = validation_notes or []
    change_summary = _summarize_changes(state.changes)
    commit_message = _suggest_commit_message(state.changes)
    pr_draft = _build_pr_draft(change_summary, commit_message, notes)
    return GitWorkflowSummary(
        state=state,
        suggested_commit_message=commit_message,
        change_summary=change_summary,
        validation_notes=notes,
        pull_request=pr_draft,
    )


def _summarize_changes(changes: list[GitFileChange]) -> list[str]:
    if not changes:
        return ["No local working tree changes detected."]

    summaries = []
    status_counts = Counter(change.description for change in changes)
    for description, count in sorted(status_counts.items()):
        summaries.append(f"{count} file(s) {description}.")

    top_paths = [change.path for change in changes[:8]]
    summaries.append("Changed paths: " + ", ".join(top_paths))
    if len(changes) > len(top_paths):
        summaries.append(f"{len(changes) - len(top_paths)} additional changed file(s) not shown.")
    return summaries


def _suggest_commit_message(changes: list[GitFileChange]) -> str:
    if not changes:
        return "No local changes to commit"

    paths = [change.path.lower() for change in changes]
    if any(path.startswith("tests/") or "test" in path for path in paths):
        prefix = "Add"
    elif any(path.endswith(".md") for path in paths):
        prefix = "Update"
    else:
        prefix = "Refine"

    if any("git" in path for path in paths):
        topic = "Git workflow awareness"
    elif any("patch" in path for path in paths):
        topic = "patch proposal workflow"
    elif any(path.endswith(".md") for path in paths):
        topic = "project documentation"
    else:
        topic = "RepoPilot workflow"
    return f"{prefix} {topic}"


def _build_pr_draft(
    change_summary: list[str],
    commit_message: str,
    validation_notes: list[str],
) -> PullRequestDraft:
    title = commit_message if commit_message != "No local changes to commit" else "No local changes detected"
    validation = validation_notes or ["Validation not provided."]
    body_lines = [
        "## What changed",
        *[f"- {line}" for line in change_summary],
        "",
        "## Why",
        "- Keep the repository workflow visible before committing, pushing, or opening a pull request.",
        "",
        "## Validation",
        *[f"- {line}" for line in validation],
        "",
        "## Risks",
        "- Git command output can vary by platform and repository state.",
        "- Review generated commit and PR text before publishing.",
    ]
    return PullRequestDraft(title=title, body="\n".join(body_lines))
