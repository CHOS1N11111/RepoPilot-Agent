"""Approval-gated validation and post-validation Agent continuation."""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .agent_loop import AgentLoopResult, run_agent_loop
from .execution import ExecutionBudget
from .llm.base import LLMClient
from .models import AgentStep, LLMCallTrace, MemoryContextItem
from .repository_map import build_repository_map
from .runtime import (
    AgentRuntime,
    RuntimeAction,
    RuntimeEventStore,
    RuntimeObservation,
    RuntimePolicy,
    advance_agent_working_state,
    stop_agent_working_state,
)
from .scanner import scan_repository
from .search import search_files


class AgentValidationError(RuntimeError):
    """Raised when an exact managed validation action cannot continue safely."""


@dataclass(frozen=True)
class AgentValidationRequest:
    cycle_id: str
    command_index: int
    command_count: int
    command: str
    observation: RuntimeObservation
    pending_approval: dict[str, Any]
    working_state: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["observation"] = self.observation.to_dict()
        return data


@dataclass(frozen=True)
class AgentValidationResult:
    cycle_id: str
    command_index: int
    command_count: int
    command: str
    status: str
    observation: RuntimeObservation
    working_state: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["observation"] = self.observation.to_dict()
        data["validation"] = dict(self.observation.data)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentValidationResult":
        observation = RuntimeObservation.from_dict(
            dict(data.get("observation") or {})
        )
        return cls(
            cycle_id=str(data.get("cycle_id") or ""),
            command_index=int(data.get("command_index") or 0),
            command_count=int(data.get("command_count") or 0),
            command=str(data.get("command") or ""),
            status=str(data.get("status") or "failed"),
            observation=observation,
            working_state=dict(data.get("working_state") or {}),
        )


def request_agent_validation(
    source_repo: str | Path,
    sandbox_path: str | Path,
    task: str,
    run_id: str,
    store: RuntimeEventStore,
    *,
    cycle_id: str,
    command_index: int,
    command_count: int,
    command: str,
    worktree_root: str | Path | None = None,
) -> AgentValidationRequest:
    """Persist an exact approval request for one configured validation command."""

    normalized = _validation_position(cycle_id, command_index, command_count, command)
    runtime = _managed_validation_runtime(
        source_repo,
        sandbox_path,
        task,
        run_id,
        store,
        [normalized[3]],
        worktree_root,
    )
    state = runtime.working_state
    if state is None:
        raise AgentValidationError("The Agent Working State is missing for validation.")
    action = RuntimeAction(
        kind="validate",
        arguments={"command": normalized[3]},
        rationale=(
            f"Validate the approved managed-worktree change with command "
            f"{normalized[1] + 1} of {normalized[2]}."
        ),
        action_id=_validation_action_id(*normalized),
        idempotency_key=_validation_action_id(*normalized),
    )
    observation = runtime.execute(action)
    if observation.status != "approval_required":
        raise AgentValidationError(
            observation.error
            or observation.summary
            or "Validation did not stop at the exact approval boundary."
        )
    state = advance_agent_working_state(
        state,
        action,
        observation,
        selected_paths=state.selected_paths,
        expected_evidence=f"Bounded validation output for: {normalized[3]}",
    )
    state = stop_agent_working_state(
        state,
        "approval_required",
        selected_paths=state.selected_paths,
    )
    runtime.record_working_state(state)
    runtime.stop("approval_required", observation.summary)
    return AgentValidationRequest(
        cycle_id=normalized[0],
        command_index=normalized[1],
        command_count=normalized[2],
        command=normalized[3],
        observation=observation,
        pending_approval=runtime.pending_approval,
        working_state=state.to_dict(),
    )


def execute_pending_agent_validation(
    source_repo: str | Path,
    sandbox_path: str | Path,
    task: str,
    run_id: str,
    store: RuntimeEventStore,
    *,
    cycle_id: str,
    command_index: int,
    command_count: int,
    expected_command: str,
    checkpoint: str,
    payload_hash: str,
    file_scope: list[str] | tuple[str, ...],
    command_allowlist: list[str] | tuple[str, ...],
    worktree_root: str | Path | None = None,
) -> AgentValidationResult:
    """Grant and execute exactly one persisted managed validation action."""

    normalized = _validation_position(
        cycle_id,
        command_index,
        command_count,
        expected_command,
    )
    runtime = _managed_validation_runtime(
        source_repo,
        sandbox_path,
        task,
        run_id,
        store,
        [normalized[3]],
        worktree_root,
    )
    request = runtime.pending_approval
    if not request or request.get("checkpoint") != checkpoint:
        raise AgentValidationError(
            "The pending validation approval checkpoint is missing or stale."
        )
    action = RuntimeAction.from_dict(dict(request.get("action") or {}))
    if (
        action.kind != "validate"
        or str(action.arguments.get("command") or "").strip() != normalized[3]
        or action.action_id != _validation_action_id(*normalized)
    ):
        raise AgentValidationError(
            "The pending validation action does not match the current validation cycle."
        )

    runtime.grant_approval(
        checkpoint,
        payload_hash=payload_hash,
        file_scope=file_scope,
        command_allowlist=command_allowlist,
    )
    observation = runtime.execute(action)
    if observation.status == "approval_required":
        raise AgentValidationError(
            "The validation grant became stale before command execution."
        )
    state = runtime.working_state
    if state is None:
        raise AgentValidationError("The Agent Working State is missing after validation.")
    state = advance_agent_working_state(
        state,
        action,
        observation,
        selected_paths=state.selected_paths,
        expected_evidence=f"Bounded validation output for: {normalized[3]}",
    )
    runtime.record_working_state(state)
    passed = bool(
        observation.status == "completed"
        and observation.data.get("passed") is True
    )
    runtime.stop(
        "validation_passed" if passed else "validation_failed",
        observation.summary,
    )
    return AgentValidationResult(
        cycle_id=normalized[0],
        command_index=normalized[1],
        command_count=normalized[2],
        command=normalized[3],
        status="passed" if passed else "failed",
        observation=observation,
        working_state=state.to_dict(),
    )


def reject_pending_agent_validation(
    source_repo: str | Path,
    sandbox_path: str | Path,
    task: str,
    run_id: str,
    store: RuntimeEventStore,
    *,
    checkpoint: str,
    expected_command: str,
    reason: str = "Rejected by the user.",
    worktree_root: str | Path | None = None,
) -> dict[str, Any]:
    runtime = _managed_validation_runtime(
        source_repo,
        sandbox_path,
        task,
        run_id,
        store,
        [expected_command],
        worktree_root,
    )
    request = runtime.pending_approval
    action = RuntimeAction.from_dict(dict(request.get("action") or {})) if request else None
    if (
        action is None
        or request.get("checkpoint") != checkpoint
        or action.kind != "validate"
        or str(action.arguments.get("command") or "").strip() != expected_command.strip()
    ):
        raise AgentValidationError("The pending validation approval is missing or stale.")
    runtime.reject_approval(checkpoint, reason)
    state = runtime.working_state
    if state is None:
        raise AgentValidationError("The Agent Working State is missing after rejection.")
    state = stop_agent_working_state(state, "approval_rejected")
    runtime.record_working_state(state)
    runtime.stop("approval_rejected", reason)
    return {
        "status": "rejected",
        "run_id": run_id,
        "checkpoint": checkpoint,
        "command": expected_command.strip(),
        "working_state": state.to_dict(),
    }


def continue_agent_after_validation(
    source_repo: str | Path,
    sandbox_path: str | Path,
    task: str,
    run_id: str,
    store: RuntimeEventStore,
    llm_client: LLMClient,
    validation_results: list[AgentValidationResult],
    *,
    max_steps: int,
    execution_budget: ExecutionBudget,
    memory_context: list[MemoryContextItem] | None = None,
    worktree_root: str | Path | None = None,
    traces: list[LLMCallTrace] | None = None,
) -> AgentLoopResult:
    """Continue the existing controller with bounded validation evidence."""

    if not validation_results:
        raise AgentValidationError(
            "At least one validation result is required before Agent continuation."
        )
    _managed_validation_runtime(
        source_repo,
        sandbox_path,
        task,
        run_id,
        store,
        [result.command for result in validation_results],
        worktree_root,
    )
    root = Path(sandbox_path).expanduser().resolve()
    files = scan_repository(root)
    hits = search_files(task, files, limit=8)
    repository_map = build_repository_map(files)
    prior_steps = [
        AgentStep(
            order=result.command_index + 1,
            action="validate",
            thought="Execute the exact human-approved validation command.",
            tool_input=result.command,
            observation=_validation_observation_text(result),
            selected_paths=[],
            expected_evidence=f"Bounded validation output for: {result.command}",
        )
        for result in validation_results
    ]
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
        prior_steps=prior_steps,
    )


def _managed_validation_runtime(
    source_repo: str | Path,
    sandbox_path: str | Path,
    task: str,
    run_id: str,
    store: RuntimeEventStore,
    allowed_commands: list[str] | tuple[str, ...],
    worktree_root: str | Path | None,
) -> AgentRuntime:
    policy = RuntimePolicy.managed_validation(
        allowed_commands=allowed_commands,
        worktree_root=(
            str(Path(worktree_root).expanduser().resolve())
            if worktree_root is not None
            else ""
        ),
    )
    runtime = AgentRuntime(
        sandbox_path,
        task,
        run_id=run_id,
        policy=policy,
        store=store,
    )
    if runtime.sandbox is None or not _same_path(runtime.sandbox.source_repo, source_repo):
        raise AgentValidationError(
            "The managed worktree does not belong to the task run's source repository."
        )
    return runtime


def _validation_position(
    cycle_id: str,
    command_index: int,
    command_count: int,
    command: str,
) -> tuple[str, int, int, str]:
    normalized_cycle = str(cycle_id).strip()
    normalized_command = str(command).strip()
    if not normalized_cycle:
        raise AgentValidationError("Validation cycle_id is required.")
    if not normalized_command:
        raise AgentValidationError("Validation command is required.")
    if (
        not isinstance(command_index, int)
        or isinstance(command_index, bool)
        or not isinstance(command_count, int)
        or isinstance(command_count, bool)
        or command_count <= 0
        or not 0 <= command_index < command_count
    ):
        raise AgentValidationError("Validation command position is invalid.")
    return normalized_cycle, command_index, command_count, normalized_command


def _validation_action_id(
    cycle_id: str,
    command_index: int,
    command_count: int,
    command: str,
) -> str:
    payload = f"{cycle_id}\0{command_index}\0{command_count}\0{command}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"validation-{command_index + 1}-{digest}"


def _validation_observation_text(result: AgentValidationResult) -> str:
    data = result.observation.data
    return "\n".join(
        [
            result.observation.summary,
            f"Command: {result.command}",
            f"Passed: {'yes' if data.get('passed') else 'no'}",
            f"Exit code: {data.get('exit_code')}",
            f"Stdout truncated: {'yes' if data.get('stdout_truncated') else 'no'}",
            str(data.get("stdout") or "(empty stdout)"),
            f"Stderr truncated: {'yes' if data.get('stderr_truncated') else 'no'}",
            str(data.get("stderr") or "(empty stderr)"),
        ]
    )


def _same_path(first: str | Path, second: str | Path) -> bool:
    return os.path.normcase(str(Path(first).expanduser().resolve())) == os.path.normcase(
        str(Path(second).expanduser().resolve())
    )
