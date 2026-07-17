"""Managed Git worktree sandboxes for isolated RepoPilot changes."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any


DEFAULT_WORKTREE_DIR_NAME = "repopilot-worktrees"
WORKTREE_ROOT_ENV = "REPOPILOT_WORKTREE_ROOT"
SAFE_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")


class WorktreeSandboxError(RuntimeError):
    """Raised when a managed worktree operation cannot be completed safely."""


class DirtyWorktreeError(WorktreeSandboxError):
    """Raised when an operation would discard uncommitted work."""


@dataclass(frozen=True)
class WorktreeSandbox:
    source_repo: str
    path: str
    head: str
    branch: str | None
    detached: bool
    clean: bool | None
    primary: bool
    managed: bool
    locked: bool = False
    lock_reason: str = ""
    prunable: bool = False
    prune_reason: str = ""
    base_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorktreeRemoval:
    source_repo: str
    path: str
    removed: bool
    forced: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def create_worktree_sandbox(
    repo_path: str | Path,
    *,
    base_ref: str = "HEAD",
    name: str | None = None,
    worktree_root: str | Path | None = None,
) -> WorktreeSandbox:
    """Create a detached worktree from a clean source repository."""

    source = _resolve_repository_root(repo_path)
    if not _worktree_clean(source):
        raise DirtyWorktreeError(
            "Source repository has uncommitted changes. Commit, stash, or revert them before creating a sandbox."
        )

    managed_root = resolve_worktree_root(worktree_root)
    if _is_relative_to(managed_root, source):
        raise WorktreeSandboxError("Worktree sandbox root must be outside the source repository.")
    managed_root.mkdir(parents=True, exist_ok=True)

    requested_ref = str(base_ref or "HEAD").strip() or "HEAD"
    commit = _resolve_commit(source, requested_ref)
    sandbox_name = _validate_or_create_name(name, source.name, commit)
    destination = (managed_root / sandbox_name).resolve()
    if destination.parent != managed_root:
        raise WorktreeSandboxError("Sandbox path escaped the managed worktree root.")
    if destination.exists():
        raise WorktreeSandboxError(f"Sandbox path already exists: {destination}")

    _run_git(source, ["worktree", "add", "--detach", str(destination), commit])
    worktrees = _list_all_worktrees(source, managed_root)
    created = next((item for item in worktrees if _same_path(item.path, destination)), None)
    if created is None:
        raise WorktreeSandboxError("Git created the worktree but it was not returned by worktree list.")
    return replace(created, base_ref=requested_ref)


def list_worktree_sandboxes(
    repo_path: str | Path,
    *,
    worktree_root: str | Path | None = None,
) -> list[WorktreeSandbox]:
    """List only linked worktrees inside RepoPilot's managed root."""

    source = _resolve_repository_root(repo_path)
    managed_root = resolve_worktree_root(worktree_root)
    return [
        item
        for item in _list_all_worktrees(source, managed_root)
        if item.managed and not item.primary
    ]


def remove_worktree_sandbox(
    repo_path: str | Path,
    worktree_path: str | Path,
    *,
    force: bool = False,
    worktree_root: str | Path | None = None,
) -> WorktreeRemoval:
    """Remove a registered managed worktree without deleting arbitrary paths."""

    source = _resolve_repository_root(repo_path)
    managed_root = resolve_worktree_root(worktree_root)
    target = Path(worktree_path).expanduser().resolve()
    worktrees = _list_all_worktrees(source, managed_root)
    selected = next((item for item in worktrees if _same_path(item.path, target)), None)
    if selected is None:
        raise WorktreeSandboxError(f"Path is not a registered Git worktree: {target}")
    if selected.primary:
        raise WorktreeSandboxError("The primary Git worktree cannot be removed as a sandbox.")
    if not selected.managed or not _is_relative_to(target, managed_root):
        raise WorktreeSandboxError("Refusing to remove a worktree outside RepoPilot's managed root.")
    if selected.locked:
        reason = f": {selected.lock_reason}" if selected.lock_reason else ""
        raise WorktreeSandboxError(f"Sandbox is locked and must be unlocked before removal{reason}")
    if selected.clean is False and not force:
        raise DirtyWorktreeError(
            "Sandbox has uncommitted changes. Review them first or explicitly force removal."
        )

    primary = next((item for item in worktrees if item.primary), None)
    command_repo = Path(primary.path) if primary else source
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(target))
    _run_git(command_repo, args)
    return WorktreeRemoval(
        source_repo=str(command_repo.resolve()),
        path=str(target),
        removed=True,
        forced=force,
    )


def resolve_worktree_root(value: str | Path | None = None) -> Path:
    configured = value or os.getenv(WORKTREE_ROOT_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(tempfile.gettempdir()) / DEFAULT_WORKTREE_DIR_NAME).resolve()


def _resolve_repository_root(repo_path: str | Path) -> Path:
    candidate = Path(repo_path).expanduser().resolve()
    if not candidate.is_dir():
        raise WorktreeSandboxError(f"Repository directory does not exist: {candidate}")
    result = _run_git(candidate, ["rev-parse", "--show-toplevel"])
    root = Path(result.stdout.strip()).resolve()
    if not root.is_dir():
        raise WorktreeSandboxError(f"Git returned an invalid repository root: {root}")
    return root


def _resolve_commit(repo_path: Path, base_ref: str) -> str:
    result = _run_git(
        repo_path,
        ["rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}"],
    )
    commit = result.stdout.strip()
    if not commit:
        raise WorktreeSandboxError(f"Could not resolve sandbox base ref: {base_ref}")
    return commit


def _validate_or_create_name(name: str | None, repo_name: str, commit: str) -> str:
    if name is not None:
        candidate = name.strip()
        if not SAFE_NAME_PATTERN.fullmatch(candidate) or candidate in {".", ".."}:
            raise WorktreeSandboxError(
                "Sandbox name must be 1-80 characters using letters, numbers, dot, underscore, or hyphen."
            )
        return candidate
    safe_repo = re.sub(r"[^A-Za-z0-9._-]+", "-", repo_name).strip(".-_") or "repo"
    return f"{safe_repo}-{commit[:8]}-{uuid.uuid4().hex[:8]}"


def _list_all_worktrees(source: Path, managed_root: Path) -> list[WorktreeSandbox]:
    result = _run_git(source, ["worktree", "list", "--porcelain"])
    blocks = [block for block in result.stdout.replace("\r\n", "\n").split("\n\n") if block.strip()]
    parsed: list[WorktreeSandbox] = []
    primary_path = ""
    for index, block in enumerate(blocks):
        fields: dict[str, str] = {}
        flags: set[str] = set()
        for line in block.splitlines():
            key, separator, value = line.partition(" ")
            if separator:
                fields[key] = value
            elif key:
                flags.add(key)
        raw_path = fields.get("worktree", "")
        if not raw_path:
            continue
        path = Path(raw_path).resolve()
        if index == 0:
            primary_path = str(path)
        branch_ref = fields.get("branch")
        branch = branch_ref.removeprefix("refs/heads/") if branch_ref else None
        primary = _same_path(path, primary_path)
        parsed.append(
            WorktreeSandbox(
                source_repo=primary_path,
                path=str(path),
                head=fields.get("HEAD", ""),
                branch=branch,
                detached="detached" in flags,
                clean=_worktree_clean(path) if path.is_dir() else None,
                primary=primary,
                managed=_is_relative_to(path, managed_root),
                locked="locked" in flags or "locked" in fields,
                lock_reason=fields.get("locked", ""),
                prunable="prunable" in flags or "prunable" in fields,
                prune_reason=fields.get("prunable", ""),
            )
        )
    return parsed


def _worktree_clean(path: Path) -> bool:
    result = _run_git(path, ["status", "--porcelain=v1", "--untracked-files=all"])
    return not result.stdout.strip()


def _run_git(repo_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise WorktreeSandboxError("Git is required to manage worktree sandboxes.") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeSandboxError("Git worktree command timed out.") from exc
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "Git worktree command failed."
        raise WorktreeSandboxError(message)
    return result


def _same_path(first: str | Path, second: str | Path) -> bool:
    return os.path.normcase(str(Path(first).resolve())) == os.path.normcase(str(Path(second).resolve()))


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
