"""Read-only Git repository inspection tools."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .models import GitCommit, GitFileChange, GitRemote, GitRepositoryState

STATUS_PATTERN = re.compile(r"^## (?P<branch>[^\.\s]+|\S+)(?:\.\.\.(?P<upstream>[^\s]+))?(?: \[(?P<tracking>.+)\])?")

STATUS_DESCRIPTIONS = {
    " ": "unchanged",
    "A": "added",
    "D": "deleted",
    "M": "modified",
    "R": "renamed",
    "C": "copied",
    "U": "unmerged",
    "?": "untracked",
    "!": "ignored",
}


def inspect_repository(repo_path: str | Path) -> GitRepositoryState:
    root = _repository_root(repo_path)
    status_output = _run_git(root, ["status", "--porcelain=v1", "-b"]).stdout
    branch, upstream, ahead, behind, changes = _parse_status(status_output)
    return GitRepositoryState(
        repo_path=str(root),
        branch=branch,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        remotes=_parse_remotes(_run_git(root, ["remote", "-v"]).stdout),
        latest_commit=_latest_commit(root),
        changes=changes,
        diff_stat=_run_git(root, ["diff", "--stat"]).stdout.strip(),
        staged_diff_stat=_run_git(root, ["diff", "--cached", "--stat"]).stdout.strip(),
    )


def get_git_diff(repo_path: str | Path, staged: bool = False) -> str:
    root = _repository_root(repo_path)
    args = ["diff", "--cached"] if staged else ["diff"]
    return _run_git(root, args).stdout


def _repository_root(repo_path: str | Path) -> Path:
    candidate = Path(repo_path).expanduser().resolve()
    result = _run_raw_git(candidate, ["rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        raise RuntimeError(f"Not a Git repository: {candidate}")
    return Path(result.stdout.strip()).resolve()


def _latest_commit(root: Path) -> GitCommit | None:
    result = _run_raw_git(
        root,
        ["log", "-1", "--pretty=format:%h%x09%s%x09%an%x09%ad", "--date=short"],
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    parts = result.stdout.strip().split("\t", maxsplit=3)
    while len(parts) < 4:
        parts.append("")
    return GitCommit(short_hash=parts[0], subject=parts[1], author=parts[2], date=parts[3])


def _run_git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    result = _run_raw_git(root, args)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"Git command failed: {' '.join(args)}")
    return result


def _run_raw_git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={root}", "-C", str(root), *args],
        text=True,
        capture_output=True,
    )


def _parse_status(output: str) -> tuple[str, str | None, int, int, list[GitFileChange]]:
    lines = output.splitlines()
    branch = "unknown"
    upstream: str | None = None
    ahead = 0
    behind = 0
    changes: list[GitFileChange] = []

    if lines:
        if lines[0].startswith("## No commits yet on "):
            branch = lines[0].removeprefix("## No commits yet on ").strip()
        else:
            match = STATUS_PATTERN.match(lines[0])
            if match:
                branch = match.group("branch")
                upstream = match.group("upstream")
                ahead, behind = _parse_tracking(match.group("tracking") or "")

    for line in lines[1:]:
        if len(line) < 4:
            continue
        index_status = line[0]
        working_tree_status = line[1]
        path = line[3:]
        changes.append(
            GitFileChange(
                path=path,
                index_status=index_status,
                working_tree_status=working_tree_status,
                description=_describe_status(index_status, working_tree_status),
            )
        )
    return branch, upstream, ahead, behind, changes


def _parse_tracking(tracking: str) -> tuple[int, int]:
    ahead = 0
    behind = 0
    for part in tracking.split(","):
        cleaned = part.strip()
        if cleaned.startswith("ahead "):
            ahead = int(cleaned.removeprefix("ahead "))
        elif cleaned.startswith("behind "):
            behind = int(cleaned.removeprefix("behind "))
    return ahead, behind


def _describe_status(index_status: str, working_tree_status: str) -> str:
    if index_status == "?" and working_tree_status == "?":
        return "untracked"
    index = STATUS_DESCRIPTIONS.get(index_status, index_status)
    working = STATUS_DESCRIPTIONS.get(working_tree_status, working_tree_status)
    if index_status != " " and working_tree_status != " ":
        return f"staged {index}, working tree {working}"
    if index_status != " ":
        return f"staged {index}"
    return f"working tree {working}"


def _parse_remotes(output: str) -> list[GitRemote]:
    remotes: list[GitRemote] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            remotes.append(GitRemote(name=parts[0], url=parts[1], kind=parts[2].strip("()")))
    return remotes
