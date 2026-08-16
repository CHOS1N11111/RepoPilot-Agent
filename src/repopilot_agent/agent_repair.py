"""Bounded repair transitions for the unified Agent controller."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import ValidationResult
from .repair_loop import (
    STOP_NO_PROPOSAL,
    STOP_NO_REPOSITORY_CHANGE,
    STOP_REPAIR_BUDGET,
    RepairAttemptRecord,
    mark_repair_attempt_stopped,
    record_agent_repair_proposal,
    record_validation_outcome,
    repair_proposal_fingerprints,
)


@dataclass(frozen=True)
class AgentRepairTransition:
    history: list[RepairAttemptRecord]
    attempt: int
    max_attempts: int
    status: str
    repair_required: bool
    trigger_failure_fingerprint: str = ""
    stop_reason: str | None = None
    message: str = ""

    @property
    def remaining_attempts(self) -> int:
        return max(self.max_attempts - self.attempt, 0)

    @property
    def next_attempt(self) -> int | None:
        if not self.repair_required or self.stop_reason or self.remaining_attempts <= 0:
            return None
        return self.attempt + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "remaining_attempts": self.remaining_attempts,
            "next_attempt": self.next_attempt,
            "status": self.status,
            "repair_required": self.repair_required,
            "trigger_failure_fingerprint": self.trigger_failure_fingerprint,
            "stop_reason": self.stop_reason,
            "message": self.message,
            "history": [item.to_dict() for item in self.history],
        }


def observe_agent_validation(
    history: list[RepairAttemptRecord],
    *,
    attempt: int,
    max_attempts: int,
    validation: list[ValidationResult],
    summary: str,
) -> AgentRepairTransition:
    current, limit = _validate_attempts(attempt, max_attempts)
    updated, decision = record_validation_outcome(
        history,
        attempt=current,
        validation=validation,
        summary=summary,
    )
    if not decision.accepted:
        return AgentRepairTransition(
            updated,
            current,
            limit,
            "stopped",
            False,
            decision.fingerprint,
            decision.stop_reason,
            decision.message,
        )
    if not decision.fingerprint:
        return AgentRepairTransition(
            updated,
            current,
            limit,
            "validation_passed",
            False,
            message=decision.message,
        )
    if current >= limit:
        message = (
            f"Validation failed after repair attempt {current}, and the configured "
            f"repair budget of {limit} attempt(s) is exhausted."
        )
        return AgentRepairTransition(
            mark_repair_attempt_stopped(updated, attempt=current, summary=message),
            current,
            limit,
            "stopped",
            False,
            decision.fingerprint,
            STOP_REPAIR_BUDGET,
            message,
        )
    return AgentRepairTransition(
        updated,
        current,
        limit,
        "repair_required",
        True,
        decision.fingerprint,
        message=decision.message,
    )


def observe_agent_repair_proposal(
    history: list[RepairAttemptRecord],
    *,
    attempt: int,
    max_attempts: int,
    trigger_failure_fingerprint: str,
    proposal_fingerprint: str,
    proposal_paths: list[str],
    summary: str,
) -> AgentRepairTransition:
    current, limit = _validate_attempts(attempt, max_attempts)
    if current <= 0:
        raise ValueError("Agent repair proposal attempts must start at 1.")
    if current > limit:
        return stop_agent_repair(
            history,
            attempt=current,
            max_attempts=limit,
            reason=STOP_REPAIR_BUDGET,
            message=f"Repair attempt {current} exceeds the configured limit of {limit}.",
            trigger_failure_fingerprint=trigger_failure_fingerprint,
        )
    updated, decision = record_agent_repair_proposal(
        history,
        attempt=current,
        trigger_failure_fingerprint=trigger_failure_fingerprint,
        proposal_fingerprint=proposal_fingerprint,
        proposal_paths=proposal_paths,
        summary=summary,
    )
    return AgentRepairTransition(
        updated,
        current,
        limit,
        "proposal_ready" if decision.accepted else "stopped",
        False,
        trigger_failure_fingerprint,
        decision.stop_reason,
        decision.message,
    )


def observe_agent_write(
    history: list[RepairAttemptRecord],
    *,
    attempt: int,
    max_attempts: int,
    changed_paths: list[str],
) -> AgentRepairTransition:
    current, limit = _validate_attempts(attempt, max_attempts)
    paths = list(
        dict.fromkeys(
            path.strip().replace("\\", "/")
            for path in changed_paths
            if path.strip()
        )
    )
    if paths:
        return AgentRepairTransition(
            list(history),
            current,
            limit,
            "write_applied",
            False,
            message=f"Approved write changed {len(paths)} repository file(s).",
        )
    message = "The approved Agent write made no repository change; repair stopped without progress."
    return AgentRepairTransition(
        mark_repair_attempt_stopped(history, attempt=current, summary=message),
        current,
        limit,
        "stopped",
        False,
        stop_reason=STOP_NO_REPOSITORY_CHANGE,
        message=message,
    )


def stop_agent_repair(
    history: list[RepairAttemptRecord],
    *,
    attempt: int,
    max_attempts: int,
    reason: str,
    message: str,
    trigger_failure_fingerprint: str = "",
) -> AgentRepairTransition:
    current, limit = _validate_attempts(attempt, max_attempts)
    normalized_reason = reason.strip() or STOP_NO_PROPOSAL
    normalized_message = message.strip() or "The unified repair controller stopped."
    return AgentRepairTransition(
        mark_repair_attempt_stopped(
            history,
            attempt=current,
            summary=normalized_message,
            trigger_failure_fingerprint=trigger_failure_fingerprint,
        ),
        current,
        limit,
        "stopped",
        False,
        stop_reason=normalized_reason,
        message=normalized_message,
    )


def render_agent_repair_context(transition: AgentRepairTransition) -> str:
    previous = [
        item
        for item in transition.history
        if item.attempt > 0 and (item.proposal_fingerprint or item.result_failure_fingerprint)
    ]
    lines = [
        "Mode: unified post-validation repair",
        f"Validated attempt: {transition.attempt}/{transition.max_attempts}",
        f"Remaining new repair attempts: {transition.remaining_attempts}",
        f"Current failure fingerprint: {_short(transition.trigger_failure_fingerprint)}",
        "Every materially new write must stop for exact human approval.",
        "Do not repeat an earlier repair proposal; inspect the failure and produce a materially different revision.",
    ]
    if previous:
        lines.append("Prior repair outcomes:")
        for item in previous[-5:]:
            lines.append(
                f"- Attempt {item.attempt} [{item.status}]: proposal "
                f"{_short(item.proposal_fingerprint)}, result "
                f"{_short(item.result_failure_fingerprint)}, paths "
                f"{', '.join(item.proposal_paths) or 'none'}"
            )
    else:
        lines.append("Prior repair outcomes: none")
    return "\n".join(lines)


def blocked_agent_repair_fingerprints(history: list[RepairAttemptRecord]) -> set[str]:
    return repair_proposal_fingerprints(history)


def _validate_attempts(attempt: int, max_attempts: int) -> tuple[int, int]:
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
        raise ValueError("Agent repair attempt must be a non-negative integer.")
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 0:
        raise ValueError("Agent repair limit must be a non-negative integer.")
    return attempt, max_attempts


def _short(value: str) -> str:
    return value[:12] if value else "none"
