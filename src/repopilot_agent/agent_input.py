"""Post-input continuation for a durable sandboxed Agent trajectory."""

from __future__ import annotations

import os
from pathlib import Path

from .agent_loop import AgentLoopResult, run_agent_loop
from .execution import ExecutionBudget
from .llm.base import LLMClient
from .models import LLMCallTrace, MemoryContextItem
from .repository_map import build_repository_map
from .runtime import RuntimeEventStore
from .scanner import scan_repository
from .search import search_files
from .worktree_sandbox import require_managed_worktree


class AgentInputError(RuntimeError):
    """Raised when an answered Agent trajectory cannot continue safely."""


def continue_agent_after_input(
    source_repo: str | Path,
    sandbox_path: str | Path,
    task: str,
    run_id: str,
    store: RuntimeEventStore,
    llm_client: LLMClient,
    *,
    max_steps: int,
    execution_budget: ExecutionBudget,
    memory_context: list[MemoryContextItem] | None = None,
    worktree_root: str | Path | None = None,
    traces: list[LLMCallTrace] | None = None,
    repair_context: str = "",
    blocked_repair_proposal_fingerprints: set[str] | None = None,
) -> AgentLoopResult:
    """Continue the same run after its exact pending question was answered."""

    sandbox = require_managed_worktree(
        sandbox_path,
        worktree_root=worktree_root,
    )
    if not _same_path(sandbox.source_repo, source_repo):
        raise AgentInputError(
            "The managed worktree does not belong to the task run's source repository."
        )

    root = Path(sandbox_path).expanduser().resolve()
    files = scan_repository(root)
    hits = search_files(task, files, limit=8)
    repository_map = build_repository_map(files)
    return run_agent_loop(
        task,
        root,
        files,
        hits,
        llm_client,
        traces=traces,
        max_steps=max_steps,
        runtime_run_id=run_id,
        runtime_store=store,
        repository_map=repository_map,
        memory_context=memory_context,
        execution_budget=execution_budget,
        allow_user_questions=True,
        allow_write_actions=True,
        managed_worktree_root=worktree_root,
        resume_existing_state=True,
        repair_context=repair_context,
        blocked_repair_proposal_fingerprints=blocked_repair_proposal_fingerprints,
    )


def _same_path(first: str | Path, second: str | Path) -> bool:
    return os.path.normcase(str(Path(first).expanduser().resolve())) == os.path.normcase(
        str(Path(second).expanduser().resolve())
    )
