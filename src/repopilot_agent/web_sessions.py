"""In-memory web workflow sessions for proposal approval."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .models import FileEditProposal, ValidationResult


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
    timeline: list[TimelineEvent] = field(default_factory=list)
    applied: bool = False
    validation: list[ValidationResult] = field(default_factory=list)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "repo_path": self.repo_path,
            "task": self.task,
            "created_at": self.created_at,
            "applied": self.applied,
            "timeline": [asdict(event) for event in self.timeline],
            "validation": [asdict(result) for result in self.validation],
        }


_SESSIONS: dict[str, ProposalSession] = {}


def create_proposal_session(
    repo_path: str,
    task: str,
    file_edits: list[FileEditProposal],
    validation_commands: list[str],
    timeline: list[TimelineEvent],
) -> ProposalSession:
    proposal_id = uuid4().hex
    session = ProposalSession(
        proposal_id=proposal_id,
        repo_path=repo_path,
        task=task,
        file_edits=file_edits,
        validation_commands=validation_commands,
        created_at=datetime.now(timezone.utc).isoformat(),
        timeline=timeline,
    )
    _SESSIONS[proposal_id] = session
    return session


def get_proposal_session(proposal_id: str) -> ProposalSession | None:
    return _SESSIONS.get(proposal_id)


def build_report_timeline(report: Any, proposal_id: str | None = None) -> list[TimelineEvent]:
    events = [
        TimelineEvent("scan", "done", f"Scanned {report.files_scanned} text file(s)."),
        TimelineEvent("search", "done", f"Selected {len(report.relevant_files)} relevant file(s)."),
        TimelineEvent("plan", "done", f"Plan source: {report.plan_metadata.source}."),
        TimelineEvent("proposal", "done", f"Proposal source: {report.patch_proposal_metadata.source}."),
    ]
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
