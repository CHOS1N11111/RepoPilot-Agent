"""File-level patch proposal generation.

The local MVP proposes change intent without applying edits. This keeps the
workflow safe while still moving from repository analysis toward implementation.
"""

from __future__ import annotations

from .llm.base import LLMClient, LLMError, LLMMessage
from .llm.json_utils import parse_json_object
from .models import (
    FileChangeProposal,
    PatchProposal,
    PatchProposalMetadata,
    PlanStep,
    RiskNote,
    SearchHit,
)

ALLOWED_CHANGE_TYPES = {"bugfix", "feature", "test", "documentation", "refinement"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
ALLOWED_RISK_LEVELS = {"high", "medium", "low"}


def propose_patch(task: str, hits: list[SearchHit], max_files: int = 4) -> PatchProposal:
    selected_hits = hits[:max_files]
    objective = _build_objective(task)
    files = [_propose_file_change(task, hit) for hit in selected_hits]
    risks = _build_risks(task, files)
    validation_suggestions = _build_validation_suggestions(task, files)
    return PatchProposal(
        objective=objective,
        files=files,
        risks=risks,
        validation_suggestions=validation_suggestions,
        ready_for_patch=bool(files),
    )


def propose_patch_with_optional_llm(
    task: str,
    hits: list[SearchHit],
    plan: list[PlanStep],
    llm_client: LLMClient | None = None,
    allow_fallback: bool = True,
) -> tuple[PatchProposal, PatchProposalMetadata]:
    if llm_client is None:
        return propose_patch(task, hits), PatchProposalMetadata(source="rules")

    try:
        proposal = _propose_llm_patch(task, hits, plan, llm_client)
    except LLMError as exc:
        if not allow_fallback:
            raise
        return (
            propose_patch(task, hits),
            PatchProposalMetadata(source="rules", model=llm_client.model, fallback_used=True, error=str(exc)),
        )
    return proposal, PatchProposalMetadata(source="llm", model=llm_client.model)


def _propose_llm_patch(
    task: str,
    hits: list[SearchHit],
    plan: list[PlanStep],
    llm_client: LLMClient,
) -> PatchProposal:
    response = llm_client.complete(
        [
            LLMMessage(
                role="system",
                content=(
                    "You are RepoPilot Agent's patch proposal module. "
                    "Return only JSON with this exact shape: "
                    '{"objective":"...","files":[{"path":"...","change_type":"bugfix|feature|test|documentation|refinement",'
                    '"rationale":"...","suggested_actions":["..."],"confidence":"high|medium|low"}],'
                    '"risks":[{"level":"low|medium|high","message":"...","mitigation":"..."}],'
                    '"validation_suggestions":["..."],"ready_for_patch":true}. '
                    "Do not generate a diff. Propose file-level implementation intent only."
                ),
            ),
            LLMMessage(role="user", content=_build_patch_prompt(task, hits, plan)),
        ]
    )
    data = parse_json_object(response)
    return _parse_patch_proposal(data)


def _parse_patch_proposal(data: dict) -> PatchProposal:
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

    return PatchProposal(
        objective=objective.strip(),
        files=files,
        risks=risks,
        validation_suggestions=[item.strip() for item in raw_validation if item.strip()],
        ready_for_patch=ready_for_patch,
    )


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


def _build_patch_prompt(task: str, hits: list[SearchHit], plan: list[PlanStep]) -> str:
    plan_lines = [f"{step.order}. {step.title}: {step.detail}" for step in plan]
    hit_blocks = []
    for hit in hits[:6]:
        hit_blocks.append(
            "\n".join(
                [
                    f"Path: {hit.path}",
                    f"Score: {hit.score}",
                    f"Reasons: {', '.join(hit.reasons)}",
                    f"Preview:\n{hit.preview[:1200]}",
                ]
            )
        )
    context = "\n\n---\n\n".join(hit_blocks) if hit_blocks else "No relevant files were selected."
    return "\n".join(
        [
            f"Task: {task}",
            "",
            "Implementation plan:",
            "\n".join(plan_lines),
            "",
            "Relevant repository context:",
            context,
            "",
            "Propose concrete file-level changes. Do not invent file paths that are not in the context unless a new test file is clearly needed.",
        ]
    )


def _build_objective(task: str) -> str:
    return f"Prepare a focused implementation for: {task}"


def _propose_file_change(task: str, hit: SearchHit) -> FileChangeProposal:
    task_lower = task.lower()
    change_type = _infer_change_type(task_lower, hit.path)
    actions = _suggest_actions(task_lower, hit)
    confidence = _confidence_from_score(hit.score)
    rationale = (
        f"`{hit.path}` matched the task context with score {hit.score}. "
        f"Signals: {', '.join(hit.reasons)}."
    )
    return FileChangeProposal(
        path=hit.path,
        change_type=change_type,
        rationale=rationale,
        suggested_actions=actions,
        confidence=confidence,
    )


def _infer_change_type(task_lower: str, path: str) -> str:
    lower_path = path.lower()
    if lower_path.startswith("tests/") or "test" in lower_path:
        return "test"
    if any(keyword in task_lower for keyword in ("doc", "readme", "documentation")):
        return "documentation"
    if any(keyword in task_lower for keyword in ("bug", "fix", "error", "fail", "broken")):
        return "bugfix"
    if any(keyword in task_lower for keyword in ("feature", "add", "implement", "support")):
        return "feature"
    return "refinement"


def _suggest_actions(task_lower: str, hit: SearchHit) -> list[str]:
    actions = [
        "Inspect the matched code path and confirm the behavior boundary.",
        "Make the smallest cohesive change in this file if the preview confirms relevance.",
    ]
    lower_path = hit.path.lower()
    if lower_path.startswith("tests/") or "test" in lower_path:
        actions = [
            "Add or update tests that capture the expected behavior.",
            "Keep test names tied to the user-facing scenario from the task.",
        ]
    elif any(keyword in task_lower for keyword in ("bug", "fix", "error", "fail", "broken")):
        actions.append("Add a regression test before or alongside the fix.")
    elif any(keyword in task_lower for keyword in ("feature", "add", "implement", "support")):
        actions.append("Expose the new behavior through the nearest existing public interface.")

    if hit.preview:
        actions.append("Use the matched preview as the first inspection point.")
    return actions


def _confidence_from_score(score: int) -> str:
    if score >= 10:
        return "high"
    if score >= 5:
        return "medium"
    return "low"


def _build_risks(task: str, files: list[FileChangeProposal]) -> list[RiskNote]:
    risks: list[RiskNote] = []
    if not files:
        return [
            RiskNote(
                level="medium",
                message="No relevant files were found for the task.",
                mitigation="Refine the task wording or expand repository indexing before generating a patch.",
            )
        ]

    if all(file.confidence == "low" for file in files):
        risks.append(
            RiskNote(
                level="medium",
                message="All proposed files have low confidence.",
                mitigation="Review broader repository context before applying any patch.",
            )
        )

    if not any(file.change_type == "test" for file in files):
        risks.append(
            RiskNote(
                level="low",
                message="No test file was selected in the proposal.",
                mitigation="Add or update tests if the change affects executable behavior.",
            )
        )

    if any(keyword in task.lower() for keyword in ("auth", "security", "permission", "token")):
        risks.append(
            RiskNote(
                level="high",
                message="The task may touch authentication, authorization, or sensitive data handling.",
                mitigation="Require focused review and run security-relevant tests before applying changes.",
            )
        )

    return risks


def _build_validation_suggestions(task: str, files: list[FileChangeProposal]) -> list[str]:
    suggestions = ["Run the narrowest relevant test command after preparing a patch."]
    if any(file.path.endswith(".py") for file in files):
        suggestions.append("python -m unittest discover -s tests")
    if any(file.path.endswith((".js", ".jsx", ".ts", ".tsx")) for file in files):
        suggestions.append("npm test")
    if any(file.change_type == "documentation" for file in files) or "readme" in task.lower():
        suggestions.append("Review rendered documentation for clarity and accuracy.")
    return suggestions
