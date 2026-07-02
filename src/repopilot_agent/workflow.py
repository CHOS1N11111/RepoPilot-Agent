"""End-to-end local workflow for RepoPilot Agent."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from .llm.base import LLMClient, LLMError
from .llm.openai_compatible import OpenAICompatibleClient
from .memory import MemoryStore, default_memory_path
from .models import LLMCallTrace, MemoryContextItem, PatchProposalMetadata, PlanMetadata, WorkflowReport
from .patch_proposer import propose_patch, propose_patch_with_optional_llm, review_patch_with_optional_llm
from .planner import create_plan, create_plan_with_optional_llm
from .safety import check_file_edits
from .scanner import scan_repository
from .search import search_files
from .validation_feedback import build_validation_feedback
from .validation_planner import build_validation_plan
from .validator import run_validation


def run_workflow(
    repo_path: str | Path,
    task: str,
    validation_commands: list[str] | None = None,
    search_limit: int = 8,
    use_llm: bool = False,
    llm_client: LLMClient | None = None,
    llm_model: str | None = None,
    allow_llm_fallback: bool = True,
    use_memory: bool = True,
    memory_context: list[MemoryContextItem] | None = None,
) -> WorkflowReport:
    root = Path(repo_path).expanduser().resolve()
    files = scan_repository(root)
    hits = search_files(task, files, limit=search_limit)
    file_contents = {repo_file.relative_path: repo_file.content for repo_file in files}
    related_memory = _resolve_memory_context(root, task, use_memory, memory_context)
    llm_traces: list[LLMCallTrace] = []
    llm_creation_error: LLMError | None = None
    if use_llm:
        if llm_client is None:
            try:
                llm_client = OpenAICompatibleClient(model=llm_model)
            except LLMError as exc:
                if not allow_llm_fallback:
                    raise
                llm_creation_error = exc
                plan = create_plan(task, hits, memory_context=related_memory)
                plan_metadata = PlanMetadata(source="rules", model=llm_model, fallback_used=True, error=str(exc))
            else:
                plan, plan_metadata = create_plan_with_optional_llm(
                    task,
                    hits,
                    llm_client=llm_client,
                    allow_fallback=allow_llm_fallback,
                    traces=llm_traces,
                    memory_context=related_memory,
                )
        else:
            plan, plan_metadata = create_plan_with_optional_llm(
                task,
                hits,
                llm_client=llm_client,
                allow_fallback=allow_llm_fallback,
                traces=llm_traces,
                memory_context=related_memory,
            )
    else:
        plan = create_plan(task, hits, memory_context=related_memory)
        plan_metadata = PlanMetadata(source="rules")

    if use_llm:
        if llm_client is None:
            patch_proposal = propose_patch(task, hits)
            patch_proposal_metadata = PatchProposalMetadata(
                source="rules",
                model=llm_model,
                fallback_used=True,
                error=str(llm_creation_error) if llm_creation_error else "LLM client is unavailable.",
            )
        else:
            patch_proposal, patch_proposal_metadata = propose_patch_with_optional_llm(
                task,
                hits,
                plan,
                llm_client=llm_client,
                allow_fallback=allow_llm_fallback,
                file_contents=file_contents,
                traces=llm_traces,
            )
    else:
        patch_proposal = propose_patch(task, hits)
        patch_proposal_metadata = PatchProposalMetadata(source="rules")

    patch_proposal = _attach_validation_plan(root, patch_proposal)
    patch_proposal = _attach_safety_check(root, task, patch_proposal)
    patch_review = None
    if use_llm and llm_client is not None:
        patch_review = review_patch_with_optional_llm(
            task,
            patch_proposal,
            llm_client=llm_client,
            allow_fallback=allow_llm_fallback,
            traces=llm_traces,
        )

    validation = run_validation(root, validation_commands or [])
    validation_feedback = build_validation_feedback(validation, task=task, repo_path=root)
    summary = _build_summary(
        task,
        files_scanned=len(files),
        relevant_count=len(hits),
        proposal_ready=patch_proposal.ready_for_patch,
        validation=validation,
    )
    return WorkflowReport(
        task=task,
        repo_path=str(root),
        files_scanned=len(files),
        relevant_files=hits,
        plan=plan,
        plan_metadata=plan_metadata,
        patch_proposal=patch_proposal,
        patch_proposal_metadata=patch_proposal_metadata,
        patch_review=patch_review,
        llm_traces=llm_traces,
        validation=validation,
        validation_feedback=validation_feedback,
        memory_context=related_memory,
        summary=summary,
    )


def _resolve_memory_context(
    repo_path: Path,
    task: str,
    use_memory: bool,
    memory_context: list[MemoryContextItem] | None,
) -> list[MemoryContextItem]:
    if memory_context is not None:
        return memory_context
    if not use_memory:
        return []
    return _load_memory_context(repo_path, task)


def _load_memory_context(repo_path: Path, task: str) -> list[MemoryContextItem]:
    try:
        return MemoryStore(default_memory_path(repo_path)).find_related_runs(task)
    except (OSError, sqlite3.Error):
        return []


def _attach_safety_check(repo_path: Path, task: str, proposal):
    if proposal is None or not proposal.file_edits:
        return proposal
    safety_check = check_file_edits(
        repo_path,
        proposal.file_edits,
        task=task,
        allowed_paths=[file.path for file in proposal.files],
    )
    return replace(
        proposal,
        apply_ready=proposal.apply_ready and safety_check.ok,
        safety_check=safety_check,
    )


def _attach_validation_plan(repo_path: Path, proposal):
    if proposal is None:
        return proposal
    changed_paths = [edit.path for edit in proposal.file_edits] or [file.path for file in proposal.files]
    validation_plan = build_validation_plan(repo_path, changed_paths)
    validation_suggestions = _merge_validation_suggestions(
        proposal.validation_suggestions,
        validation_plan.commands,
        validation_plan.notes,
    )
    return replace(
        proposal,
        validation_plan=validation_plan,
        validation_suggestions=validation_suggestions,
    )


def _merge_validation_suggestions(
    existing: list[str],
    commands: list[str],
    notes: list[str],
) -> list[str]:
    merged: list[str] = []
    for item in [*existing, *commands, *notes]:
        if item and item not in merged:
            merged.append(item)
    return merged


def _build_summary(
    task: str,
    files_scanned: int,
    relevant_count: int,
    proposal_ready: bool,
    validation: list,
) -> str:
    validation_count = len(validation)
    failed = [result for result in validation if result.exit_code not in (0, None)]
    rejected = [result for result in validation if not result.allowed]
    parts = [
        f"RepoPilot analyzed the task: {task}",
        f"Scanned {files_scanned} text files and selected {relevant_count} relevant files for review.",
    ]
    if proposal_ready:
        parts.append("Prepared file-level change proposals for user review.")
    else:
        parts.append("No patch proposal was prepared because no relevant files were selected.")
    if validation_count:
        parts.append(f"Ran {validation_count} validation command(s).")
    if failed:
        parts.append(f"{len(failed)} validation command(s) failed and need inspection.")
    if rejected:
        parts.append(f"{len(rejected)} command(s) were rejected by the allowlist.")
    if not validation_count:
        parts.append("No validation commands were provided for this run.")
    return " ".join(parts)
