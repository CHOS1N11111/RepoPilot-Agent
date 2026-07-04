"""Strict JSON schema parsing helpers for LLM outputs."""

from __future__ import annotations

from pathlib import PurePosixPath

from .base import LLMError
from .json_utils import parse_json_object
from ..models import (
    AgentAction,
    FileChangeProposal,
    FileEditProposal,
    PatchReview,
    PlanStep,
    RiskNote,
)

ALLOWED_CHANGE_TYPES = {"bugfix", "feature", "test", "documentation", "refinement"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
ALLOWED_RISK_LEVELS = {"high", "medium", "low"}
ALLOWED_AGENT_ACTIONS = {"search_files", "read_file", "inspect_git_status", "finish"}


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


def parse_agent_action_json(response: str) -> AgentAction:
    data = parse_json_object(response)
    thought = data.get("thought")
    action = data.get("action")
    if not isinstance(thought, str) or not thought.strip():
        raise LLMError("Agent action JSON must include a non-empty thought.")
    if action not in ALLOWED_AGENT_ACTIONS:
        raise LLMError(f"Invalid agent action: {action}")

    query = data.get("query", "")
    path = data.get("path", "")
    summary = data.get("summary", "")
    selected_paths = data.get("selected_paths", [])
    if not isinstance(query, str):
        raise LLMError("Agent action query must be a string when provided.")
    if not isinstance(path, str):
        raise LLMError("Agent action path must be a string when provided.")
    if not isinstance(summary, str):
        raise LLMError("Agent action summary must be a string when provided.")
    if not isinstance(selected_paths, list) or not all(isinstance(item, str) for item in selected_paths):
        raise LLMError("Agent action selected_paths must be a list of strings.")
    if action == "search_files" and not query.strip():
        raise LLMError("search_files action requires a non-empty query.")
    if action == "read_file" and not path.strip():
        raise LLMError("read_file action requires a non-empty path.")

    return AgentAction(
        thought=thought.strip(),
        action=action,
        query=query.strip(),
        path=path.strip(),
        summary=summary.strip(),
        selected_paths=[item.strip() for item in selected_paths if item.strip()],
    )


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
