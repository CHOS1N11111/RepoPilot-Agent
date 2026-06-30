"""Structured pre-apply safety checks for proposed file edits."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .models import FileEditProposal

BLOCKED_FILENAMES = {".env", ".env.local", "log.md"}
BLOCKED_DIRS = {".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv"}
BLOCKING_LEVELS = {"high"}
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]{3,}")


@dataclass(frozen=True)
class SafetyFinding:
    level: str
    code: str
    path: str | None
    message: str
    mitigation: str


@dataclass(frozen=True)
class SafetyCheckResult:
    ok: bool
    findings: list[SafetyFinding]
    checked_files: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SafetyCheckError(ValueError):
    def __init__(self, result: SafetyCheckResult) -> None:
        self.result = result
        summary = "; ".join(f"{finding.code}: {finding.message}" for finding in result.findings)
        super().__init__(summary or "Safety check failed.")


def check_file_edits(
    repo_path: str | Path,
    edits: list[FileEditProposal],
    task: str = "",
    allowed_paths: set[str] | list[str] | None = None,
) -> SafetyCheckResult:
    root = Path(repo_path).expanduser().resolve()
    findings: list[SafetyFinding] = []
    checked_files: list[str] = []
    seen_paths: set[str] = set()
    normalized_allowed: set[str] = set()
    for path in allowed_paths or []:
        try:
            normalized_allowed.add(_normalize_path(path))
        except SafetyCheckError as exc:
            findings.extend(exc.result.findings)
    task_terms = _task_terms(task)

    if not root.exists() or not root.is_dir():
        findings.append(
            SafetyFinding(
                level="high",
                code="repo_unavailable",
                path=None,
                message=f"Repository path does not exist or is not a directory: {root}",
                mitigation="Resolve the repository path before applying edits.",
            )
        )
        return _result(findings, checked_files)

    if not edits:
        findings.append(
            SafetyFinding(
                level="medium",
                code="no_edits",
                path=None,
                message="No file edits were provided.",
                mitigation="Generate an apply-ready proposal before applying changes.",
            )
        )
        return _result(findings, checked_files)

    for edit in edits:
        try:
            relative_path = _normalize_path(edit.path)
        except SafetyCheckError as exc:
            checked_files.append(edit.path)
            findings.extend(exc.result.findings)
            continue
        checked_files.append(relative_path)
        parts = PurePosixPath(relative_path).parts
        if relative_path in seen_paths:
            findings.append(
                SafetyFinding(
                    level="high",
                    code="duplicate_edit",
                    path=relative_path,
                    message="The proposal contains multiple edits for the same file.",
                    mitigation="Merge duplicate edits into one complete replacement before applying.",
                )
            )
        seen_paths.add(relative_path)

        findings.extend(_check_path(root, relative_path, parts, normalized_allowed))
        target = _resolve_target(root, parts)
        if target is None:
            continue
        original = _read_existing(target)
        findings.extend(_check_content(relative_path, original, edit.new_content))
        relevance = _check_task_relevance(relative_path, edit, task_terms)
        if relevance is not None:
            findings.append(relevance)

    return _result(findings, checked_files)


def _check_path(
    root: Path,
    relative_path: str,
    parts: tuple[str, ...],
    allowed_paths: set[str],
) -> list[SafetyFinding]:
    findings: list[SafetyFinding] = []
    if any(part in BLOCKED_DIRS for part in parts):
        findings.append(
            SafetyFinding(
                level="high",
                code="blocked_directory",
                path=relative_path,
                message="The edit targets a blocked directory.",
                mitigation="Choose a normal repository source or test file instead.",
            )
        )
    if parts[-1] in BLOCKED_FILENAMES:
        findings.append(
            SafetyFinding(
                level="high",
                code="blocked_file",
                path=relative_path,
                message="The edit targets a blocked file.",
                mitigation="Do not edit secrets, local logs, or protected local-only files.",
            )
        )
    target = _resolve_target(root, parts)
    if target is None:
        findings.append(
            SafetyFinding(
                level="high",
                code="path_escape",
                path=relative_path,
                message="The edit path could not be resolved safely inside the repository.",
                mitigation="Use a repository-relative path without traversal.",
            )
        )
    elif not _is_relative_to(target, root):
        findings.append(
            SafetyFinding(
                level="high",
                code="path_escape",
                path=relative_path,
                message="The edit path resolves outside the repository.",
                mitigation="Use a repository-relative path inside the selected repository.",
            )
        )
    if allowed_paths and relative_path not in allowed_paths:
        findings.append(
            SafetyFinding(
                level="high",
                code="path_not_in_proposal",
                path=relative_path,
                message="The edit path was not part of the approved proposal file list.",
                mitigation="Regenerate the proposal or approve a proposal that explicitly includes this file.",
            )
        )
    return findings


def _check_content(relative_path: str, original: str | None, new_content: str) -> list[SafetyFinding]:
    findings: list[SafetyFinding] = []
    if original is not None and original.strip() and not new_content.strip():
        findings.append(
            SafetyFinding(
                level="high",
                code="empty_overwrite",
                path=relative_path,
                message="The edit would replace a non-empty file with empty content.",
                mitigation="Regenerate the edit or manually inspect the proposed replacement.",
            )
        )
    if original is not None and _is_large_deletion(original, new_content):
        findings.append(
            SafetyFinding(
                level="high",
                code="large_deletion",
                path=relative_path,
                message="The edit removes most of the existing file content.",
                mitigation="Split broad rewrites into smaller reviewed changes or confirm the deletion manually.",
            )
        )
    repeated_line = _dominant_repeated_line(new_content)
    if repeated_line:
        findings.append(
            SafetyFinding(
                level="medium",
                code="repeated_content",
                path=relative_path,
                message="The replacement content contains a suspiciously repeated line.",
                mitigation="Inspect the generated content before applying.",
            )
        )
    if original == new_content:
        findings.append(
            SafetyFinding(
                level="medium",
                code="no_effective_change",
                path=relative_path,
                message="The edit does not change the current file content.",
                mitigation="Skip this edit or regenerate the proposal with a concrete change.",
            )
        )
    return findings


def _check_task_relevance(
    relative_path: str,
    edit: FileEditProposal,
    task_terms: set[str],
) -> SafetyFinding | None:
    if not task_terms:
        return None
    haystack = " ".join([relative_path, edit.rationale, edit.new_content[:2_000]]).lower()
    if any(term in haystack for term in task_terms):
        return None
    return SafetyFinding(
        level="medium",
        code="weak_task_relevance",
        path=relative_path,
        message="The edit has weak textual overlap with the task.",
        mitigation="Confirm this file is relevant before applying the proposal.",
    )


def _normalize_path(path: str) -> str:
    normalized = PurePosixPath(path.replace("\\", "/"))
    parts = normalized.parts
    if normalized.is_absolute() or ".." in parts or any(part in {"", "."} for part in parts):
        raise SafetyCheckError(
            SafetyCheckResult(
                ok=False,
                checked_files=[],
                findings=[
                    SafetyFinding(
                        level="high",
                        code="unsafe_path",
                        path=path,
                        message="The edit path is not a safe repository-relative path.",
                        mitigation="Use a normal relative path without absolute prefixes or parent traversal.",
                    )
                ],
            )
        )
    return normalized.as_posix()


def _resolve_target(root: Path, parts: tuple[str, ...]) -> Path | None:
    try:
        return (root / Path(*parts)).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _read_existing(target: Path) -> str | None:
    if not target.exists():
        return None
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return target.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            return ""


def _is_large_deletion(original: str, new_content: str) -> bool:
    original_lines = original.splitlines()
    new_lines = new_content.splitlines()
    if len(original_lines) < 8:
        return False
    if len(new_lines) <= 2 and len(original_lines) >= 8:
        return True
    original_chars = len(original.strip())
    new_chars = len(new_content.strip())
    if original_chars < 400:
        return False
    return new_chars < original_chars * 0.25


def _dominant_repeated_line(content: str) -> str | None:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if len(lines) < 20:
        return None
    counts: dict[str, int] = {}
    for line in lines:
        counts[line] = counts.get(line, 0) + 1
    line, count = max(counts.items(), key=lambda item: item[1])
    if count >= 12 and count / len(lines) >= 0.6:
        return line
    return None


def _task_terms(task: str) -> set[str]:
    ignored = {"and", "bug", "change", "code", "fix", "for", "from", "issue", "task", "the", "this", "with"}
    return {token.lower() for token in TOKEN_PATTERN.findall(task) if token.lower() not in ignored}


def _result(findings: list[SafetyFinding], checked_files: list[str]) -> SafetyCheckResult:
    return SafetyCheckResult(
        ok=not any(finding.level in BLOCKING_LEVELS for finding in findings),
        findings=findings,
        checked_files=checked_files,
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
