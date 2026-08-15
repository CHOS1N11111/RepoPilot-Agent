"""Persistent state for sandboxed RepoPilot task runs."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .execution import (
    AcceptanceCriterion,
    CompletionEvidence,
    ExecutionBudget,
    ExecutionUsage,
    completion_from_record,
    criteria_from_records,
    execution_budget_state,
)
from .execution_profile import TaskRunExecutionProfile, execution_profile_from_record
from .repair_loop import RepairAttemptRecord, repair_attempts_from_records
from .worktree_sandbox import WorktreeSandboxError, list_worktree_sandboxes


TASK_RUN_STATUSES = {
    "queued",
    "creating_sandbox",
    "exploring",
    "awaiting_approval",
    "applying",
    "review_pending",
    "validating",
    "diagnosing",
    "replanning",
    "repair_pending",
    "pausing",
    "paused",
    "cancelling",
    "cancelled",
    "completed",
    "failed",
    "interrupted",
}

ACTIVE_TASK_RUN_STATUSES = {
    "queued",
    "creating_sandbox",
    "exploring",
    "applying",
    "validating",
    "diagnosing",
    "replanning",
    "pausing",
    "cancelling",
}

RESUMABLE_TASK_RUN_STATUSES = {"paused", "cancelled", "failed", "interrupted"}

RESUME_CHECKPOINT_SOURCE = "source_restart"
RESUME_CHECKPOINT_SANDBOX = "sandbox_analysis"
RESUME_CHECKPOINT_INSPECTION = "sandbox_inspection"
RESUME_CHECKPOINT_APPROVAL = "approval"
RESUME_CHECKPOINT_REPAIR = "repair_approval"
RESUME_CHECKPOINT_BLOCKED = "blocked"
MAX_TASK_RUN_CHECKPOINTS = 100


class TaskRunError(RuntimeError):
    """Raised when a task-run operation is invalid or unsafe."""


@dataclass(frozen=True)
class TaskRunEvent:
    status: str
    detail: str
    created_at: str


@dataclass(frozen=True)
class TaskRunCheckpoint:
    sequence: int
    phase: str
    status: str
    detail: str
    next_action: str
    created_at: str
    sandbox_path: str | None = None
    sandbox_head: str | None = None
    proposal_id: str | None = None
    execution_usage: dict[str, int] = field(default_factory=dict)
    execution_remaining: dict[str, int] = field(default_factory=dict)
    repair_attempt: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TaskRunResumePlan:
    checkpoint: str
    target_status: str
    reuse_sandbox: bool
    requires_sandbox: bool
    requires_clean_sandbox: bool
    blocked_reason: str = ""

    @property
    def allowed(self) -> bool:
        return not self.blocked_reason and self.checkpoint != RESUME_CHECKPOINT_BLOCKED

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["allowed"] = self.allowed
        return data


@dataclass
class TaskRun:
    run_id: str
    source_repo: str
    task: str
    validation_commands: list[str]
    created_at: str
    updated_at: str
    status: str = "queued"
    message: str = "Task run queued."
    sandbox_path: str | None = None
    sandbox_head: str | None = None
    proposal_id: str | None = None
    history_run_id: str | None = None
    delivery_branch: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    pause_requested: bool = False
    cancel_requested: bool = False
    resume_status: str | None = None
    events: list[TaskRunEvent] = field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)
    execution_budget: ExecutionBudget = field(default_factory=ExecutionBudget)
    execution_profile: TaskRunExecutionProfile | None = None
    execution_usage: ExecutionUsage = field(default_factory=ExecutionUsage)
    completion_evidence: CompletionEvidence | None = None
    repair_history: list[RepairAttemptRecord] = field(default_factory=list)
    repair_stop_reason: str | None = None
    repair_stop_message: str = ""
    auto_repair_enabled: bool = False
    interrupted_from: str | None = None
    interrupted_at: str | None = None
    interruption_reason: str | None = None
    resume_checkpoint: str | None = None
    last_resume_checkpoint: str | None = None
    resume_blocked_reason: str | None = None
    resume_count: int = 0
    last_resumed_at: str | None = None
    checkpoints: list[TaskRunCheckpoint] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        return asdict(self)

    def to_public_dict(self) -> dict[str, Any]:
        data = self.to_record()
        resume_plan = build_task_run_resume_plan(self)
        data["can_pause"] = self.status in ACTIVE_TASK_RUN_STATUSES and self.status not in {
            "pausing",
            "cancelling",
        }
        data["can_resume"] = self.status in RESUMABLE_TASK_RUN_STATUSES and resume_plan.allowed
        data["can_cancel"] = self.status not in {"cancelled", "completed"}
        data["can_approve"] = self.status == "awaiting_approval" and bool(self.proposal_id)
        pending_runtime_approval = (
            self.result.get("agent_pending_approval")
            if isinstance(self.result, dict)
            else None
        )
        data["can_approve_runtime"] = bool(
            self.status == "awaiting_approval"
            and isinstance(pending_runtime_approval, dict)
            and pending_runtime_approval.get("checkpoint")
        )
        data["can_repair"] = self.status == "repair_pending"
        data["can_create_branch"] = self.status == "completed" and not self.delivery_branch
        data["execution_budget"] = execution_budget_state(self.execution_budget, self.execution_usage)
        data["execution_profile"] = (
            self.execution_profile.to_dict() if self.execution_profile else None
        )
        data["acceptance_criteria"] = [item.to_dict() for item in self.acceptance_criteria]
        data["completion_evidence"] = (
            self.completion_evidence.to_dict() if self.completion_evidence else None
        )
        data["repair_history"] = [item.to_dict() for item in self.repair_history]
        data["checkpoints"] = [item.to_dict() for item in self.checkpoints]
        data["latest_checkpoint"] = (
            self.checkpoints[-1].to_dict() if self.checkpoints else None
        )
        if self.status in RESUMABLE_TASK_RUN_STATUSES:
            data["resume_checkpoint"] = resume_plan.checkpoint or self.resume_checkpoint
            data["resume_blocked_reason"] = resume_plan.blocked_reason or self.resume_blocked_reason
        else:
            data["resume_checkpoint"] = self.resume_checkpoint
            data["resume_blocked_reason"] = self.resume_blocked_reason
        data["resume_plan"] = resume_plan.to_dict()
        return data


_TASK_RUNS: dict[str, TaskRun] = {}
_TASK_RUN_LOCK = threading.RLock()


def create_task_run(
    source_repo: str | Path,
    task: str,
    validation_commands: list[str],
    execution_budget: ExecutionBudget | None = None,
    execution_profile: TaskRunExecutionProfile | None = None,
    auto_repair_enabled: bool = False,
) -> TaskRun:
    now = _now()
    task_run = TaskRun(
        run_id=uuid4().hex,
        source_repo=str(Path(source_repo).expanduser().resolve()),
        task=task,
        validation_commands=list(validation_commands),
        created_at=now,
        updated_at=now,
        events=[TaskRunEvent("queued", "Task run queued.", now)],
        execution_budget=execution_budget or ExecutionBudget(),
        execution_profile=execution_profile,
        auto_repair_enabled=auto_repair_enabled,
    )
    record_task_run_checkpoint(
        task_run,
        "task_queued",
        "Task run accepted and waiting for sandbox creation.",
        "create_sandbox",
    )
    return task_run


def get_task_run(run_id: str) -> TaskRun | None:
    with _TASK_RUN_LOCK:
        return _TASK_RUNS.get(run_id)


def cache_task_run(task_run: TaskRun) -> TaskRun:
    with _TASK_RUN_LOCK:
        _TASK_RUNS[task_run.run_id] = task_run
    return task_run


def clear_task_runs() -> None:
    with _TASK_RUN_LOCK:
        _TASK_RUNS.clear()


def task_run_from_record(record: dict[str, Any], mark_interrupted: bool = False) -> TaskRun:
    status = str(record.get("status") or "queued")
    if status not in TASK_RUN_STATUSES:
        status = "failed"
    events = [_event_from_record(item) for item in record.get("events", []) if isinstance(item, dict)]
    task_run = TaskRun(
        run_id=str(record.get("run_id") or ""),
        source_repo=str(record.get("source_repo") or ""),
        task=str(record.get("task") or ""),
        validation_commands=_string_list(record.get("validation_commands")),
        created_at=str(record.get("created_at") or _now()),
        updated_at=str(record.get("updated_at") or _now()),
        status=status,
        message=str(record.get("message") or ""),
        sandbox_path=_optional_string(record.get("sandbox_path")),
        sandbox_head=_optional_string(record.get("sandbox_head")),
        proposal_id=_optional_string(record.get("proposal_id")),
        history_run_id=_optional_string(record.get("history_run_id")),
        delivery_branch=_optional_string(record.get("delivery_branch")),
        result=record.get("result") if isinstance(record.get("result"), dict) else None,
        error=_optional_string(record.get("error")),
        pause_requested=bool(record.get("pause_requested")),
        cancel_requested=bool(record.get("cancel_requested")),
        resume_status=_optional_string(record.get("resume_status")),
        events=events,
        acceptance_criteria=criteria_from_records(record.get("acceptance_criteria")),
        execution_budget=ExecutionBudget.from_dict(record.get("execution_budget")),
        execution_profile=execution_profile_from_record(record.get("execution_profile")),
        execution_usage=ExecutionUsage.from_dict(record.get("execution_usage")),
        completion_evidence=completion_from_record(record.get("completion_evidence")),
        repair_history=repair_attempts_from_records(record.get("repair_history")),
        repair_stop_reason=_optional_string(record.get("repair_stop_reason")),
        repair_stop_message=str(record.get("repair_stop_message") or ""),
        auto_repair_enabled=bool(record.get("auto_repair_enabled", False)),
        interrupted_from=_optional_string(record.get("interrupted_from")),
        interrupted_at=_optional_string(record.get("interrupted_at")),
        interruption_reason=_optional_string(record.get("interruption_reason")),
        resume_checkpoint=_optional_string(record.get("resume_checkpoint")),
        last_resume_checkpoint=_optional_string(record.get("last_resume_checkpoint")),
        resume_blocked_reason=_optional_string(record.get("resume_blocked_reason")),
        resume_count=_nonnegative_int(record.get("resume_count")),
        last_resumed_at=_optional_string(record.get("last_resumed_at")),
        checkpoints=_checkpoints_from_records(record.get("checkpoints")),
    )
    if mark_interrupted and task_run.status in ACTIVE_TASK_RUN_STATUSES:
        mark_task_run_interrupted(task_run)
    return cache_task_run(task_run)


def mark_task_run_interrupted(
    task_run: TaskRun,
    reason: str = "server_restart",
) -> TaskRun:
    if task_run.status not in ACTIVE_TASK_RUN_STATUSES:
        return task_run
    previous_status = task_run.status
    detected_at = _now()
    if not task_run.resume_status:
        task_run.resume_status = previous_status
    if previous_status == "cancelling":
        checkpoint, blocked_reason = _resume_checkpoint_for_state(task_run, previous_status)
    elif task_run.resume_checkpoint:
        checkpoint = task_run.resume_checkpoint
        blocked_reason = task_run.resume_blocked_reason or ""
    else:
        state = task_run.resume_status if previous_status == "pausing" else previous_status
        checkpoint, blocked_reason = _resume_checkpoint_for_state(task_run, state or previous_status)
    update_task_run(
        task_run,
        "interrupted",
        "The server stopped while this task was active. No work was resumed automatically.",
        error="Task execution was interrupted by a server restart.",
        interrupted_from=previous_status,
        interrupted_at=detected_at,
        interruption_reason=reason,
        resume_checkpoint=checkpoint,
        resume_blocked_reason=blocked_reason or None,
    )
    record_task_run_checkpoint(
        task_run,
        "interrupted",
        "Server interruption recorded after the previous process stopped.",
        "inspect_sandbox",
    )
    return task_run


def update_task_run(
    task_run: TaskRun,
    status: str,
    message: str,
    **fields: Any,
) -> TaskRun:
    if status not in TASK_RUN_STATUSES:
        raise ValueError(f"Unknown task-run status: {status}")
    with _TASK_RUN_LOCK:
        for name, value in fields.items():
            if not hasattr(task_run, name):
                raise ValueError(f"Unknown task-run field: {name}")
            setattr(task_run, name, value)
        now = _now()
        changed = task_run.status != status or task_run.message != message
        task_run.status = status
        task_run.message = message
        task_run.updated_at = now
        if changed:
            task_run.events.append(TaskRunEvent(status, message, now))
        _TASK_RUNS[task_run.run_id] = task_run
    return task_run


def record_task_run_checkpoint(
    task_run: TaskRun,
    phase: str,
    detail: str,
    next_action: str,
) -> TaskRunCheckpoint:
    with _TASK_RUN_LOCK:
        sequence = task_run.checkpoints[-1].sequence + 1 if task_run.checkpoints else 1
        budget_state = execution_budget_state(task_run.execution_budget, task_run.execution_usage)
        checkpoint = TaskRunCheckpoint(
            sequence=sequence,
            phase=phase.strip() or "unknown",
            status=task_run.status,
            detail=detail.strip(),
            next_action=next_action.strip(),
            created_at=_now(),
            sandbox_path=task_run.sandbox_path,
            sandbox_head=task_run.sandbox_head,
            proposal_id=task_run.proposal_id,
            execution_usage=_int_mapping(budget_state.get("usage")),
            execution_remaining=_int_mapping(budget_state.get("remaining")),
            repair_attempt=max(
                (item.attempt for item in task_run.repair_history),
                default=0,
            ),
        )
        task_run.checkpoints.append(checkpoint)
        if len(task_run.checkpoints) > MAX_TASK_RUN_CHECKPOINTS:
            task_run.checkpoints = task_run.checkpoints[-MAX_TASK_RUN_CHECKPOINTS:]
        task_run.updated_at = checkpoint.created_at
        _TASK_RUNS[task_run.run_id] = task_run
    return checkpoint


def request_task_run_pause(task_run: TaskRun) -> TaskRun:
    if task_run.status in {"awaiting_approval", "repair_pending"}:
        task_run.resume_status = task_run.status
        task_run.pause_requested = False
        task_run.resume_checkpoint, blocked = _resume_checkpoint_for_state(
            task_run,
            task_run.status,
        )
        task_run.resume_blocked_reason = blocked or None
        update_task_run(task_run, "paused", "Task run paused at the approval checkpoint.")
        record_task_run_checkpoint(
            task_run,
            "paused",
            "Task run paused after reaching an approval boundary.",
            "manual_resume_or_cancel",
        )
        return task_run
    if task_run.status not in ACTIVE_TASK_RUN_STATUSES or task_run.status in {"pausing", "cancelling"}:
        raise TaskRunError(f"Task run cannot be paused while it is {task_run.status}.")
    task_run.pause_requested = True
    task_run.resume_status = task_run.status
    task_run.resume_checkpoint, blocked = _resume_checkpoint_for_state(task_run, task_run.status)
    task_run.resume_blocked_reason = blocked or None
    return update_task_run(
        task_run,
        "pausing",
        "Pause requested. It will take effect after the current safe operation finishes.",
    )


def request_task_run_cancel(task_run: TaskRun) -> TaskRun:
    if task_run.status in {"cancelled", "completed"}:
        raise TaskRunError(f"Task run cannot be cancelled while it is {task_run.status}.")
    task_run.cancel_requested = True
    if not task_run.resume_status:
        task_run.resume_status = task_run.status
    if not task_run.resume_checkpoint:
        task_run.resume_checkpoint, blocked = _resume_checkpoint_for_state(task_run, task_run.status)
        task_run.resume_blocked_reason = blocked or None
    if task_run.status in {
        "awaiting_approval",
        "repair_pending",
        "review_pending",
        "paused",
        "failed",
        "interrupted",
    }:
        update_task_run(
            task_run,
            "cancelled",
            "Task run cancelled. Its sandbox was preserved for inspection.",
        )
        record_task_run_checkpoint(
            task_run,
            "cancelled",
            "Cancellation completed without removing the task sandbox.",
            "inspect_sandbox",
        )
        return task_run
    return update_task_run(
        task_run,
        "cancelling",
        "Cancellation requested. It will take effect after the current safe operation finishes.",
    )


def checkpoint_task_run(task_run: TaskRun, resume_status: str) -> bool:
    """Return True when a worker should stop at a requested checkpoint."""
    if task_run.cancel_requested:
        update_task_run(
            task_run,
            "cancelled",
            "Task run cancelled at a safe checkpoint. Its sandbox was preserved for inspection.",
        )
        record_task_run_checkpoint(
            task_run,
            "cancelled",
            "Cancellation completed at a safe worker boundary.",
            "inspect_sandbox",
        )
        return True
    if task_run.pause_requested:
        task_run.pause_requested = False
        task_run.resume_status = resume_status
        task_run.resume_checkpoint, blocked = _resume_checkpoint_for_state(task_run, resume_status)
        task_run.resume_blocked_reason = blocked or None
        update_task_run(task_run, "paused", "Task run paused at a safe checkpoint.")
        record_task_run_checkpoint(
            task_run,
            "paused",
            "Pause completed at a safe worker boundary.",
            "manual_resume_or_cancel",
        )
        return True
    return False


def build_task_run_resume_plan(task_run: TaskRun) -> TaskRunResumePlan:
    if task_run.status not in RESUMABLE_TASK_RUN_STATUSES:
        return TaskRunResumePlan(
            checkpoint="",
            target_status=task_run.status,
            reuse_sandbox=False,
            requires_sandbox=False,
            requires_clean_sandbox=False,
            blocked_reason=f"Task run cannot be resumed while it is {task_run.status}.",
        )
    checkpoint = task_run.resume_checkpoint
    blocked_reason = task_run.resume_blocked_reason or ""
    if not checkpoint:
        if task_run.status == "interrupted":
            state = task_run.interrupted_from or task_run.resume_status or ""
        elif task_run.status == "paused":
            state = task_run.resume_status or ""
        else:
            state = task_run.status
        checkpoint, derived_block = _resume_checkpoint_for_state(task_run, state)
        blocked_reason = blocked_reason or derived_block
    if checkpoint == RESUME_CHECKPOINT_APPROVAL:
        if not task_run.proposal_id:
            blocked_reason = blocked_reason or "The saved approval checkpoint has no proposal."
        return TaskRunResumePlan(checkpoint, "awaiting_approval", False, False, False, blocked_reason)
    if checkpoint == RESUME_CHECKPOINT_REPAIR:
        if not task_run.proposal_id:
            blocked_reason = blocked_reason or "The saved repair checkpoint has no proposal."
        return TaskRunResumePlan(checkpoint, "repair_pending", False, False, False, blocked_reason)
    if checkpoint == RESUME_CHECKPOINT_SOURCE:
        return TaskRunResumePlan(checkpoint, "queued", False, False, False, blocked_reason)
    if checkpoint == RESUME_CHECKPOINT_SANDBOX:
        return TaskRunResumePlan(checkpoint, "queued", True, True, True, blocked_reason)
    if checkpoint == RESUME_CHECKPOINT_INSPECTION:
        return TaskRunResumePlan(checkpoint, "queued", True, True, True, blocked_reason)
    return TaskRunResumePlan(
        checkpoint=RESUME_CHECKPOINT_BLOCKED,
        target_status=task_run.status,
        reuse_sandbox=False,
        requires_sandbox=False,
        requires_clean_sandbox=False,
        blocked_reason=blocked_reason or "No safe manual resume checkpoint is available.",
    )


def validate_task_run_resume_request(
    task_run: TaskRun,
    checkpoint: str,
    confirmed: bool,
) -> TaskRunResumePlan:
    if task_run.status not in RESUMABLE_TASK_RUN_STATUSES:
        raise TaskRunError(f"Task run cannot be resumed while it is {task_run.status}.")
    if not confirmed:
        raise TaskRunError("Explicit manual resume confirmation is required.")
    plan = build_task_run_resume_plan(task_run)
    if not plan.allowed:
        raise TaskRunError(plan.blocked_reason)
    requested = checkpoint.strip()
    if requested != plan.checkpoint:
        raise TaskRunError(
            f"Resume checkpoint changed from {requested or '(missing)'} to {plan.checkpoint}; review it again."
        )
    return plan


def prepare_task_run_resume(
    task_run: TaskRun,
    checkpoint: str,
    confirmed: bool,
) -> TaskRun:
    plan = validate_task_run_resume_request(task_run, checkpoint, confirmed)
    if task_run.cancel_requested:
        task_run.cancel_requested = False
    task_run.pause_requested = False
    task_run.error = None
    task_run.last_resume_checkpoint = plan.checkpoint
    task_run.resume_checkpoint = None
    task_run.resume_blocked_reason = None
    task_run.resume_count += 1
    task_run.last_resumed_at = _now()
    if plan.checkpoint in {RESUME_CHECKPOINT_APPROVAL, RESUME_CHECKPOINT_REPAIR}:
        update_task_run(
            task_run,
            plan.target_status,
            "Task run manually resumed at the saved approval checkpoint.",
        )
        record_task_run_checkpoint(
            task_run,
            "manual_resume",
            "Manual resume restored the saved approval boundary.",
            "review_repair_proposal"
            if plan.checkpoint == RESUME_CHECKPOINT_REPAIR
            else "review_proposal",
        )
        return task_run
    if plan.checkpoint == RESUME_CHECKPOINT_SOURCE:
        message = "Manual resume confirmed. Task run queued with a new sandbox."
    elif plan.checkpoint == RESUME_CHECKPOINT_INSPECTION:
        message = "Sandbox inspection confirmed. Task run queued to restart analysis safely."
    else:
        message = "Manual resume confirmed. Task run queued from the preserved sandbox checkpoint."
    update_task_run(task_run, "queued", message)
    record_task_run_checkpoint(
        task_run,
        "manual_resume",
        "Manual resume preflight completed successfully.",
        "explore_repository" if plan.reuse_sandbox else "create_sandbox",
    )
    return task_run


def _resume_checkpoint_for_state(task_run: TaskRun, state: str) -> tuple[str, str]:
    if state == "awaiting_approval" and task_run.proposal_id:
        return RESUME_CHECKPOINT_APPROVAL, ""
    if state == "repair_pending" and task_run.proposal_id:
        return RESUME_CHECKPOINT_REPAIR, ""
    if state == "cancelling":
        return RESUME_CHECKPOINT_BLOCKED, "Cancellation was in progress when the server stopped."
    if state in {"applying", "validating", "diagnosing", "replanning"}:
        if task_run.sandbox_path:
            return RESUME_CHECKPOINT_INSPECTION, ""
        return RESUME_CHECKPOINT_BLOCKED, "The interrupted write phase has no sandbox to inspect."
    if state in {"queued", "creating_sandbox"}:
        if task_run.sandbox_path:
            return RESUME_CHECKPOINT_SANDBOX, ""
        return RESUME_CHECKPOINT_SOURCE, ""
    if state in {"exploring", "pausing"}:
        if task_run.sandbox_path:
            return RESUME_CHECKPOINT_SANDBOX, ""
        return RESUME_CHECKPOINT_BLOCKED, "The saved analysis checkpoint has no sandbox."
    if task_run.sandbox_path:
        return RESUME_CHECKPOINT_INSPECTION, ""
    return RESUME_CHECKPOINT_SOURCE, ""


def create_task_run_branch(task_run: TaskRun, branch_name: str, confirmed: bool) -> str:
    if not confirmed:
        raise TaskRunError("Explicit branch creation confirmation is required.")
    if task_run.status != "completed":
        raise TaskRunError("A feature branch can only be created after the task run completes successfully.")
    if task_run.delivery_branch:
        raise TaskRunError(f"Task run already uses branch {task_run.delivery_branch}.")
    if not task_run.sandbox_path:
        raise TaskRunError("Task run does not have a sandbox.")
    name = branch_name.strip()
    if not name:
        raise TaskRunError("Branch name is required.")
    sandbox = Path(task_run.sandbox_path).expanduser().resolve()
    if not sandbox.is_dir():
        raise TaskRunError(f"Task-run sandbox no longer exists: {sandbox}")
    try:
        managed = list_worktree_sandboxes(task_run.source_repo)
    except WorktreeSandboxError as exc:
        raise TaskRunError(str(exc)) from exc
    if not any(Path(item.path).resolve() == sandbox for item in managed):
        raise TaskRunError("Task-run branch creation is limited to registered managed worktrees.")
    _run_git(sandbox, ["check-ref-format", "--branch", name])
    exists = _run_git(sandbox, ["show-ref", "--verify", "--quiet", f"refs/heads/{name}"], check=False)
    if exists.returncode == 0:
        raise TaskRunError(f"Local branch already exists: {name}")
    _run_git(sandbox, ["switch", "-c", name])
    task_run.delivery_branch = name
    update_task_run(
        task_run,
        "completed",
        f"Created local feature branch {name}. Changes remain uncommitted and unpushed.",
    )
    return name


def _run_git(repo_path: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        raise TaskRunError("Git is required for task-run delivery.") from exc
    except subprocess.TimeoutExpired as exc:
        raise TaskRunError("Git task-run delivery command timed out.") from exc
    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "Git command failed."
        raise TaskRunError(message)
    return result


def _event_from_record(record: dict[str, Any]) -> TaskRunEvent:
    return TaskRunEvent(
        status=str(record.get("status") or "unknown"),
        detail=str(record.get("detail") or ""),
        created_at=str(record.get("created_at") or _now()),
    )


def _checkpoints_from_records(value: object) -> list[TaskRunCheckpoint]:
    if not isinstance(value, list):
        return []
    checkpoints: list[TaskRunCheckpoint] = []
    last_sequence = 0
    for item in value:
        if not isinstance(item, dict):
            continue
        requested_sequence = _nonnegative_int(item.get("sequence"))
        sequence = requested_sequence if requested_sequence > last_sequence else last_sequence + 1
        checkpoints.append(
            TaskRunCheckpoint(
                sequence=sequence,
                phase=str(item.get("phase") or "unknown"),
                status=str(item.get("status") or "unknown"),
                detail=str(item.get("detail") or ""),
                next_action=str(item.get("next_action") or ""),
                created_at=str(item.get("created_at") or _now()),
                sandbox_path=_optional_string(item.get("sandbox_path")),
                sandbox_head=_optional_string(item.get("sandbox_head")),
                proposal_id=_optional_string(item.get("proposal_id")),
                execution_usage=_int_mapping(item.get("execution_usage")),
                execution_remaining=_int_mapping(item.get("execution_remaining")),
                repair_attempt=_nonnegative_int(item.get("repair_attempt")),
            )
        )
        last_sequence = sequence
    return checkpoints[-MAX_TASK_RUN_CHECKPOINTS:]


def _int_mapping(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _nonnegative_int(item) for key, item in value.items()}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _nonnegative_int(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
