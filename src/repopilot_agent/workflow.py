"""End-to-end local workflow for RepoPilot Agent."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import replace
from pathlib import Path

from .agent_loop import AgentLoopResult, run_agent_loop, select_agent_hits
from .execution import (
    ExecutionBudget,
    ExecutionUsage,
    build_acceptance_criteria,
    execution_budget_state,
    pending_completion_evidence,
)
from .git_tools import get_git_diff
from .llm.base import LLMClient, LLMError
from .llm.openai_compatible import OpenAICompatibleClient
from .memory import MemoryStore, default_memory_path
from .models import LLMCallTrace, MemoryContextItem, PatchProposalMetadata, PlanMetadata, WorkflowReport
from .patch_proposer import propose_patch, propose_patch_with_optional_llm, review_patch_with_optional_llm
from .planner import create_plan, create_plan_with_optional_llm
from .runtime import RuntimeEventStore, SQLiteRuntimeStore
from .repository_map import build_repository_map, render_repository_map
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
    llm_json_mode: bool | None = None,
    llm_timeout_seconds: int | None = None,
    use_memory: bool = True,
    memory_context: list[MemoryContextItem] | None = None,
    iterative_agent: bool = False,
    agent_max_steps: int = 6,
    agent_run_id: str | None = None,
    agent_event_store: RuntimeEventStore | None = None,
    execution_budget: ExecutionBudget | None = None,
) -> WorkflowReport:
    started_at = time.monotonic()
    budget = execution_budget or ExecutionBudget(max_agent_steps=agent_max_steps)
    agent_step_limit = min(agent_max_steps, budget.max_agent_steps, budget.max_tool_calls)
    root = Path(repo_path).expanduser().resolve()
    files = scan_repository(root)
    hits = search_files(task, files, limit=search_limit)
    repository_map = build_repository_map(files)
    repository_map_context = render_repository_map(
        repository_map,
        task,
        seed_paths=[hit.path for hit in hits],
    )
    file_contents = {repo_file.relative_path: repo_file.content for repo_file in files}
    related_memory = _resolve_memory_context(root, task, use_memory, memory_context)
    agent_acceptance_criteria = (
        build_acceptance_criteria(task, [], []) if iterative_agent else []
    )
    current_diff = _load_current_diff(root) if use_llm and iterative_agent else ""
    llm_traces: list[LLMCallTrace] = []
    llm_creation_error: LLMError | None = None
    agent_result: AgentLoopResult | None = None
    if use_llm:
        if llm_client is None:
            try:
                llm_client = OpenAICompatibleClient(
                    model=llm_model,
                    json_mode=llm_json_mode,
                    timeout_seconds=llm_timeout_seconds,
                )
            except LLMError as exc:
                if not allow_llm_fallback:
                    raise
                llm_creation_error = exc
                plan = create_plan(task, hits, memory_context=related_memory)
                plan_metadata = PlanMetadata(source="rules", model=llm_model, fallback_used=True, error=str(exc))
        if llm_client is not None:
            if iterative_agent:
                try:
                    runtime_store = agent_event_store
                    if runtime_store is None:
                        try:
                            runtime_store = SQLiteRuntimeStore(MemoryStore(default_memory_path(root)))
                        except (OSError, sqlite3.Error):
                            runtime_store = None
                    agent_result = run_agent_loop(
                        task,
                        root,
                        files,
                        hits,
                        llm_client,
                        traces=llm_traces,
                        max_steps=agent_step_limit,
                        runtime_run_id=agent_run_id,
                        runtime_store=runtime_store,
                        repository_map=repository_map,
                        memory_context=related_memory,
                        current_diff=current_diff,
                        acceptance_criteria=agent_acceptance_criteria,
                        execution_budget=budget,
                    )
                    hits = select_agent_hits(hits, files, agent_result.selected_paths, search_limit)
                    repository_map_context = render_repository_map(
                        repository_map,
                        task,
                        seed_paths=[hit.path for hit in hits],
                    )
                except LLMError:
                    if not allow_llm_fallback:
                        raise
            plan, plan_metadata = create_plan_with_optional_llm(
                task,
                hits,
                llm_client=llm_client,
                allow_fallback=allow_llm_fallback,
                traces=llm_traces,
                memory_context=related_memory,
                repository_map_context=repository_map_context,
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
                repository_map_context=repository_map_context,
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
    proposed_paths = [edit.path for edit in patch_proposal.file_edits] if patch_proposal else []
    planned_validation = list(validation_commands or [])
    if not planned_validation and patch_proposal and patch_proposal.validation_plan:
        planned_validation = list(patch_proposal.validation_plan.commands)
    acceptance_criteria = build_acceptance_criteria(task, proposed_paths, planned_validation)
    runtime_tool_calls = sum(
        1 for event in (agent_result.events if agent_result else []) if event.event_type == "action_started"
    )
    usage = ExecutionUsage(
        agent_steps=len(agent_result.steps) if agent_result else 0,
        tool_calls=runtime_tool_calls,
        validation_commands=len(validation),
        elapsed_ms=max(int((time.monotonic() - started_at) * 1000), 0),
    )
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
        agent_steps=agent_result.steps if agent_result else [],
        agent_run_id=agent_result.runtime_run_id if agent_result else None,
        agent_events=agent_result.events if agent_result else [],
        agent_state=(
            agent_result.working_state.to_dict()
            if agent_result and agent_result.working_state
            else {}
        ),
        llm_traces=llm_traces,
        validation=validation,
        validation_feedback=validation_feedback,
        memory_context=related_memory,
        repository_map=repository_map.to_summary(
            task,
            seed_paths=[hit.path for hit in hits],
        ),
        acceptance_criteria=[criterion.to_dict() for criterion in acceptance_criteria],
        execution_budget=execution_budget_state(budget, usage),
        completion_evidence=pending_completion_evidence(acceptance_criteria).to_dict(),
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


def _load_current_diff(repo_path: Path) -> str:
    try:
        unstaged = get_git_diff(repo_path)
        staged = get_git_diff(repo_path, staged=True)
    except (OSError, RuntimeError):
        return ""
    sections: list[str] = []
    if unstaged.strip():
        sections.append(f"Unstaged diff:\n{unstaged.strip()}")
    if staged.strip():
        sections.append(f"Staged diff:\n{staged.strip()}")
    return "\n\n".join(sections)


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
