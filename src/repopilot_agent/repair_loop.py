"""Progress tracking and stop decisions for bounded validation-repair loops."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from .models import FileEditProposal, ValidationFeedback, ValidationResult


STOP_REPEATED_FAILURE = "repeated_validation_failure"
STOP_REPEATED_PROPOSAL = "repeated_repair_proposal"
STOP_NO_REPOSITORY_CHANGE = "no_repository_change"
STOP_REPAIR_BUDGET = "repair_budget_exhausted"
STOP_EXECUTION_BUDGET = "execution_budget_exhausted"
STOP_NO_PROPOSAL = "no_apply_ready_proposal"

_DURATION_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds)\b", re.IGNORECASE)
_ADDRESS_PATTERN = re.compile(r"0x[0-9a-f]+", re.IGNORECASE)
_WHITESPACE_PATTERN = re.compile(r"\s+")


class RepairLoopStopped(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class RepairAttemptRecord:
    attempt: int
    status: str
    trigger_failure_fingerprint: str = ""
    proposal_fingerprint: str = ""
    result_failure_fingerprint: str = ""
    summary: str = ""
    proposal_paths: list[str] = field(default_factory=list)
    validation_commands: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RepairDecision:
    accepted: bool
    stop_reason: str | None
    message: str
    fingerprint: str = ""


def validation_failure_fingerprint(validation: list[ValidationResult]) -> str:
    failures: list[dict[str, Any]] = []
    for result in validation:
        if result.allowed and result.exit_code in (0, None):
            continue
        failures.append(
            {
                "command": _normalize_text(result.command, 500),
                "allowed": result.allowed,
                "exit_code": result.exit_code,
                "stdout": _normalize_output(result.stdout),
                "stderr": _normalize_output(result.stderr),
            }
        )
    if not failures:
        return ""
    failures.sort(key=lambda item: (item["command"], str(item["exit_code"])))
    return _fingerprint(failures)


def repair_proposal_fingerprint(edits: list[FileEditProposal]) -> str:
    if not edits:
        return ""
    content = [
        {
            "path": _normalize_path(edit.path),
            "content_sha256": hashlib.sha256(edit.new_content.encode("utf-8")).hexdigest(),
        }
        for edit in edits
    ]
    content.sort(key=lambda item: item["path"])
    return _fingerprint(content)


def validation_feedback_fingerprint(feedback: ValidationFeedback | None) -> str:
    if feedback is None:
        return ""
    return _fingerprint(
        {
            "summary": _normalize_text(feedback.summary, 1000),
            "failures": [
                {
                    "command": _normalize_text(item.command, 500),
                    "exit_code": item.exit_code,
                    "excerpt": _normalize_output(item.output_excerpt),
                    "signals": sorted(item.signals),
                }
                for item in feedback.failures
            ],
            "suspected_files": sorted(feedback.suspected_files),
        }
    )


def proposal_changes_repository(repo_path: str | Path, edits: list[FileEditProposal]) -> bool:
    root = Path(repo_path).expanduser().resolve()
    for edit in edits:
        relative = PurePosixPath(_normalize_path(edit.path))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Unsafe repair proposal path: {edit.path}")
        target = root / Path(*relative.parts)
        if not target.exists():
            return True
        if not target.is_file():
            raise ValueError(f"Repair proposal target is not a file: {edit.path}")
        try:
            current = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Repair proposals require UTF-8 text files: {edit.path}") from exc
        if current != edit.new_content:
            return True
    return False


def record_validation_outcome(
    history: list[RepairAttemptRecord],
    *,
    attempt: int,
    validation: list[ValidationResult],
    summary: str,
) -> tuple[list[RepairAttemptRecord], RepairDecision]:
    fingerprint = validation_failure_fingerprint(validation)
    existing = _record_for_attempt(history, attempt)
    if not fingerprint:
        record = replace(
            existing or RepairAttemptRecord(attempt=attempt, status="completed"),
            status="completed",
            result_failure_fingerprint="",
            summary=summary.strip() or "Validation passed.",
            validation_commands=[item.command for item in validation],
        )
        return _upsert(history, record), RepairDecision(True, None, "Validation passed.")

    trigger = existing.trigger_failure_fingerprint if existing else ""
    repeated = bool(attempt > 0 and trigger and fingerprint == trigger)
    status = "stopped" if repeated else "validation_failed"
    record = replace(
        existing or RepairAttemptRecord(attempt=attempt, status=status),
        status=status,
        result_failure_fingerprint=fingerprint,
        summary=summary.strip() or "Validation failed.",
        validation_commands=[item.command for item in validation],
    )
    updated = _upsert(history, record)
    if repeated:
        return updated, RepairDecision(
            False,
            STOP_REPEATED_FAILURE,
            "Validation produced the same failure fingerprint as the repair trigger; the loop stopped without progress.",
            fingerprint,
        )
    return updated, RepairDecision(
        True,
        None,
        "Validation failure changed or this is the initial failure; another bounded repair may proceed.",
        fingerprint,
    )


def record_repair_proposal(
    history: list[RepairAttemptRecord],
    *,
    attempt: int,
    trigger_failure_fingerprint: str,
    edits: list[FileEditProposal],
    summary: str,
) -> tuple[list[RepairAttemptRecord], RepairDecision]:
    fingerprint = repair_proposal_fingerprint(edits)
    prior_fingerprints = {
        item.proposal_fingerprint
        for item in history
        if item.proposal_fingerprint and item.attempt < attempt
    }
    repeated = bool(fingerprint and fingerprint in prior_fingerprints)
    record = RepairAttemptRecord(
        attempt=attempt,
        status="stopped" if repeated else "proposal_ready",
        trigger_failure_fingerprint=trigger_failure_fingerprint,
        proposal_fingerprint=fingerprint,
        summary=summary.strip() or "Repair proposal generated.",
        proposal_paths=sorted({_normalize_path(edit.path) for edit in edits}),
    )
    updated = _upsert(history, record)
    if repeated:
        return updated, RepairDecision(
            False,
            STOP_REPEATED_PROPOSAL,
            "The generated repair proposal matches an earlier repair proposal; the loop stopped without progress.",
            fingerprint,
        )
    return updated, RepairDecision(True, None, "A new repair proposal is ready for human approval.", fingerprint)


def latest_failure_fingerprint(history: list[RepairAttemptRecord]) -> str:
    for item in sorted(history, key=lambda record: record.attempt, reverse=True):
        if item.result_failure_fingerprint:
            return item.result_failure_fingerprint
    return ""


def mark_repair_attempt_stopped(
    history: list[RepairAttemptRecord],
    *,
    attempt: int,
    summary: str,
) -> list[RepairAttemptRecord]:
    existing = _record_for_attempt(history, attempt)
    record = replace(
        existing or RepairAttemptRecord(attempt=attempt, status="stopped"),
        status="stopped",
        summary=summary.strip(),
    )
    return _upsert(history, record)


def repair_attempts_from_records(records: object) -> list[RepairAttemptRecord]:
    if not isinstance(records, list):
        return []
    attempts: list[RepairAttemptRecord] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        attempt = _nonnegative_int(item.get("attempt"))
        attempts.append(
            RepairAttemptRecord(
                attempt=attempt,
                status=str(item.get("status") or "unknown"),
                trigger_failure_fingerprint=str(item.get("trigger_failure_fingerprint") or ""),
                proposal_fingerprint=str(item.get("proposal_fingerprint") or ""),
                result_failure_fingerprint=str(item.get("result_failure_fingerprint") or ""),
                summary=str(item.get("summary") or ""),
                proposal_paths=_string_list(item.get("proposal_paths")),
                validation_commands=_string_list(item.get("validation_commands")),
            )
        )
    return sorted(attempts, key=lambda record: record.attempt)


def _record_for_attempt(history: list[RepairAttemptRecord], attempt: int) -> RepairAttemptRecord | None:
    return next((item for item in history if item.attempt == attempt), None)


def _upsert(history: list[RepairAttemptRecord], record: RepairAttemptRecord) -> list[RepairAttemptRecord]:
    updated = [item for item in history if item.attempt != record.attempt]
    updated.append(record)
    return sorted(updated, key=lambda item: item.attempt)


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_output(value: str) -> str:
    normalized = value.replace("\\", "/")
    normalized = _DURATION_PATTERN.sub("<duration>", normalized)
    normalized = _ADDRESS_PATTERN.sub("<address>", normalized)
    return _normalize_text(normalized, 4000)


def _normalize_text(value: str, limit: int) -> str:
    clean = _WHITESPACE_PATTERN.sub(" ", value).strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit]


def _normalize_path(value: str) -> str:
    return value.strip().replace("\\", "/")


def _nonnegative_int(value: object) -> int:
    try:
        return max(int(value), 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]
