"""Policy-gated tool implementations used by the unified agent runtime."""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from ..git_tools import get_git_diff, inspect_repository
from ..models import FileEditProposal, RepoFile
from ..patch_apply import FileRollbackSnapshot, apply_file_edits
from ..repository_map import RepositoryMap, build_repository_map, rank_repository_map
from ..scanner import scan_repository
from ..search import search_files
from ..structured_patch import (
    apply_structured_patch,
    file_sha256,
    parse_structured_patch,
    preview_structured_patch,
)
from ..validator import run_validation
from .models import RuntimeAction, RuntimePolicy
from .virtual_patch import VirtualPatchOverlay


MAX_FILE_CHARS = 12_000
MAX_DIFF_CHARS = 24_000
MAX_SEARCH_RESULTS = 8


class RuntimeToolError(RuntimeError):
    """Raised when a typed runtime tool receives invalid or unsafe input."""


@dataclass(frozen=True)
class RuntimeToolResult:
    summary: str
    data: dict[str, Any]
    status: str = "completed"
    rollback_snapshots: tuple[FileRollbackSnapshot, ...] = ()


@dataclass(frozen=True)
class RuntimeSideEffectPreview:
    status: str
    summary: str
    file_scope: tuple[str, ...] = ()
    command_allowlist: tuple[str, ...] = ()
    baseline_hashes: dict[str, str] = field(default_factory=dict)
    baseline_exists: dict[str, bool] = field(default_factory=dict)
    diff: str = ""
    diff_truncated: bool = False
    data: dict[str, Any] = field(default_factory=dict)


class RuntimeToolContext:
    def __init__(
        self,
        repo_path: str | Path,
        task: str,
        policy: RuntimePolicy,
        files: list[RepoFile] | None = None,
        repository_map: RepositoryMap | None = None,
    ) -> None:
        self.repo_path = Path(repo_path).expanduser().resolve()
        if not self.repo_path.is_dir():
            raise FileNotFoundError(f"Repository path does not exist: {self.repo_path}")
        self.task = task
        self.policy = policy
        self.files = list(files) if files is not None else scan_repository(self.repo_path)
        self.repository_map = repository_map or build_repository_map(self.files)
        self.selected_paths: list[str] = []
        self.virtual_patches = VirtualPatchOverlay(self.repo_path)

    def refresh_files(self) -> None:
        self.files = scan_repository(self.repo_path)
        self.repository_map = build_repository_map(self.files)

    def select_path(self, path: str) -> None:
        if path not in self.selected_paths:
            self.selected_paths.append(path)


def execute_runtime_tool(action: RuntimeAction, context: RuntimeToolContext) -> RuntimeToolResult:
    if action.kind == "search_files":
        query = _required_string(action, "query")
        hits = search_files(query, context.files, limit=MAX_SEARCH_RESULTS)
        return RuntimeToolResult(
            summary=f"Found {len(hits)} repository file(s) for query: {query}",
            data={"query": query, "hits": [asdict(hit) for hit in hits]},
        )

    if action.kind == "read_file":
        path = _normalize_relative_path(_required_string(action, "path"))
        target = _safe_target(context.repo_path, path)
        if target.is_file():
            try:
                content = target.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise RuntimeToolError(f"Repository file is not UTF-8 text: {path}") from exc
        else:
            snapshot = next((repo_file for repo_file in context.files if repo_file.relative_path == path), None)
            if snapshot is None:
                raise RuntimeToolError(f"Repository file does not exist: {path}")
            content = snapshot.content
        context.select_path(path)
        clipped, truncated = _clip(content, MAX_FILE_CHARS)
        return RuntimeToolResult(
            summary=f"Read {path}{' (truncated)' if truncated else ''}.",
            data={
                "path": path,
                "content": clipped,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "truncated": truncated,
                "selected_paths": list(context.selected_paths),
            },
        )

    if action.kind == "inspect_repository_map":
        query = str(action.arguments.get("query") or context.task).strip()
        limit = action.arguments.get("limit", MAX_SEARCH_RESULTS)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise RuntimeToolError("inspect_repository_map limit must be an integer from 1 to 20.")
        matches = rank_repository_map(
            query,
            context.repository_map,
            seed_paths=context.selected_paths,
            limit=limit,
        )
        return RuntimeToolResult(
            summary=f"Mapped {context.repository_map.symbol_count} symbols; selected {len(matches)} relevant file(s).",
            data={
                "query": query,
                "files_indexed": context.repository_map.files_indexed,
                "symbols_indexed": context.repository_map.symbol_count,
                "relations_indexed": context.repository_map.relation_count,
                "parse_errors": context.repository_map.parse_error_count,
                "matches": [match.to_dict() for match in matches],
            },
        )

    if action.kind == "inspect_git_status":
        state = inspect_repository(context.repo_path)
        return RuntimeToolResult(summary=f"Inspected Git branch {state.branch}.", data=asdict(state))

    if action.kind == "inspect_diff":
        staged = bool(action.arguments.get("staged"))
        diff = get_git_diff(context.repo_path, staged=staged)
        clipped, truncated = _clip(diff, MAX_DIFF_CHARS)
        return RuntimeToolResult(
            summary=f"Inspected {'staged' if staged else 'working tree'} diff.",
            data={"diff": clipped, "staged": staged, "truncated": truncated},
        )

    if action.kind == "propose_patch":
        arguments = dict(action.arguments)
        arguments["rationale"] = action.rationale
        patch = parse_structured_patch(arguments, action_name="propose_patch")
        outcome = context.virtual_patches.propose(patch)
        if outcome.status == "completed" and not outcome.data.get("removed"):
            context.select_path(patch.path)
        data = dict(outcome.data)
        data["diff"], data["diff_truncated"] = _clip(
            str(data.get("diff") or ""),
            MAX_DIFF_CHARS,
        )
        data["selected_paths"] = list(context.selected_paths)
        return RuntimeToolResult(
            summary=outcome.message,
            data=data,
            status=outcome.status,
        )

    if action.kind == "inspect_proposed_diff":
        outcome = context.virtual_patches.inspect()
        data = dict(outcome.data)
        data["diff"], data["diff_truncated"] = _clip(
            str(data.get("diff") or ""),
            MAX_DIFF_CHARS,
        )
        return RuntimeToolResult(
            summary=outcome.message,
            data=data,
            status=outcome.status,
        )

    if action.kind == "edit_file":
        path = _normalize_relative_path(_required_string(action, "path"))
        new_content = action.arguments.get("new_content")
        if not isinstance(new_content, str):
            raise RuntimeToolError("edit_file requires new_content as a string.")
        rationale = str(action.arguments.get("rationale") or action.rationale or "Runtime-approved edit.")
        target = _safe_target(context.repo_path, path)
        before_exists, before_content = _read_write_target(target, path)
        allowed_paths = list(context.policy.allowed_edit_paths) or [path]
        result = apply_file_edits(
            context.repo_path,
            [FileEditProposal(path=path, new_content=new_content, rationale=rationale)],
            task=context.task,
            allowed_paths=allowed_paths,
        )
        diff, truncated = _clip(result.diff, MAX_DIFF_CHARS)
        after_exists, after_content = _read_write_target(target, path)
        evidence = _write_evidence(
            path,
            before_exists,
            before_content,
            after_exists,
            after_content,
        )
        snapshots = (
            FileRollbackSnapshot(
                path=path,
                existed=before_exists,
                original_content=before_content if before_exists else None,
                applied_content=after_content,
            ),
        ) if result.applied else ()
        refresh_error = _refresh_after_write(context, path)
        return RuntimeToolResult(
            summary=result.message,
            data={
                "applied": result.applied,
                "changed_files": result.changed_files,
                "diff": diff,
                "diff_truncated": truncated,
                "resulting_diff": diff,
                "resulting_diff_truncated": truncated,
                "write_evidence": [evidence],
                "rollback_available": bool(snapshots),
                "rollback_snapshot_paths": [item.path for item in snapshots],
                "refresh_error": refresh_error,
                "selected_paths": list(context.selected_paths),
            },
            rollback_snapshots=snapshots,
        )

    if action.kind == "apply_patch":
        patch = parse_structured_patch(action.arguments)
        target = _safe_target(context.repo_path, patch.path)
        before_exists, before_content = _read_write_target(target, patch.path)
        allowed_paths = list(context.policy.allowed_edit_paths) or [patch.path]
        result = apply_structured_patch(
            context.repo_path,
            patch,
            task=context.task,
            allowed_paths=allowed_paths,
        )
        after_exists, after_content = _read_write_target(target, patch.path)
        evidence = _write_evidence(
            patch.path,
            before_exists,
            before_content,
            after_exists,
            after_content,
        )
        snapshots = (
            FileRollbackSnapshot(
                path=patch.path,
                existed=before_exists,
                original_content=before_content if before_exists else None,
                applied_content=after_content,
            ),
        ) if result.applied and before_content != after_content else ()
        refresh_error = (
            _refresh_after_write(context, patch.path)
            if result.applied
            else None
        )
        data = result.to_dict()
        data["diff"], data["diff_truncated"] = _clip(result.diff, MAX_DIFF_CHARS)
        resulting_diff, resulting_truncated = _clip(
            _safe_current_diff(context.repo_path, result.diff),
            MAX_DIFF_CHARS,
        )
        data.update(
            {
                "resulting_diff": resulting_diff,
                "resulting_diff_truncated": resulting_truncated,
                "write_evidence": [evidence],
                "rollback_available": bool(snapshots),
                "rollback_snapshot_paths": [item.path for item in snapshots],
                "refresh_error": refresh_error,
                "selected_paths": list(context.selected_paths),
            }
        )
        return RuntimeToolResult(
            summary=result.message,
            data=data,
            status=result.status,
            rollback_snapshots=snapshots,
        )

    if action.kind in {"run_command", "validate"}:
        command = _required_string(action, "command")
        results = run_validation(context.repo_path, [command])
        if not results:
            raise RuntimeToolError("Validation runner returned no result.")
        result = results[0]
        return RuntimeToolResult(
            summary=(
                f"Command completed with exit code {result.exit_code}."
                if result.allowed
                else "Command was rejected by the validation allowlist."
            ),
            data=asdict(result),
        )

    if action.kind == "finish":
        requested = action.arguments.get("selected_paths", [])
        if requested is not None and not isinstance(requested, list):
            raise RuntimeToolError("finish selected_paths must be a list.")
        for raw_path in requested or []:
            if isinstance(raw_path, str):
                path = _normalize_relative_path(raw_path)
                if _safe_target(context.repo_path, path).is_file():
                    context.select_path(path)
        summary = str(action.arguments.get("summary") or "Agent runtime finished.").strip()
        return RuntimeToolResult(
            summary=summary,
            data={"summary": summary, "selected_paths": list(context.selected_paths), "finished": True},
        )

    raise RuntimeToolError(f"Action {action.kind} is not implemented by the runtime tool registry.")


def preview_runtime_side_effect(
    action: RuntimeAction,
    context: RuntimeToolContext,
) -> RuntimeSideEffectPreview:
    """Describe the exact side effect without writing files or running commands."""
    if action.kind == "edit_file":
        path = _normalize_relative_path(_required_string(action, "path"))
        new_content = action.arguments.get("new_content")
        if not isinstance(new_content, str):
            raise RuntimeToolError("edit_file requires new_content as a string.")
        target = _safe_target(context.repo_path, path)
        target_exists = target.exists()
        if target_exists and not target.is_file():
            raise RuntimeToolError(f"Repository edit target is not a file: {path}")
        try:
            original = target.read_text(encoding="utf-8") if target_exists else ""
        except UnicodeDecodeError as exc:
            raise RuntimeToolError(f"Repository file is not UTF-8 text: {path}") from exc
        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
        clipped, truncated = _clip(diff, MAX_DIFF_CHARS)
        return RuntimeSideEffectPreview(
            status="ready",
            summary=f"Prepared an exact approval preview for edit_file on {path}.",
            file_scope=(path,),
            baseline_hashes={path: file_sha256(original)},
            baseline_exists={path: target_exists},
            diff=clipped,
            diff_truncated=truncated,
        )

    if action.kind == "apply_patch":
        patch = parse_structured_patch(action.arguments)
        target = _safe_target(context.repo_path, patch.path)
        if not target.is_file():
            raise RuntimeToolError(f"Repository file does not exist: {patch.path}")
        try:
            current = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeToolError(f"Repository file is not UTF-8 text: {patch.path}") from exc
        result, _updated = preview_structured_patch(patch, current)
        clipped, truncated = _clip(result.diff, MAX_DIFF_CHARS)
        return RuntimeSideEffectPreview(
            status="ready" if result.status in {"ready", "no_change"} else result.status,
            summary=result.message,
            file_scope=(patch.path,),
            baseline_hashes={patch.path: file_sha256(current)},
            baseline_exists={patch.path: True},
            diff=clipped,
            diff_truncated=truncated,
            data=result.to_dict(),
        )

    if action.kind in {"run_command", "validate"}:
        command = _required_string(action, "command")
        return RuntimeSideEffectPreview(
            status="ready",
            summary=f"Prepared an exact approval preview for {action.kind}.",
            command_allowlist=(command,),
        )

    raise RuntimeToolError(f"Action {action.kind} is not a runtime side effect.")


def _required_string(action: RuntimeAction, name: str) -> str:
    value = action.arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeToolError(f"{action.kind} requires a non-empty {name} string.")
    return value.strip()


def _normalize_relative_path(path: str) -> str:
    normalized = PurePosixPath(path.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts or any(part in {"", "."} for part in normalized.parts):
        raise RuntimeToolError(f"Unsafe repository-relative path: {path}")
    return normalized.as_posix()


def _safe_target(root: Path, path: str) -> Path:
    target = (root / Path(*PurePosixPath(path).parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise RuntimeToolError(f"Path escapes the repository: {path}") from exc
    return target


def _read_write_target(target: Path, path: str) -> tuple[bool, str]:
    if target.exists() and not target.is_file():
        raise RuntimeToolError(f"Repository edit target is not a file: {path}")
    if not target.exists():
        return False, ""
    try:
        return True, target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeToolError(f"Repository file is not UTF-8 text: {path}") from exc


def _write_evidence(
    path: str,
    before_exists: bool,
    before_content: str,
    after_exists: bool,
    after_content: str,
) -> dict[str, Any]:
    return {
        "path": path,
        "before_exists": before_exists,
        "before_sha256": file_sha256(before_content) if before_exists else None,
        "after_exists": after_exists,
        "after_sha256": file_sha256(after_content) if after_exists else None,
    }


def _safe_current_diff(root: Path, fallback: str = "") -> str:
    try:
        return get_git_diff(root)
    except Exception:
        return fallback


def _refresh_after_write(context: RuntimeToolContext, path: str) -> str | None:
    context.select_path(path)
    try:
        context.refresh_files()
    except Exception as exc:
        return str(exc)
    return None


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    marker = "\n[...truncated by RepoPilot runtime...]"
    return text[: limit - len(marker)] + marker, True
