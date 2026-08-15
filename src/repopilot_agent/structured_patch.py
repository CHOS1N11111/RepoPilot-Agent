"""Conflict-aware exact-text patches for agent-authored repository edits."""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .models import FileEditProposal
from .patch_apply import apply_file_edits


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_STRUCTURED_PATCH_HUNKS = 20
MAX_STRUCTURED_PATCH_TEXT_CHARS = 12_000
MAX_STRUCTURED_PATCH_TOTAL_CHARS = 24_000
_PATCH_LOCK = threading.RLock()


@dataclass(frozen=True)
class PatchHunk:
    old_text: str
    new_text: str
    expected_occurrences: int = 1


@dataclass(frozen=True)
class StructuredPatch:
    path: str
    expected_sha256: str
    hunks: list[PatchHunk]
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SyntaxCheck:
    status: str
    language: str
    message: str


@dataclass(frozen=True)
class StructuredPatchResult:
    status: str
    path: str
    applied: bool
    message: str
    expected_sha256: str
    current_sha256: str | None
    resulting_sha256: str | None
    hunks_applied: int
    diff: str
    syntax_check: SyntaxCheck
    conflicts: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def file_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def build_structured_patch(
    path: str,
    original: str,
    updated: str,
    *,
    rationale: str = "",
    context_lines: int = 2,
) -> StructuredPatch:
    """Convert a complete replacement into bounded, uniquely anchored exact-text hunks."""
    normalized_path = _normalize_path(path)
    if original == updated:
        raise ValueError(f"Cannot build a structured patch for unchanged content: {normalized_path}")
    if not original:
        raise ValueError(f"Structured patch generation currently requires a non-empty file: {normalized_path}")
    syntax_check = check_syntax(normalized_path, updated)
    if syntax_check.status == "failed":
        raise ValueError(syntax_check.message)

    old_lines = original.splitlines(keepends=True)
    new_lines = updated.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    groups = list(matcher.get_grouped_opcodes(n=max(context_lines, 0)))
    hunks: list[PatchHunk] = []
    for group in groups:
        old_start, old_end = group[0][1], group[-1][2]
        new_start, new_end = group[0][3], group[-1][4]
        old_start, old_end, new_start, new_end = _expand_unique_hunk(
            original,
            old_lines,
            new_lines,
            old_start,
            old_end,
            new_start,
            new_end,
        )
        old_text = "".join(old_lines[old_start:old_end])
        new_text = "".join(new_lines[new_start:new_end])
        if not old_text or original.count(old_text) != 1:
            return StructuredPatch(
                path=normalized_path,
                expected_sha256=file_sha256(original),
                hunks=[PatchHunk(original, updated)],
                rationale=rationale.strip(),
            )
        hunks.append(PatchHunk(old_text, new_text))
    if not hunks:
        raise ValueError(f"No changed hunks were generated for: {normalized_path}")
    return StructuredPatch(
        path=normalized_path,
        expected_sha256=file_sha256(original),
        hunks=hunks,
        rationale=rationale.strip(),
    )


def structured_patch_from_record(record: object) -> StructuredPatch:
    return parse_structured_patch(record)


def current_file_sha256(repo_path: str | Path, path: str) -> str:
    root = Path(repo_path).expanduser().resolve()
    target = _safe_existing_target(root, path)
    return file_sha256(_read_utf8(target, path))


def parse_structured_patch(
    arguments: object,
    *,
    action_name: str = "apply_patch",
) -> StructuredPatch:
    if not isinstance(arguments, dict):
        raise ValueError(f"{action_name} arguments must be an object.")
    path = arguments.get("path")
    expected_sha256 = arguments.get("expected_sha256")
    raw_hunks = arguments.get("hunks")
    rationale = arguments.get("rationale", "")
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"{action_name} requires a non-empty path string.")
    if not isinstance(expected_sha256, str) or not SHA256_PATTERN.fullmatch(expected_sha256.lower()):
        raise ValueError(
            f"{action_name} requires expected_sha256 as a 64-character SHA-256 hex digest."
        )
    if not isinstance(raw_hunks, list) or not raw_hunks:
        raise ValueError(f"{action_name} requires at least one hunk.")
    if len(raw_hunks) > MAX_STRUCTURED_PATCH_HUNKS:
        raise ValueError(
            f"{action_name} cannot contain more than {MAX_STRUCTURED_PATCH_HUNKS} hunks."
        )
    if not isinstance(rationale, str):
        raise ValueError(f"{action_name} rationale must be a string.")

    hunks: list[PatchHunk] = []
    total_chars = 0
    for index, raw_hunk in enumerate(raw_hunks, start=1):
        if not isinstance(raw_hunk, dict):
            raise ValueError(f"Patch hunk {index} must be an object.")
        old_text = raw_hunk.get("old_text")
        new_text = raw_hunk.get("new_text")
        expected_occurrences = raw_hunk.get("expected_occurrences", 1)
        if not isinstance(old_text, str) or not old_text:
            raise ValueError(f"Patch hunk {index} requires non-empty old_text.")
        if not isinstance(new_text, str):
            raise ValueError(f"Patch hunk {index} requires new_text as a string.")
        if (
            len(old_text) > MAX_STRUCTURED_PATCH_TEXT_CHARS
            or len(new_text) > MAX_STRUCTURED_PATCH_TEXT_CHARS
        ):
            raise ValueError(
                f"Patch hunk {index} text cannot exceed "
                f"{MAX_STRUCTURED_PATCH_TEXT_CHARS} characters."
            )
        total_chars += len(old_text) + len(new_text)
        if not isinstance(expected_occurrences, int) or isinstance(expected_occurrences, bool):
            raise ValueError(f"Patch hunk {index} expected_occurrences must be an integer.")
        if not 1 <= expected_occurrences <= 100:
            raise ValueError(
                f"Patch hunk {index} expected_occurrences must be from 1 to 100."
            )
        hunks.append(PatchHunk(old_text, new_text, expected_occurrences))
    if total_chars > MAX_STRUCTURED_PATCH_TOTAL_CHARS:
        raise ValueError(
            f"{action_name} hunk text exceeds the "
            f"{MAX_STRUCTURED_PATCH_TOTAL_CHARS}-character total limit."
        )
    return StructuredPatch(
        path=_normalize_path(path),
        expected_sha256=expected_sha256.lower(),
        hunks=hunks,
        rationale=rationale.strip(),
    )


def preview_structured_patch(
    patch: StructuredPatch,
    current_content: str,
) -> tuple[StructuredPatchResult, str | None]:
    """Evaluate an exact-text patch without writing to the filesystem."""
    current_hash = file_sha256(current_content)
    if current_hash != patch.expected_sha256:
        return (
            _conflict_result(
                patch,
                current_hash,
                "The file changed after it was read; re-read it before retrying.",
                [
                    {
                        "kind": "stale_file",
                        "expected": patch.expected_sha256,
                        "actual": current_hash,
                    }
                ],
            ),
            None,
        )

    updated = current_content
    for index, hunk in enumerate(patch.hunks, start=1):
        occurrences = updated.count(hunk.old_text)
        if occurrences != hunk.expected_occurrences:
            return (
                _conflict_result(
                    patch,
                    current_hash,
                    f"Patch hunk {index} matched {occurrences} time(s), "
                    f"expected {hunk.expected_occurrences}.",
                    [
                        {
                            "kind": "hunk_match",
                            "hunk": index,
                            "expected_occurrences": hunk.expected_occurrences,
                            "actual_occurrences": occurrences,
                        }
                    ],
                ),
                None,
            )
        updated = updated.replace(
            hunk.old_text,
            hunk.new_text,
            hunk.expected_occurrences,
        )

    syntax_check = check_syntax(patch.path, updated)
    if syntax_check.status == "failed":
        return (
            StructuredPatchResult(
                status="rejected",
                path=patch.path,
                applied=False,
                message=syntax_check.message,
                expected_sha256=patch.expected_sha256,
                current_sha256=current_hash,
                resulting_sha256=None,
                hunks_applied=0,
                diff="",
                syntax_check=syntax_check,
                conflicts=[],
            ),
            None,
        )
    if updated == current_content:
        return (
            StructuredPatchResult(
                status="no_change",
                path=patch.path,
                applied=False,
                message="The patch produced no file-content change.",
                expected_sha256=patch.expected_sha256,
                current_sha256=current_hash,
                resulting_sha256=current_hash,
                hunks_applied=len(patch.hunks),
                diff="",
                syntax_check=syntax_check,
                conflicts=[],
            ),
            current_content,
        )
    resulting_hash = file_sha256(updated)
    return (
        StructuredPatchResult(
            status="ready",
            path=patch.path,
            applied=False,
            message=f"Prepared {len(patch.hunks)} structured patch hunk(s) for {patch.path}.",
            expected_sha256=patch.expected_sha256,
            current_sha256=current_hash,
            resulting_sha256=resulting_hash,
            hunks_applied=len(patch.hunks),
            diff=_build_diff(patch.path, current_content, updated),
            syntax_check=syntax_check,
            conflicts=[],
        ),
        updated,
    )


def apply_structured_patch(
    repo_path: str | Path,
    patch: StructuredPatch,
    *,
    task: str = "",
    allowed_paths: list[str] | tuple[str, ...] | None = None,
) -> StructuredPatchResult:
    root = Path(repo_path).expanduser().resolve()
    target = _safe_existing_target(root, patch.path)
    with _PATCH_LOCK:
        original = _read_utf8(target, patch.path)
        preview, updated = preview_structured_patch(patch, original)
        if preview.status != "ready" or updated is None:
            return preview
        original_hash = preview.current_sha256 or file_sha256(original)

        pre_write = _read_utf8(target, patch.path)
        pre_write_hash = file_sha256(pre_write)
        if pre_write_hash != patch.expected_sha256:
            return _conflict_result(
                patch,
                pre_write_hash,
                "The file changed while the patch was being prepared; re-read it before retrying.",
                [{"kind": "concurrent_modification", "expected": patch.expected_sha256, "actual": pre_write_hash}],
            )

        diff = preview.diff
        apply_file_edits(
            root,
            [FileEditProposal(path=patch.path, new_content=updated, rationale=patch.rationale or "Structured runtime patch.")],
            task=task,
            allowed_paths=list(allowed_paths or [patch.path]),
        )
        readback = _read_utf8(target, patch.path)
        resulting_hash = file_sha256(readback)
        expected_result_hash = file_sha256(updated)
        if resulting_hash != expected_result_hash:
            return StructuredPatchResult(
                status="verification_failed",
                path=patch.path,
                applied=True,
                message="Post-edit readback did not match the content RepoPilot attempted to write.",
                expected_sha256=patch.expected_sha256,
                current_sha256=resulting_hash,
                resulting_sha256=resulting_hash,
                hunks_applied=len(patch.hunks),
                diff=diff,
                syntax_check=preview.syntax_check,
                conflicts=[{"kind": "readback_mismatch", "expected": expected_result_hash, "actual": resulting_hash}],
            )
        return StructuredPatchResult(
            status="applied",
            path=patch.path,
            applied=True,
            message=f"Applied {len(patch.hunks)} structured patch hunk(s) to {patch.path}.",
            expected_sha256=patch.expected_sha256,
            current_sha256=original_hash,
            resulting_sha256=resulting_hash,
            hunks_applied=len(patch.hunks),
            diff=diff,
            syntax_check=preview.syntax_check,
            conflicts=[],
        )


def check_syntax(path: str, content: str) -> SyntaxCheck:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".py":
        try:
            ast.parse(content, filename=path)
        except SyntaxError as exc:
            return SyntaxCheck("failed", "Python", f"Python syntax check failed at line {exc.lineno or 0}: {exc.msg}")
        return SyntaxCheck("passed", "Python", "Python syntax check passed.")
    if suffix == ".json":
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            return SyntaxCheck("failed", "JSON", f"JSON syntax check failed at line {exc.lineno}: {exc.msg}")
        return SyntaxCheck("passed", "JSON", "JSON syntax check passed.")
    return SyntaxCheck("skipped", suffix.lstrip(".").upper() or "text", "No built-in syntax parser is configured for this file type.")


def _conflict_result(
    patch: StructuredPatch,
    current_hash: str,
    message: str,
    conflicts: list[dict[str, Any]],
) -> StructuredPatchResult:
    return StructuredPatchResult(
        status="conflict",
        path=patch.path,
        applied=False,
        message=message,
        expected_sha256=patch.expected_sha256,
        current_sha256=current_hash,
        resulting_sha256=None,
        hunks_applied=0,
        diff="",
        syntax_check=SyntaxCheck("not_run", "unknown", "Syntax check was not run because the patch conflicted."),
        conflicts=conflicts,
    )


def _build_diff(path: str, original: str, updated: str) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _expand_unique_hunk(
    original: str,
    old_lines: list[str],
    new_lines: list[str],
    old_start: int,
    old_end: int,
    new_start: int,
    new_end: int,
) -> tuple[int, int, int, int]:
    while True:
        old_text = "".join(old_lines[old_start:old_end])
        if old_text and original.count(old_text) == 1:
            return old_start, old_end, new_start, new_end
        expanded = False
        if old_start > 0 and new_start > 0 and old_lines[old_start - 1] == new_lines[new_start - 1]:
            old_start -= 1
            new_start -= 1
            expanded = True
        if old_end < len(old_lines) and new_end < len(new_lines) and old_lines[old_end] == new_lines[new_end]:
            old_end += 1
            new_end += 1
            expanded = True
        if not expanded:
            return old_start, old_end, new_start, new_end


def _normalize_path(path: str) -> str:
    normalized = PurePosixPath(path.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts or any(part in {"", "."} for part in normalized.parts):
        raise ValueError(f"Unsafe repository-relative path: {path}")
    return normalized.as_posix()


def _safe_existing_target(root: Path, path: str) -> Path:
    if not root.is_dir():
        raise FileNotFoundError(f"Repository path does not exist: {root}")
    normalized = _normalize_path(path)
    target = (root / Path(*PurePosixPath(normalized).parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Patch path escapes the repository: {path}") from exc
    if not target.is_file():
        raise FileNotFoundError(f"Structured patches currently require an existing file: {normalized}")
    return target


def _read_utf8(target: Path, path: str) -> str:
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Structured patches require a UTF-8 text file: {path}") from exc
