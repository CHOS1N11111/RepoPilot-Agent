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
    "You are RepoPilot Agent's non-writing repository analysis and proposed-edit loop. "
    "Choose exactly one next decision and return only JSON with this exact shape: "
    '{"version":2,"rationale":"why this action is useful",'
    '"action":{"kind":"search_files|read_file|inspect_repository_map|inspect_git_status|inspect_diff|propose_patch|inspect_proposed_diff|ask_user|finish",'
    '"arguments":{}},"expected_evidence":"what result would make this action useful",'
    '"state_update":{"focus":"current investigation focus","add_findings":[],'
    '"add_open_questions":[],"resolve_open_questions":[],'
    '"plan_updates":[{"step_id":"stable id","title":"short step","detail":"specific action",'
    '"status":"pending|in_progress|completed","evidence_action_ids":[]}],'
    '"acceptance_updates":[{"criterion_id":"stable id","kind":"analysis",'
    '"description":"observable completion condition","required":true,'
    '"evidence_action_ids":[],"evidence_summary":""}]},'
    '"finish_reason":"","user_question":""}. '
    "Action arguments must be exactly one of: search_files {query}; read_file {path}; "
    "inspect_repository_map with optional {query, limit}; inspect_git_status {}; "
    "inspect_diff with optional {staged}; propose_patch {path, expected_sha256, hunks}; "
    "inspect_proposed_diff {}; ask_user {}; "
    "finish {selected_paths}. "
    "propose_patch hunks contain old_text, new_text, and optional expected_occurrences. "
    "Use only non-writing actions. A proposed patch changes an in-memory overlay, never the repository. "
    "Read a file before its first proposal and use the read_file SHA-256. For a later revision, use the "
    "resulting virtual SHA-256 from the previous proposal. Inspect the cumulative proposed diff after the "
    "latest revision. If the real baseline becomes stale, re-read the file before proposing again. "
    "Only propose edits when the task asks for a repository change; explanation and inspection tasks do not need one. "
    "Always provide non-empty rationale and expected_evidence. "
    "Only add findings already supported by previous observations; expected future tool output is not a finding. "
    "Use plan_updates to add or revise implementation steps. A completed plan step must cite one or more "
    "previously completed evidence-producing action ids. Use acceptance_updates to add or revise criteria and "
    "attach evidence from previously completed evidence-producing action ids. Search candidates, the current "
    "action, user questions, and finish are not completion evidence. "
    "Keep evidence fields empty when no evidence has been observed. "
    "Use finish_reason only for finish, and set it to a non-empty completion summary. "
    "Keep finish_reason empty for all other actions. For ask_user, provide a non-empty user_question "
    "and add exactly that question to state_update.add_open_questions. Keep user_question empty for every other action. "
    "Only choose actions listed as available in the user prompt. "
    "For normal code, documentation, or explanation tasks, start by searching or reading files. "
    "Use inspect_git_status only when the task is about Git state, diffs, branches, delivery, or local changes. "
    "Treat search results as candidates; read important files before selecting them. "
    "Use finish only when Working State reports completion readiness."
)

AGENT_WRITE_SYSTEM_PROMPT = (
    AGENT_SYSTEM_PROMPT
    .replace(
        "non-writing repository analysis and proposed-edit loop",
        "approval-gated managed-worktree analysis and edit loop",
    )
    .replace(
        "search_files|read_file|inspect_repository_map|inspect_git_status|inspect_diff|propose_patch|inspect_proposed_diff|ask_user|finish",
        "search_files|read_file|inspect_repository_map|inspect_git_status|inspect_diff|propose_patch|inspect_proposed_diff|apply_patch|edit_file|ask_user|finish",
    )
    .replace(
        "inspect_proposed_diff {}; ask_user {}; ",
        "inspect_proposed_diff {}; apply_patch {path, expected_sha256, hunks}; "
        "edit_file {path, new_content}; ask_user {}; ",
    )
    .replace(
        "Use only non-writing actions. A proposed patch changes an in-memory overlay, never the repository. ",
        "Use write actions only after the latest virtual proposal was inspected. "
        "apply_patch or edit_file pauses for exact human approval and may write only the managed worktree. "
        "propose_patch still changes only an in-memory overlay. ",
    )
    + " A write action must reproduce the complete latest inspected virtual content exactly. "
    "Never request commands, validation, commits, pushes, branches, or pull requests."
)


def agent_system_prompt(allow_write_actions: bool = False) -> str:
    return AGENT_WRITE_SYSTEM_PROMPT if allow_write_actions else AGENT_SYSTEM_PROMPT


def build_planner_prompt(
    task: str,
    context: str,
    context_summary: str = "",
    memory_context: list[MemoryContextItem] | None = None,
    repository_map_context: str = "",
    agent_state_context: str = "",
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
            "Task-relevant repository map:",
            repository_map_context or "No repository map context was available.",
            "",
            "Agent plan and acceptance handoff:",
            _clip(agent_state_context, 6_000)
            if agent_state_context
            else "No iterative Agent state was provided.",
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
    repository_map_context: str = "",
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
            "Task-relevant repository map:",
            repository_map_context or "No repository map context was available.",
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
    managed_context: str,
    step_number: int,
    max_steps: int,
    allow_user_questions: bool = False,
    allow_write_actions: bool = False,
) -> str:
    actions = [
        "- search_files: find repo files by task-focused query.",
        "- read_file: inspect one repo-relative file returned by search or initial context.",
        "- inspect_repository_map: inspect task-relevant symbols, dependencies, and source/test relations.",
        "- inspect_git_status: inspect local branch, changes, and diff stats for Git-related tasks.",
        "- inspect_diff: inspect the current working-tree or staged diff.",
        "- propose_patch: apply exact hunks to a run-scoped in-memory overlay without writing the repository.",
        "- inspect_proposed_diff: review the cumulative virtual diff and recheck real baseline hashes.",
    ]
    if allow_write_actions:
        actions.extend(
            [
                "- apply_patch: request approval for exact hunks that reproduce an inspected virtual proposal in the managed worktree.",
                "- edit_file: request approval for complete file content that reproduces an inspected virtual proposal in the managed worktree.",
            ]
        )
    if allow_user_questions:
        actions.append(
            "- ask_user: pause when essential information cannot be obtained from repository tools."
        )
    actions.append("- finish: stop exploration and select the files most useful for planning/proposal.")
    return "\n".join(
        [
            f"Task: {task}",
            f"Step: {step_number} of {max_steps}",
            "",
            "Available managed-worktree actions:"
            if allow_write_actions
            else "Available non-writing actions:",
            *actions,
            "",
            "Managed context packet:",
            managed_context or "No managed context is available.",
            "",
            (
                "Choose the single next decision that will most improve repository understanding. "
                "For non-Git tasks, prefer search_files or read_file before inspect_git_status. "
                "Use inspect_diff when exact changed lines matter. Ask the user only when the answer is essential "
                "and cannot be obtained with the available repository tools. "
                "For change tasks, read the target before propose_patch, then inspect_proposed_diff after the latest revision. "
                + (
                    "After inspection, use apply_patch or edit_file only when the requested write exactly matches that virtual revision; "
                    "the controller will pause for human approval before writing. "
                    if allow_write_actions
                    else ""
                )
                + "Prefer finish if the useful files are already known. Update focus and open questions "
                "conservatively, revise the plan when evidence changes the approach, and only record findings "
                "or completion evidence supported by previous observations and their action ids."
            ),
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
