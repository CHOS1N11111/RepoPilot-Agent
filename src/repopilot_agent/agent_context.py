"""Deterministic, section-budgeted context assembly for Agent decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .execution import AcceptanceCriterion
from .models import AgentStep, MemoryContextItem, SearchHit
from .runtime.state import AgentWorkingState, render_agent_working_state


SECTION_TRUNCATION_MARKER = "\n[...section truncated...]"
PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.DOTALL,
)
SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|authorization|password|"
    r"client[_-]?secret|refresh[_-]?token|github[_-]?token|openai[_-]?api[_-]?key)"
    r"\b\s*[\"']?\s*(?::|=(?!=))"
)
TOKEN_VALUE_PATTERN = re.compile(
    r"(?i)\b(?:sk-[a-z0-9_-]{8,}|gh[pousr]_[a-z0-9_]{8,}|github_pat_[a-z0-9_]{8,})\b"
)
BEARER_TOKEN_PATTERN = re.compile(r"(?i)\bBearer\s+[a-z0-9._~+/-]{8,}")


@dataclass(frozen=True)
class AgentContextBudget:
    max_chars: int = 20_000
    working_state_chars: int = 2_400
    remaining_budget_chars: int = 700
    acceptance_criteria_chars: int = 1_400
    pinned_memory_chars: int = 1_800
    repository_map_chars: int = 2_500
    current_diff_chars: int = 2_400
    recent_observations_chars: int = 4_200
    older_evidence_chars: int = 1_600
    initial_context_chars: int = 2_500
    recent_observation_count: int = 3

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"Agent context budget {name} must be a positive integer.")


@dataclass(frozen=True)
class AgentContextSection:
    name: str
    title: str
    priority: int
    limit_chars: int
    original_chars: int
    included_chars: int
    truncated: bool
    omitted: bool
    redacted: bool


@dataclass(frozen=True)
class AgentContextPacket:
    text: str
    summary: str
    sections: list[AgentContextSection]
    omitted_sections: list[str]


AGENT_CONTEXT_BUDGET = AgentContextBudget()


def build_agent_context_packet(
    working_state: AgentWorkingState,
    initial_hits: list[SearchHit],
    steps: list[AgentStep],
    *,
    repository_map_context: str = "",
    memory_context: list[MemoryContextItem] | None = None,
    current_diff: str = "",
    acceptance_criteria: list[AcceptanceCriterion] | None = None,
    remaining_budget: dict[str, int] | None = None,
    budget: AgentContextBudget = AGENT_CONTEXT_BUDGET,
) -> AgentContextPacket:
    """Build the bounded context packet supplied to one Agent decision call."""

    recent_count = min(len(steps), budget.recent_observation_count)
    older_steps = steps[:-recent_count] if recent_count else list(steps)
    recent_steps = steps[-recent_count:] if recent_count else []
    raw_sections = [
        (
            "working_state",
            "Agent Working State",
            1,
            budget.working_state_chars,
            render_agent_working_state(working_state),
        ),
        (
            "remaining_budget",
            "Remaining Execution Budget",
            2,
            budget.remaining_budget_chars,
            _render_remaining_budget(remaining_budget),
        ),
        (
            "acceptance_criteria",
            "Acceptance Criteria",
            3,
            budget.acceptance_criteria_chars,
            _render_acceptance_criteria(acceptance_criteria),
        ),
        (
            "pinned_memory",
            "Pinned Memory",
            4,
            budget.pinned_memory_chars,
            _render_pinned_memory(memory_context),
        ),
        (
            "repository_map",
            "Task-Relevant Repository Map",
            5,
            budget.repository_map_chars,
            repository_map_context or "No repository map context is available.",
        ),
        (
            "current_diff",
            "Current Git Diff",
            6,
            budget.current_diff_chars,
            current_diff or "No current staged or unstaged Git diff is available.",
        ),
        (
            "recent_observations",
            "Recent Detailed Observations",
            7,
            budget.recent_observations_chars,
            _render_recent_observations(recent_steps),
        ),
        (
            "older_evidence",
            "Summarized Older Evidence",
            8,
            budget.older_evidence_chars,
            _render_older_evidence(older_steps),
        ),
        (
            "initial_context",
            "Initial Ranked Repository Context",
            9,
            budget.initial_context_chars,
            _render_initial_context(initial_hits),
        ),
    ]
    return _assemble_sections(raw_sections, budget.max_chars)


def redact_context_secrets(text: str) -> str:
    """Redact common credential assignments and complete private-key blocks."""

    without_keys = PRIVATE_KEY_PATTERN.sub("[REDACTED PRIVATE KEY]", text)
    without_tokens = TOKEN_VALUE_PATTERN.sub("[REDACTED TOKEN]", without_keys)
    without_tokens = BEARER_TOKEN_PATTERN.sub("Bearer [REDACTED TOKEN]", without_tokens)
    redacted_lines: list[str] = []
    for line in without_tokens.splitlines(keepends=True):
        match = SENSITIVE_ASSIGNMENT_PATTERN.search(line)
        if match is None:
            redacted_lines.append(line)
            continue
        newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        redacted_lines.append(f"{line[:match.end()]} [REDACTED]{newline}")
    return "".join(redacted_lines)


def _assemble_sections(
    raw_sections: list[tuple[str, str, int, int, str]],
    max_chars: int,
) -> AgentContextPacket:
    blocks: list[str] = []
    sections: list[AgentContextSection] = []
    omitted_sections: list[str] = []
    used_chars = 0

    for name, title, priority, section_limit, raw_content in raw_sections:
        content = str(raw_content or "").strip()
        redacted_content = redact_context_secrets(content)
        redacted = redacted_content != content
        separator_chars = 2 if blocks else 0
        header = f"## {title}\n"
        available = max_chars - used_chars - separator_chars - len(header)
        if available <= 0:
            omitted_sections.append(name)
            sections.append(
                AgentContextSection(
                    name=name,
                    title=title,
                    priority=priority,
                    limit_chars=section_limit,
                    original_chars=len(redacted_content),
                    included_chars=0,
                    truncated=bool(redacted_content),
                    omitted=True,
                    redacted=redacted,
                )
            )
            continue

        included = _clip_text(redacted_content, min(section_limit, available))
        block = f"{header}{included}"
        blocks.append(block)
        used_chars += separator_chars + len(block)
        sections.append(
            AgentContextSection(
                name=name,
                title=title,
                priority=priority,
                limit_chars=section_limit,
                original_chars=len(redacted_content),
                included_chars=len(included),
                truncated=len(included) < len(redacted_content),
                omitted=False,
                redacted=redacted,
            )
        )

    text = "\n\n".join(blocks)
    return AgentContextPacket(
        text=text,
        summary=_build_packet_summary(max_chars, text, sections),
        sections=sections,
        omitted_sections=omitted_sections,
    )


def _render_remaining_budget(remaining: dict[str, int] | None) -> str:
    values = remaining or {}
    labels = [
        ("agent_steps", "Agent steps"),
        ("tool_calls", "Tool calls"),
        ("validation_commands", "Validation commands"),
        ("elapsed_ms", "Elapsed time (ms)"),
    ]
    return "\n".join(
        f"- {label}: {max(_as_int(values.get(name)), 0)} remaining"
        for name, label in labels
    )


def _render_acceptance_criteria(
    criteria: list[AcceptanceCriterion] | None,
) -> str:
    if not criteria:
        return "No acceptance criteria are available yet."
    lines: list[str] = []
    for criterion in criteria:
        if isinstance(criterion, dict):
            criterion_id = str(criterion.get("criterion_id") or "criterion")
            kind = str(criterion.get("kind") or "unknown")
            description = str(criterion.get("description") or "No description.")
            required = bool(criterion.get("required", True))
            evidence_ref = str(criterion.get("evidence_ref") or "")
        else:
            criterion_id = criterion.criterion_id
            kind = criterion.kind
            description = criterion.description
            required = criterion.required
            evidence_ref = criterion.evidence_ref or ""
        evidence = f"; evidence: {evidence_ref}" if evidence_ref else ""
        lines.append(
            f"- {criterion_id} [{kind}, {'required' if required else 'optional'}]: "
            f"{description}{evidence}"
        )
    return "\n".join(lines)


def _render_pinned_memory(memory_context: list[MemoryContextItem] | None) -> str:
    pinned = [item for item in memory_context or [] if item.pinned][:3]
    if not pinned:
        return "No pinned memory was selected."
    lines: list[str] = []
    for item in pinned:
        validation = "; ".join(item.validation[:3]) or "none"
        lines.append(
            f"- {item.task} ({item.mode}, {'applied' if item.applied else 'open'}). "
            f"Summary: {_single_line(item.summary, 500)} Validation: {validation}."
        )
    return "\n".join(lines)


def _render_recent_observations(steps: list[AgentStep]) -> str:
    if not steps:
        return "No recent observations are available."
    blocks: list[str] = []
    for step in steps:
        blocks.append(
            "\n".join(
                [
                    f"Step {step.order}: {step.action}",
                    f"Rationale: {step.thought}",
                    f"Input: {step.tool_input or '(none)'}",
                    f"Expected evidence: {step.expected_evidence or '(none)'}",
                    f"Observation:\n{step.observation or '(none)'}",
                ]
            )
        )
    return "\n\n---\n\n".join(blocks)


def _render_older_evidence(steps: list[AgentStep]) -> str:
    if not steps:
        return "No older evidence needed summarization."
    return "\n".join(
        f"- Step {step.order} {step.action}; input={_single_line(step.tool_input or '(none)', 120)}; "
        f"observed={_single_line(step.observation or '(none)', 260)}"
        for step in steps
    )


def _render_initial_context(hits: list[SearchHit]) -> str:
    if not hits:
        return "No initial repository context was selected."
    blocks: list[str] = []
    for hit in hits[:5]:
        blocks.append(
            "\n".join(
                [
                    f"Path: {hit.path}",
                    f"Score: {hit.score}",
                    f"Reasons: {', '.join(hit.reasons) or 'none'}",
                    f"Preview: {_clip_text(hit.preview, 700) or '(empty)'}",
                ]
            )
        )
    return "\n\n---\n\n".join(blocks)


def _build_packet_summary(
    max_chars: int,
    text: str,
    sections: list[AgentContextSection],
) -> str:
    details: list[str] = []
    for section in sections:
        if section.omitted:
            state = "omitted"
        elif section.truncated:
            state = "truncated"
        else:
            state = "full"
        if section.redacted:
            state = f"{state}, redacted"
        details.append(
            f"{section.name} {section.included_chars}/{section.limit_chars} chars ({state})"
        )
    return (
        f"Agent context: {len(text)}/{max_chars} chars. Sections: "
        f"{'; '.join(details)}."
    )


def _clip_text(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(SECTION_TRUNCATION_MARKER):
        return text[:limit]
    return text[: limit - len(SECTION_TRUNCATION_MARKER)] + SECTION_TRUNCATION_MARKER


def _single_line(text: str, limit: int) -> str:
    return " ".join(_clip_text(text, limit).split())


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
