"""Deterministic task planning for the local MVP."""

from __future__ import annotations

from .models import PlanStep, SearchHit


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
