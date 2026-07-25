"""In-memory web workflow sessions for proposal approval."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from .execution import (
    AcceptanceCriterion,
    CompletionEvidence,
    ExecutionBudget,
    ExecutionUsage,
    build_acceptance_criteria,
    completion_from_record,
    criteria_from_records,
    execution_budget_state,
    pending_completion_evidence,
)
from .models import FileEditProposal, ValidationFailureDetail, ValidationFeedback, ValidationResult
from .patch_apply import FileRollbackSnapshot
from .repair_loop import RepairAttemptRecord, repair_attempts_from_records
from .structured_patch import StructuredPatch, build_structured_patch, structured_patch_from_record


DEFAULT_MAX_REPAIR_ATTEMPTS = 2


@dataclass(frozen=True)
class TimelineEvent:
    step: str
    status: str
    detail: str


@dataclass
class ProposalSession:
    proposal_id: str
    repo_path: str
    task: str
    file_edits: list[FileEditProposal]
    validation_commands: list[str]
    created_at: str
    parent_proposal_id: str | None = None
    repair_attempt: int = 0
    max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS
    allowed_paths: list[str] = field(default_factory=list)
    approved_paths: list[str] = field(default_factory=list)
    applied_paths: list[str] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)
    applied: bool = False
    reverted: bool = False
    rollback_snapshot: list[FileRollbackSnapshot] = field(default_factory=list)
    validation: list[ValidationResult] = field(default_factory=list)
    validation_feedback: ValidationFeedback | None = None
    structured_patches: list[StructuredPatch] = field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)
    execution_budget: ExecutionBudget = field(default_factory=ExecutionBudget)
    execution_usage: ExecutionUsage = field(default_factory=ExecutionUsage)
    completion_evidence: CompletionEvidence | None = None
    root_task: str = ""
    repair_history: list[RepairAttemptRecord] = field(default_factory=list)
    repair_stop_reason: str | None = None
    repair_stop_message: str = ""
    auto_repair_enabled: bool = False

    def repair_budget_remaining(self) -> int:
        return max(self.max_repair_attempts - self.repair_attempt, 0)

    def repair_budget_exhausted(self) -> bool:
        return self.validation_feedback is not None and self.repair_budget_remaining() <= 0

    def next_repair_attempt(self) -> int | None:
        if self.repair_budget_remaining() <= 0:
            return None
        return self.repair_attempt + 1

    def to_public_dict(self) -> dict[str, Any]:
        rollback_available = bool(self.applied and not self.reverted and self.rollback_snapshot)
        return {
            "proposal_id": self.proposal_id,
            "parent_proposal_id": self.parent_proposal_id,
            "repo_path": self.repo_path,
            "task": self.task,
            "created_at": self.created_at,
            "applied": self.applied,
            "reverted": self.reverted,
            "rollback_available": rollback_available,
            "repair_attempt": self.repair_attempt,
            "max_repair_attempts": self.max_repair_attempts,
            "repair_budget_remaining": self.repair_budget_remaining(),
            "next_repair_attempt": self.next_repair_attempt(),
            "repair_budget_exhausted": self.repair_budget_exhausted(),
            "allowed_paths": self.allowed_paths,
            "approved_paths": self.approved_paths,
            "applied_paths": self.applied_paths,
            "timeline": [asdict(event) for event in self.timeline],
            "validation": [asdict(result) for result in self.validation],
            "validation_feedback": asdict(self.validation_feedback) if self.validation_feedback else None,
            "structured_patches": [
                {
                    "path": patch.path,
                    "expected_sha256": patch.expected_sha256,
                    "hunk_count": len(patch.hunks),
                }
                for patch in self.structured_patches
            ],
            "acceptance_criteria": [criterion.to_dict() for criterion in self.acceptance_criteria],
            "execution_budget": execution_budget_state(self.execution_budget, self.execution_usage),
            "completion_evidence": (
                self.completion_evidence.to_dict() if self.completion_evidence else None
            ),
            "root_task": self.root_task or self.task,
            "repair_history": [item.to_dict() for item in self.repair_history],
            "repair_stop_reason": self.repair_stop_reason,
            "repair_stop_message": self.repair_stop_message,
            "auto_repair_enabled": self.auto_repair_enabled,
        }


_SESSIONS: dict[str, ProposalSession] = {}


def create_proposal_session(
    repo_path: str,
    task: str,
    file_edits: list[FileEditProposal],
    validation_commands: list[str],
    timeline: list[TimelineEvent],
    allowed_paths: list[str] | None = None,
    parent_proposal_id: str | None = None,
    repair_attempt: int = 0,
    max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS,
    acceptance_criteria: list[AcceptanceCriterion] | None = None,
    execution_budget: ExecutionBudget | None = None,
    execution_usage: ExecutionUsage | None = None,
    root_task: str | None = None,
    repair_history: list[RepairAttemptRecord] | None = None,
    repair_stop_reason: str | None = None,
    repair_stop_message: str = "",
    auto_repair_enabled: bool = False,
) -> ProposalSession:
    proposal_id = uuid4().hex
    criteria = acceptance_criteria or build_acceptance_criteria(
        task,
        [edit.path for edit in file_edits],
        validation_commands,
    )
    budget = execution_budget or ExecutionBudget()
    session = ProposalSession(
        proposal_id=proposal_id,
        repo_path=repo_path,
        task=task,
        file_edits=file_edits,
        validation_commands=validation_commands,
        created_at=datetime.now(timezone.utc).isoformat(),
        parent_proposal_id=parent_proposal_id,
        repair_attempt=max(_int_value(repair_attempt, 0), 0),
        max_repair_attempts=max(_int_value(max_repair_attempts, DEFAULT_MAX_REPAIR_ATTEMPTS), 0),
        allowed_paths=allowed_paths or [edit.path for edit in file_edits],
        timeline=timeline,
        structured_patches=_build_session_structured_patches(repo_path, file_edits),
        acceptance_criteria=criteria,
        execution_budget=budget,
        execution_usage=execution_usage or ExecutionUsage(),
        completion_evidence=pending_completion_evidence(criteria),
        root_task=(root_task or task).strip(),
        repair_history=list(repair_history or []),
        repair_stop_reason=repair_stop_reason,
        repair_stop_message=repair_stop_message,
        auto_repair_enabled=auto_repair_enabled,
    )
    _SESSIONS[proposal_id] = session
    return session


def get_proposal_session(proposal_id: str) -> ProposalSession | None:
    return _SESSIONS.get(proposal_id)


def cache_proposal_session(session: ProposalSession) -> ProposalSession:
    _SESSIONS[session.proposal_id] = session
    return session


def clear_proposal_sessions() -> None:
    _SESSIONS.clear()


def proposal_session_to_record(session: ProposalSession) -> dict[str, Any]:
    return {
        "proposal_id": session.proposal_id,
        "parent_proposal_id": session.parent_proposal_id,
        "repo_path": session.repo_path,
        "task": session.task,
        "file_edits": [asdict(edit) for edit in session.file_edits],
        "validation_commands": session.validation_commands,
        "created_at": session.created_at,
        "repair_attempt": session.repair_attempt,
        "max_repair_attempts": session.max_repair_attempts,
        "allowed_paths": session.allowed_paths,
        "approved_paths": session.approved_paths,
        "applied_paths": session.applied_paths,
        "timeline": [asdict(event) for event in session.timeline],
        "applied": session.applied,
        "reverted": session.reverted,
        "rollback_snapshot": [asdict(snapshot) for snapshot in session.rollback_snapshot],
        "validation": [asdict(result) for result in session.validation],
        "validation_feedback": asdict(session.validation_feedback) if session.validation_feedback else None,
        "structured_patches": [patch.to_dict() for patch in session.structured_patches],
        "acceptance_criteria": [criterion.to_dict() for criterion in session.acceptance_criteria],
        "execution_budget": session.execution_budget.to_dict(),
        "execution_usage": session.execution_usage.to_dict(),
        "completion_evidence": (
            session.completion_evidence.to_dict() if session.completion_evidence else None
        ),
        "root_task": session.root_task or session.task,
        "repair_history": [item.to_dict() for item in session.repair_history],
        "repair_stop_reason": session.repair_stop_reason,
        "repair_stop_message": session.repair_stop_message,
        "auto_repair_enabled": session.auto_repair_enabled,
    }


def proposal_session_from_record(record: dict[str, Any]) -> ProposalSession:
    session = ProposalSession(
        proposal_id=str(record["proposal_id"]),
        repo_path=str(record["repo_path"]),
        task=str(record["task"]),
        file_edits=[_file_edit(item) for item in record.get("file_edits", [])],
        validation_commands=_string_list(record.get("validation_commands", [])),
        created_at=str(record.get("created_at") or datetime.now(timezone.utc).isoformat()),
        parent_proposal_id=_optional_string(record.get("parent_proposal_id")),
        repair_attempt=max(_int_value(record.get("repair_attempt"), 0), 0),
        max_repair_attempts=max(
            _int_value(record.get("max_repair_attempts"), DEFAULT_MAX_REPAIR_ATTEMPTS),
            0,
        ),
        allowed_paths=_string_list(record.get("allowed_paths", [])),
        approved_paths=_string_list(record.get("approved_paths", [])),
        applied_paths=_string_list(record.get("applied_paths", [])),
        timeline=[_timeline_event(item) for item in record.get("timeline", [])],
        applied=bool(record.get("applied")),
        reverted=bool(record.get("reverted")),
        rollback_snapshot=[_rollback_snapshot(item) for item in record.get("rollback_snapshot", [])],
        validation=[_validation_result(item) for item in record.get("validation", [])],
        validation_feedback=_validation_feedback(record.get("validation_feedback")),
        structured_patches=[
            structured_patch_from_record(item)
            for item in record.get("structured_patches", [])
            if isinstance(item, dict)
        ],
        acceptance_criteria=criteria_from_records(record.get("acceptance_criteria")),
        execution_budget=ExecutionBudget.from_dict(record.get("execution_budget")),
        execution_usage=ExecutionUsage.from_dict(record.get("execution_usage")),
        completion_evidence=completion_from_record(record.get("completion_evidence")),
        root_task=str(record.get("root_task") or record.get("task") or ""),
        repair_history=repair_attempts_from_records(record.get("repair_history")),
        repair_stop_reason=_optional_string(record.get("repair_stop_reason")),
        repair_stop_message=str(record.get("repair_stop_message") or ""),
        auto_repair_enabled=bool(record.get("auto_repair_enabled", False)),
    )
    if not session.acceptance_criteria:
        session.acceptance_criteria = build_acceptance_criteria(
            session.task,
            [edit.path for edit in session.file_edits],
            session.validation_commands,
        )
    if session.completion_evidence is None:
        session.completion_evidence = pending_completion_evidence(session.acceptance_criteria)
    if not session.structured_patches and not session.applied:
        session.structured_patches = _build_session_structured_patches(
            session.repo_path,
            session.file_edits,
        )
    return cache_proposal_session(session)


def build_report_timeline(report: Any, proposal_id: str | None = None) -> list[TimelineEvent]:
    events = [
        TimelineEvent("scan", "done", f"Scanned {report.files_scanned} text file(s)."),
        TimelineEvent("search", "done", f"Selected {len(report.relevant_files)} relevant file(s)."),
    ]
    agent_steps = getattr(report, "agent_steps", [])
    if agent_steps:
        events.append(TimelineEvent("agent", "done", f"Completed {len(agent_steps)} typed runtime step(s)."))
    else:
        events.append(TimelineEvent("agent", "skipped", "Iterative agent was not run."))
    events.extend(
        [
            TimelineEvent("plan", "done", f"Plan source: {report.plan_metadata.source}."),
            TimelineEvent("proposal", "done", f"Proposal source: {report.patch_proposal_metadata.source}."),
        ]
    )
    proposal = report.patch_proposal
    if proposal and proposal.proposed_diff:
        events.append(TimelineEvent("diff", "done", "Prepared a proposed diff for review."))
    else:
        events.append(TimelineEvent("diff", "skipped", "No proposed diff is available."))
    review = getattr(report, "patch_review", None)
    if review:
        status = "done" if review.approved_for_apply else "warning"
        events.append(TimelineEvent("review", status, f"Review risk: {review.risk_level}. {review.summary}"))
    criteria = getattr(report, "acceptance_criteria", [])
    if criteria:
        events.append(
            TimelineEvent(
                "acceptance",
                "ready",
                f"Defined {len(criteria)} acceptance criterion/criteria for this task.",
            )
        )
    budget = getattr(report, "execution_budget", {})
    if budget:
        usage = budget.get("usage", {})
        status = "warning" if budget.get("exhausted") else "ready"
        events.append(
            TimelineEvent(
                "budget",
                status,
                (
                    f"Used {usage.get('agent_steps', 0)} agent step(s) and "
                    f"{usage.get('tool_calls', 0)} tool call(s)."
                ),
            )
        )
    if proposal_id:
        events.append(TimelineEvent("approval", "pending", f"Waiting for approval on proposal {proposal_id}."))
    return events


def append_timeline(session: ProposalSession, step: str, status: str, detail: str) -> None:
    session.timeline.append(TimelineEvent(step, status, detail))


def _build_session_structured_patches(
    repo_path: str,
    edits: list[FileEditProposal],
) -> list[StructuredPatch]:
    root = Path(repo_path).expanduser().resolve()
    patches: list[StructuredPatch] = []
    for edit in edits:
        relative = PurePosixPath(edit.path.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Unsafe proposal path: {edit.path}")
        target = root / Path(*relative.parts)
        if not target.is_file():
            continue
        try:
            original = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if not original or original == edit.new_content:
            continue
        patches.append(
            build_structured_patch(
                edit.path,
                original,
                edit.new_content,
                rationale=edit.rationale,
            )
        )
    return patches


def _file_edit(data: dict[str, Any]) -> FileEditProposal:
    return FileEditProposal(
        path=str(data.get("path") or ""),
        new_content=str(data.get("new_content") or ""),
        rationale=str(data.get("rationale") or ""),
    )


def _timeline_event(data: dict[str, Any]) -> TimelineEvent:
    return TimelineEvent(
        step=str(data.get("step") or ""),
        status=str(data.get("status") or ""),
        detail=str(data.get("detail") or ""),
    )


def _rollback_snapshot(data: dict[str, Any]) -> FileRollbackSnapshot:
    original_content = data.get("original_content")
    return FileRollbackSnapshot(
        path=str(data.get("path") or ""),
        existed=bool(data.get("existed")),
        original_content=str(original_content) if original_content is not None else None,
        applied_content=str(data.get("applied_content") or ""),
    )


def _validation_result(data: dict[str, Any]) -> ValidationResult:
    exit_code = data.get("exit_code")
    return ValidationResult(
        command=str(data.get("command") or ""),
        allowed=bool(data.get("allowed")),
        exit_code=int(exit_code) if exit_code is not None else None,
        stdout=str(data.get("stdout") or ""),
        stderr=str(data.get("stderr") or ""),
    )


def _validation_feedback(data: dict[str, Any] | None) -> ValidationFeedback | None:
    if not isinstance(data, dict):
        return None
    return ValidationFeedback(
        summary=str(data.get("summary") or ""),
        failures=[_validation_failure(item) for item in data.get("failures", [])],
        suspected_files=_string_list(data.get("suspected_files", [])),
        repair_steps=_string_list(data.get("repair_steps", [])),
        repair_task=str(data.get("repair_task") or ""),
        source=str(data.get("source") or "rules"),
    )


def _validation_failure(data: dict[str, Any]) -> ValidationFailureDetail:
    exit_code = data.get("exit_code")
    return ValidationFailureDetail(
        command=str(data.get("command") or ""),
        exit_code=int(exit_code) if exit_code is not None else None,
        output_excerpt=str(data.get("output_excerpt") or ""),
        suspected_files=_string_list(data.get("suspected_files", [])),
        signals=_string_list(data.get("signals", [])),
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_value(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
