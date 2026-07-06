"""In-memory web workflow sessions for proposal approval."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .models import FileEditProposal, ValidationFailureDetail, ValidationFeedback, ValidationResult
from .patch_apply import FileRollbackSnapshot


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
    allowed_paths: list[str] = field(default_factory=list)
    approved_paths: list[str] = field(default_factory=list)
    applied_paths: list[str] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)
    applied: bool = False
    reverted: bool = False
    rollback_snapshot: list[FileRollbackSnapshot] = field(default_factory=list)
    validation: list[ValidationResult] = field(default_factory=list)
    validation_feedback: ValidationFeedback | None = None

    def to_public_dict(self) -> dict[str, Any]:
        rollback_available = bool(self.applied and not self.reverted and self.rollback_snapshot)
        return {
            "proposal_id": self.proposal_id,
            "repo_path": self.repo_path,
            "task": self.task,
            "created_at": self.created_at,
            "applied": self.applied,
            "reverted": self.reverted,
            "rollback_available": rollback_available,
            "allowed_paths": self.allowed_paths,
            "approved_paths": self.approved_paths,
            "applied_paths": self.applied_paths,
            "timeline": [asdict(event) for event in self.timeline],
            "validation": [asdict(result) for result in self.validation],
            "validation_feedback": asdict(self.validation_feedback) if self.validation_feedback else None,
        }


_SESSIONS: dict[str, ProposalSession] = {}


def create_proposal_session(
    repo_path: str,
    task: str,
    file_edits: list[FileEditProposal],
    validation_commands: list[str],
    timeline: list[TimelineEvent],
    allowed_paths: list[str] | None = None,
) -> ProposalSession:
    proposal_id = uuid4().hex
    session = ProposalSession(
        proposal_id=proposal_id,
        repo_path=repo_path,
        task=task,
        file_edits=file_edits,
        validation_commands=validation_commands,
        created_at=datetime.now(timezone.utc).isoformat(),
        allowed_paths=allowed_paths or [edit.path for edit in file_edits],
        timeline=timeline,
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
        "repo_path": session.repo_path,
        "task": session.task,
        "file_edits": [asdict(edit) for edit in session.file_edits],
        "validation_commands": session.validation_commands,
        "created_at": session.created_at,
        "allowed_paths": session.allowed_paths,
        "approved_paths": session.approved_paths,
        "applied_paths": session.applied_paths,
        "timeline": [asdict(event) for event in session.timeline],
        "applied": session.applied,
        "reverted": session.reverted,
        "rollback_snapshot": [asdict(snapshot) for snapshot in session.rollback_snapshot],
        "validation": [asdict(result) for result in session.validation],
        "validation_feedback": asdict(session.validation_feedback) if session.validation_feedback else None,
    }


def proposal_session_from_record(record: dict[str, Any]) -> ProposalSession:
    session = ProposalSession(
        proposal_id=str(record["proposal_id"]),
        repo_path=str(record["repo_path"]),
        task=str(record["task"]),
        file_edits=[_file_edit(item) for item in record.get("file_edits", [])],
        validation_commands=_string_list(record.get("validation_commands", [])),
        created_at=str(record.get("created_at") or datetime.now(timezone.utc).isoformat()),
        allowed_paths=_string_list(record.get("allowed_paths", [])),
        approved_paths=_string_list(record.get("approved_paths", [])),
        applied_paths=_string_list(record.get("applied_paths", [])),
        timeline=[_timeline_event(item) for item in record.get("timeline", [])],
        applied=bool(record.get("applied")),
        reverted=bool(record.get("reverted")),
        rollback_snapshot=[_rollback_snapshot(item) for item in record.get("rollback_snapshot", [])],
        validation=[_validation_result(item) for item in record.get("validation", [])],
        validation_feedback=_validation_feedback(record.get("validation_feedback")),
    )
    return cache_proposal_session(session)


def build_report_timeline(report: Any, proposal_id: str | None = None) -> list[TimelineEvent]:
    events = [
        TimelineEvent("scan", "done", f"Scanned {report.files_scanned} text file(s)."),
        TimelineEvent("search", "done", f"Selected {len(report.relevant_files)} relevant file(s)."),
    ]
    agent_steps = getattr(report, "agent_steps", [])
    if agent_steps:
        events.append(TimelineEvent("agent", "done", f"Completed {len(agent_steps)} read-only exploration step(s)."))
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
    if proposal_id:
        events.append(TimelineEvent("approval", "pending", f"Waiting for approval on proposal {proposal_id}."))
    return events


def append_timeline(session: ProposalSession, step: str, status: str, detail: str) -> None:
    session.timeline.append(TimelineEvent(step, status, detail))


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
