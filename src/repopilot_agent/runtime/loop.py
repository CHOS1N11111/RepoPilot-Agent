"""Reusable action-observation loop with policy and recovery checkpoints."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from ..models import RepoFile
from ..repository_map import RepositoryMap
from .approval import (
    DEFAULT_APPROVAL_TTL_SECONDS,
    MAX_APPROVAL_TTL_SECONDS,
    RuntimeApprovalGrant,
    RuntimeApprovalRequest,
    approval_payload_hash,
    normalize_command_allowlist,
    normalize_file_scope,
    utc_timestamp,
)
from .models import (
    SIDE_EFFECT_ACTIONS,
    STOPPING_OBSERVATION_STATUSES,
    RuntimeAction,
    RuntimeEvent,
    RuntimeObservation,
    RuntimePolicy,
    RuntimeRunResult,
)
from .store import InMemoryRuntimeStore, RuntimeEventStore
from .state import AgentWorkingState, latest_agent_working_state
from .tools import (
    RuntimeSideEffectPreview,
    RuntimeToolContext,
    execute_runtime_tool,
    preview_runtime_side_effect,
)


class AgentRuntime:
    def __init__(
        self,
        repo_path: str | Path,
        task: str,
        *,
        run_id: str | None = None,
        policy: RuntimePolicy | None = None,
        store: RuntimeEventStore | None = None,
        files: list[RepoFile] | None = None,
        repository_map: RepositoryMap | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.run_id = run_id or uuid4().hex
        self.task = task
        self.policy = policy or RuntimePolicy.read_only()
        self.store = store or InMemoryRuntimeStore()
        self.context = RuntimeToolContext(
            repo_path,
            task,
            self.policy,
            files=files,
            repository_map=repository_map,
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._started = any(event.event_type == "run_started" for event in self.events)
        self._stopped = False

    @property
    def events(self):
        return self.store.list_events(self.run_id)

    @property
    def selected_paths(self) -> list[str]:
        return list(self.context.selected_paths)

    @property
    def working_state(self) -> AgentWorkingState | None:
        return latest_agent_working_state(self.events, default_objective=self.task)

    @property
    def proposed_edits(self) -> list[dict]:
        return self.context.virtual_patches.metadata()

    @property
    def proposed_diff(self) -> str:
        return self.context.virtual_patches.current_diff()

    @property
    def pending_approval(self) -> dict:
        request_event = self._latest_approval_request_event()
        if request_event is None:
            return {}
        request = _approval_request_from_event(request_event)
        if request is None:
            return {}
        for event in self.events:
            if event.sequence <= request_event.sequence:
                continue
            if event.event_type not in {"approval_granted", "approval_rejected"}:
                continue
            if str(event.payload.get("checkpoint") or "") == request.checkpoint:
                return {}
        return request.to_dict()

    def grant_approval(
        self,
        checkpoint: str,
        *,
        payload_hash: str,
        file_scope: list[str] | tuple[str, ...],
        command_allowlist: list[str] | tuple[str, ...],
        expires_in_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
    ) -> RuntimeApprovalGrant:
        """Persist a user grant after verifying the exact pending request echo."""
        self.start()
        request_event = self._approval_request_event(checkpoint)
        request = _approval_request_from_event(request_event) if request_event else None
        if request is None:
            return self._reject_grant(checkpoint, "Approval checkpoint does not exist.")
        latest = self._latest_approval_request_event(request.action_id)
        if latest is None or latest.event_id != request_event.event_id:
            return self._reject_grant(checkpoint, "Approval checkpoint is stale.", request)
        if not isinstance(payload_hash, str):
            return self._reject_grant(
                checkpoint,
                "Approval payload hash must be a string.",
                request,
            )
        try:
            normalized_files = normalize_file_scope(file_scope)
            normalized_commands = normalize_command_allowlist(command_allowlist)
        except ValueError as exc:
            return self._reject_grant(checkpoint, str(exc), request)
        existing = self._grant_for_checkpoint(checkpoint)
        if existing is not None:
            if existing.is_expired(self._now()):
                return self._reject_grant(
                    checkpoint,
                    "The previous grant expired; execute the action again for a fresh checkpoint.",
                    request,
                )
            if (
                existing.payload_hash == payload_hash.lower()
                and existing.file_scope == normalized_files
                and existing.command_allowlist == normalized_commands
            ):
                return existing
            return self._reject_grant(checkpoint, "Approval checkpoint was already granted.", request)
        if self._approval_rejected(checkpoint):
            return self._reject_grant(checkpoint, "Approval checkpoint was already rejected.", request)
        if payload_hash.lower() != request.payload_hash:
            return self._reject_grant(checkpoint, "Approval payload hash does not match the request.", request)
        if normalized_files != request.file_scope:
            return self._reject_grant(checkpoint, "Approval file scope differs from the exact request.", request)
        if normalized_commands != request.command_allowlist:
            return self._reject_grant(
                checkpoint,
                "Approval command allowlist differs from the exact request.",
                request,
            )
        if (
            not isinstance(expires_in_seconds, int)
            or isinstance(expires_in_seconds, bool)
            or not 1 <= expires_in_seconds <= MAX_APPROVAL_TTL_SECONDS
        ):
            return self._reject_grant(
                checkpoint,
                f"Approval expiration must be from 1 to {MAX_APPROVAL_TTL_SECONDS} seconds.",
                request,
            )
        granted_at = self._now()
        grant = RuntimeApprovalGrant(
            grant_id=uuid4().hex,
            checkpoint=request.checkpoint,
            run_id=self.run_id,
            action_id=request.action_id,
            action_kind=request.action_kind,
            payload_hash=request.payload_hash,
            file_scope=request.file_scope,
            command_allowlist=request.command_allowlist,
            granted_at=utc_timestamp(granted_at),
            expires_at=utc_timestamp(granted_at + timedelta(seconds=expires_in_seconds)),
        )
        action = RuntimeAction.from_dict(request.action)
        self.store.append_event(
            self.run_id,
            "approval_granted",
            action=action,
            payload={
                "checkpoint": request.checkpoint,
                "approval_grant": grant.to_dict(),
            },
        )
        return grant

    def reject_approval(self, checkpoint: str, reason: str = "Rejected by the user.") -> None:
        self.start()
        request_event = self._approval_request_event(checkpoint)
        request = _approval_request_from_event(request_event) if request_event else None
        if request is None:
            raise ValueError("Approval checkpoint does not exist.")
        latest = self._latest_approval_request_event(request.action_id)
        if latest is None or latest.event_id != request_event.event_id:
            raise ValueError("Approval checkpoint is stale.")
        if self._grant_for_checkpoint(checkpoint) is not None:
            raise ValueError("Approval checkpoint was already granted.")
        if self._approval_rejected(checkpoint):
            return
        action = RuntimeAction.from_dict(request.action)
        self.store.append_event(
            self.run_id,
            "approval_rejected",
            action=action,
            payload={
                "checkpoint": request.checkpoint,
                "reason": reason.strip() or "Rejected by the user.",
            },
        )

    def record_working_state(self, state: AgentWorkingState) -> RuntimeEvent:
        self.start()
        return self.store.append_event(
            self.run_id,
            "working_state_updated",
            payload={"working_state": state.to_dict()},
        )

    def record_decision(
        self,
        action: RuntimeAction,
        decision: dict,
    ) -> RuntimeEvent:
        self.start()
        return self.store.append_event(
            self.run_id,
            "decision_recorded",
            action=action,
            payload={"decision": dict(decision), "action": action.to_dict()},
        )

    def block_finish(
        self,
        action: RuntimeAction,
        blockers: list[str],
    ) -> RuntimeObservation:
        self.start()
        if action.kind != "finish":
            raise ValueError("Only finish actions may use the completion gate.")
        normalized_blockers = [str(item) for item in blockers if str(item).strip()]
        requirement = (
            "plan, acceptance evidence, and proposed-edit review"
            if any(item.startswith("proposal:") for item in normalized_blockers)
            else "plan and acceptance evidence"
        )
        summary = (
            f"Finish blocked until {requirement} are complete: "
            f"{', '.join(normalized_blockers)}."
        )
        observation = RuntimeObservation(
            action_id=action.action_id,
            action_kind=action.kind,
            status="acceptance_incomplete",
            summary=summary,
            data={"blockers": normalized_blockers},
        )
        self.store.append_event(
            self.run_id,
            "finish_blocked",
            action=action,
            payload={
                "action": action.to_dict(),
                "observation": observation.to_dict(),
            },
        )
        return observation

    def start(self) -> None:
        if self._started:
            return
        self.store.append_event(
            self.run_id,
            "run_started",
            payload={"task": self.task, "repo_path": str(self.context.repo_path)},
        )
        self._started = True

    def execute(self, action: RuntimeAction) -> RuntimeObservation:
        self.start()
        if self._stopped:
            return RuntimeObservation(
                action_id=action.action_id,
                action_kind=action.kind,
                status="failed",
                summary="Runtime has already stopped.",
                error="No actions can execute after run_stopped.",
            )

        scope_decision, scope_reason = self.policy.evaluate(
            action,
            approval_granted=action.kind in SIDE_EFFECT_ACTIONS,
        )
        if scope_decision == "deny":
            return self._deny_action(action, scope_reason)

        if action.kind in SIDE_EFFECT_ACTIONS:
            existing = self.store.lookup(self.run_id, action)
            if existing.status == "completed" and existing.observation:
                observation = existing.observation.as_replayed()
                self.store.append_event(
                    self.run_id,
                    "action_replayed",
                    action=action,
                    payload={"observation": observation.to_dict()},
                )
                return observation
            if existing.status == "conflict":
                observation = RuntimeObservation(
                    action_id=action.action_id,
                    action_kind=action.kind,
                    status="failed",
                    summary="Idempotency key conflicts with a different action.",
                    error="Choose a new idempotency key for the changed action payload.",
                )
                self.store.append_event(
                    self.run_id,
                    "action_conflict",
                    action=action,
                    payload={"observation": observation.to_dict()},
                )
                return observation

        approval_grant: RuntimeApprovalGrant | None = None
        if action.kind in SIDE_EFFECT_ACTIONS:
            try:
                preview = preview_runtime_side_effect(action, self.context)
            except Exception as exc:
                return self._failed_preview(action, str(exc))
            if preview.status != "ready":
                observation = RuntimeObservation(
                    action_id=action.action_id,
                    action_kind=action.kind,
                    status=preview.status,
                    summary=preview.summary,
                    data={
                        **preview.data,
                        "diff": preview.diff,
                        "diff_truncated": preview.diff_truncated,
                    },
                )
                self.store.append_event(
                    self.run_id,
                    "action_conflict" if preview.status == "conflict" else "action_failed",
                    action=action,
                    payload={"action": action.to_dict(), "observation": observation.to_dict()},
                )
                return observation
            payload_hash = approval_payload_hash(
                action,
                file_scope=preview.file_scope,
                command_allowlist=preview.command_allowlist,
                baseline_hashes=preview.baseline_hashes,
                baseline_exists=preview.baseline_exists,
                diff=preview.diff,
            )
            approval_grant, invalid_reason = self._valid_approval_grant(
                action,
                payload_hash,
                preview.file_scope,
                preview.command_allowlist,
            )
            if approval_grant is None:
                request = self._build_approval_request(action, preview, payload_hash)
                if invalid_reason:
                    self.store.append_event(
                        self.run_id,
                        "approval_invalidated",
                        action=action,
                        payload={
                            "checkpoint": request.checkpoint,
                            "payload_hash": payload_hash,
                            "reason": invalid_reason,
                        },
                    )
                reason = (
                    invalid_reason
                    or f"Action {action.action_id} requires explicit approval before execution."
                )
                observation = RuntimeObservation(
                    action_id=action.action_id,
                    action_kind=action.kind,
                    status="approval_required",
                    summary=reason,
                    data={
                        "action": action.to_dict(),
                        "approval_request": request.to_dict(),
                        "diff": request.diff,
                        "diff_truncated": request.diff_truncated,
                    },
                )
                self.store.append_event(
                    self.run_id,
                    "approval_required",
                    action=action,
                    payload={
                        "action": action.to_dict(),
                        "approval_request": request.to_dict(),
                        "observation": observation.to_dict(),
                    },
                )
                return observation

        decision, reason = self.policy.evaluate(
            action,
            approval_granted=approval_grant is not None,
        )
        if decision == "deny":
            return self._deny_action(action, reason)
        if decision == "approval":
            return self._failed_preview(action, "Approval resolution failed.")
        authorization_payload = {"action": action.to_dict(), "reason": reason}
        if approval_grant is not None:
            authorization_payload["approval_grant"] = approval_grant.to_dict()
        self.store.append_event(
            self.run_id,
            "action_authorized",
            action=action,
            payload=authorization_payload,
        )
        if approval_grant is not None:
            self.store.append_event(
                self.run_id,
                "approval_consumed",
                action=action,
                payload={
                    "checkpoint": approval_grant.checkpoint,
                    "grant_id": approval_grant.grant_id,
                    "payload_hash": approval_grant.payload_hash,
                },
            )
        if action.kind == "ask_user":
            question = str(action.arguments.get("question") or "").strip()
            if not question:
                question = "The agent needs additional user input before continuing."
            observation = RuntimeObservation(
                action_id=action.action_id,
                action_kind=action.kind,
                status="input_required",
                summary=question,
                data={"question": question},
            )
            self.store.append_event(
                self.run_id,
                "input_required",
                action=action,
                payload={"action": action.to_dict(), "observation": observation.to_dict()},
            )
            return observation

        reservation = self.store.reserve(self.run_id, action)
        if reservation.status == "completed" and reservation.observation:
            observation = reservation.observation.as_replayed()
            self.store.append_event(
                self.run_id,
                "action_replayed",
                action=action,
                payload={"observation": observation.to_dict()},
            )
            return observation
        if reservation.status == "in_progress":
            observation = RuntimeObservation(
                action_id=action.action_id,
                action_kind=action.kind,
                status="recovery_required",
                summary="The action was interrupted before its result was recorded.",
                error="Automatic replay is blocked to avoid duplicating a possible side effect.",
            )
            self.store.append_event(
                self.run_id,
                "action_recovery_required",
                action=action,
                payload={"observation": observation.to_dict()},
            )
            return observation
        if reservation.status == "conflict":
            observation = RuntimeObservation(
                action_id=action.action_id,
                action_kind=action.kind,
                status="failed",
                summary="Idempotency key conflicts with a different action.",
                error="Choose a new idempotency key for the changed action payload.",
            )
            self.store.append_event(
                self.run_id,
                "action_conflict",
                action=action,
                payload={"observation": observation.to_dict()},
            )
            return observation

        self.store.append_event(
            self.run_id,
            "action_started",
            action=action,
            payload={"action": action.to_dict()},
        )
        try:
            tool_result = execute_runtime_tool(action, self.context)
            observation = RuntimeObservation(
                action_id=action.action_id,
                action_kind=action.kind,
                status=tool_result.status,
                summary=tool_result.summary,
                data=tool_result.data,
            )
        except Exception as exc:
            observation = RuntimeObservation(
                action_id=action.action_id,
                action_kind=action.kind,
                status="failed",
                summary=f"Action {action.kind} failed.",
                error=str(exc),
            )
        observation = self.store.complete(self.run_id, action, observation)
        self.store.append_event(
            self.run_id,
            "action_completed"
            if observation.status in {"completed", "applied", "no_change"}
            else "action_conflict"
            if observation.status == "conflict"
            else "action_failed",
            action=action,
            payload={"observation": observation.to_dict()},
        )
        return observation

    def stop(self, reason: str, summary: str = "") -> None:
        if self._stopped:
            return
        self.start()
        self.store.append_event(
            self.run_id,
            "run_stopped",
            payload={
                "reason": reason,
                "summary": summary,
                "selected_paths": self.selected_paths,
            },
        )
        self._stopped = True

    def run(
        self,
        choose_action: Callable[[list[RuntimeObservation]], RuntimeAction],
        *,
        max_steps: int,
    ) -> RuntimeRunResult:
        if max_steps <= 0:
            raise ValueError("Runtime max_steps must be greater than 0.")
        observations: list[RuntimeObservation] = []
        stop_reason = "step_limit"
        summary = ""
        for _ in range(max_steps):
            action = choose_action(list(observations))
            observation = self.execute(action)
            observations.append(observation)
            if observation.status in STOPPING_OBSERVATION_STATUSES:
                stop_reason = observation.status
                summary = observation.summary
                break
            if action.kind == "finish":
                stop_reason = "finished"
                summary = str(observation.data.get("summary") or observation.summary)
                break
        self.stop(stop_reason, summary)
        status = "completed" if stop_reason == "finished" else "waiting" if stop_reason in {
            "approval_required",
            "input_required",
            "recovery_required",
        } else "stopped"
        return RuntimeRunResult(
            run_id=self.run_id,
            status=status,
            stop_reason=stop_reason,
            observations=observations,
            events=self.events,
            selected_paths=self.selected_paths,
            summary=summary,
            pending_approval=self.pending_approval,
        )

    def _build_approval_request(
        self,
        action: RuntimeAction,
        preview: RuntimeSideEffectPreview,
        payload_hash: str,
    ) -> RuntimeApprovalRequest:
        latest_event = self._latest_approval_request_event(action.action_id)
        latest = _approval_request_from_event(latest_event) if latest_event else None
        if (
            latest is not None
            and latest.payload_hash == payload_hash
            and not self._approval_rejected(latest.checkpoint)
            and self._grant_for_checkpoint(latest.checkpoint) is None
        ):
            return latest
        return RuntimeApprovalRequest(
            checkpoint=uuid4().hex,
            run_id=self.run_id,
            action_id=action.action_id,
            action_kind=action.kind,
            payload_hash=payload_hash,
            action=action.to_dict(),
            file_scope=preview.file_scope,
            command_allowlist=preview.command_allowlist,
            baseline_hashes=preview.baseline_hashes,
            baseline_exists=preview.baseline_exists,
            diff=preview.diff,
            diff_truncated=preview.diff_truncated,
            requested_at=utc_timestamp(self._now()),
        )

    def _valid_approval_grant(
        self,
        action: RuntimeAction,
        payload_hash: str,
        file_scope: tuple[str, ...],
        command_allowlist: tuple[str, ...],
    ) -> tuple[RuntimeApprovalGrant | None, str]:
        grants: list[RuntimeApprovalGrant] = []
        for event in self.events:
            if event.event_type != "approval_granted":
                continue
            raw = event.payload.get("approval_grant")
            try:
                grant = RuntimeApprovalGrant.from_dict(raw) if isinstance(raw, dict) else None
            except ValueError:
                grant = None
            if grant and grant.action_id == action.action_id:
                grants.append(grant)
        if not grants:
            return None, ""
        grant = grants[-1]
        latest_request_event = self._latest_approval_request_event(action.action_id)
        latest_request = _approval_request_from_event(latest_request_event) if latest_request_event else None
        if latest_request is None or latest_request.checkpoint != grant.checkpoint:
            return None, "The existing approval belongs to a stale checkpoint."
        if grant.run_id != self.run_id or grant.action_kind != action.kind:
            return None, "The existing approval does not match this runtime action."
        if grant.is_expired(self._now()):
            return None, "The existing approval grant expired."
        if grant.payload_hash != payload_hash:
            return None, "The action payload or repository baseline changed after approval."
        if grant.file_scope != file_scope:
            return None, "The action file scope changed after approval."
        if grant.command_allowlist != command_allowlist:
            return None, "The action command scope changed after approval."
        if self._approval_rejected(grant.checkpoint):
            return None, "The approval checkpoint was rejected."
        return grant, ""

    def _latest_approval_request_event(self, action_id: str | None = None):
        for event in reversed(self.events):
            if event.event_type != "approval_required":
                continue
            request = _approval_request_from_event(event)
            if request is None:
                continue
            if action_id is None or request.action_id == action_id:
                return event
        return None

    def _approval_request_event(self, checkpoint: str):
        for event in reversed(self.events):
            request = _approval_request_from_event(event)
            if request and request.checkpoint == checkpoint:
                return event
        return None

    def _grant_for_checkpoint(self, checkpoint: str) -> RuntimeApprovalGrant | None:
        for event in reversed(self.events):
            if event.event_type != "approval_granted":
                continue
            raw = event.payload.get("approval_grant")
            try:
                grant = RuntimeApprovalGrant.from_dict(raw) if isinstance(raw, dict) else None
            except ValueError:
                continue
            if grant and grant.checkpoint == checkpoint:
                return grant
        return None

    def _approval_rejected(self, checkpoint: str) -> bool:
        return any(
            event.event_type == "approval_rejected"
            and str(event.payload.get("checkpoint") or "") == checkpoint
            for event in self.events
        )

    def _reject_grant(
        self,
        checkpoint: str,
        reason: str,
        request: RuntimeApprovalRequest | None = None,
    ):
        action = RuntimeAction.from_dict(request.action) if request else None
        self.store.append_event(
            self.run_id,
            "approval_grant_rejected",
            action=action,
            payload={"checkpoint": checkpoint, "reason": reason},
        )
        raise ValueError(reason)

    def _deny_action(self, action: RuntimeAction, reason: str) -> RuntimeObservation:
        observation = RuntimeObservation(
            action_id=action.action_id,
            action_kind=action.kind,
            status="policy_denied",
            summary=reason,
            error=reason,
        )
        self.store.append_event(
            self.run_id,
            "action_denied",
            action=action,
            payload={"action": action.to_dict(), "observation": observation.to_dict()},
        )
        return observation

    def _failed_preview(self, action: RuntimeAction, reason: str) -> RuntimeObservation:
        observation = RuntimeObservation(
            action_id=action.action_id,
            action_kind=action.kind,
            status="failed",
            summary=f"Could not prepare an approval preview for {action.kind}.",
            error=reason,
        )
        self.store.append_event(
            self.run_id,
            "action_failed",
            action=action,
            payload={"action": action.to_dict(), "observation": observation.to_dict()},
        )
        return observation

    def _now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            raise ValueError("Runtime approval clock must return a timezone-aware timestamp.")
        return current.astimezone(timezone.utc)


def _approval_request_from_event(event: RuntimeEvent | None) -> RuntimeApprovalRequest | None:
    if event is None or event.event_type != "approval_required":
        return None
    raw = event.payload.get("approval_request")
    try:
        return RuntimeApprovalRequest.from_dict(raw) if isinstance(raw, dict) else None
    except ValueError:
        return None
