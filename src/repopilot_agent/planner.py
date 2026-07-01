"""Task planning with optional LLM support and deterministic fallback."""

from __future__ import annotations

from .context_builder import PLANNER_CONTEXT_BUDGET, build_context_packet
from .llm.base import LLMClient, LLMError, LLMMessage
from .llm.prompts import PLAN_SYSTEM_PROMPT, build_planner_prompt
from .llm.schema import parse_plan_steps_json
from .llm.tracing import record_llm_fallback, traced_llm_json_call
from .models import LLMCallTrace, MemoryContextItem, PlanMetadata, PlanStep, SearchHit


def create_plan(
    task: str,
    hits: list[SearchHit],
    memory_context: list[MemoryContextItem] | None = None,
) -> list[PlanStep]:
    task_lower = task.lower()
    focus = ", ".join(hit.path for hit in hits[:3]) if hits else "the repository structure"
    related_memory = memory_context or []
    plan: list[PlanStep] = []

    def append_step(title: str, detail: str) -> None:
        plan.append(PlanStep(order=len(plan) + 1, title=title, detail=detail))

    append_step("Clarify task intent", f"Interpret the request and identify the expected behavior: {task}")
    append_step("Inspect relevant context", f"Review likely relevant files and surrounding code: {focus}.")
    if related_memory:
        memory_summary = "; ".join(
            f"{item.task} ({'applied' if item.applied else 'open'}, score {item.score})"
            for item in related_memory[:3]
        )
        append_step(
            "Review related memory",
            f"Compare against previous related runs before changing code: {memory_summary}.",
        )

    if any(keyword in task_lower for keyword in ("bug", "fix", "error", "fail", "broken")):
        append_step(
            "Reproduce or isolate the failure",
            "Use targeted tests, logs, or a minimal scenario to confirm the current behavior.",
        )
    elif any(keyword in task_lower for keyword in ("feature", "add", "implement", "support")):
        append_step(
            "Design the implementation",
            "Identify the smallest cohesive change that adds the requested behavior.",
        )
    else:
        append_step(
            "Define the change boundary",
            "Decide which modules should change and which existing behavior must remain stable.",
        )

    append_step(
        "Prepare a focused patch",
        "Make the smallest code change that satisfies the task while following existing project conventions.",
    )
    append_step(
        "Validate the result",
        "Run the safest relevant validation commands and inspect failures before summarizing.",
    )
    append_step(
        "Summarize implementation",
        "Report changed files, validation results, risks, and suggested follow-up work.",
    )
    return plan


def create_plan_with_optional_llm(
    task: str,
    hits: list[SearchHit],
    llm_client: LLMClient | None = None,
    allow_fallback: bool = True,
    traces: list[LLMCallTrace] | None = None,
    memory_context: list[MemoryContextItem] | None = None,
) -> tuple[list[PlanStep], PlanMetadata]:
    if llm_client is None:
        return create_plan(task, hits, memory_context=memory_context), PlanMetadata(source="rules")

    try:
        plan = _create_llm_plan(task, hits, llm_client, traces, memory_context=memory_context)
    except LLMError as exc:
        if not allow_fallback:
            raise
        context_summary = build_context_packet(hits, budget=PLANNER_CONTEXT_BUDGET).summary
        record_llm_fallback(traces, "planner", llm_client.model, str(exc), context_summary=context_summary)
        return (
            create_plan(task, hits, memory_context=memory_context),
            PlanMetadata(source="rules", model=llm_client.model, fallback_used=True, error=str(exc)),
        )
    return plan, PlanMetadata(source="llm", model=llm_client.model)


def _create_llm_plan(
    task: str,
    hits: list[SearchHit],
    llm_client: LLMClient,
    traces: list[LLMCallTrace] | None = None,
    memory_context: list[MemoryContextItem] | None = None,
) -> list[PlanStep]:
    context_packet = build_context_packet(hits, budget=PLANNER_CONTEXT_BUDGET)
    return traced_llm_json_call(
        "planner",
        llm_client,
        [
            LLMMessage(role="system", content=PLAN_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=build_planner_prompt(
                    task,
                    context_packet.text,
                    context_packet.summary,
                    memory_context=memory_context,
                ),
            ),
        ],
        parse_plan_steps_json,
        traces,
        context_summary=context_packet.summary,
    )
