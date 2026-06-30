"""Human-approved file edit application for RepoPilot."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .git_tools import get_git_diff
from .models import FileEditProposal
from .safety import (
    BLOCKED_DIRS,
    BLOCKED_FILENAMES,
    SafetyCheckError,
    SafetyCheckResult,
    check_file_edits,
)


@dataclass(frozen=True)
class ApplyResult:
    applied: bool
    changed_files: list[str]
    diff: str
    message: str
    safety_check: SafetyCheckResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def apply_file_edits(
    repo_path: str | Path,
    edits: list[FileEditProposal],
    task: str = "",
    allowed_paths: list[str] | set[str] | None = None,
) -> ApplyResult:
    root = Path(repo_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Repository path does not exist: {root}")
    if not edits:
        return ApplyResult(applied=False, changed_files=[], diff=_safe_git_diff(root), message="No edits to apply.")

    safety_check = check_file_edits(root, edits, task=task, allowed_paths=allowed_paths)
    if not safety_check.ok:
        raise SafetyCheckError(safety_check)

    changed_files: list[str] = []
    for edit in edits:
        target = _safe_target(root, edit.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.read_text(encoding="utf-8") if target.exists() else None
        if existing != edit.new_content:
            target.write_text(edit.new_content, encoding="utf-8")
            changed_files.append(edit.path)

    message = f"Applied {len(changed_files)} file edit(s)." if changed_files else "No file content changed."
    return ApplyResult(
        applied=bool(changed_files),
        changed_files=changed_files,
        diff=_safe_git_diff(root),
        message=message,
        safety_check=safety_check,
    )


def parse_file_edits(raw_edits: object) -> list[FileEditProposal]:
    if not isinstance(raw_edits, list):
        raise ValueError("file_edits must be a list.")
    edits: list[FileEditProposal] = []
    for raw_edit in raw_edits:
        if not isinstance(raw_edit, dict):
            raise ValueError("Each file edit must be an object.")
        path = raw_edit.get("path")
        new_content = raw_edit.get("new_content")
        rationale = raw_edit.get("rationale", "")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Each file edit must include a non-empty path.")
        if not isinstance(new_content, str):
            raise ValueError(f"File edit for {path} must include new_content as a string.")
        if not isinstance(rationale, str):
            raise ValueError(f"File edit for {path} must include rationale as a string.")
        edits.append(FileEditProposal(path=_normalize_path(path), new_content=new_content, rationale=rationale.strip()))
    return edits


def _safe_target(root: Path, path: str) -> Path:
    relative_path = _normalize_path(path)
    parts = PurePosixPath(relative_path).parts
    if any(part in BLOCKED_DIRS for part in parts):
        raise ValueError(f"Refusing to edit blocked directory path: {relative_path}")
    if parts[-1] in BLOCKED_FILENAMES:
        raise ValueError(f"Refusing to edit blocked file: {relative_path}")
    target = (root / Path(*parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Refusing to edit path outside repository: {relative_path}") from exc
    return target


def _normalize_path(path: str) -> str:
    normalized = PurePosixPath(path.replace("\\", "/"))
    parts = normalized.parts
    if normalized.is_absolute() or ".." in parts or any(part in {"", "."} for part in parts):
        raise ValueError(f"Unsafe file edit path: {path}")
    return normalized.as_posix()


def _safe_git_diff(root: Path) -> str:
    try:
        return get_git_diff(root)
    except Exception:
        return ""
