"""Prompt templates for RepoPilot LLM modules."""

from __future__ import annotations

from ..models import PlanStep, SearchHit


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


def build_planner_prompt(task: str, hits: list[SearchHit]) -> str:
    context_lines = []
    for hit in hits[:6]:
        context_lines.append(
            "\n".join(
                [
                    f"Path: {hit.path}",
                    f"Score: {hit.score}",
                    f"Reasons: {', '.join(hit.reasons)}",
                    f"Preview:\n{hit.preview[:1200]}",
                ]
            )
        )
    context = "\n\n---\n\n".join(context_lines) if context_lines else "No relevant files were selected."
    return "\n".join(
        [
            f"Task: {task}",
            "",
            "Relevant repository context:",
            context,
            "",
            "Generate a concrete implementation plan that a developer can follow.",
        ]
    )


def build_patch_prompt(
    task: str,
    hits: list[SearchHit],
    plan: list[PlanStep],
    file_contents: dict[str, str] | None = None,
) -> str:
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
                    f"Current file content:\n{(file_contents or {}).get(hit.path, '')[:12000]}",
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
            "Propose concrete file-level changes. Only include file_edits for paths shown in the context. "
            "When editing a file, return its complete post-edit content in new_content.",
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
