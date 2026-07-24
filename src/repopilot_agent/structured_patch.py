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


def parse_structured_patch(arguments: object) -> StructuredPatch:
    if not isinstance(arguments, dict):
        raise ValueError("apply_patch arguments must be an object.")
    path = arguments.get("path")
    expected_sha256 = arguments.get("expected_sha256")
    raw_hunks = arguments.get("hunks")
    rationale = arguments.get("rationale", "")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("apply_patch requires a non-empty path string.")
    if not isinstance(expected_sha256, str) or not SHA256_PATTERN.fullmatch(expected_sha256.lower()):
        raise ValueError("apply_patch requires expected_sha256 as a 64-character SHA-256 hex digest.")
    if not isinstance(raw_hunks, list) or not raw_hunks:
        raise ValueError("apply_patch requires at least one hunk.")
    if not isinstance(rationale, str):
        raise ValueError("apply_patch rationale must be a string.")

    hunks: list[PatchHunk] = []
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
        if not isinstance(expected_occurrences, int) or isinstance(expected_occurrences, bool):
            raise ValueError(f"Patch hunk {index} expected_occurrences must be an integer.")
        if expected_occurrences <= 0:
            raise ValueError(f"Patch hunk {index} expected_occurrences must be greater than zero.")
        hunks.append(PatchHunk(old_text, new_text, expected_occurrences))
    return StructuredPatch(
        path=_normalize_path(path),
        expected_sha256=expected_sha256.lower(),
        hunks=hunks,
        rationale=rationale.strip(),
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
        original_hash = file_sha256(original)
        if original_hash != patch.expected_sha256:
            return _conflict_result(
                patch,
                original_hash,
                "The file changed after it was read; re-read it before retrying.",
                [{"kind": "stale_file", "expected": patch.expected_sha256, "actual": original_hash}],
            )

        updated = original
        for index, hunk in enumerate(patch.hunks, start=1):
            occurrences = updated.count(hunk.old_text)
            if occurrences != hunk.expected_occurrences:
                return _conflict_result(
                    patch,
                    original_hash,
                    f"Patch hunk {index} matched {occurrences} time(s), expected {hunk.expected_occurrences}.",
                    [
                        {
                            "kind": "hunk_match",
                            "hunk": index,
                            "expected_occurrences": hunk.expected_occurrences,
                            "actual_occurrences": occurrences,
                        }
                    ],
                )
            updated = updated.replace(hunk.old_text, hunk.new_text, hunk.expected_occurrences)

        syntax_check = check_syntax(patch.path, updated)
        if syntax_check.status == "failed":
            return StructuredPatchResult(
                status="rejected",
                path=patch.path,
                applied=False,
                message=syntax_check.message,
                expected_sha256=patch.expected_sha256,
                current_sha256=original_hash,
                resulting_sha256=None,
                hunks_applied=0,
                diff="",
                syntax_check=syntax_check,
                conflicts=[],
            )
        if updated == original:
            return StructuredPatchResult(
                status="no_change",
                path=patch.path,
                applied=False,
                message="The patch produced no file-content change.",
                expected_sha256=patch.expected_sha256,
                current_sha256=original_hash,
                resulting_sha256=original_hash,
                hunks_applied=len(patch.hunks),
                diff="",
                syntax_check=syntax_check,
                conflicts=[],
            )

        pre_write = _read_utf8(target, patch.path)
        pre_write_hash = file_sha256(pre_write)
        if pre_write_hash != patch.expected_sha256:
            return _conflict_result(
                patch,
                pre_write_hash,
                "The file changed while the patch was being prepared; re-read it before retrying.",
                [{"kind": "concurrent_modification", "expected": patch.expected_sha256, "actual": pre_write_hash}],
            )

        diff = _build_diff(patch.path, original, updated)
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
                syntax_check=syntax_check,
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
            syntax_check=syntax_check,
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
