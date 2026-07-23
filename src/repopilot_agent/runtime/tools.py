"""Policy-gated tool implementations used by the unified agent runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..git_tools import get_git_diff, inspect_repository
from ..models import FileEditProposal, RepoFile
from ..patch_apply import apply_file_edits
from ..scanner import scan_repository
from ..search import search_files
from ..validator import run_validation
from .models import RuntimeAction, RuntimePolicy


MAX_FILE_CHARS = 12_000
MAX_DIFF_CHARS = 24_000
MAX_SEARCH_RESULTS = 8


class RuntimeToolError(RuntimeError):
    """Raised when a typed runtime tool receives invalid or unsafe input."""


@dataclass(frozen=True)
class RuntimeToolResult:
    summary: str
    data: dict[str, Any]


class RuntimeToolContext:
    def __init__(
        self,
        repo_path: str | Path,
        task: str,
        policy: RuntimePolicy,
        files: list[RepoFile] | None = None,
    ) -> None:
        self.repo_path = Path(repo_path).expanduser().resolve()
        if not self.repo_path.is_dir():
            raise FileNotFoundError(f"Repository path does not exist: {self.repo_path}")
        self.task = task
        self.policy = policy
        self.files = list(files) if files is not None else scan_repository(self.repo_path)
        self.selected_paths: list[str] = []

    def refresh_files(self) -> None:
        self.files = scan_repository(self.repo_path)

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
                "truncated": truncated,
                "selected_paths": list(context.selected_paths),
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

    if action.kind == "edit_file":
        path = _normalize_relative_path(_required_string(action, "path"))
        new_content = action.arguments.get("new_content")
        if not isinstance(new_content, str):
            raise RuntimeToolError("edit_file requires new_content as a string.")
        rationale = str(action.arguments.get("rationale") or action.rationale or "Runtime-approved edit.")
        allowed_paths = list(context.policy.allowed_edit_paths) or [path]
        result = apply_file_edits(
            context.repo_path,
            [FileEditProposal(path=path, new_content=new_content, rationale=rationale)],
            task=context.task,
            allowed_paths=allowed_paths,
        )
        context.refresh_files()
        context.select_path(path)
        diff, truncated = _clip(result.diff, MAX_DIFF_CHARS)
        return RuntimeToolResult(
            summary=result.message,
            data={
                "applied": result.applied,
                "changed_files": result.changed_files,
                "diff": diff,
                "diff_truncated": truncated,
                "selected_paths": list(context.selected_paths),
            },
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


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    marker = "\n[...truncated by RepoPilot runtime...]"
    return text[: limit - len(marker)] + marker, True
