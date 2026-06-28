"""File-level patch proposal generation.

The local MVP proposes change intent without applying edits. This keeps the
workflow safe while still moving from repository analysis toward implementation.
"""

from __future__ import annotations

from .models import FileChangeProposal, PatchProposal, RiskNote, SearchHit


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
