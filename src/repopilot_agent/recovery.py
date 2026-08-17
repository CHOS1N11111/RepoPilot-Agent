"""Read-only recovery readiness checks for persistent task runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .execution_profile import (
    ExecutionProfileComparison,
    TaskRunExecutionProfile,
    compare_execution_profiles,
)
from .git_tools import inspect_repository
from .runtime import RuntimeEvent, analyze_runtime_recovery
from .task_runs import (
    RESUME_CHECKPOINT_APPROVAL,
    RESUME_CHECKPOINT_REPAIR,
    RESUME_CHECKPOINT_SOURCE,
    TaskRun,
    build_task_run_resume_plan,
)


@dataclass(frozen=True)
class RecoveryReadinessCheck:
    name: str
    status: str
    detail: str
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TaskRunRecoveryReadiness:
    ready: bool
    checkpoint: str
    target_status: str
    reuse_sandbox: bool
    checked_at: str
    summary: str
    checks: list[RecoveryReadinessCheck]
    execution_profile_comparison: ExecutionProfileComparison | None = None
    runtime_recovery: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["checks"] = [item.to_dict() for item in self.checks]
        data["execution_profile_comparison"] = (
            self.execution_profile_comparison.to_dict()
            if self.execution_profile_comparison
            else None
        )
        data["runtime_recovery"] = self.runtime_recovery
        data["blockers"] = [
            item.detail
            for item in self.checks
            if item.required and item.status == "failed"
        ]
        data["warnings"] = [
            item.detail for item in self.checks if item.status == "warning"
        ]
        return data


def inspect_task_run_recovery(
    task_run: TaskRun,
    proposal_record: object = None,
    current_execution_profile: TaskRunExecutionProfile | None = None,
    runtime_events: list[RuntimeEvent] | None = None,
) -> TaskRunRecoveryReadiness:
    """Inspect recovery prerequisites without changing task or repository state."""

    plan = build_task_run_resume_plan(task_run)
    checks: list[RecoveryReadinessCheck] = []
    checks.append(
        RecoveryReadinessCheck(
            name="resume_plan",
            status="passed" if plan.allowed else "failed",
            detail=(
                f"Resume Plan selects {plan.checkpoint} and targets {plan.target_status}."
                if plan.allowed
                else plan.blocked_reason
            ),
        )
    )
    checks.append(_execution_checkpoint_check(task_run))
    runtime_recovery = None
    if runtime_events is not None:
        runtime_plan = analyze_runtime_recovery(
            runtime_events,
            objective=task_run.task,
        )
        runtime_recovery = runtime_plan.to_dict()
        checks.append(_runtime_recovery_check(runtime_recovery, bool(runtime_events)))

    if plan.allowed and plan.checkpoint == RESUME_CHECKPOINT_SOURCE:
        checks.extend(_repository_checks(task_run.source_repo, "source", require_clean=True))
    elif plan.allowed and plan.requires_sandbox:
        checks.extend(
            _repository_checks(
                task_run.sandbox_path,
                "sandbox",
                require_clean=plan.requires_clean_sandbox,
            )
        )
        checks.append(_sandbox_head_check(task_run))
    else:
        checks.append(
            RecoveryReadinessCheck(
                name="repository_target",
                status="not_required",
                detail="This recovery checkpoint does not restart repository analysis.",
                required=False,
            )
        )

    profile_comparison = None
    if current_execution_profile is not None:
        profile_comparison = compare_execution_profiles(
            task_run.execution_profile,
            current_execution_profile,
        )
        checks.append(
            RecoveryReadinessCheck(
                name="execution_profile",
                status="passed" if profile_comparison.status == "matched" else "warning",
                detail=profile_comparison.summary,
                required=False,
            )
        )

    if plan.checkpoint in {RESUME_CHECKPOINT_APPROVAL, RESUME_CHECKPOINT_REPAIR}:
        checks.append(_proposal_check(task_run, proposal_record))
    else:
        checks.append(
            RecoveryReadinessCheck(
                name="proposal_session",
                status="not_required",
                detail="This recovery checkpoint does not restore a proposal session.",
                required=False,
            )
        )

    blockers = [item.detail for item in checks if item.required and item.status == "failed"]
    ready = plan.allowed and not blockers
    if ready:
        summary = (
            "Stored recovery state checks passed. Manual confirmation and request configuration "
            "are still required."
        )
    else:
        summary = "Recovery preflight failed: " + (blockers[0] if blockers else plan.blocked_reason)
    return TaskRunRecoveryReadiness(
        ready=ready,
        checkpoint=plan.checkpoint,
        target_status=plan.target_status,
        reuse_sandbox=plan.reuse_sandbox,
        checked_at=_now(),
        summary=summary,
        checks=checks,
        execution_profile_comparison=profile_comparison,
        runtime_recovery=runtime_recovery,
    )


def _runtime_recovery_check(
    recovery: dict[str, Any],
    has_events: bool,
) -> RecoveryReadinessCheck:
    if not has_events:
        return RecoveryReadinessCheck(
            name="runtime_action",
            status="warning",
            detail="This legacy task has no Runtime action events; phase-level recovery will be used.",
            required=False,
        )
    next_step = str(recovery.get("next_step") or "")
    pending = recovery.get("pending_action")
    if next_step == "await_input":
        return RecoveryReadinessCheck(
            name="runtime_action",
            status="failed",
            detail="The Runtime is waiting for durable user input, which is not resumable yet.",
        )
    if bool(recovery.get("requires_confirmation")) and isinstance(pending, dict):
        return RecoveryReadinessCheck(
            name="runtime_action",
            status="warning",
            detail=(
                f"Interrupted {pending.get('action_kind', 'side-effect')} action "
                f"{pending.get('action_id', '(unknown)')} has an unknown outcome and requires "
                "exact confirmation before continuation."
            ),
            required=False,
        )
    return RecoveryReadinessCheck(
        name="runtime_action",
        status="passed",
        detail=str(recovery.get("summary") or "Runtime action recovery is ready."),
    )


def _execution_checkpoint_check(task_run: TaskRun) -> RecoveryReadinessCheck:
    if not task_run.checkpoints:
        return RecoveryReadinessCheck(
            name="execution_checkpoint",
            status="warning",
            detail="This legacy task has no execution-checkpoint history to compare.",
            required=False,
        )
    latest = task_run.checkpoints[-1]
    failures: list[str] = []
    warnings: list[str] = []
    if latest.status != task_run.status:
        warnings.append(
            f"latest checkpoint status is {latest.status}, while the task status is {task_run.status}"
        )
    if latest.sandbox_path and task_run.sandbox_path and not _same_path(
        latest.sandbox_path,
        task_run.sandbox_path,
    ):
        failures.append("the latest checkpoint references a different sandbox")
    if latest.proposal_id and latest.proposal_id != task_run.proposal_id:
        failures.append("the latest checkpoint references a different proposal")
    if failures:
        return RecoveryReadinessCheck(
            name="execution_checkpoint",
            status="failed",
            detail="Execution checkpoint mismatch: " + "; ".join(failures) + ".",
        )
    if warnings:
        return RecoveryReadinessCheck(
            name="execution_checkpoint",
            status="warning",
            detail="Execution checkpoint warning: " + "; ".join(warnings) + ".",
            required=False,
        )
    return RecoveryReadinessCheck(
        name="execution_checkpoint",
        status="passed",
        detail=f"Execution checkpoint #{latest.sequence} matches the persisted task references.",
    )


def _repository_checks(
    value: str | None,
    label: str,
    *,
    require_clean: bool,
) -> list[RecoveryReadinessCheck]:
    display = label.capitalize()
    if not value:
        return [
            RecoveryReadinessCheck(
                name=f"{label}_exists",
                status="failed",
                detail=f"The saved task {label} path is missing.",
            )
        ]
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        return [
            RecoveryReadinessCheck(
                name=f"{label}_exists",
                status="failed",
                detail=f"The saved task {label} no longer exists: {path}",
            )
        ]
    checks = [
        RecoveryReadinessCheck(
            name=f"{label}_exists",
            status="passed",
            detail=f"{display} directory exists: {path}",
        )
    ]
    try:
        repository = inspect_repository(path)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        checks.append(
            RecoveryReadinessCheck(
                name=f"{label}_git",
                status="failed",
                detail=f"{display} Git inspection failed: {exc}",
            )
        )
        return checks
    checks.append(
        RecoveryReadinessCheck(
            name=f"{label}_git",
            status="passed",
            detail=f"{display} is a readable Git worktree.",
        )
    )
    if require_clean:
        checks.append(
            RecoveryReadinessCheck(
                name=f"{label}_clean",
                status="passed" if repository.clean else "failed",
                detail=(
                    f"The task {label} is clean."
                    if repository.clean
                    else f"The task {label} has uncommitted changes. Inspect or revert them before resuming."
                ),
            )
        )
    else:
        checks.append(
            RecoveryReadinessCheck(
                name=f"{label}_clean",
                status="not_required",
                detail=f"A clean {label} is not required for this checkpoint.",
                required=False,
            )
        )
    return checks


def _sandbox_head_check(task_run: TaskRun) -> RecoveryReadinessCheck:
    if not task_run.sandbox_path or not Path(task_run.sandbox_path).is_dir():
        return RecoveryReadinessCheck(
            name="sandbox_head",
            status="failed",
            detail="Sandbox HEAD cannot be checked because the sandbox is unavailable.",
        )
    if not task_run.sandbox_head:
        return RecoveryReadinessCheck(
            name="sandbox_head",
            status="warning",
            detail="The task record has no saved sandbox HEAD to compare.",
            required=False,
        )
    try:
        repository = inspect_repository(task_run.sandbox_path)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        return RecoveryReadinessCheck(
            name="sandbox_head",
            status="failed",
            detail=f"Sandbox HEAD inspection failed: {exc}",
        )
    current = repository.latest_commit.short_hash if repository.latest_commit else ""
    if current and task_run.sandbox_head.startswith(current):
        return RecoveryReadinessCheck(
            name="sandbox_head",
            status="passed",
            detail=f"Sandbox HEAD still matches {current}.",
        )
    return RecoveryReadinessCheck(
        name="sandbox_head",
        status="failed",
        detail=(
            f"Sandbox HEAD changed from {task_run.sandbox_head[:12]} to {current or 'unknown'}. "
            "Inspect the worktree before resuming."
        ),
    )


def _proposal_check(
    task_run: TaskRun,
    proposal_record: object,
) -> RecoveryReadinessCheck:
    if not task_run.proposal_id:
        return RecoveryReadinessCheck(
            name="proposal_session",
            status="failed",
            detail="The recovery checkpoint has no proposal id.",
        )
    if proposal_record is None:
        return RecoveryReadinessCheck(
            name="proposal_session",
            status="failed",
            detail=f"Persisted proposal session {task_run.proposal_id} was not found.",
        )
    if not isinstance(proposal_record, dict):
        return RecoveryReadinessCheck(
            name="proposal_session",
            status="failed",
            detail=f"Persisted proposal session {task_run.proposal_id} is invalid.",
        )
    record_id = str(proposal_record.get("proposal_id") or "").strip()
    if record_id != task_run.proposal_id:
        return RecoveryReadinessCheck(
            name="proposal_session",
            status="failed",
            detail="The persisted proposal session id does not match the task run.",
        )
    record_repo = str(proposal_record.get("repo_path") or "").strip()
    if task_run.sandbox_path and (not record_repo or not _same_path(record_repo, task_run.sandbox_path)):
        return RecoveryReadinessCheck(
            name="proposal_session",
            status="failed",
            detail="The persisted proposal session references a different sandbox.",
        )
    return RecoveryReadinessCheck(
        name="proposal_session",
        status="passed",
        detail=f"Persisted proposal session {task_run.proposal_id} is available.",
    )


def _same_path(left: str, right: str) -> bool:
    return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
