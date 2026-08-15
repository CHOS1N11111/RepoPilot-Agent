"""Exact approval continuation for one managed-worktree Agent write."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .runtime import (
    AgentRuntime,
    RuntimeAction,
    RuntimeEventStore,
    RuntimeObservation,
    RuntimePolicy,
    advance_agent_working_state,
    create_agent_working_state,
    stop_agent_working_state,
)


class AgentWriteError(RuntimeError):
    """Raised when an exact pending Agent write cannot continue safely."""


@dataclass(frozen=True)
class AgentWriteResult:
    status: str
    run_id: str
    action_id: str
    sandbox: dict[str, Any]
    write_observation: RuntimeObservation
    diff_observation: RuntimeObservation | None
    working_state: dict[str, Any]
    rollback_snapshot_paths: list[str]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["write_observation"] = self.write_observation.to_dict()
        data["diff_observation"] = (
            self.diff_observation.to_dict() if self.diff_observation else None
        )
        data["rollback_available"] = bool(self.rollback_snapshot_paths)
        data["resulting_diff"] = (
            str(self.diff_observation.data.get("diff") or "")
            if self.diff_observation
            else str(
                self.write_observation.data.get("resulting_diff")
                or self.write_observation.data.get("diff")
                or ""
            )
        )
        return data


def execute_pending_agent_write(
    source_repo: str | Path,
    sandbox_path: str | Path,
    task: str,
    run_id: str,
    store: RuntimeEventStore,
    *,
    checkpoint: str,
    payload_hash: str,
    file_scope: list[str] | tuple[str, ...],
    command_allowlist: list[str] | tuple[str, ...],
    worktree_root: str | Path | None = None,
) -> AgentWriteResult:
    """Grant and execute exactly one persisted pending write, then inspect its diff."""

    runtime = _managed_runtime(
        source_repo,
        sandbox_path,
        task,
        run_id,
        store,
        file_scope,
        worktree_root,
    )
    request = runtime.pending_approval
    if not request or request.get("checkpoint") != checkpoint:
        raise AgentWriteError("The pending runtime approval checkpoint is missing or stale.")
    action = RuntimeAction.from_dict(dict(request.get("action") or {}))
    if action.kind not in {"apply_patch", "edit_file"}:
        raise AgentWriteError("Only pending apply_patch or edit_file actions may use the write continuation.")

    runtime.grant_approval(
        checkpoint,
        payload_hash=payload_hash,
        file_scope=file_scope,
        command_allowlist=command_allowlist,
    )
    write_observation = runtime.execute(action)
    state = runtime.working_state or create_agent_working_state(task)

    if write_observation.status == "approval_required":
        state = advance_agent_working_state(
            state,
            action,
            write_observation,
            selected_paths=_merge_paths(state.selected_paths, list(file_scope)),
            expected_evidence="A fresh exact approval after the managed-worktree baseline changed.",
        )
        state = stop_agent_working_state(
            state,
            "approval_required",
            selected_paths=state.selected_paths,
        )
        runtime.record_working_state(state)
        runtime.stop("approval_required", write_observation.summary)
        return AgentWriteResult(
            status="approval_required",
            run_id=run_id,
            action_id=action.action_id,
            sandbox=runtime.sandbox.to_dict() if runtime.sandbox else {},
            write_observation=write_observation,
            diff_observation=None,
            working_state=state.to_dict(),
            rollback_snapshot_paths=[],
        )

    if write_observation.status not in {"completed", "applied", "no_change"}:
        runtime.stop(write_observation.status, write_observation.summary)
        raise AgentWriteError(
            write_observation.error
            or write_observation.summary
            or "The approved managed-worktree write failed."
        )

    selected_paths = _merge_paths(state.selected_paths, list(file_scope))
    state = advance_agent_working_state(
        state,
        action,
        write_observation,
        selected_paths=selected_paths,
        expected_evidence="Before/after hashes and the resulting managed-worktree diff.",
    )
    runtime.record_working_state(state)

    diff_action = RuntimeAction(
        kind="inspect_diff",
        arguments={"staged": False},
        rationale="Observe the complete managed-worktree diff after the approved write.",
        action_id=f"{action.action_id}-resulting-diff",
        idempotency_key=f"{action.idempotency_key or action.action_id}-resulting-diff",
    )
    diff_observation = runtime.execute(diff_action)
    state = advance_agent_working_state(
        state,
        diff_action,
        diff_observation,
        selected_paths=selected_paths,
        expected_evidence="The current Git diff produced by the approved write.",
    )
    runtime.record_working_state(state)
    state = stop_agent_working_state(
        state,
        "write_complete",
        selected_paths=selected_paths,
    )
    runtime.record_working_state(state)
    runtime.stop("write_complete", "Approved managed-worktree write completed and its diff was observed.")
    snapshots = runtime.rollback_snapshots(action.action_id)
    return AgentWriteResult(
        status="completed",
        run_id=run_id,
        action_id=action.action_id,
        sandbox=runtime.sandbox.to_dict() if runtime.sandbox else {},
        write_observation=write_observation,
        diff_observation=diff_observation,
        working_state=state.to_dict(),
        rollback_snapshot_paths=[snapshot.path for snapshot in snapshots],
    )


def reject_pending_agent_write(
    source_repo: str | Path,
    sandbox_path: str | Path,
    task: str,
    run_id: str,
    store: RuntimeEventStore,
    *,
    checkpoint: str,
    file_scope: list[str] | tuple[str, ...],
    reason: str = "Rejected by the user.",
    worktree_root: str | Path | None = None,
) -> dict[str, Any]:
    runtime = _managed_runtime(
        source_repo,
        sandbox_path,
        task,
        run_id,
        store,
        file_scope,
        worktree_root,
    )
    runtime.reject_approval(checkpoint, reason)
    state = runtime.working_state or create_agent_working_state(task)
    state = stop_agent_working_state(state, "approval_rejected")
    runtime.record_working_state(state)
    runtime.stop("approval_rejected", reason)
    return {
        "status": "rejected",
        "run_id": run_id,
        "checkpoint": checkpoint,
        "working_state": state.to_dict(),
    }


def _managed_runtime(
    source_repo: str | Path,
    sandbox_path: str | Path,
    task: str,
    run_id: str,
    store: RuntimeEventStore,
    file_scope: list[str] | tuple[str, ...],
    worktree_root: str | Path | None,
) -> AgentRuntime:
    policy = RuntimePolicy.managed_worktree(
        allowed_edit_paths=list(file_scope),
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
        raise AgentWriteError(
            "The managed worktree does not belong to the task run's source repository."
        )
    return runtime


def _same_path(first: str | Path, second: str | Path) -> bool:
    return os.path.normcase(str(Path(first).expanduser().resolve())) == os.path.normcase(
        str(Path(second).expanduser().resolve())
    )


def _merge_paths(existing: list[str], additions: list[str]) -> list[str]:
    result = list(existing)
    for path in additions:
        normalized = str(path).replace("\\", "/")
        if normalized and normalized not in result:
            result.append(normalized)
    return result
