"""Task planning with optional LLM support and deterministic fallback."""

from __future__ import annotations

from .llm.base import LLMClient, LLMError, LLMMessage
from .llm.json_utils import parse_json_object
from .models import PlanMetadata, PlanStep, SearchHit


def create_plan(task: str, hits: list[SearchHit]) -> list[PlanStep]:
    task_lower = task.lower()
    focus = ", ".join(hit.path for hit in hits[:3]) if hits else "the repository structure"
    plan = [
        PlanStep(
            order=1,
            title="Clarify task intent",
            detail=f"Interpret the request and identify the expected behavior: {task}",
        ),
        PlanStep(
            order=2,
            title="Inspect relevant context",
            detail=f"Review likely relevant files and surrounding code: {focus}.",
        ),
    ]

    if any(keyword in task_lower for keyword in ("bug", "fix", "error", "fail", "broken")):
        plan.append(
            PlanStep(
                order=3,
                title="Reproduce or isolate the failure",
                detail="Use targeted tests, logs, or a minimal scenario to confirm the current behavior.",
            )
        )
    elif any(keyword in task_lower for keyword in ("feature", "add", "implement", "support")):
        plan.append(
            PlanStep(
                order=3,
                title="Design the implementation",
                detail="Identify the smallest cohesive change that adds the requested behavior.",
            )
        )
    else:
        plan.append(
            PlanStep(
                order=3,
                title="Define the change boundary",
                detail="Decide which modules should change and which existing behavior must remain stable.",
            )
        )

    plan.extend(
        [
            PlanStep(
                order=4,
                title="Prepare a focused patch",
                detail="Make the smallest code change that satisfies the task while following existing project conventions.",
            ),
            PlanStep(
                order=5,
                title="Validate the result",
                detail="Run the safest relevant validation commands and inspect failures before summarizing.",
            ),
            PlanStep(
                order=6,
                title="Summarize implementation",
                detail="Report changed files, validation results, risks, and suggested follow-up work.",
            ),
        ]
    )
    return plan


def create_plan_with_optional_llm(
    task: str,
    hits: list[SearchHit],
    llm_client: LLMClient | None = None,
    allow_fallback: bool = True,
) -> tuple[list[PlanStep], PlanMetadata]:
    if llm_client is None:
        return create_plan(task, hits), PlanMetadata(source="rules")

    try:
        plan = _create_llm_plan(task, hits, llm_client)
    except LLMError as exc:
        if not allow_fallback:
            raise
        return (
            create_plan(task, hits),
            PlanMetadata(source="rules", model=llm_client.model, fallback_used=True, error=str(exc)),
        )
    return plan, PlanMetadata(source="llm", model=llm_client.model)


def _create_llm_plan(task: str, hits: list[SearchHit], llm_client: LLMClient) -> list[PlanStep]:
    response = llm_client.complete(
        [
            LLMMessage(
                role="system",
                content=(
                    "You are RepoPilot Agent's planning module. "
                    "Return only JSON with this shape: "
                    '{"steps":[{"title":"short title","detail":"specific engineering action"}]}. '
                    "Create 4 to 8 practical software engineering steps. "
                    "Do not include markdown or extra prose."
                ),
            ),
            LLMMessage(role="user", content=_build_planner_prompt(task, hits)),
        ]
    )
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


def _build_planner_prompt(task: str, hits: list[SearchHit]) -> str:
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
