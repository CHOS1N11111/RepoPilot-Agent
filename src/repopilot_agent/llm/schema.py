"""Strict JSON schema parsing helpers for LLM outputs."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

from .base import LLMError
from .json_utils import parse_json_object
from ..models import (
    AGENT_DECISION_VERSION,
    AgentDecision,
    AgentStateUpdate,
    FileChangeProposal,
    FileEditProposal,
    PatchReview,
    PlanStep,
    RiskNote,
)

ALLOWED_CHANGE_TYPES = {"bugfix", "feature", "test", "documentation", "refinement"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
ALLOWED_RISK_LEVELS = {"high", "medium", "low"}
ALLOWED_AGENT_DECISION_ACTIONS = {
    "search_files",
    "read_file",
    "inspect_repository_map",
    "inspect_git_status",
    "finish",
}
AGENT_DECISION_KEYS = {
    "version",
    "rationale",
    "action",
    "expected_evidence",
    "state_update",
    "finish_reason",
    "user_question",
}
AGENT_STATE_UPDATE_KEYS = {
    "focus",
    "add_findings",
    "add_open_questions",
    "resolve_open_questions",
}
MAX_DECISION_TEXT_CHARS = 2_000
MAX_STATE_ITEM_CHARS = 500
MAX_STATE_UPDATE_ITEMS = 12
MAX_DECISION_PATHS = 50


def parse_plan_steps_json(response: str) -> list[PlanStep]:
    data = parse_json_object(response)
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise LLMError("LLM plan JSON must contain a non-empty 'steps' list.")

    steps: list[PlanStep] = []
    for index, raw_step in enumerate(raw_steps, start=1):
        if not isinstance(raw_step, dict):
            raise LLMError("Each LLM plan step must be an object.")
        title = raw_step.get("title")
        detail = raw_step.get("detail")
        if not isinstance(title, str) or not title.strip():
            raise LLMError("Each LLM plan step must include a non-empty title.")
        if not isinstance(detail, str) or not detail.strip():
            raise LLMError("Each LLM plan step must include a non-empty detail.")
        steps.append(PlanStep(order=index, title=title.strip(), detail=detail.strip()))
    return steps


def parse_agent_decision_json(response: str) -> AgentDecision:
    data = parse_json_object(response)
    _require_exact_keys(data, AGENT_DECISION_KEYS, "Agent decision")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != AGENT_DECISION_VERSION:
        raise LLMError(f"Agent decision version must be {AGENT_DECISION_VERSION}.")
    rationale = _required_bounded_string(data, "rationale", MAX_DECISION_TEXT_CHARS, "Agent decision")
    expected_evidence = _required_bounded_string(
        data,
        "expected_evidence",
        MAX_DECISION_TEXT_CHARS,
        "Agent decision",
    )
    finish_reason = _optional_bounded_string(
        data,
        "finish_reason",
        MAX_DECISION_TEXT_CHARS,
        "Agent decision",
    )
    user_question = _optional_bounded_string(
        data,
        "user_question",
        MAX_STATE_ITEM_CHARS,
        "Agent decision",
    )
    action = data.get("action")
    if not isinstance(action, dict):
        raise LLMError("Agent decision action must be an object.")
    _require_exact_keys(action, {"kind", "arguments"}, "Agent decision action")
    action_kind = action.get("kind")
    if action_kind not in ALLOWED_AGENT_DECISION_ACTIONS:
        raise LLMError(f"Invalid Agent decision action: {action_kind}")
    raw_arguments = action.get("arguments")
    if not isinstance(raw_arguments, dict):
        raise LLMError("Agent decision action arguments must be an object.")
    action_arguments = _parse_agent_decision_arguments(action_kind, raw_arguments)
    if action_kind == "finish" and not finish_reason:
        raise LLMError("finish decisions require a non-empty finish_reason.")
    if action_kind != "finish" and finish_reason:
        raise LLMError("finish_reason must be empty unless the action is finish.")
    raw_state_update = data.get("state_update")
    if not isinstance(raw_state_update, dict):
        raise LLMError("Agent decision state_update must be an object.")
    _require_exact_keys(raw_state_update, AGENT_STATE_UPDATE_KEYS, "Agent decision state_update")
    state_update = AgentStateUpdate(
        focus=_optional_bounded_string(
            raw_state_update,
            "focus",
            MAX_STATE_ITEM_CHARS,
            "Agent decision state_update",
        ),
        add_findings=_bounded_string_list(raw_state_update, "add_findings"),
        add_open_questions=_bounded_string_list(raw_state_update, "add_open_questions"),
        resolve_open_questions=_bounded_string_list(
            raw_state_update,
            "resolve_open_questions",
        ),
    )
    added_questions = {
        _normalized_text(item) for item in state_update.add_open_questions
    }
    resolved_questions = {
        _normalized_text(item) for item in state_update.resolve_open_questions
    }
    if added_questions & resolved_questions:
        raise LLMError(
            "Agent decision cannot add and resolve the same open question."
        )
    return AgentDecision(
        version=version,
        rationale=rationale,
        action_kind=action_kind,
        action_arguments=action_arguments,
        expected_evidence=expected_evidence,
        state_update=state_update,
        finish_reason=finish_reason,
        user_question=user_question,
    )


def parse_agent_action_json(response: str) -> AgentDecision:
    """Compatibility entry point for callers using the previous parser name."""

    return parse_agent_decision_json(response)


def _parse_agent_decision_arguments(
    action_kind: str,
    arguments: dict,
) -> dict:
    if action_kind == "search_files":
        _require_exact_keys(arguments, {"query"}, "search_files arguments")
        return {
            "query": _required_bounded_string(
                arguments,
                "query",
                MAX_STATE_ITEM_CHARS,
                "search_files arguments",
            )
        }
    if action_kind == "read_file":
        _require_exact_keys(arguments, {"path"}, "read_file arguments")
        path = _required_bounded_string(
            arguments,
            "path",
            MAX_STATE_ITEM_CHARS,
            "read_file arguments",
        )
        return {"path": _normalize_agent_path(path)}
    if action_kind == "inspect_repository_map":
        _reject_unknown_keys(
            arguments,
            {"query", "limit"},
            "inspect_repository_map arguments",
        )
        parsed: dict = {}
        if "query" in arguments:
            parsed["query"] = _optional_bounded_string(
                arguments,
                "query",
                MAX_STATE_ITEM_CHARS,
                "inspect_repository_map arguments",
            )
        if "limit" in arguments:
            limit = arguments["limit"]
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
                raise LLMError("inspect_repository_map limit must be an integer from 1 to 20.")
            parsed["limit"] = limit
        return parsed
    if action_kind == "inspect_git_status":
        _require_exact_keys(arguments, set(), "inspect_git_status arguments")
        return {}
    if action_kind == "finish":
        _require_exact_keys(arguments, {"selected_paths"}, "finish arguments")
        return {
            "selected_paths": _bounded_path_list(
                arguments,
                "selected_paths",
            )
        }
    raise LLMError(f"Agent decision action is not implemented: {action_kind}")


def _require_exact_keys(value: dict, expected: set[str], label: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise LLMError(f"{label} is missing required field(s): {', '.join(missing)}.")
    if unknown:
        raise LLMError(f"{label} contains unknown field(s): {', '.join(unknown)}.")


def _reject_unknown_keys(value: dict, allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise LLMError(f"{label} contains unknown field(s): {', '.join(unknown)}.")


def _required_bounded_string(
    value: dict,
    name: str,
    limit: int,
    label: str,
) -> str:
    parsed = _optional_bounded_string(value, name, limit, label)
    if not parsed:
        raise LLMError(f"{label} must include a non-empty {name}.")
    return parsed


def _optional_bounded_string(
    value: dict,
    name: str,
    limit: int,
    label: str,
) -> str:
    raw = value.get(name, "")
    if not isinstance(raw, str):
        raise LLMError(f"{label} {name} must be a string.")
    parsed = raw.strip()
    if len(parsed) > limit:
        raise LLMError(f"{label} {name} exceeds the {limit}-character limit.")
    return parsed


def _bounded_string_list(value: dict, name: str) -> list[str]:
    raw = value.get(name)
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise LLMError(f"Agent decision state_update {name} must be a list of strings.")
    if len(raw) > MAX_STATE_UPDATE_ITEMS:
        raise LLMError(
            f"Agent decision state_update {name} exceeds the "
            f"{MAX_STATE_UPDATE_ITEMS}-item limit."
        )
    parsed: list[str] = []
    seen: set[str] = set()
    for item in raw:
        normalized = " ".join(item.split())
        if not normalized:
            raise LLMError(f"Agent decision state_update {name} cannot contain empty items.")
        if len(normalized) > MAX_STATE_ITEM_CHARS:
            raise LLMError(
                f"Agent decision state_update {name} contains an item over "
                f"{MAX_STATE_ITEM_CHARS} characters."
            )
        key = normalized.casefold()
        if key not in seen:
            parsed.append(normalized)
            seen.add(key)
    return parsed


def _bounded_path_list(value: dict, name: str) -> list[str]:
    raw = value.get(name)
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise LLMError(f"Agent decision {name} must be a list of strings.")
    if len(raw) > MAX_DECISION_PATHS:
        raise LLMError(f"Agent decision {name} exceeds the {MAX_DECISION_PATHS}-path limit.")
    paths: list[str] = []
    seen: set[str] = set()
    for item in raw:
        path = _normalize_agent_path(item)
        if path not in seen:
            paths.append(path)
            seen.add(path)
    return paths


def _normalize_agent_path(value: str) -> str:
    path = value.strip()
    if not path:
        raise LLMError("Agent decision paths must not be empty.")
    if len(path) > MAX_STATE_ITEM_CHARS:
        raise LLMError(
            f"Agent decision path exceeds the {MAX_STATE_ITEM_CHARS}-character limit."
        )
    normalized = PurePosixPath(path.replace("\\", "/"))
    windows_path = PureWindowsPath(path)
    if (
        normalized.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or ".." in normalized.parts
        or any(part in {"", "."} for part in normalized.parts)
    ):
        raise LLMError(f"Unsafe Agent decision path: {value}")
    return normalized.as_posix()


def _normalized_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def parse_patch_proposal_json(response: str) -> dict:
    data = parse_json_object(response)
    objective = data.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        raise LLMError("Patch proposal JSON must include a non-empty objective.")

    raw_files = data.get("files")
    if not isinstance(raw_files, list):
        raise LLMError("Patch proposal JSON must include a files list.")
    files = [_parse_file_change(item) for item in raw_files]

    raw_risks = data.get("risks", [])
    if not isinstance(raw_risks, list):
        raise LLMError("Patch proposal risks must be a list.")
    risks = [_parse_risk(item) for item in raw_risks]

    raw_validation = data.get("validation_suggestions", [])
    if not isinstance(raw_validation, list) or not all(isinstance(item, str) for item in raw_validation):
        raise LLMError("Patch proposal validation_suggestions must be a list of strings.")

    ready_for_patch = data.get("ready_for_patch")
    if not isinstance(ready_for_patch, bool):
        raise LLMError("Patch proposal ready_for_patch must be a boolean.")

    raw_file_edits = data.get("file_edits", [])
    if not isinstance(raw_file_edits, list):
        raise LLMError("Patch proposal file_edits must be a list.")
    file_edits = [_parse_file_edit(item, files) for item in raw_file_edits]

    return {
        "objective": objective.strip(),
        "files": files,
        "risks": risks,
        "validation_suggestions": [item.strip() for item in raw_validation if item.strip()],
        "ready_for_patch": ready_for_patch,
        "file_edits": file_edits,
    }


def parse_patch_review_json(response: str, model: str | None = None) -> PatchReview:
    data = parse_json_object(response)
    summary = data.get("summary")
    risk_level = data.get("risk_level")
    concerns = data.get("concerns", [])
    suggested_tests = data.get("suggested_tests", [])
    approved_for_apply = data.get("approved_for_apply")

    if not isinstance(summary, str) or not summary.strip():
        raise LLMError("Patch review JSON must include a non-empty summary.")
    if risk_level not in ALLOWED_RISK_LEVELS:
        raise LLMError(f"Invalid patch review risk_level: {risk_level}")
    if not isinstance(concerns, list) or not all(isinstance(item, str) for item in concerns):
        raise LLMError("Patch review concerns must be a list of strings.")
    if not isinstance(suggested_tests, list) or not all(isinstance(item, str) for item in suggested_tests):
        raise LLMError("Patch review suggested_tests must be a list of strings.")
    if not isinstance(approved_for_apply, bool):
        raise LLMError("Patch review approved_for_apply must be a boolean.")

    return PatchReview(
        summary=summary.strip(),
        risk_level=risk_level,
        concerns=[item.strip() for item in concerns if item.strip()],
        suggested_tests=[item.strip() for item in suggested_tests if item.strip()],
        approved_for_apply=approved_for_apply,
        source="llm",
        model=model,
    )


def normalize_proposal_path(path: str) -> str:
    normalized = PurePosixPath(path.replace("\\", "/"))
    parts = normalized.parts
    if normalized.is_absolute() or ".." in parts or any(part in {"", "."} for part in parts):
        raise LLMError(f"Unsafe file edit path: {path}")
    return normalized.as_posix()


def _parse_file_change(item: object) -> FileChangeProposal:
    if not isinstance(item, dict):
        raise LLMError("Each patch proposal file entry must be an object.")
    path = item.get("path")
    change_type = item.get("change_type")
    rationale = item.get("rationale")
    suggested_actions = item.get("suggested_actions")
    confidence = item.get("confidence")

    if not isinstance(path, str) or not path.strip():
        raise LLMError("Each file proposal must include a non-empty path.")
    if change_type not in ALLOWED_CHANGE_TYPES:
        raise LLMError(f"Invalid change_type for {path}: {change_type}")
    if not isinstance(rationale, str) or not rationale.strip():
        raise LLMError(f"File proposal for {path} must include a non-empty rationale.")
    if not isinstance(suggested_actions, list) or not all(isinstance(action, str) for action in suggested_actions):
        raise LLMError(f"File proposal for {path} must include suggested_actions as strings.")
    if confidence not in ALLOWED_CONFIDENCE:
        raise LLMError(f"Invalid confidence for {path}: {confidence}")

    actions = [action.strip() for action in suggested_actions if action.strip()]
    if not actions:
        raise LLMError(f"File proposal for {path} must include at least one suggested action.")
    return FileChangeProposal(
        path=path.strip(),
        change_type=change_type,
        rationale=rationale.strip(),
        suggested_actions=actions,
        confidence=confidence,
    )


def _parse_file_edit(item: object, files: list[FileChangeProposal]) -> FileEditProposal:
    if not isinstance(item, dict):
        raise LLMError("Each file edit proposal must be an object.")
    path = item.get("path")
    new_content = item.get("new_content")
    rationale = item.get("rationale")
    if not isinstance(path, str) or not path.strip():
        raise LLMError("Each file edit proposal must include a non-empty path.")
    clean_path = normalize_proposal_path(path)
    known_paths = {file.path for file in files}
    if clean_path not in known_paths:
        raise LLMError(f"File edit path was not included in proposed files: {clean_path}")
    if not isinstance(new_content, str):
        raise LLMError(f"File edit for {clean_path} must include new_content as a string.")
    if not isinstance(rationale, str) or not rationale.strip():
        raise LLMError(f"File edit for {clean_path} must include a non-empty rationale.")
    return FileEditProposal(path=clean_path, new_content=new_content, rationale=rationale.strip())


def _parse_risk(item: object) -> RiskNote:
    if not isinstance(item, dict):
        raise LLMError("Each risk entry must be an object.")
    level = item.get("level")
    message = item.get("message")
    mitigation = item.get("mitigation")
    if level not in ALLOWED_RISK_LEVELS:
        raise LLMError(f"Invalid risk level: {level}")
    if not isinstance(message, str) or not message.strip():
        raise LLMError("Each risk must include a non-empty message.")
    if not isinstance(mitigation, str) or not mitigation.strip():
        raise LLMError("Each risk must include a non-empty mitigation.")
    return RiskNote(level=level, message=message.strip(), mitigation=mitigation.strip())
