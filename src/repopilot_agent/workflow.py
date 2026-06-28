"""End-to-end local workflow for RepoPilot Agent."""

from __future__ import annotations

from pathlib import Path

from .models import WorkflowReport
from .planner import create_plan
from .scanner import scan_repository
from .search import search_files
from .validator import run_validation


def run_workflow(
    repo_path: str | Path,
    task: str,
    validation_commands: list[str] | None = None,
    search_limit: int = 8,
) -> WorkflowReport:
    root = Path(repo_path).expanduser().resolve()
    files = scan_repository(root)
    hits = search_files(task, files, limit=search_limit)
    plan = create_plan(task, hits)
    validation = run_validation(root, validation_commands or [])
    summary = _build_summary(task, files_scanned=len(files), relevant_count=len(hits), validation=validation)
    return WorkflowReport(
        task=task,
        repo_path=str(root),
        files_scanned=len(files),
        relevant_files=hits,
        plan=plan,
        validation=validation,
        summary=summary,
    )


def _build_summary(task: str, files_scanned: int, relevant_count: int, validation: list) -> str:
    validation_count = len(validation)
    failed = [result for result in validation if result.exit_code not in (0, None)]
    rejected = [result for result in validation if not result.allowed]
    parts = [
        f"RepoPilot analyzed the task: {task}",
        f"Scanned {files_scanned} text files and selected {relevant_count} relevant files for review.",
    ]
    if validation_count:
        parts.append(f"Ran {validation_count} validation command(s).")
    if failed:
        parts.append(f"{len(failed)} validation command(s) failed and need inspection.")
    if rejected:
        parts.append(f"{len(rejected)} command(s) were rejected by the allowlist.")
    if not validation_count:
        parts.append("No validation commands were provided for this run.")
    return " ".join(parts)
