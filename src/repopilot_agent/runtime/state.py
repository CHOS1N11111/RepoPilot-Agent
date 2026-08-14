"""Compact, persistent working-state snapshots for iterative Agent controllers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from ..models import AgentStateUpdate
from .models import RuntimeAction, RuntimeEvent, RuntimeObservation


AGENT_WORKING_STATE_VERSION = 2
MAX_RECENT_OBSERVATIONS = 8
MAX_OBJECTIVE_CHARS = 2_000
MAX_OBSERVATION_SUMMARY_CHARS = 500
MAX_SELECTED_PATHS = 50
MAX_STATE_FINDINGS = 20
MAX_STATE_QUESTIONS = 12
MAX_STATE_ITEM_CHARS = 500


@dataclass(frozen=True)
class AgentStateObservation:
    iteration: int
    action_id: str
    action_kind: str
    status: str
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentWorkingState:
    version: int
    objective: str
    focus: str
    phase: str
    status: str
    iteration: int
    selected_paths: list[str]
    findings: list[str]
    open_questions: list[str]
    expected_evidence: str
    recent_observations: list[AgentStateObservation]
    stop_reason: str | None
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["recent_observations"] = [item.to_dict() for item in self.recent_observations]
        return data


def create_agent_working_state(objective: str) -> AgentWorkingState:
    return AgentWorkingState(
        version=AGENT_WORKING_STATE_VERSION,
        objective=_bounded_text(objective, MAX_OBJECTIVE_CHARS),
        focus="",
        phase="exploration",
        status="running",
        iteration=0,
        selected_paths=[],
        findings=[],
        open_questions=[],
        expected_evidence="",
        recent_observations=[],
        stop_reason=None,
        updated_at=_now(),
    )


def advance_agent_working_state(
    state: AgentWorkingState,
    action: RuntimeAction,
    observation: RuntimeObservation,
    *,
    selected_paths: list[str],
    state_update: AgentStateUpdate | None = None,
    expected_evidence: str = "",
) -> AgentWorkingState:
    iteration = state.iteration + 1
    recent = [
        *state.recent_observations,
        AgentStateObservation(
            iteration=iteration,
            action_id=_bounded_text(action.action_id, 200),
            action_kind=_bounded_text(action.kind, 100),
            status=_bounded_text(observation.status, 100),
            summary=_bounded_text(observation.summary, MAX_OBSERVATION_SUMMARY_CHARS),
        ),
    ][-MAX_RECENT_OBSERVATIONS:]
    phase, status, stop_reason = _transition(action, observation)
    focus, findings, open_questions = _apply_state_update(state, state_update)
    return replace(
        state,
        version=AGENT_WORKING_STATE_VERSION,
        focus=focus,
        phase=phase,
        status=status,
        iteration=iteration,
        selected_paths=_normalize_paths(selected_paths),
        findings=findings,
        open_questions=open_questions,
        expected_evidence=_bounded_text(expected_evidence, MAX_STATE_ITEM_CHARS),
        recent_observations=recent,
        stop_reason=stop_reason,
        updated_at=_now(),
    )


def stop_agent_working_state(
    state: AgentWorkingState,
    reason: str,
    *,
    selected_paths: list[str] | None = None,
) -> AgentWorkingState:
    normalized = _bounded_text(reason, 100) or "stopped"
    if normalized == "finished":
        phase = "completed"
        status = "completed"
    elif normalized == "failed":
        phase = "failed"
        status = "failed"
    elif normalized in {"approval_required", "input_required", "recovery_required"}:
        phase = {
            "approval_required": "approval",
            "input_required": "input",
            "recovery_required": "recovery",
        }[normalized]
        status = "waiting"
    else:
        phase = state.phase
        status = "stopped"
    return replace(
        state,
        version=AGENT_WORKING_STATE_VERSION,
        phase=phase,
        status=status,
        selected_paths=(
            _normalize_paths(selected_paths)
            if selected_paths is not None
            else state.selected_paths
        ),
        stop_reason=normalized,
        updated_at=_now(),
    )


def agent_working_state_from_record(
    value: object,
    *,
    default_objective: str = "",
) -> AgentWorkingState | None:
    if (
        not isinstance(value, dict)
        or not value
        or not any(key in value for key in {"phase", "status", "iteration"})
    ):
        return None
    observations = value.get("recent_observations")
    parsed_observations: list[AgentStateObservation] = []
    if isinstance(observations, list):
        for item in observations[-MAX_RECENT_OBSERVATIONS:]:
            if not isinstance(item, dict):
                continue
            parsed_observations.append(
                AgentStateObservation(
                    iteration=_nonnegative_int(item.get("iteration")),
                    action_id=_bounded_text(item.get("action_id"), 200),
                    action_kind=_bounded_text(item.get("action_kind"), 100),
                    status=_bounded_text(item.get("status"), 100) or "unknown",
                    summary=_bounded_text(
                        item.get("summary"),
                        MAX_OBSERVATION_SUMMARY_CHARS,
                    ),
                )
            )
    return AgentWorkingState(
        version=max(_positive_int(value.get("version"), AGENT_WORKING_STATE_VERSION), 1),
        objective=_bounded_text(
            value.get("objective") or default_objective,
            MAX_OBJECTIVE_CHARS,
        ),
        focus=_bounded_text(value.get("focus"), MAX_STATE_ITEM_CHARS),
        phase=_bounded_text(value.get("phase"), 100) or "exploration",
        status=_bounded_text(value.get("status"), 100) or "running",
        iteration=_nonnegative_int(value.get("iteration")),
        selected_paths=_normalize_paths(value.get("selected_paths")),
        findings=_state_items_from_record(value.get("findings"), MAX_STATE_FINDINGS),
        open_questions=_state_items_from_record(
            value.get("open_questions"),
            MAX_STATE_QUESTIONS,
        ),
        expected_evidence=_bounded_text(
            value.get("expected_evidence"),
            MAX_STATE_ITEM_CHARS,
        ),
        recent_observations=parsed_observations,
        stop_reason=(
            _bounded_text(value.get("stop_reason"), 100)
            if value.get("stop_reason") is not None
            else None
        ),
        updated_at=_bounded_text(value.get("updated_at"), 100),
    )


def latest_agent_working_state(
    events: list[RuntimeEvent],
    *,
    default_objective: str = "",
) -> AgentWorkingState | None:
    for event in reversed(events):
        if event.event_type != "working_state_updated":
            continue
        state = agent_working_state_from_record(
            event.payload.get("working_state"),
            default_objective=default_objective,
        )
        if state is not None:
            return state
    return None


def render_agent_working_state(state: AgentWorkingState) -> str:
    selected = ", ".join(state.selected_paths) or "none"
    findings = "\n".join(f"- {item}" for item in state.findings) or "- none"
    open_questions = (
        "\n".join(f"- {item}" for item in state.open_questions) or "- none"
    )
    observations = "\n".join(
        f"- #{item.iteration} {item.action_kind} [{item.status}]: {item.summary}"
        for item in state.recent_observations
    ) or "- none"
    return "\n".join(
        [
            f"Objective: {state.objective}",
            f"Current focus: {state.focus or 'none'}",
            f"Phase: {state.phase}",
            f"Status: {state.status}",
            f"Iteration: {state.iteration}",
            f"Selected paths: {selected}",
            "Findings:",
            findings,
            "Open questions:",
            open_questions,
            f"Expected evidence: {state.expected_evidence or 'none'}",
            f"Stop reason: {state.stop_reason or 'none'}",
            "Recent action observations:",
            observations,
        ]
    )


def _apply_state_update(
    state: AgentWorkingState,
    update: AgentStateUpdate | None,
) -> tuple[str, list[str], list[str]]:
    if update is None:
        return state.focus, list(state.findings), list(state.open_questions)
    focus = _bounded_text(update.focus, MAX_STATE_ITEM_CHARS) or state.focus
    findings = _merge_state_items(
        state.findings,
        update.add_findings,
        MAX_STATE_FINDINGS,
    )
    resolved = {_normalized_item_key(item) for item in update.resolve_open_questions}
    remaining_questions = [
        item for item in state.open_questions if _normalized_item_key(item) not in resolved
    ]
    open_questions = _merge_state_items(
        remaining_questions,
        update.add_open_questions,
        MAX_STATE_QUESTIONS,
    )
    return focus, findings, open_questions


def _merge_state_items(
    existing: list[str],
    additions: list[str],
    limit: int,
) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for item in [*existing, *additions]:
        normalized = _bounded_text(" ".join(str(item).split()), MAX_STATE_ITEM_CHARS)
        key = normalized.casefold()
        if normalized and key not in seen:
            merged.append(normalized)
            seen.add(key)
    return merged[-limit:]


def _state_items_from_record(value: object, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return _merge_state_items([], [item for item in value if isinstance(item, str)], limit)


def _normalized_item_key(value: str) -> str:
    return " ".join(value.split()).strip().casefold()


def _transition(
    action: RuntimeAction,
    observation: RuntimeObservation,
) -> tuple[str, str, str | None]:
    if observation.status == "approval_required":
        return "approval", "waiting", "approval_required"
    if observation.status == "input_required":
        return "input", "waiting", "input_required"
    if observation.status == "recovery_required":
        return "recovery", "waiting", "recovery_required"
    if observation.status in {"failed", "policy_denied", "verification_failed"}:
        return "failed", "failed", observation.status
    if action.kind == "finish" and observation.status == "completed":
        return "completed", "completed", "finished"
    phase = {
        "search_files": "exploration",
        "read_file": "inspection",
        "inspect_repository_map": "inspection",
        "inspect_git_status": "inspection",
        "inspect_diff": "inspection",
        "edit_file": "editing",
        "apply_patch": "editing",
        "run_command": "validation",
        "validate": "validation",
        "ask_user": "input",
    }.get(action.kind, "exploration")
    return phase, "running", None


def _normalize_paths(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        candidate = item.strip().replace("\\", "/")[:500]
        normalized = PurePosixPath(candidate)
        windows_path = PureWindowsPath(candidate)
        normalized_path = normalized.as_posix()
        if (
            not candidate
            or normalized.is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
            or ".." in normalized.parts
            or any(part in {"", "."} for part in normalized.parts)
            or normalized_path in seen
        ):
            continue
        paths.append(normalized_path)
        seen.add(normalized_path)
        if len(paths) >= MAX_SELECTED_PATHS:
            break
    return paths


def _bounded_text(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _nonnegative_int(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
