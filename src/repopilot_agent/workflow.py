"""End-to-end local workflow for RepoPilot Agent."""

from __future__ import annotations

from pathlib import Path

from .llm.base import LLMClient, LLMError
from .llm.openai_compatible import OpenAICompatibleClient
from .models import PlanMetadata, WorkflowReport
from .patch_proposer import propose_patch
from .planner import create_plan, create_plan_with_optional_llm
from .scanner import scan_repository
from .search import search_files
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
) -> WorkflowReport:
    root = Path(repo_path).expanduser().resolve()
    files = scan_repository(root)
    hits = search_files(task, files, limit=search_limit)
    if use_llm:
        if llm_client is None:
            try:
                llm_client = OpenAICompatibleClient(model=llm_model)
            except LLMError as exc:
                if not allow_llm_fallback:
                    raise
                plan = create_plan(task, hits)
                plan_metadata = PlanMetadata(source="rules", model=llm_model, fallback_used=True, error=str(exc))
            else:
                plan, plan_metadata = create_plan_with_optional_llm(
                    task,
                    hits,
                    llm_client=llm_client,
                    allow_fallback=allow_llm_fallback,
                )
        else:
            plan, plan_metadata = create_plan_with_optional_llm(
                task,
                hits,
                llm_client=llm_client,
                allow_fallback=allow_llm_fallback,
            )
    else:
        plan = create_plan(task, hits)
        plan_metadata = PlanMetadata(source="rules")
    patch_proposal = propose_patch(task, hits)
    validation = run_validation(root, validation_commands or [])
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
        validation=validation,
        summary=summary,
    )


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
