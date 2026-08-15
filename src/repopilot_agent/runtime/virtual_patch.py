"""Run-scoped virtual patch overlay for non-writing Agent proposals."""

from __future__ import annotations

import difflib
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..structured_patch import (
    StructuredPatch,
    SyntaxCheck,
    file_sha256,
    preview_structured_patch,
)


MAX_VIRTUAL_FILES = 12
MAX_VIRTUAL_FILE_CHARS = 250_000


@dataclass
class VirtualPatchEntry:
    path: str
    base_content: str
    base_sha256: str
    current_content: str
    current_sha256: str
    revision: int
    hunk_count: int
    status: str = "proposed"
    inspected: bool = False

    def metadata(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "base_sha256": self.base_sha256,
            "current_sha256": self.current_sha256,
            "revision": self.revision,
            "hunk_count": self.hunk_count,
            "status": self.status,
            "inspected": self.inspected,
        }


@dataclass(frozen=True)
class VirtualPatchOutcome:
    status: str
    message: str
    data: dict[str, Any]


class VirtualPatchOverlay:
    """Accumulates exact-text edits in memory while guarding the disk baseline."""

    def __init__(self, repo_path: str | Path) -> None:
        self.repo_path = Path(repo_path).expanduser().resolve()
        self._entries: dict[str, VirtualPatchEntry] = {}

    def propose(self, patch: StructuredPatch) -> VirtualPatchOutcome:
        disk_content = self._read_file(patch.path)
        disk_sha256 = file_sha256(disk_content)
        entry = self._entries.get(patch.path)
        baseline_reset = False

        if entry is not None and disk_sha256 != entry.base_sha256:
            entry.status = "conflict"
            entry.inspected = False
            if patch.expected_sha256 != disk_sha256:
                return self._conflict(
                    patch,
                    "The real repository file changed after the virtual proposal was created; "
                    "re-read it before revising.",
                    "stale_repository",
                    expected=entry.base_sha256,
                    actual=disk_sha256,
                )
            del self._entries[patch.path]
            entry = None
            baseline_reset = True

        if entry is None:
            if len(self._entries) >= MAX_VIRTUAL_FILES:
                raise ValueError(
                    f"Virtual proposals cannot include more than {MAX_VIRTUAL_FILES} files."
                )
            current_content = disk_content
            base_content = disk_content
            base_sha256 = disk_sha256
            revision = 1
            prior_hunk_count = 0
        else:
            if patch.expected_sha256 != entry.current_sha256:
                return self._conflict(
                    patch,
                    "The virtual proposal has a newer revision; inspect its current SHA-256 "
                    "before revising.",
                    "stale_virtual_revision",
                    expected=entry.current_sha256,
                    actual=patch.expected_sha256,
                )
            current_content = entry.current_content
            base_content = entry.base_content
            base_sha256 = entry.base_sha256
            revision = entry.revision + 1
            prior_hunk_count = entry.hunk_count

        preview, updated_content = preview_structured_patch(patch, current_content)
        if preview.status != "ready" or updated_content is None:
            data = preview.to_dict()
            data.update(
                {
                    "proposal_status": preview.status,
                    "base_sha256": base_sha256,
                    "revision": entry.revision if entry else 0,
                    "baseline_reset": baseline_reset,
                    "repository_written": False,
                }
            )
            return VirtualPatchOutcome(preview.status, preview.message, data)

        cumulative_diff = _build_diff(patch.path, base_content, updated_content)
        if not cumulative_diff:
            self._entries.pop(patch.path, None)
            return VirtualPatchOutcome(
                "completed",
                f"Virtual changes for {patch.path} now match the real baseline and were removed.",
                {
                    "proposal_status": "removed",
                    "path": patch.path,
                    "base_sha256": base_sha256,
                    "previous_sha256": preview.current_sha256,
                    "resulting_sha256": base_sha256,
                    "revision": revision,
                    "hunks_applied": preview.hunks_applied,
                    "hunk_count": 0,
                    "diff": "",
                    "syntax_check": asdict(preview.syntax_check),
                    "conflicts": [],
                    "inspected": True,
                    "removed": True,
                    "baseline_reset": baseline_reset,
                    "repository_written": False,
                },
            )

        resulting_sha256 = file_sha256(updated_content)
        new_entry = VirtualPatchEntry(
            path=patch.path,
            base_content=base_content,
            base_sha256=base_sha256,
            current_content=updated_content,
            current_sha256=resulting_sha256,
            revision=revision,
            hunk_count=prior_hunk_count + preview.hunks_applied,
        )
        self._entries[patch.path] = new_entry
        return VirtualPatchOutcome(
            "completed",
            f"Proposed virtual revision {revision} for {patch.path}; the repository was not written.",
            {
                "proposal_status": "proposed",
                **new_entry.metadata(),
                "previous_sha256": preview.current_sha256,
                "resulting_sha256": resulting_sha256,
                "hunks_applied": preview.hunks_applied,
                "diff": cumulative_diff,
                "syntax_check": asdict(preview.syntax_check),
                "conflicts": [],
                "inspection_required": True,
                "baseline_reset": baseline_reset,
                "repository_written": False,
            },
        )

    def inspect(self) -> VirtualPatchOutcome:
        if not self._entries:
            return VirtualPatchOutcome(
                "completed",
                "No virtual proposed edits are pending.",
                {
                    "proposal_status": "empty",
                    "files": [],
                    "diff": "",
                    "conflicts": [],
                    "repository_written": False,
                },
            )

        conflicts: list[dict[str, Any]] = []
        for entry in self._ordered_entries():
            disk_sha256 = file_sha256(self._read_file(entry.path))
            if disk_sha256 != entry.base_sha256:
                entry.status = "conflict"
                entry.inspected = False
                conflicts.append(
                    {
                        "kind": "stale_repository",
                        "path": entry.path,
                        "expected": entry.base_sha256,
                        "actual": disk_sha256,
                    }
                )
        if conflicts:
            return VirtualPatchOutcome(
                "conflict",
                "The real repository changed after one or more virtual proposals were created.",
                {
                    "proposal_status": "conflict",
                    "files": self.metadata(),
                    "diff": "",
                    "conflicts": conflicts,
                    "repository_written": False,
                },
            )

        for entry in self._entries.values():
            entry.status = "inspected"
            entry.inspected = True
        diff = self.current_diff()
        return VirtualPatchOutcome(
            "completed",
            f"Inspected {len(self._entries)} virtual proposed file(s); the repository was not written.",
            {
                "proposal_status": "inspected",
                "files": self.metadata(),
                "diff": diff,
                "conflicts": [],
                "repository_written": False,
            },
        )

    def metadata(self) -> list[dict[str, Any]]:
        return [entry.metadata() for entry in self._ordered_entries()]

    def current_diff(self) -> str:
        return "".join(
            _build_diff(entry.path, entry.base_content, entry.current_content)
            for entry in self._ordered_entries()
        )

    def _ordered_entries(self) -> list[VirtualPatchEntry]:
        return [self._entries[path] for path in sorted(self._entries)]

    def _read_file(self, path: str) -> str:
        target = (self.repo_path / Path(*PurePosixPath(path).parts)).resolve()
        try:
            target.relative_to(self.repo_path)
        except ValueError as exc:
            raise ValueError(f"Virtual patch path escapes the repository: {path}") from exc
        if not target.is_file():
            raise FileNotFoundError(
                f"Virtual patches currently require an existing file: {path}"
            )
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Virtual patches require a UTF-8 text file: {path}") from exc
        if len(content) > MAX_VIRTUAL_FILE_CHARS:
            raise ValueError(
                f"Virtual patch target exceeds the {MAX_VIRTUAL_FILE_CHARS}-character limit: {path}"
            )
        return content

    def _conflict(
        self,
        patch: StructuredPatch,
        message: str,
        kind: str,
        *,
        expected: str,
        actual: str,
    ) -> VirtualPatchOutcome:
        return VirtualPatchOutcome(
            "conflict",
            message,
            {
                "proposal_status": "conflict",
                "path": patch.path,
                "expected_sha256": patch.expected_sha256,
                "current_sha256": (
                    expected if kind == "stale_virtual_revision" else actual
                ),
                "resulting_sha256": None,
                "hunks_applied": 0,
                "diff": "",
                "syntax_check": asdict(
                    SyntaxCheck(
                        "not_run",
                        "unknown",
                        "Syntax check was not run because the virtual patch conflicted.",
                    )
                ),
                "conflicts": [
                    {
                        "kind": kind,
                        "path": patch.path,
                        "expected": expected,
                        "actual": actual,
                    }
                ],
                "repository_written": False,
            },
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
