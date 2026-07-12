"""Git workflow summaries and PR draft generation."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import re

from .git_tools import get_remote_default_branch, inspect_repository
from .github_tools import parse_github_remote
from .models import (
    GitFileChange,
    GitRemote,
    GitWorkflowSummary,
    GitHubRepositoryRef,
    PullRequestDraft,
    PullRequestReadiness,
)

PROTECTED_BRANCHES = {"main", "master", "trunk", "develop", "dev"}
SAFE_GIT_ARG_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")


def build_git_workflow_summary(
    repo_path: str | Path,
    validation_notes: list[str] | None = None,
) -> GitWorkflowSummary:
    state = inspect_repository(repo_path)
    notes = validation_notes or []
    change_summary = _summarize_changes(state.changes)
    commit_message = _suggest_commit_message(state.changes)
    pr_draft = _build_pr_draft(change_summary, commit_message, notes)
    pr_readiness = build_pull_request_readiness(
        repo_path,
        pull_request_title=pr_draft.title,
    )
    return GitWorkflowSummary(
        state=state,
        suggested_commit_message=commit_message,
        change_summary=change_summary,
        validation_notes=notes,
        pull_request=pr_draft,
        pr_readiness=pr_readiness,
    )


def build_pull_request_readiness(
    repo_path: str | Path,
    base_branch: str | None = None,
    pull_request_title: str | None = None,
) -> PullRequestReadiness:
    state = inspect_repository(repo_path)
    github_remote, repository = _github_remote_and_repository(state.remotes)
    remote_name = github_remote.name if github_remote else None
    base = base_branch or (
        get_remote_default_branch(repo_path, remote_name) if remote_name else None
    ) or "main"
    head = state.branch
    clean_worktree = state.clean
    upstream_pushed = bool(state.upstream) and state.ahead == 0
    needs_commit = not clean_worktree
    needs_push = bool(repository) and (not state.upstream or state.ahead > 0)
    blockers: list[str] = []
    warnings: list[str] = []
    next_steps: list[str] = []
    suggested_commands: list[str] = []

    if repository is None:
        blockers.append("No GitHub remote was found for this repository.")
        next_steps.append("Add a GitHub remote before preparing a pull request.")
        suggested_commands.append("git remote -v")
    if head in {"unknown", "HEAD"} or head.startswith("(HEAD"):
        blockers.append("The repository is not on a named branch.")
        next_steps.append("Create or switch to a named feature branch.")
        suggested_commands.append("git switch -c feature/repopilot-change")
    elif head in PROTECTED_BRANCHES or head == base:
        blockers.append(
            f"Current branch '{head}' looks like a base branch; create a feature branch first."
        )
        next_steps.append("Move the work to a feature branch before opening a pull request.")
        suggested_commands.append("git switch -c feature/repopilot-change")

    if needs_commit:
        blockers.append("The working tree has uncommitted changes.")
        next_steps.append("Review, stage, and commit local changes before opening a pull request.")
        suggested_commands.extend(
            [
                "git status --short",
                "git add <files>",
                f"git commit -m {_quote_git_arg(pull_request_title or 'Update RepoPilot workflow')}",
            ]
        )
    if state.behind > 0:
        blockers.append(f"The branch is behind its upstream by {state.behind} commit(s).")
        next_steps.append("Update the branch before preparing the pull request.")
        suggested_commands.append("git pull --ff-only")
    if repository is not None and not state.upstream and head not in {"unknown", "HEAD"}:
        blockers.append("The current branch has no upstream branch on GitHub.")
        next_steps.append("Push the branch to GitHub and set the upstream.")
        suggested_commands.append(f"git push -u {remote_name or 'origin'} {_quote_git_arg(head)}")
    elif repository is not None and state.ahead > 0:
        blockers.append(f"The branch is ahead of upstream by {state.ahead} commit(s).")
        next_steps.append("Push local commits to GitHub before creating the pull request.")
        suggested_commands.append("git push")

    if repository is not None and state.upstream and state.ahead == 0 and not needs_commit:
        warnings.append("RepoPilot does not commit or push; PR creation requires explicit confirmation.")
        next_steps.append("Review the generated title, body, and PR command before publishing.")

    create_pr_command = None
    if repository is not None and head not in {"unknown", "HEAD"} and not head.startswith("(HEAD"):
        create_pr_command = (
            f"gh pr create --repo {repository.owner}/{repository.repo} "
            f"--base {_quote_git_arg(base)} "
            f"--head {_quote_git_arg(head)} "
            f"--title {_quote_git_arg(pull_request_title or 'Update RepoPilot workflow')} "
            "--body-file PR_BODY.md"
        )
        if not blockers:
            suggested_commands.append(create_pr_command)

    ready = not blockers
    if ready and not next_steps:
        next_steps.append("Review the generated PR draft and create the pull request when ready.")

    return PullRequestReadiness(
        ready=ready,
        repository=repository,
        remote_name=remote_name,
        branch=head,
        upstream=state.upstream,
        base_branch=base,
        head_branch=head,
        clean_worktree=clean_worktree,
        upstream_pushed=upstream_pushed,
        needs_commit=needs_commit,
        needs_push=needs_push,
        blockers=blockers,
        warnings=warnings,
        next_steps=_dedupe(next_steps),
        suggested_commands=_dedupe(suggested_commands),
        create_pr_command=create_pr_command,
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


def _github_remote_and_repository(remotes: list[GitRemote]) -> tuple[GitRemote | None, GitHubRepositoryRef | None]:
    for remote in remotes:
        if remote.kind != "fetch":
            continue
        parsed = parse_github_remote(remote.url)
        if parsed is None:
            continue
        owner, repo = parsed
        return remote, GitHubRepositoryRef(owner=owner, repo=repo, html_url=f"https://github.com/{owner}/{repo}")
    return None, None


def _quote_git_arg(value: str) -> str:
    if SAFE_GIT_ARG_PATTERN.match(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
