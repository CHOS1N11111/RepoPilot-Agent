"""Persistent state for sandboxed RepoPilot task runs."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .worktree_sandbox import WorktreeSandboxError, list_worktree_sandboxes


TASK_RUN_STATUSES = {
    "queued",
    "creating_sandbox",
    "exploring",
    "awaiting_approval",
    "applying",
    "validating",
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
    "pausing",
    "cancelling",
}

RESUMABLE_TASK_RUN_STATUSES = {"paused", "cancelled", "failed", "interrupted"}


class TaskRunError(RuntimeError):
    """Raised when a task-run operation is invalid or unsafe."""


@dataclass(frozen=True)
class TaskRunEvent:
    status: str
    detail: str
    created_at: str


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

    def to_record(self) -> dict[str, Any]:
        return asdict(self)

    def to_public_dict(self) -> dict[str, Any]:
        data = self.to_record()
        data["can_pause"] = self.status in ACTIVE_TASK_RUN_STATUSES and self.status not in {
            "pausing",
            "cancelling",
        }
        data["can_resume"] = self.status in RESUMABLE_TASK_RUN_STATUSES
        data["can_cancel"] = self.status not in {"cancelled", "completed"}
        data["can_approve"] = self.status == "awaiting_approval" and bool(self.proposal_id)
        data["can_repair"] = self.status == "repair_pending"
        data["can_create_branch"] = self.status == "completed" and not self.delivery_branch
        return data


_TASK_RUNS: dict[str, TaskRun] = {}
_TASK_RUN_LOCK = threading.RLock()


def create_task_run(
    source_repo: str | Path,
    task: str,
    validation_commands: list[str],
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
    )
    return cache_task_run(task_run)


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
    )
    if mark_interrupted and task_run.status in ACTIVE_TASK_RUN_STATUSES:
        update_task_run(
            task_run,
            "interrupted",
            "The server stopped while this task was active. Resume it to restart from a safe checkpoint.",
            error="Task execution was interrupted by a server restart.",
        )
    return cache_task_run(task_run)


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


def request_task_run_pause(task_run: TaskRun) -> TaskRun:
    if task_run.status in {"awaiting_approval", "repair_pending"}:
        task_run.resume_status = task_run.status
        task_run.pause_requested = False
        return update_task_run(task_run, "paused", "Task run paused at the approval checkpoint.")
    if task_run.status not in ACTIVE_TASK_RUN_STATUSES or task_run.status in {"pausing", "cancelling"}:
        raise TaskRunError(f"Task run cannot be paused while it is {task_run.status}.")
    task_run.pause_requested = True
    task_run.resume_status = task_run.status
    return update_task_run(
        task_run,
        "pausing",
        "Pause requested. It will take effect after the current safe operation finishes.",
    )


def request_task_run_cancel(task_run: TaskRun) -> TaskRun:
    if task_run.status in {"cancelled", "completed"}:
        raise TaskRunError(f"Task run cannot be cancelled while it is {task_run.status}.")
    task_run.cancel_requested = True
    if task_run.status in {"awaiting_approval", "repair_pending", "paused", "failed", "interrupted"}:
        return update_task_run(
            task_run,
            "cancelled",
            "Task run cancelled. Its sandbox was preserved for inspection.",
        )
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
        return True
    if task_run.pause_requested:
        task_run.pause_requested = False
        task_run.resume_status = resume_status
        update_task_run(task_run, "paused", "Task run paused at a safe checkpoint.")
        return True
    return False


def prepare_task_run_resume(task_run: TaskRun) -> TaskRun:
    if task_run.status not in RESUMABLE_TASK_RUN_STATUSES:
        raise TaskRunError(f"Task run cannot be resumed while it is {task_run.status}.")
    if task_run.cancel_requested:
        task_run.cancel_requested = False
    task_run.pause_requested = False
    task_run.error = None
    status = task_run.resume_status or ("exploring" if task_run.sandbox_path else "queued")
    if status in {"awaiting_approval", "repair_pending"} and task_run.proposal_id:
        return update_task_run(task_run, status, "Task run resumed at the approval checkpoint.")
    return update_task_run(task_run, "queued", "Task run queued to resume from its sandbox checkpoint.")


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


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
