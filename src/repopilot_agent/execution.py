"""Acceptance, budget, and completion contracts for validation-aware task execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .models import ValidationResult


DEFAULT_EXECUTION_TIMEOUT_SECONDS = 600
DEFAULT_MAX_TOOL_CALLS = 12
DEFAULT_MAX_VALIDATION_COMMANDS = 4


@dataclass(frozen=True)
class AcceptanceCriterion:
    criterion_id: str
    kind: str
    description: str
    required: bool = True
    evidence_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CriterionEvidence:
    criterion_id: str
    status: str
    summary: str
    sources: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExecutionBudget:
    max_agent_steps: int = 6
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    max_validation_commands: int = DEFAULT_MAX_VALIDATION_COMMANDS
    max_elapsed_seconds: int = DEFAULT_EXECUTION_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"Execution budget {name} must be a positive integer.")

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> "ExecutionBudget":
        values = data if isinstance(data, dict) else {}
        return cls(
            max_agent_steps=_positive_int(values.get("max_agent_steps"), 6),
            max_tool_calls=_positive_int(values.get("max_tool_calls"), DEFAULT_MAX_TOOL_CALLS),
            max_validation_commands=_positive_int(
                values.get("max_validation_commands"),
                DEFAULT_MAX_VALIDATION_COMMANDS,
            ),
            max_elapsed_seconds=_positive_int(
                values.get("max_elapsed_seconds"),
                DEFAULT_EXECUTION_TIMEOUT_SECONDS,
            ),
        )


@dataclass(frozen=True)
class ExecutionUsage:
    agent_steps: int = 0
    tool_calls: int = 0
    validation_commands: int = 0
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> "ExecutionUsage":
        values = data if isinstance(data, dict) else {}
        return cls(
            agent_steps=_nonnegative_int(values.get("agent_steps")),
            tool_calls=_nonnegative_int(values.get("tool_calls")),
            validation_commands=_nonnegative_int(values.get("validation_commands")),
            elapsed_ms=_nonnegative_int(values.get("elapsed_ms")),
        )

    def add(
        self,
        *,
        agent_steps: int = 0,
        tool_calls: int = 0,
        validation_commands: int = 0,
        elapsed_ms: int = 0,
    ) -> "ExecutionUsage":
        return ExecutionUsage(
            agent_steps=self.agent_steps + max(agent_steps, 0),
            tool_calls=self.tool_calls + max(tool_calls, 0),
            validation_commands=self.validation_commands + max(validation_commands, 0),
            elapsed_ms=self.elapsed_ms + max(elapsed_ms, 0),
        )


@dataclass(frozen=True)
class CompletionEvidence:
    status: str
    summary: str
    criteria: list[CriterionEvidence]
    changed_files: list[str] = field(default_factory=list)
    validation_commands: list[str] = field(default_factory=list)
    diff_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_acceptance_criteria(
    task: str,
    proposed_paths: list[str],
    validation_commands: list[str],
) -> list[AcceptanceCriterion]:
    paths = _dedupe(proposed_paths)
    commands = _dedupe(validation_commands)
    if not paths:
        return [
            AcceptanceCriterion(
                criterion_id="analysis_complete",
                kind="analysis",
                description=f"Repository analysis addresses the task: {_clip(task)}",
            )
        ]

    criteria = [
        AcceptanceCriterion(
            criterion_id="task_change",
            kind="change",
            description=f"At least one approved repository change addresses: {_clip(task)}",
        ),
        AcceptanceCriterion(
            criterion_id="approval_scope",
            kind="safety",
            description="Every changed file is included in the human-approved proposal scope.",
        ),
    ]
    for index, command in enumerate(commands, start=1):
        criteria.append(
            AcceptanceCriterion(
                criterion_id=f"validation_{index}",
                kind="validation",
                description=f"Validation passes: {command}",
                evidence_ref=command,
            )
        )
    if not commands:
        criteria.append(
            AcceptanceCriterion(
                criterion_id="manual_validation",
                kind="manual",
                description="No automated validation command was available; inspect the diff manually.",
                required=False,
            )
        )
    return criteria


def pending_completion_evidence(criteria: list[AcceptanceCriterion]) -> CompletionEvidence:
    if criteria and all(item.kind == "analysis" for item in criteria):
        return CompletionEvidence(
            status="passed",
            summary="Repository analysis completed without an apply-ready change.",
            criteria=[
                CriterionEvidence(item.criterion_id, "passed", "Repository analysis completed.")
                for item in criteria
            ],
        )
    return CompletionEvidence(
        status="pending",
        summary="Waiting for approved edits and validation evidence.",
        criteria=[
            CriterionEvidence(item.criterion_id, "pending", "Evidence has not been collected yet.")
            for item in criteria
        ],
    )


def evaluate_completion(
    criteria: list[AcceptanceCriterion],
    *,
    changed_files: list[str],
    approved_paths: list[str],
    validation: list[ValidationResult],
    diff: str,
) -> CompletionEvidence:
    changed = _dedupe(changed_files)
    approved = set(approved_paths)
    validation_by_command = {result.command: result for result in validation}
    evidence: list[CriterionEvidence] = []

    for criterion in criteria:
        if criterion.kind == "change":
            status = "passed" if changed else "failed"
            summary = (
                f"Applied changes to {len(changed)} approved file(s)."
                if changed
                else "No repository file content changed."
            )
            evidence.append(CriterionEvidence(criterion.criterion_id, status, summary, changed))
            continue
        if criterion.kind == "safety":
            unexpected = [path for path in changed if path not in approved]
            status = "passed" if not unexpected else "failed"
            summary = (
                "All changed files were inside the approved scope."
                if not unexpected
                else f"Unexpected changed files: {', '.join(unexpected)}."
            )
            evidence.append(CriterionEvidence(criterion.criterion_id, status, summary, changed))
            continue
        if criterion.kind == "validation":
            command = criterion.evidence_ref or criterion.description.removeprefix("Validation passes: ")
            result = validation_by_command.get(command)
            if result is None:
                evidence.append(
                    CriterionEvidence(
                        criterion.criterion_id,
                        "pending",
                        "The required validation command was not run.",
                        [command],
                    )
                )
            elif result.allowed and result.exit_code == 0:
                evidence.append(
                    CriterionEvidence(
                        criterion.criterion_id,
                        "passed",
                        "Validation completed with exit code 0.",
                        [command],
                    )
                )
            else:
                detail = "rejected by allowlist" if not result.allowed else f"exit code {result.exit_code}"
                evidence.append(
                    CriterionEvidence(
                        criterion.criterion_id,
                        "failed",
                        f"Validation did not pass: {detail}.",
                        [command],
                    )
                )
            continue
        if criterion.kind == "manual":
            evidence.append(
                CriterionEvidence(
                    criterion.criterion_id,
                    "not_run",
                    "Automated evidence is unavailable; manual diff review remains recommended.",
                )
            )
            continue
        evidence.append(
            CriterionEvidence(
                criterion.criterion_id,
                "passed",
                "Repository analysis completed.",
            )
        )

    by_id = {item.criterion_id: item for item in evidence}
    required = [item for item in criteria if item.required]
    failed = [item for item in required if by_id[item.criterion_id].status == "failed"]
    pending = [item for item in required if by_id[item.criterion_id].status == "pending"]
    if failed:
        status = "failed"
        summary = f"{len(failed)} required acceptance criterion/criteria failed."
    elif pending:
        status = "pending"
        summary = f"{len(pending)} required acceptance criterion/criteria still need evidence."
    else:
        status = "passed"
        summary = f"All {len(required)} required acceptance criterion/criteria passed."
    return CompletionEvidence(
        status=status,
        summary=summary,
        criteria=evidence,
        changed_files=changed,
        validation_commands=[result.command for result in validation],
        diff_available=bool(diff.strip()),
    )


def execution_budget_state(budget: ExecutionBudget, usage: ExecutionUsage) -> dict[str, Any]:
    elapsed_limit_ms = budget.max_elapsed_seconds * 1000
    remaining = {
        "agent_steps": max(budget.max_agent_steps - usage.agent_steps, 0),
        "tool_calls": max(budget.max_tool_calls - usage.tool_calls, 0),
        "validation_commands": max(budget.max_validation_commands - usage.validation_commands, 0),
        "elapsed_ms": max(elapsed_limit_ms - usage.elapsed_ms, 0),
    }
    exhausted_reasons: list[str] = []
    if usage.agent_steps > budget.max_agent_steps:
        exhausted_reasons.append("agent step budget exceeded")
    if usage.tool_calls > budget.max_tool_calls:
        exhausted_reasons.append("tool call budget exceeded")
    if usage.validation_commands > budget.max_validation_commands:
        exhausted_reasons.append("validation command budget exceeded")
    if usage.elapsed_ms > elapsed_limit_ms:
        exhausted_reasons.append("elapsed time budget exceeded")
    return {
        "limits": budget.to_dict(),
        "usage": usage.to_dict(),
        "remaining": remaining,
        "exhausted": bool(exhausted_reasons),
        "exhausted_reasons": exhausted_reasons,
    }


def criteria_from_records(records: object) -> list[AcceptanceCriterion]:
    if not isinstance(records, list):
        return []
    criteria: list[AcceptanceCriterion] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        criterion_id = str(record.get("criterion_id") or "").strip()
        description = str(record.get("description") or "").strip()
        if not criterion_id or not description:
            continue
        criteria.append(
            AcceptanceCriterion(
                criterion_id=criterion_id,
                kind=str(record.get("kind") or "analysis"),
                description=description,
                required=bool(record.get("required", True)),
                evidence_ref=(
                    str(record.get("evidence_ref")).strip()
                    if record.get("evidence_ref") is not None
                    else None
                ),
            )
        )
    return criteria


def completion_from_record(record: object) -> CompletionEvidence | None:
    if not isinstance(record, dict):
        return None
    raw_criteria = record.get("criteria")
    criteria: list[CriterionEvidence] = []
    if isinstance(raw_criteria, list):
        for item in raw_criteria:
            if not isinstance(item, dict):
                continue
            criteria.append(
                CriterionEvidence(
                    criterion_id=str(item.get("criterion_id") or ""),
                    status=str(item.get("status") or "pending"),
                    summary=str(item.get("summary") or ""),
                    sources=_string_list(item.get("sources")),
                )
            )
    return CompletionEvidence(
        status=str(record.get("status") or "pending"),
        summary=str(record.get("summary") or ""),
        criteria=criteria,
        changed_files=_string_list(record.get("changed_files")),
        validation_commands=_string_list(record.get("validation_commands")),
        diff_available=bool(record.get("diff_available")),
    )


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _nonnegative_int(value: object) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        clean = item.strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result


def _clip(text: str, limit: int = 500) -> str:
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."
