"""Compact, persistent working-state snapshots for iterative Agent controllers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from ..models import AgentStateUpdate
from .models import RuntimeAction, RuntimeEvent, RuntimeObservation


AGENT_WORKING_STATE_VERSION = 4
MAX_RECENT_OBSERVATIONS = 8
MAX_OBJECTIVE_CHARS = 2_000
MAX_OBSERVATION_SUMMARY_CHARS = 500
MAX_SELECTED_PATHS = 50
MAX_STATE_FINDINGS = 20
MAX_STATE_QUESTIONS = 12
MAX_STATE_ITEM_CHARS = 500
MAX_AGENT_PLAN_ITEMS = 12
MAX_AGENT_ACCEPTANCE_CRITERIA = 12
MAX_AGENT_PROPOSED_EDITS = 12
MAX_EVIDENCE_ACTION_IDS = 8
SUCCESSFUL_EVIDENCE_STATUSES = frozenset({"completed", "applied", "no_change"})
EVIDENCE_ACTION_KINDS = frozenset(
    {
        "read_file",
        "inspect_repository_map",
        "inspect_git_status",
        "inspect_diff",
        "apply_patch",
        "edit_file",
        "run_command",
        "validate",
    }
)


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
class AgentPlanItem:
    step_id: str
    title: str
    detail: str
    status: str
    evidence_action_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentAcceptanceState:
    criterion_id: str
    kind: str
    description: str
    required: bool
    status: str
    evidence_action_ids: list[str]
    evidence_summary: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentProposedEditState:
    path: str
    base_sha256: str
    current_sha256: str
    revision: int
    hunk_count: int
    status: str
    inspected: bool

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
    plan: list[AgentPlanItem]
    acceptance_criteria: list[AgentAcceptanceState]
    proposed_edits: list[AgentProposedEditState]
    recent_observations: list[AgentStateObservation]
    stop_reason: str | None
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["plan"] = [item.to_dict() for item in self.plan]
        data["acceptance_criteria"] = [
            item.to_dict() for item in self.acceptance_criteria
        ]
        data["proposed_edits"] = [item.to_dict() for item in self.proposed_edits]
        data["recent_observations"] = [item.to_dict() for item in self.recent_observations]
        return data


def create_agent_working_state(
    objective: str,
    *,
    acceptance_criteria: list[Any] | None = None,
) -> AgentWorkingState:
    normalized_objective = _bounded_text(objective, MAX_OBJECTIVE_CHARS)
    return AgentWorkingState(
        version=AGENT_WORKING_STATE_VERSION,
        objective=normalized_objective,
        focus="",
        phase="exploration",
        status="running",
        iteration=0,
        selected_paths=[],
        findings=[],
        open_questions=[],
        expected_evidence="",
        plan=[
            AgentPlanItem(
                step_id="investigate_repository",
                title="Investigate repository evidence",
                detail=normalized_objective or "Understand the requested repository task.",
                status="in_progress",
                evidence_action_ids=[],
            )
        ],
        acceptance_criteria=_initial_acceptance_state(
            acceptance_criteria,
            normalized_objective,
        ),
        proposed_edits=[],
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
    updated_state = apply_agent_state_update(state, state_update)
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
    proposed_edits = _advance_proposed_edits(
        updated_state.proposed_edits,
        action,
        observation,
    )
    return replace(
        updated_state,
        version=AGENT_WORKING_STATE_VERSION,
        phase=phase,
        status=status,
        iteration=iteration,
        selected_paths=_normalize_paths(selected_paths),
        expected_evidence=_bounded_text(expected_evidence, MAX_STATE_ITEM_CHARS),
        proposed_edits=proposed_edits,
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
        plan=_plan_from_record(value.get("plan")),
        acceptance_criteria=_acceptance_from_record(
            value.get("acceptance_criteria")
        ),
        proposed_edits=_proposed_edits_from_record(value.get("proposed_edits")),
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
        f"- #{item.iteration} {item.action_id} {item.action_kind} "
        f"[{item.status}]: {item.summary}"
        for item in state.recent_observations
    ) or "- none"
    plan = "\n".join(
        f"- {item.step_id} [{item.status}]: {item.title}. {item.detail}"
        + (
            f" Evidence: {', '.join(item.evidence_action_ids)}."
            if item.evidence_action_ids
            else ""
        )
        for item in state.plan
    ) or "- none"
    acceptance = "\n".join(
        f"- {item.criterion_id} [{item.status}, "
        f"{'required' if item.required else 'optional'}]: {item.description}"
        + (
            f" Evidence: {', '.join(item.evidence_action_ids)}; {item.evidence_summary}."
            if item.evidence_action_ids
            else ""
        )
        for item in state.acceptance_criteria
    ) or "- none"
    proposed_edits = "\n".join(
        f"- {item.path} [revision {item.revision}, {item.status}, "
        f"{'inspected' if item.inspected else 'inspection required'}]: "
        f"{item.hunk_count} cumulative hunk(s), virtual SHA-256 {item.current_sha256}"
        for item in state.proposed_edits
    ) or "- none"
    blockers = agent_completion_blockers(state)
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
            "Implementation plan:",
            plan,
            "Acceptance state:",
            acceptance,
            "Virtual proposed edits:",
            proposed_edits,
            f"Completion ready: {'yes' if not blockers else 'no'}",
            f"Completion blockers: {'; '.join(blockers) if blockers else 'none'}",
            f"Expected evidence: {state.expected_evidence or 'none'}",
            f"Stop reason: {state.stop_reason or 'none'}",
            "Recent action observations:",
            observations,
        ]
    )


def apply_agent_state_update(
    state: AgentWorkingState,
    update: AgentStateUpdate | None,
) -> AgentWorkingState:
    if update is None:
        return state
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
    plan = _apply_plan_updates(state, update.plan_updates)
    acceptance = _apply_acceptance_updates(state, update.acceptance_updates)
    return replace(
        state,
        version=AGENT_WORKING_STATE_VERSION,
        focus=focus,
        findings=findings,
        open_questions=open_questions,
        plan=plan,
        acceptance_criteria=acceptance,
        updated_at=_now(),
    )


def agent_completion_blockers(state: AgentWorkingState) -> list[str]:
    blockers = [
        f"plan:{item.step_id}"
        for item in state.plan
        if item.status != "completed"
    ]
    blockers.extend(
        f"acceptance:{item.criterion_id}"
        for item in state.acceptance_criteria
        if item.required and item.status != "passed"
    )
    if not state.plan:
        blockers.append("plan:missing")
    if not state.acceptance_criteria:
        blockers.append("acceptance:missing")
    blockers.extend(
        f"proposal:{item.path}:{'conflict' if item.status == 'conflict' else 'uninspected'}"
        for item in state.proposed_edits
        if item.status == "conflict" or not item.inspected
    )
    return blockers


def agent_completion_ready(state: AgentWorkingState) -> bool:
    return not agent_completion_blockers(state)


def _apply_plan_updates(state: AgentWorkingState, updates: list[Any]) -> list[AgentPlanItem]:
    plan = list(state.plan)
    positions = {item.step_id.casefold(): index for index, item in enumerate(plan)}
    for update in updates:
        evidence_ids = _validated_evidence_ids(state, update.evidence_action_ids)
        if update.status == "completed" and not evidence_ids:
            raise ValueError("Completed plan state requires observation evidence.")
        if update.status != "completed" and evidence_ids:
            raise ValueError("Only completed plan state may retain observation evidence.")
        item = AgentPlanItem(
            step_id=_bounded_identifier(update.step_id),
            title=_bounded_text(update.title, MAX_STATE_ITEM_CHARS),
            detail=_bounded_text(update.detail, MAX_STATE_ITEM_CHARS),
            status=update.status if update.status in {"pending", "in_progress", "completed"} else "pending",
            evidence_action_ids=evidence_ids,
        )
        key = item.step_id.casefold()
        if key in positions:
            plan[positions[key]] = item
        elif item.step_id:
            positions[key] = len(plan)
            plan.append(item)
    return plan[-MAX_AGENT_PLAN_ITEMS:]


def _apply_acceptance_updates(
    state: AgentWorkingState,
    updates: list[Any],
) -> list[AgentAcceptanceState]:
    criteria = list(state.acceptance_criteria)
    positions = {
        item.criterion_id.casefold(): index for index, item in enumerate(criteria)
    }
    for update in updates:
        key = _bounded_identifier(update.criterion_id).casefold()
        existing = criteria[positions[key]] if key in positions else None
        evidence_ids = _validated_evidence_ids(state, update.evidence_action_ids)
        evidence_summary = _bounded_text(
            update.evidence_summary,
            MAX_STATE_ITEM_CHARS,
        )
        if evidence_ids and not evidence_summary:
            raise ValueError("Acceptance evidence requires a non-empty summary.")
        if evidence_summary and not evidence_ids:
            raise ValueError("Acceptance evidence summary requires observation evidence.")
        item = AgentAcceptanceState(
            criterion_id=_bounded_identifier(update.criterion_id),
            kind=_bounded_text(update.kind, 100) or (existing.kind if existing else "analysis"),
            description=_bounded_text(update.description, MAX_STATE_ITEM_CHARS),
            required=(existing.required if existing and existing.required else bool(update.required)),
            status="passed" if evidence_ids else (existing.status if existing else "pending"),
            evidence_action_ids=(
                evidence_ids
                if evidence_ids
                else list(existing.evidence_action_ids) if existing else []
            ),
            evidence_summary=(
                evidence_summary
                if evidence_ids
                else existing.evidence_summary if existing else ""
            ),
        )
        if key in positions:
            criteria[positions[key]] = item
        elif item.criterion_id:
            positions[key] = len(criteria)
            criteria.append(item)
    return criteria[-MAX_AGENT_ACCEPTANCE_CRITERIA:]


def _validated_evidence_ids(state: AgentWorkingState, action_ids: list[str]) -> list[str]:
    if not action_ids:
        return []
    observations = {
        item.action_id.casefold(): item
        for item in state.recent_observations
        if item.status in SUCCESSFUL_EVIDENCE_STATUSES
        and item.action_kind in EVIDENCE_ACTION_KINDS
    }
    result: list[str] = []
    for action_id in action_ids:
        key = action_id.casefold()
        if key not in observations:
            raise ValueError(
                "Evidence action id is not a completed evidence-producing "
                f"observation: {action_id}."
            )
        canonical = observations[key].action_id
        if canonical not in result:
            result.append(canonical)
    return result[:MAX_EVIDENCE_ACTION_IDS]


def _initial_acceptance_state(
    criteria: list[Any] | None,
    objective: str,
) -> list[AgentAcceptanceState]:
    source = criteria or [
        {
            "criterion_id": "analysis_complete",
            "kind": "analysis",
            "description": f"Repository evidence addresses the task: {objective}",
            "required": True,
        }
    ]
    result: list[AgentAcceptanceState] = []
    for raw in source[:MAX_AGENT_ACCEPTANCE_CRITERIA]:
        if isinstance(raw, dict):
            criterion_id = raw.get("criterion_id")
            kind = raw.get("kind")
            description = raw.get("description")
            required = raw.get("required", True)
        else:
            criterion_id = getattr(raw, "criterion_id", "")
            kind = getattr(raw, "kind", "analysis")
            description = getattr(raw, "description", "")
            required = getattr(raw, "required", True)
        normalized_id = _bounded_identifier(criterion_id)
        normalized_description = _bounded_text(description, MAX_STATE_ITEM_CHARS)
        if not normalized_id or not normalized_description:
            continue
        result.append(
            AgentAcceptanceState(
                criterion_id=normalized_id,
                kind=_bounded_text(kind, 100) or "analysis",
                description=normalized_description,
                required=bool(required),
                status="pending",
                evidence_action_ids=[],
                evidence_summary="",
            )
        )
    return result


def _advance_proposed_edits(
    current: list[AgentProposedEditState],
    action: RuntimeAction,
    observation: RuntimeObservation,
) -> list[AgentProposedEditState]:
    edits = list(current)
    if action.kind == "propose_patch":
        path = _normalized_single_path(observation.data.get("path"))
        if not path:
            path = _normalized_single_path(action.arguments.get("path"))
        if not path:
            return edits
        position = next(
            (index for index, item in enumerate(edits) if item.path == path),
            None,
        )
        if observation.status == "completed" and observation.data.get("removed"):
            return [item for item in edits if item.path != path]
        if observation.status == "completed" and observation.data.get("proposal_status") == "proposed":
            parsed = _proposed_edit_from_record(observation.data)
            if parsed is None:
                return edits
            if position is None:
                edits.append(parsed)
            else:
                edits[position] = parsed
        elif (
            observation.status == "conflict"
            and position is not None
            and any(
                isinstance(item, dict) and item.get("kind") == "stale_repository"
                for item in observation.data.get("conflicts") or []
            )
        ):
            edits[position] = replace(
                edits[position],
                status="conflict",
                inspected=False,
            )
    elif action.kind == "inspect_proposed_diff":
        raw_files = observation.data.get("files")
        if isinstance(raw_files, list):
            parsed_files = [
                parsed
                for raw in raw_files
                if (parsed := _proposed_edit_from_record(raw)) is not None
            ]
            if parsed_files or observation.data.get("proposal_status") == "empty":
                edits = parsed_files
    return edits[-MAX_AGENT_PROPOSED_EDITS:]


def _proposed_edits_from_record(value: object) -> list[AgentProposedEditState]:
    if not isinstance(value, list):
        return []
    return [
        parsed
        for raw in value[-MAX_AGENT_PROPOSED_EDITS:]
        if (parsed := _proposed_edit_from_record(raw)) is not None
    ]


def _proposed_edit_from_record(value: object) -> AgentProposedEditState | None:
    if not isinstance(value, dict):
        return None
    path = _normalized_single_path(value.get("path"))
    base_sha256 = _normalized_sha256(value.get("base_sha256"))
    current_sha256 = _normalized_sha256(
        value.get("current_sha256") or value.get("resulting_sha256")
    )
    if not path or not base_sha256 or not current_sha256:
        return None
    status = _bounded_text(value.get("status"), 50)
    if status not in {"proposed", "inspected", "conflict"}:
        status = "inspected" if bool(value.get("inspected")) else "proposed"
    inspected = bool(value.get("inspected")) and status == "inspected"
    return AgentProposedEditState(
        path=path,
        base_sha256=base_sha256,
        current_sha256=current_sha256,
        revision=_positive_int(value.get("revision"), 1),
        hunk_count=_nonnegative_int(value.get("hunk_count")),
        status=status,
        inspected=inspected,
    )


def _normalized_single_path(value: object) -> str:
    paths = _normalize_paths([value] if isinstance(value, str) else [])
    return paths[0] if paths else ""


def _normalized_sha256(value: object) -> str:
    candidate = _bounded_text(value, 64).lower()
    if len(candidate) != 64 or not all(character in "0123456789abcdef" for character in candidate):
        return ""
    return candidate


def _plan_from_record(value: object) -> list[AgentPlanItem]:
    if not isinstance(value, list):
        return []
    result: list[AgentPlanItem] = []
    for raw in value[-MAX_AGENT_PLAN_ITEMS:]:
        if not isinstance(raw, dict):
            continue
        step_id = _bounded_identifier(raw.get("step_id"))
        title = _bounded_text(raw.get("title"), MAX_STATE_ITEM_CHARS)
        detail = _bounded_text(raw.get("detail"), MAX_STATE_ITEM_CHARS)
        status = str(raw.get("status") or "pending")
        evidence_action_ids = _record_identifiers(raw.get("evidence_action_ids"))
        if not step_id or not title or not detail:
            continue
        result.append(
            AgentPlanItem(
                step_id=step_id,
                title=title,
                detail=detail,
                status=(
                    "completed"
                    if status == "completed" and evidence_action_ids
                    else status if status in {"pending", "in_progress"} else "pending"
                ),
                evidence_action_ids=evidence_action_ids if status == "completed" else [],
            )
        )
    return result


def _acceptance_from_record(value: object) -> list[AgentAcceptanceState]:
    if not isinstance(value, list):
        return []
    result: list[AgentAcceptanceState] = []
    for raw in value[-MAX_AGENT_ACCEPTANCE_CRITERIA:]:
        if not isinstance(raw, dict):
            continue
        criterion_id = _bounded_identifier(raw.get("criterion_id"))
        description = _bounded_text(raw.get("description"), MAX_STATE_ITEM_CHARS)
        if not criterion_id or not description:
            continue
        status = str(raw.get("status") or "pending")
        evidence_action_ids = _record_identifiers(raw.get("evidence_action_ids"))
        evidence_summary = _bounded_text(
            raw.get("evidence_summary"),
            MAX_STATE_ITEM_CHARS,
        )
        passed = status == "passed" and bool(evidence_action_ids) and bool(evidence_summary)
        result.append(
            AgentAcceptanceState(
                criterion_id=criterion_id,
                kind=_bounded_text(raw.get("kind"), 100) or "analysis",
                description=description,
                required=bool(raw.get("required", True)),
                status="passed" if passed else "pending",
                evidence_action_ids=evidence_action_ids if passed else [],
                evidence_summary=evidence_summary if passed else "",
            )
        )
    return result


def _record_identifiers(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for raw in value:
        identifier = _bounded_identifier(raw)
        if identifier and identifier not in result:
            result.append(identifier)
    return result[:MAX_EVIDENCE_ACTION_IDS]


def _bounded_identifier(value: object) -> str:
    candidate = _bounded_text(value, 100)
    if not all(character.isalnum() or character in "_.-" for character in candidate):
        return ""
    return candidate


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
        "propose_patch": "proposal",
        "inspect_proposed_diff": "proposal_review",
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
