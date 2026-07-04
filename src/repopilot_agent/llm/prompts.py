"""Prompt templates for RepoPilot LLM modules."""

from __future__ import annotations

from ..models import MemoryContextItem, PlanStep


PLAN_SYSTEM_PROMPT = (
    "You are RepoPilot Agent's planning module. "
    "Return only JSON with this shape: "
    '{"steps":[{"title":"short title","detail":"specific engineering action"}]}. '
    "Create 4 to 8 practical software engineering steps. "
    "Do not include markdown or extra prose."
)

PATCH_SYSTEM_PROMPT = (
    "You are RepoPilot Agent's patch proposal module. "
    "Return only JSON with this exact shape: "
    '{"objective":"...","files":[{"path":"...","change_type":"bugfix|feature|test|documentation|refinement",'
    '"rationale":"...","suggested_actions":["..."],"confidence":"high|medium|low"}],'
    '"risks":[{"level":"low|medium|high","message":"...","mitigation":"..."}],'
    '"validation_suggestions":["..."],"ready_for_patch":true,'
    '"file_edits":[{"path":"...","new_content":"complete file content after edit","rationale":"..."}]}. '
    "For file_edits, include complete replacement content for existing context files only. "
    "Use an empty file_edits list if you are not confident enough to edit."
)

PATCH_REVIEW_SYSTEM_PROMPT = (
    "You are RepoPilot Agent's patch review module. "
    "Review the proposed diff against the task and return only JSON with this exact shape: "
    '{"summary":"...","risk_level":"low|medium|high","concerns":["..."],'
    '"suggested_tests":["..."],"approved_for_apply":true}. '
    "Do not approve if the diff appears unrelated, unsafe, or unsupported by context."
)

AGENT_SYSTEM_PROMPT = (
    "You are RepoPilot Agent's read-only repository exploration loop. "
    "Choose exactly one next action and return only JSON with this shape: "
    '{"thought":"why this action is useful","action":"search_files|read_file|inspect_git_status|finish",'
    '"query":"search query if action is search_files","path":"repo-relative path if action is read_file",'
    '"selected_paths":["repo-relative paths useful for the final proposal"],"summary":"brief finish summary"}. '
    "Use only read-only actions. Do not propose file edits here. "
    "Use finish once enough context has been gathered."
)


def build_planner_prompt(
    task: str,
    context: str,
    context_summary: str = "",
    memory_context: list[MemoryContextItem] | None = None,
) -> str:
    return "\n".join(
        [
            f"Task: {task}",
            "",
            "Context budget summary:",
            context_summary or "No context budget summary was provided.",
            "",
            "Pinned memory:",
            _format_memory_context(_filter_memory(memory_context, pinned=True), "No pinned memory was selected."),
            "",
            "Related memory:",
            _format_memory_context(
                _filter_memory(memory_context, pinned=False),
                "No related memory was found.",
            ),
            "",
            "Relevant repository context:",
            context,
            "",
            "Generate a concrete implementation plan that a developer can follow.",
        ]
    )


def build_patch_prompt(
    task: str,
    plan: list[PlanStep],
    context: str,
    context_summary: str = "",
    editable_paths: list[str] | None = None,
) -> str:
    plan_lines = [f"{step.order}. {step.title}: {step.detail}" for step in plan]
    editable = ", ".join(editable_paths or []) or "none"
    return "\n".join(
        [
            f"Task: {task}",
            "",
            "Implementation plan:",
            "\n".join(plan_lines),
            "",
            "Context budget summary:",
            context_summary or "No context budget summary was provided.",
            "",
            "Files eligible for direct file_edits:",
            editable,
            "",
            "Relevant repository context:",
            context,
            "",
            "Propose concrete file-level changes. Only include file_edits for paths shown in the context. "
            "Only include file_edits for paths listed as eligible for direct file_edits. "
            "When editing a file, return its complete post-edit content in new_content. "
            "If a relevant file is not eligible for direct edits, describe suggested_actions instead.",
        ]
    )


def build_patch_review_prompt(task: str, proposed_diff: str, validation_suggestions: list[str]) -> str:
    return "\n".join(
        [
            f"Task: {task}",
            "",
            "Proposed diff:",
            proposed_diff[:20000] or "No proposed diff.",
            "",
            "Validation suggestions:",
            "\n".join(f"- {item}" for item in validation_suggestions) or "No validation suggestions.",
            "",
            "Review whether the diff is focused, relevant, and safe enough for user-approved application.",
        ]
    )


def build_agent_prompt(
    task: str,
    initial_context: str,
    observations: str,
    step_number: int,
    max_steps: int,
) -> str:
    return "\n".join(
        [
            f"Task: {task}",
            f"Step: {step_number} of {max_steps}",
            "",
            "Available read-only actions:",
            "- search_files: find repo files by task-focused query.",
            "- read_file: inspect one repo-relative file returned by search or initial context.",
            "- inspect_git_status: inspect local branch, changes, and diff stats.",
            "- finish: stop exploration and select the files most useful for planning/proposal.",
            "",
            "Initial ranked context:",
            initial_context or "No initial context was selected.",
            "",
            "Previous observations:",
            observations or "No previous observations.",
            "",
            "Choose the single next action that will most improve repository understanding. "
            "Prefer finish if the useful files are already known.",
        ]
    )


def _filter_memory(memory_context: list[MemoryContextItem] | None, pinned: bool) -> list[MemoryContextItem]:
    return [item for item in memory_context or [] if item.pinned is pinned]


def _format_memory_context(memory_context: list[MemoryContextItem] | None, empty_message: str) -> str:
    if not memory_context:
        return empty_message
    lines = []
    for item in memory_context[:3]:
        status_parts = []
        if item.pinned:
            status_parts.append("pinned")
        status_parts.append("applied" if item.applied else "open")
        status = ", ".join(status_parts)
        reasons = "; ".join(item.reasons[:3]) or f"score {item.score}"
        validation = "; ".join(item.validation[:3]) if item.validation else "no saved validation"
        lines.append(
            "- "
            f"{item.task} ({item.mode}, {status}, score {item.score}). "
            f"Reasons: {reasons}. "
            f"Summary: {_clip(item.summary)} "
            f"Validation: {validation}."
        )
    return "\n".join(lines)


def _clip(text: str, limit: int = 500) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
