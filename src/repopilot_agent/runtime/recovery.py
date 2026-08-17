"""Deterministic exact-action recovery from durable Runtime events."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ..llm.base import LLMError
from ..llm.schema import parse_agent_decision_json
from .models import (
    READ_ONLY_ACTIONS,
    SIDE_EFFECT_ACTIONS,
    SUPPORTED_ACTIONS,
    RuntimeAction,
    RuntimeEvent,
    RuntimeObservation,
)
from .state import (
    AgentWorkingState,
    advance_agent_working_state,
    agent_working_state_from_record,
    create_agent_working_state,
)


RUNTIME_RECOVERY_VERSION = 1
RECOVERY_NEXT_DECISION = "next_decision"
RECOVERY_EXECUTE_PENDING = "execute_pending"
RECOVERY_RETRY_READ_ONLY = "retry_read_only"
RECOVERY_AWAIT_APPROVAL = "await_approval"
RECOVERY_AWAIT_INPUT = "await_input"
RECOVERY_CONFIRM_SIDE_EFFECT = "confirm_side_effect"
RECOVERY_STOPPED = "stopped"

_TERMINAL_EVENT_TYPES = {
    "action_completed",
    "action_failed",
    "action_conflict",
    "action_denied",
    "action_replayed",
    "action_recovered",
    "action_recovery_confirmed",
    "finish_blocked",
}
_WAITING_EVENT_TYPES = {"approval_required", "input_required"}


@dataclass(frozen=True)
class RuntimeActionRecovery:
    """Classification of the one exact action that controls resume behavior."""

    classification: str
    action: RuntimeAction
    first_sequence: int
    last_sequence: int
    started_sequence: int | None = None
    observation: RuntimeObservation | None = None
    confirmation_token: str = ""
    summary: str = ""

    @property
    def requires_confirmation(self) -> bool:
        return self.classification == "ambiguous_side_effect"

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "action_id": self.action.action_id,
            "action_kind": self.action.kind,
            "idempotency_key": self.action.effective_idempotency_key,
            "payload_hash": runtime_action_payload_hash(self.action),
            "arguments": _public_action_arguments(self.action),
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "started_sequence": self.started_sequence,
            "observation_status": self.observation.status if self.observation else "",
            "confirmation_token": self.confirmation_token,
            "requires_confirmation": self.requires_confirmation,
            "summary": _redact_public_text(self.summary),
        }


@dataclass(frozen=True)
class RuntimeRecoveryPlan:
    """A replay-safe plan reconstructed only from persisted Runtime events."""

    run_id: str
    status: str
    next_step: str
    summary: str
    latest_snapshot_sequence: int
    recovered_through_sequence: int
    working_state: AgentWorkingState
    pending_action: RuntimeActionRecovery | None = None
    replayed_observations: tuple[RuntimeObservation, ...] = ()
    context_actions: tuple[RuntimeAction, ...] = field(default=(), repr=False)

    @property
    def requires_confirmation(self) -> bool:
        return bool(self.pending_action and self.pending_action.requires_confirmation)

    @property
    def can_continue(self) -> bool:
        return self.next_step in {
            RECOVERY_NEXT_DECISION,
            RECOVERY_EXECUTE_PENDING,
            RECOVERY_RETRY_READ_ONLY,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": RUNTIME_RECOVERY_VERSION,
            "run_id": self.run_id,
            "status": self.status,
            "next_step": self.next_step,
            "summary": _redact_public_text(self.summary),
            "latest_snapshot_sequence": self.latest_snapshot_sequence,
            "recovered_through_sequence": self.recovered_through_sequence,
            "working_state_iteration": self.working_state.iteration,
            "working_state_status": self.working_state.status,
            "pending_action": (
                self.pending_action.to_dict() if self.pending_action else None
            ),
            "replayed_observations": [
                {
                    "action_id": item.action_id,
                    "action_kind": item.action_kind,
                    "status": item.status,
                    "summary": _redact_public_text(item.summary),
                }
                for item in self.replayed_observations
            ],
            "can_continue": self.can_continue,
            "requires_confirmation": self.requires_confirmation,
        }


@dataclass
class _ActionTrack:
    action: RuntimeAction
    first_sequence: int
    last_sequence: int
    decision: dict[str, Any] | None = None
    decision_sequence: int | None = None
    started_sequence: int | None = None
    observation: RuntimeObservation | None = None
    observation_sequence: int | None = None
    waiting_observation: RuntimeObservation | None = None
    waiting_sequence: int | None = None
    approval_granted_sequence: int | None = None
    approval_rejected_sequence: int | None = None


def analyze_runtime_recovery(
    events: list[RuntimeEvent],
    *,
    objective: str = "",
) -> RuntimeRecoveryPlan:
    """Reconstruct Working State and classify the exact unresolved action."""

    ordered = sorted(
        (event for event in events if event.sequence > 0),
        key=lambda event: event.sequence,
    )
    run_id = ordered[-1].run_id if ordered else ""
    snapshot_sequence, state = _latest_valid_snapshot(ordered, objective)
    tracks = _build_action_tracks(ordered)

    replay_items: list[tuple[int, _ActionTrack, RuntimeObservation]] = []
    for track in tracks.values():
        if (
            track.waiting_observation is not None
            and (track.waiting_sequence or 0) > snapshot_sequence
        ):
            replay_items.append(
                (track.waiting_sequence or 0, track, track.waiting_observation)
            )
        if (
            track.observation is not None
            and (track.observation_sequence or 0) > snapshot_sequence
        ):
            replay_items.append(
                (track.observation_sequence or 0, track, track.observation)
            )
    replay_items.sort(key=lambda item: item[0])
    applied_decisions: set[str] = set()
    for _, track, observation in replay_items:
        apply_decision = track.action.action_id not in applied_decisions
        state = _fold_observation(
            state,
            track,
            observation,
            snapshot_sequence,
            apply_decision=apply_decision,
        )
        applied_decisions.add(track.action.action_id)

    pending = _latest_unresolved_action(tracks, run_id)
    next_step, status, summary = _resume_disposition(pending, state)
    replayed_observations = _context_observations(state, tracks)
    context_actions = _virtual_context_actions(state, tracks)
    recovered_through = max(
        [snapshot_sequence, *(sequence for sequence, _, _ in replay_items)],
        default=snapshot_sequence,
    )
    return RuntimeRecoveryPlan(
        run_id=run_id,
        status=status,
        next_step=next_step,
        summary=summary,
        latest_snapshot_sequence=snapshot_sequence,
        recovered_through_sequence=recovered_through,
        working_state=state,
        pending_action=pending,
        replayed_observations=replayed_observations,
        context_actions=context_actions,
    )


def runtime_action_payload_hash(action: RuntimeAction) -> str:
    payload = json.dumps(
        {
            "kind": action.kind,
            "arguments": action.arguments,
            "idempotency_key": action.effective_idempotency_key,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def runtime_recovery_confirmation_token(
    run_id: str,
    action: RuntimeAction,
    started_sequence: int,
) -> str:
    payload = "\0".join(
        [
            str(RUNTIME_RECOVERY_VERSION),
            run_id,
            action.action_id,
            runtime_action_payload_hash(action),
            str(started_sequence),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_action_tracks(events: list[RuntimeEvent]) -> dict[str, _ActionTrack]:
    tracks: dict[str, _ActionTrack] = {}
    for event in events:
        action = _action_from_event(event)
        action_id = action.action_id if action else str(event.action_id or "")
        if not action_id:
            continue
        track = tracks.get(action_id)
        if track is None:
            if action is None:
                continue
            track = _ActionTrack(action, event.sequence, event.sequence)
            tracks[action_id] = track
        elif action is not None:
            if runtime_action_payload_hash(track.action) != runtime_action_payload_hash(action):
                track = _ActionTrack(action, event.sequence, event.sequence)
                tracks[action_id] = track
        track.last_sequence = max(track.last_sequence, event.sequence)
        if event.event_type == "decision_recorded":
            raw_decision = event.payload.get("decision")
            track.decision = dict(raw_decision) if isinstance(raw_decision, dict) else None
            track.decision_sequence = event.sequence
        if event.event_type in {"action_started", "action_recovery_started"}:
            track.started_sequence = event.sequence
        observation = _observation_from_event(event)
        if event.event_type in _TERMINAL_EVENT_TYPES and observation is not None:
            track.observation = observation
            track.observation_sequence = event.sequence
        elif event.event_type in _WAITING_EVENT_TYPES and observation is not None:
            track.waiting_observation = observation
            track.waiting_sequence = event.sequence
        if event.event_type == "approval_granted":
            track.approval_granted_sequence = event.sequence
        elif event.event_type == "approval_rejected":
            track.approval_rejected_sequence = event.sequence
    return tracks


def _latest_unresolved_action(
    tracks: dict[str, _ActionTrack],
    run_id: str,
) -> RuntimeActionRecovery | None:
    unresolved: list[RuntimeActionRecovery] = []
    for track in tracks.values():
        if track.observation_sequence is not None:
            continue
        if track.approval_rejected_sequence is not None:
            continue
        if (
            track.waiting_observation is not None
            and track.waiting_observation.status == "input_required"
        ):
            unresolved.append(
                _classified(track, "input_required", "Waiting for durable user input.")
            )
            continue
        if (
            track.waiting_observation is not None
            and track.waiting_observation.status == "approval_required"
            and track.approval_granted_sequence is None
        ):
            unresolved.append(
                _classified(
                    track,
                    "awaiting_approval",
                    "The original exact approval checkpoint is still pending.",
                )
            )
            continue
        if track.started_sequence is not None:
            if track.action.kind in SIDE_EFFECT_ACTIONS:
                token = runtime_recovery_confirmation_token(
                    run_id,
                    track.action,
                    track.started_sequence,
                )
                unresolved.append(
                    _classified(
                        track,
                        "ambiguous_side_effect",
                        "The side-effect action started but has no durable terminal observation.",
                        confirmation_token=token,
                    )
                )
            elif track.action.kind in READ_ONLY_ACTIONS:
                unresolved.append(
                    _classified(
                        track,
                        "interrupted_read_only",
                        "The read-only action can be safely retried with the same reservation.",
                    )
                )
            continue
        unresolved.append(
            _classified(
                track,
                "not_started",
                "The exact persisted action was decided but never started.",
            )
        )
    if not unresolved:
        return None
    return max(unresolved, key=lambda item: item.last_sequence)


def _classified(
    track: _ActionTrack,
    classification: str,
    summary: str,
    *,
    confirmation_token: str = "",
) -> RuntimeActionRecovery:
    return RuntimeActionRecovery(
        classification=classification,
        action=track.action,
        first_sequence=track.first_sequence,
        last_sequence=track.last_sequence,
        started_sequence=track.started_sequence,
        observation=track.waiting_observation,
        confirmation_token=confirmation_token,
        summary=summary,
    )


def _resume_disposition(
    pending: RuntimeActionRecovery | None,
    state: AgentWorkingState,
) -> tuple[str, str, str]:
    if pending is not None:
        if pending.classification == "awaiting_approval":
            return RECOVERY_AWAIT_APPROVAL, "waiting", pending.summary
        if pending.classification == "input_required":
            return RECOVERY_AWAIT_INPUT, "waiting", pending.summary
        if pending.classification == "interrupted_read_only":
            return RECOVERY_RETRY_READ_ONLY, "ready", pending.summary
        if pending.classification == "ambiguous_side_effect":
            return RECOVERY_CONFIRM_SIDE_EFFECT, "confirmation_required", pending.summary
        return RECOVERY_EXECUTE_PENDING, "ready", pending.summary
    if state.status == "completed" or state.stop_reason == "finished":
        return RECOVERY_STOPPED, "stopped", "The persisted Agent trajectory already finished."
    if state.stop_reason == "input_required":
        return RECOVERY_AWAIT_INPUT, "waiting", "The persisted Agent trajectory is waiting for user input."
    return (
        RECOVERY_NEXT_DECISION,
        "ready",
        "Durable state is reconstructed; continue from the next controller decision.",
    )


def _fold_observation(
    state: AgentWorkingState,
    track: _ActionTrack,
    observation: RuntimeObservation,
    snapshot_sequence: int,
    *,
    apply_decision: bool,
) -> AgentWorkingState:
    state_update = None
    expected_evidence = ""
    if (
        apply_decision
        and track.decision is not None
        and (track.decision_sequence or 0) > snapshot_sequence
    ):
        try:
            decision = parse_agent_decision_json(
                json.dumps(track.decision, ensure_ascii=False),
                allowed_actions=set(SUPPORTED_ACTIONS),
            )
            state_update = decision.state_update
            expected_evidence = decision.expected_evidence
        except LLMError:
            state_update = None
    selected_paths = _merge_selected_paths(state.selected_paths, track.action, observation)
    try:
        return advance_agent_working_state(
            state,
            track.action,
            observation,
            selected_paths=selected_paths,
            state_update=state_update,
            expected_evidence=expected_evidence,
        )
    except ValueError:
        return advance_agent_working_state(
            state,
            track.action,
            observation,
            selected_paths=selected_paths,
            expected_evidence=expected_evidence,
        )


def _context_observations(
    state: AgentWorkingState,
    tracks: dict[str, _ActionTrack],
) -> tuple[RuntimeObservation, ...]:
    observations: list[RuntimeObservation] = []
    seen: set[str] = set()
    for state_observation in state.recent_observations:
        track = tracks.get(state_observation.action_id)
        if (
            track is None
            or track.observation is None
            or track.action.action_id in seen
            or track.action.kind not in READ_ONLY_ACTIONS
            or track.observation.status in {"approval_required", "input_required"}
        ):
            continue
        observations.append(track.observation.as_replayed())
        seen.add(track.action.action_id)
    return tuple(observations)


def _virtual_context_actions(
    state: AgentWorkingState,
    tracks: dict[str, _ActionTrack],
) -> tuple[RuntimeAction, ...]:
    active_paths = {item.path for item in state.proposed_edits}
    if not active_paths:
        return ()
    ordered = sorted(
        (
            track
            for track in tracks.values()
            if track.action.kind == "propose_patch"
            and str(track.action.arguments.get("path") or "").replace("\\", "/")
            in active_paths
            and track.observation is not None
            and track.observation.status == "completed"
        ),
        key=lambda track: track.observation_sequence or 0,
    )
    return tuple(track.action for track in ordered)


def _latest_valid_snapshot(
    events: list[RuntimeEvent],
    objective: str,
) -> tuple[int, AgentWorkingState]:
    for event in reversed(events):
        if event.event_type != "working_state_updated":
            continue
        state = agent_working_state_from_record(
            event.payload.get("working_state"),
            default_objective=objective,
        )
        if state is not None:
            return event.sequence, state
    return 0, create_agent_working_state(objective)


def _action_from_event(event: RuntimeEvent) -> RuntimeAction | None:
    candidates = [event.payload.get("action")]
    request = event.payload.get("approval_request")
    if isinstance(request, dict):
        candidates.append(request.get("action"))
    decision = event.payload.get("decision")
    if isinstance(decision, dict):
        decision_action = decision.get("action")
        if isinstance(decision_action, dict):
            candidates.append(
                {
                    **decision_action,
                    "action_id": event.action_id,
                    "idempotency_key": event.idempotency_key,
                    "rationale": decision.get("rationale", ""),
                }
            )
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        try:
            return RuntimeAction.from_dict(raw)
        except (TypeError, ValueError):
            continue
    return None


def _observation_from_event(event: RuntimeEvent) -> RuntimeObservation | None:
    raw = event.payload.get("observation")
    if not isinstance(raw, dict):
        return None
    try:
        observation = RuntimeObservation.from_dict(raw)
    except (TypeError, ValueError):
        return None
    return observation if observation.action_id else None


def _merge_selected_paths(
    existing: list[str],
    action: RuntimeAction,
    observation: RuntimeObservation,
) -> list[str]:
    result = list(existing)
    candidates: list[object] = []
    if action.kind in {"read_file", "propose_patch", "edit_file", "apply_patch"}:
        candidates.append(action.arguments.get("path"))
    candidates.append(observation.data.get("path"))
    selected = action.arguments.get("selected_paths")
    if isinstance(selected, list):
        candidates.extend(selected)
    for raw in candidates:
        if not isinstance(raw, str):
            continue
        path = raw.strip().replace("\\", "/")
        if path and path not in result:
            result.append(path)
    return result


def _public_action_arguments(action: RuntimeAction) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    for name in ("path", "command", "query", "staged"):
        value = action.arguments.get(name)
        if isinstance(value, (str, bool, int, float)):
            arguments[name] = (
                _redact_public_text(value)
                if isinstance(value, str)
                else value
            )
    selected = action.arguments.get("selected_paths")
    if isinstance(selected, list):
        arguments["selected_paths"] = [
            _redact_public_text(item)
            for item in selected[:20]
            if isinstance(item, str)
        ]
    hunks = action.arguments.get("hunks")
    if isinstance(hunks, list):
        arguments["hunk_count"] = len(hunks)
    if "new_content" in action.arguments:
        arguments["new_content_chars"] = len(str(action.arguments.get("new_content") or ""))
    return arguments


def _redact_public_text(value: object) -> str:
    from ..agent_context import redact_context_secrets

    return redact_context_secrets(str(value or ""))[:1_000]
