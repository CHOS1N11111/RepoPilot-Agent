"""File-level patch proposal generation.

The local MVP proposes change intent without applying edits. This keeps the
workflow safe while still moving from repository analysis toward implementation.
"""

from __future__ import annotations

import difflib

from .llm.base import LLMClient, LLMError, LLMMessage
from .llm.prompts import PATCH_REVIEW_SYSTEM_PROMPT, PATCH_SYSTEM_PROMPT, build_patch_prompt, build_patch_review_prompt
from .llm.schema import parse_patch_proposal_json, parse_patch_review_json
from .llm.tracing import record_llm_fallback, traced_llm_json_call
from .models import (
    FileChangeProposal,
    FileEditProposal,
    LLMCallTrace,
    PatchProposal,
    PatchProposalMetadata,
    PatchReview,
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
    file_contents: dict[str, str] | None = None,
    traces: list[LLMCallTrace] | None = None,
) -> tuple[PatchProposal, PatchProposalMetadata]:
    if llm_client is None:
        return propose_patch(task, hits), PatchProposalMetadata(source="rules")

    try:
        proposal = _propose_llm_patch(task, hits, plan, llm_client, file_contents or {}, traces)
    except LLMError as exc:
        if not allow_fallback:
            raise
        record_llm_fallback(traces, "patch_proposal", llm_client.model, str(exc))
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
    file_contents: dict[str, str],
    traces: list[LLMCallTrace] | None = None,
) -> PatchProposal:
    parsed = traced_llm_json_call(
        "patch_proposal",
        llm_client,
        [
            LLMMessage(role="system", content=PATCH_SYSTEM_PROMPT),
            LLMMessage(role="user", content=build_patch_prompt(task, hits, plan, file_contents)),
        ],
        parse_patch_proposal_json,
        traces,
    )
    proposed_diff = _build_proposed_diff(parsed["file_edits"], file_contents)
    return PatchProposal(
        objective=parsed["objective"],
        files=parsed["files"],
        risks=parsed["risks"],
        validation_suggestions=parsed["validation_suggestions"],
        ready_for_patch=parsed["ready_for_patch"],
        file_edits=parsed["file_edits"],
        proposed_diff=proposed_diff,
        apply_ready=bool(parsed["file_edits"]),
    )


def review_patch_with_optional_llm(
    task: str,
    proposal: PatchProposal,
    llm_client: LLMClient | None = None,
    allow_fallback: bool = True,
    traces: list[LLMCallTrace] | None = None,
) -> PatchReview | None:
    if llm_client is None or not proposal.proposed_diff:
        return None
    try:
        return traced_llm_json_call(
            "patch_review",
            llm_client,
            [
                LLMMessage(role="system", content=PATCH_REVIEW_SYSTEM_PROMPT),
                LLMMessage(
                    role="user",
                    content=build_patch_review_prompt(
                        task,
                        proposal.proposed_diff,
                        proposal.validation_suggestions,
                    ),
                ),
            ],
            lambda response: parse_patch_review_json(response, model=llm_client.model),
            traces,
        )
    except LLMError as exc:
        if not allow_fallback:
            raise
        record_llm_fallback(traces, "patch_review", llm_client.model, str(exc))
        return PatchReview(
            summary="LLM patch review was unavailable; rely on manual review before applying.",
            risk_level="medium",
            concerns=[str(exc)],
            suggested_tests=proposal.validation_suggestions,
            approved_for_apply=False,
            source="rules",
            model=llm_client.model,
            fallback_used=True,
            error=str(exc),
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


def _build_proposed_diff(file_edits: list[FileEditProposal], original_contents: object) -> str:
    if not file_edits or not isinstance(original_contents, dict):
        return ""
    chunks: list[str] = []
    for edit in file_edits:
        original = original_contents.get(edit.path)
        if not isinstance(original, str):
            continue
        chunks.extend(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                edit.new_content.splitlines(keepends=True),
                fromfile=f"a/{edit.path}",
                tofile=f"b/{edit.path}",
                lineterm="",
            )
        )
    return "\n".join(chunks)
