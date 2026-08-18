"""Deterministic, secret-safe observability for Agent Runtime trajectories."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .redaction import redact_context_secrets
from .runtime.models import SIDE_EFFECT_ACTIONS
from .runtime.state import EVIDENCE_ACTION_KINDS, SUCCESSFUL_EVIDENCE_STATUSES


AGENT_TRAJECTORY_VERSION = 1
MAX_TRAJECTORY_FRAMES = 1_000
MAX_TRAJECTORY_SUMMARY_CHARS = 500
MAX_TRAJECTORY_GAPS = 20

_TERMINAL_ACTION_EVENTS = frozenset(
    {
        "action_completed",
        "action_recovered",
        "action_failed",
        "action_conflict",
    }
)
_RECOVERY_EVENTS = frozenset(
    {
        "action_recovery_started",
        "action_recovered",
        "action_recovery_required",
        "action_recovery_confirmed",
        "action_replayed",
    }
)


@dataclass(frozen=True)
class TrajectoryFrame:
    sequence: int
    elapsed_ms: int | None
    category: str
    event_type: str
    action_id: str
    action_kind: str
    status: str
    summary: str
    tool_call_cost: int
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentTrajectory:
    schema_version: int
    run_id: str
    fingerprint: str
    event_count: int
    frame_count: int
    omitted_frames: int
    integrity: dict[str, Any]
    metrics: dict[str, Any]
    frames: list[TrajectoryFrame]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "fingerprint": self.fingerprint,
            "event_count": self.event_count,
            "frame_count": self.frame_count,
            "omitted_frames": self.omitted_frames,
            "integrity": dict(self.integrity),
            "metrics": dict(self.metrics),
            "frames": [frame.to_dict() for frame in self.frames],
        }


def build_agent_trajectory(
    events: Iterable[Any],
    *,
    run_id: str = "",
    steps: Iterable[Any] = (),
    traces: Iterable[Any] = (),
    working_state: Any = None,
    stop_reason: str = "",
    completion_ready: bool = False,
    repair_history: Iterable[Any] = (),
    execution_budget: Any = None,
) -> AgentTrajectory:
    """Fold one persisted Runtime stream into a bounded replay and metrics."""

    records = [_record(event) for event in events]
    records = [record for record in records if record]
    timestamps = [_timestamp(record.get("created_at")) for record in records]
    first_timestamp = next((value for value in timestamps if value is not None), None)
    all_frames = [
        _frame_from_event(record, timestamps[index], first_timestamp)
        for index, record in enumerate(records)
    ]
    frames = _bounded_frames(all_frames)
    omitted_frames = max(len(all_frames) - len(frames), 0)
    sequence_integrity = _sequence_integrity(records)
    metrics = _trajectory_metrics(
        records,
        steps=steps,
        traces=traces,
        working_state=working_state,
        stop_reason=stop_reason,
        completion_ready=completion_ready,
        repair_history=repair_history,
        execution_budget=execution_budget,
        timestamps=timestamps,
    )
    resolved_run_id = run_id.strip() or next(
        (str(record.get("run_id") or "").strip() for record in records if record.get("run_id")),
        "",
    )
    return AgentTrajectory(
        schema_version=AGENT_TRAJECTORY_VERSION,
        run_id=resolved_run_id,
        fingerprint=_trajectory_fingerprint(all_frames),
        event_count=len(records),
        frame_count=len(frames),
        omitted_frames=omitted_frames,
        integrity=sequence_integrity,
        metrics=metrics,
        frames=frames,
    )


def build_agent_trajectory_from_record(record: Any) -> dict[str, Any]:
    """Build a trajectory from a WorkflowReport or its serialized record."""

    events = list(_value(record, "agent_events", []) or [])
    working_state = _value(record, "agent_state", {})
    if not _record(working_state):
        working_state = _latest_working_state(events)
    return build_agent_trajectory(
        events,
        run_id=str(_value(record, "agent_run_id", "") or ""),
        steps=_value(record, "agent_steps", []),
        traces=_value(record, "llm_traces", []),
        working_state=working_state,
        stop_reason=str(_value(record, "agent_stop_reason", "") or ""),
        completion_ready=bool(_value(record, "agent_completion_ready", False)),
        repair_history=_value(record, "repair_history", []),
        execution_budget=_value(record, "execution_budget", {}),
    ).to_dict()


def _latest_working_state(events: Iterable[Any]) -> dict[str, Any]:
    records = [_record(event) for event in events]
    for event in reversed(records):
        if event.get("event_type") != "working_state_updated":
            continue
        state = _mapping(_mapping(event.get("payload")).get("working_state"))
        if state:
            return state
    return {}


def _frame_from_event(
    event: dict[str, Any],
    timestamp: datetime | None,
    first_timestamp: datetime | None,
) -> TrajectoryFrame:
    payload = _mapping(event.get("payload"))
    observation = _mapping(payload.get("observation"))
    action = _mapping(payload.get("action"))
    action_kind = str(
        observation.get("action_kind")
        or action.get("kind")
        or _mapping(payload.get("approval_request")).get("action_kind")
        or ""
    )
    event_type = str(event.get("event_type") or "unknown")
    elapsed_ms = None
    if timestamp is not None and first_timestamp is not None:
        elapsed_ms = max(int((timestamp - first_timestamp).total_seconds() * 1_000), 0)
    return TrajectoryFrame(
        sequence=_non_negative_int(event.get("sequence")),
        elapsed_ms=elapsed_ms,
        category=_event_category(event_type),
        event_type=event_type,
        action_id=str(event.get("action_id") or observation.get("action_id") or ""),
        action_kind=action_kind,
        status=_event_status(event_type, payload, observation),
        summary=_event_summary(event_type, payload, observation, action_kind),
        tool_call_cost=(
            _positive_int(payload.get("tool_call_cost"), default=1)
            if event_type in {"action_started", "action_recovery_started"}
            else 0
        ),
        replayed=bool(observation.get("replayed")) or event_type == "action_replayed",
    )


def _trajectory_metrics(
    events: list[dict[str, Any]],
    *,
    steps: Iterable[Any],
    traces: Iterable[Any],
    working_state: Any,
    stop_reason: str,
    completion_ready: bool,
    repair_history: Iterable[Any],
    execution_budget: Any,
    timestamps: list[datetime | None],
) -> dict[str, Any]:
    event_counts = Counter(str(event.get("event_type") or "unknown") for event in events)
    action_sequence = [
        _event_action_kind(event)
        for event in events
        if event.get("event_type") == "decision_recorded" and _event_action_kind(event)
    ]
    if not action_sequence:
        action_sequence = [
            str(_value(step, "action", "") or "")
            for step in steps
            if str(_value(step, "action", "") or "")
        ]
    action_counts = Counter(action_sequence)
    tool_calls = _started_tool_calls(events)
    budget_usage = _mapping(_mapping(execution_budget).get("usage"))
    if not events:
        tool_calls = _non_negative_int(budget_usage.get("tool_calls"))

    successful_tools = 0
    failed_tools = 0
    evidence_units = 0
    successful_evidence_ids: set[str] = set()
    for event in events:
        if event.get("event_type") not in _TERMINAL_ACTION_EVENTS:
            continue
        payload = _mapping(event.get("payload"))
        observation = _mapping(payload.get("observation"))
        action_kind = str(observation.get("action_kind") or _event_action_kind(event) or "")
        status = str(observation.get("status") or "failed")
        completed, failed, evidence = _observation_units(action_kind, status, observation)
        successful_tools += completed
        failed_tools += failed
        evidence_units += evidence
        if status in SUCCESSFUL_EVIDENCE_STATUSES and action_kind in EVIDENCE_ACTION_KINDS:
            action_id = str(event.get("action_id") or observation.get("action_id") or "")
            if action_id:
                successful_evidence_ids.add(action_id)

    authorized: set[str] = set()
    unauthorized_side_effects: list[str] = []
    for event in events:
        event_type = str(event.get("event_type") or "")
        action_id = str(event.get("action_id") or "")
        action_kind = _event_action_kind(event)
        if event_type == "action_authorized" and action_id:
            authorized.add(action_id)
        if (
            event_type in {"action_started", "action_recovery_started"}
            and action_kind in SIDE_EFFECT_ACTIONS
            and action_id not in authorized
        ):
            unauthorized_side_effects.append(action_id or action_kind)

    repair_attempts = _repair_attempts(events, repair_history)
    evidence = _evidence_metrics(working_state, successful_evidence_ids)
    llm = _llm_metrics(traces)
    resolved_stop_reason = stop_reason.strip() or _stop_reason(events)
    valid_timestamps = [value for value in timestamps if value is not None]
    duration_ms = (
        max(int((valid_timestamps[-1] - valid_timestamps[0]).total_seconds() * 1_000), 0)
        if len(valid_timestamps) >= 2
        else 0
    )
    tool_efficiency = None
    if tool_calls:
        tool_efficiency = round(min(evidence_units / tool_calls, 1.0), 4)
    tool_result_total = successful_tools + failed_tools
    tool_success_rate = (
        round(successful_tools / tool_result_total, 4) if tool_result_total else None
    )
    return {
        "duration_ms": duration_ms,
        "stop_reason": resolved_stop_reason,
        "completion_ready": bool(completion_ready),
        "decisions": len(action_sequence),
        "action_sequence": action_sequence,
        "action_counts": dict(sorted(action_counts.items())),
        "event_counts": dict(sorted(event_counts.items())),
        "tool_calls": tool_calls,
        "successful_tool_results": successful_tools,
        "failed_tool_results": failed_tools,
        "tool_success_rate": tool_success_rate,
        "evidence_tool_efficiency": tool_efficiency,
        **evidence,
        "approval_requests": event_counts["approval_required"],
        "approval_grants": event_counts["approval_granted"],
        "approval_consumed": event_counts["approval_consumed"],
        "approval_rejections": event_counts["approval_rejected"],
        "policy_denials": event_counts["action_denied"],
        "unauthorized_side_effects": len(unauthorized_side_effects),
        "unauthorized_action_ids": unauthorized_side_effects[:20],
        "recovery_events": sum(event_counts[name] for name in _RECOVERY_EVENTS),
        "replayed_actions": event_counts["action_replayed"],
        "repair_cycles": len(repair_attempts),
        "repair_attempts": repair_attempts,
        "repair_stop_reason": _repair_stop_reason(events),
        "input_requests": event_counts["input_required"],
        "input_answers": event_counts["input_received"],
        "llm": llm,
    }


def _evidence_metrics(
    working_state: Any,
    successful_action_ids: set[str],
) -> dict[str, Any]:
    state = _record(working_state)
    items: list[dict[str, Any]] = []
    for item in _list(state.get("plan")):
        record = _record(item)
        if record.get("status") == "completed":
            items.append(record)
    for item in _list(state.get("acceptance_criteria")):
        record = _record(item)
        if record.get("status") == "passed":
            items.append(record)
    covered = 0
    reference_count = 0
    for item in items:
        references = [str(value) for value in _list(item.get("evidence_action_ids")) if str(value)]
        valid = [value for value in references if value in successful_action_ids]
        reference_count += len(valid)
        if valid:
            covered += 1
    coverage = round(covered / len(items), 4) if items else None
    return {
        "evidence_eligible_items": len(items),
        "evidence_covered_items": covered,
        "evidence_reference_count": reference_count,
        "evidence_coverage": coverage,
    }


def _llm_metrics(traces: Iterable[Any]) -> dict[str, Any]:
    records = [_record(trace) for trace in traces]
    calls = [
        record
        for record in records
        if record.get("latency_ms") is not None or bool(record.get("prompt_preview"))
    ]
    provider_calls = 0
    estimated_calls = 0
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    prompt_chars = 0
    output_chars = 0
    for trace in calls:
        prompt = str(trace.get("prompt_preview") or "")
        output = str(trace.get("raw_output") or "")
        prompt_chars += len(prompt)
        output_chars += len(output)
        exact_input = _optional_non_negative_int(trace.get("input_tokens"))
        exact_output = _optional_non_negative_int(trace.get("output_tokens"))
        exact_total = _optional_non_negative_int(trace.get("total_tokens"))
        if exact_total is not None or exact_input is not None or exact_output is not None:
            provider_calls += 1
            exact_input = exact_input or 0
            exact_output = exact_output or 0
            input_tokens += exact_input
            output_tokens += exact_output
            total_tokens += exact_total if exact_total is not None else exact_input + exact_output
        else:
            estimated_calls += 1
            estimated_input = _estimated_tokens(prompt)
            estimated_output = _estimated_tokens(output)
            input_tokens += estimated_input
            output_tokens += estimated_output
            total_tokens += estimated_input + estimated_output
    if provider_calls and estimated_calls:
        token_source = "mixed"
    elif provider_calls:
        token_source = "provider"
    elif estimated_calls:
        token_source = "estimated"
    else:
        token_source = "none"
    return {
        "calls": len(calls),
        "failures": sum(
            1 for trace in calls if trace.get("error") or not bool(trace.get("parsed"))
        ),
        "fallbacks": sum(1 for trace in records if bool(trace.get("fallback_used"))),
        "latency_ms": sum(_non_negative_int(trace.get("latency_ms")) for trace in calls),
        "observed_prompt_chars": prompt_chars,
        "observed_output_chars": output_chars,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "provider_usage_calls": provider_calls,
        "estimated_usage_calls": estimated_calls,
        "token_source": token_source,
    }


def _observation_units(
    action_kind: str,
    status: str,
    observation: dict[str, Any],
) -> tuple[int, int, int]:
    data = _mapping(observation.get("data"))
    if action_kind == "parallel_read":
        results = [_record(value) for value in _list(data.get("results"))]
        completed = sum(value.get("status") == "completed" for value in results)
        failed = max(len(results) - completed, 0)
        evidence = sum(
            value.get("status") == "completed"
            and str(value.get("action_kind") or "") in EVIDENCE_ACTION_KINDS
            for value in results
        )
        return completed, failed, evidence
    successful = status in SUCCESSFUL_EVIDENCE_STATUSES
    return (
        1 if successful else 0,
        0 if successful else 1,
        1 if successful and action_kind in EVIDENCE_ACTION_KINDS else 0,
    )


def _started_tool_calls(events: list[dict[str, Any]]) -> int:
    total = 0
    for event in events:
        if event.get("event_type") not in {"action_started", "action_recovery_started"}:
            continue
        payload = _mapping(event.get("payload"))
        stored = _optional_positive_int(payload.get("tool_call_cost"))
        if stored is not None:
            total += stored
            continue
        action = _mapping(payload.get("action"))
        members = _list(_mapping(action.get("arguments")).get("actions"))
        total += len(members) if action.get("kind") == "parallel_read" and members else 1
    return total


def _repair_attempts(events: list[dict[str, Any]], repair_history: Iterable[Any]) -> list[int]:
    attempts: set[int] = set()
    for event in events:
        payload = _mapping(event.get("payload"))
        repair = _mapping(payload.get("repair"))
        attempt = _optional_positive_int(repair.get("attempt"))
        if attempt is not None:
            attempts.add(attempt)
    for item in repair_history:
        attempt = _optional_positive_int(_value(item, "attempt", None))
        if attempt is not None:
            attempts.add(attempt)
    return sorted(attempts)


def _repair_stop_reason(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        repair = _mapping(_mapping(event.get("payload")).get("repair"))
        reason = str(repair.get("stop_reason") or "").strip()
        if reason:
            return reason
    return ""


def _stop_reason(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        if event.get("event_type") != "run_stopped":
            continue
        return str(_mapping(event.get("payload")).get("reason") or "").strip()
    return ""


def _sequence_integrity(events: list[dict[str, Any]]) -> dict[str, Any]:
    sequences = [_non_negative_int(event.get("sequence")) for event in events]
    positive = [value for value in sequences if value > 0]
    counts = Counter(positive)
    duplicates = sorted(value for value, count in counts.items() if count > 1)
    unique = sorted(counts)
    gaps: list[dict[str, int]] = []
    for previous, current in zip(unique, unique[1:]):
        if current > previous + 1:
            gaps.append({"after": previous, "before": current})
    ordered = positive == sorted(positive) and len(positive) == len(events)
    starts_at_one = not unique or unique[0] == 1
    return {
        "valid": ordered and starts_at_one and not duplicates and not gaps,
        "ordered": ordered,
        "starts_at_one": starts_at_one,
        "duplicate_sequences": duplicates[:MAX_TRAJECTORY_GAPS],
        "sequence_gaps": gaps[:MAX_TRAJECTORY_GAPS],
    }


def _trajectory_fingerprint(frames: list[TrajectoryFrame]) -> str:
    canonical = [
        {
            "sequence": frame.sequence,
            "category": frame.category,
            "event_type": frame.event_type,
            "action_id": frame.action_id,
            "action_kind": frame.action_kind,
            "status": frame.status,
            "summary": frame.summary,
            "tool_call_cost": frame.tool_call_cost,
            "replayed": frame.replayed,
        }
        for frame in frames
    ]
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _bounded_frames(frames: list[TrajectoryFrame]) -> list[TrajectoryFrame]:
    if len(frames) <= MAX_TRAJECTORY_FRAMES:
        return frames
    first_count = MAX_TRAJECTORY_FRAMES // 2
    return [*frames[:first_count], *frames[-(MAX_TRAJECTORY_FRAMES - first_count) :]]


def _event_action_kind(event: dict[str, Any]) -> str:
    payload = _mapping(event.get("payload"))
    action = _mapping(payload.get("action"))
    observation = _mapping(payload.get("observation"))
    return str(action.get("kind") or observation.get("action_kind") or "")


def _event_status(
    event_type: str,
    payload: dict[str, Any],
    observation: dict[str, Any],
) -> str:
    if observation.get("status"):
        return str(observation["status"])
    if event_type == "run_stopped":
        return str(payload.get("reason") or "stopped")
    suffixes = {
        "decision_recorded": "recorded",
        "action_authorized": "authorized",
        "action_started": "started",
        "action_recovery_started": "started",
        "approval_required": "waiting",
        "approval_granted": "granted",
        "approval_consumed": "consumed",
        "input_required": "waiting",
        "input_received": "received",
        "working_state_updated": "updated",
        "run_started": "started",
    }
    return suffixes.get(event_type, str(payload.get("status") or "recorded"))


def _event_summary(
    event_type: str,
    payload: dict[str, Any],
    observation: dict[str, Any],
    action_kind: str,
) -> str:
    if event_type == "input_received":
        return "Bound a user answer to the pending input request."
    if event_type == "working_state_updated":
        state = _mapping(payload.get("working_state"))
        iteration = _non_negative_int(state.get("iteration"))
        status = str(state.get("status") or "updated")
        return f"Working State iteration {iteration} is {status}."
    candidates = [
        observation.get("summary"),
        observation.get("error"),
        payload.get("summary"),
        payload.get("message"),
        payload.get("reason"),
    ]
    text = next((str(value).strip() for value in candidates if str(value or "").strip()), "")
    if not text:
        label = event_type.replace("_", " ")
        text = f"{label}{f' for {action_kind}' if action_kind else ''}."
    return redact_context_secrets(text)[:MAX_TRAJECTORY_SUMMARY_CHARS]


def _event_category(event_type: str) -> str:
    if event_type == "decision_recorded":
        return "decision"
    if event_type.startswith("approval_"):
        return "approval"
    if event_type.startswith("input_"):
        return "interaction"
    if event_type.startswith("repair_"):
        return "repair"
    if event_type.startswith("working_state_"):
        return "state"
    if event_type in _RECOVERY_EVENTS:
        return "recovery"
    if event_type in {"action_authorized", "action_denied", "policy_denied"}:
        return "policy"
    if event_type in {"action_started"}:
        return "execution"
    if event_type.startswith("action_"):
        return "observation"
    if event_type.startswith("run_"):
        return "lifecycle"
    if event_type.startswith("finish_"):
        return "completion"
    if event_type.startswith("rollback_"):
        return "safety"
    return "runtime"


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _estimated_tokens(text: str) -> int:
    return (len(text) + 3) // 4 if text else 0


def _record(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        return dict(result) if isinstance(result, dict) else {}
    return {}


def _value(value: Any, name: str, default: Any) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _non_negative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _positive_int(value: Any, *, default: int) -> int:
    parsed = _optional_positive_int(value)
    return parsed if parsed is not None else default


def _optional_positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _optional_non_negative_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None
