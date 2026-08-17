"""Durable, exact-question contracts for Agent user interaction."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


INPUT_PROTOCOL_VERSION = 1
INPUT_TYPE_TEXT = "text"
MAX_INPUT_QUESTION_CHARS = 1_000
MAX_INPUT_ANSWER_CHARS = 4_000
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class RuntimeInputRequest:
    checkpoint: str
    run_id: str
    action_id: str
    question: str
    question_hash: str
    requested_at: str
    resume_phase: str = "exploration"
    input_type: str = INPUT_TYPE_TEXT
    version: int = INPUT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.version != INPUT_PROTOCOL_VERSION:
            raise ValueError(f"Unsupported input request version: {self.version}")
        _required_identity(self.checkpoint, self.run_id, self.action_id)
        _validate_text(self.question, "question", MAX_INPUT_QUESTION_CHARS)
        if self.input_type != INPUT_TYPE_TEXT:
            raise ValueError(f"Unsupported Runtime input type: {self.input_type}")
        if not _SHA256_PATTERN.fullmatch(self.question_hash):
            raise ValueError("Input request question_hash must be a SHA-256 hex digest.")
        expected_hash = runtime_input_question_hash(
            self.run_id,
            self.action_id,
            self.question,
            self.input_type,
        )
        if self.question_hash != expected_hash:
            raise ValueError("Input request question_hash does not match the exact question.")
        if self.checkpoint != runtime_input_checkpoint(expected_hash):
            raise ValueError("Input request checkpoint does not match the exact question.")
        _validate_text(self.resume_phase, "resume_phase", 100)
        _parse_timestamp(self.requested_at, "requested_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "checkpoint": self.checkpoint,
            "run_id": self.run_id,
            "action_id": self.action_id,
            "input_type": self.input_type,
            "question": self.question,
            "question_hash": self.question_hash,
            "resume_phase": self.resume_phase,
            "requested_at": self.requested_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeInputRequest":
        if not isinstance(data, dict):
            raise ValueError("Input request record must be an object.")
        return cls(
            version=_strict_int(data.get("version", INPUT_PROTOCOL_VERSION), "version"),
            checkpoint=str(data.get("checkpoint") or ""),
            run_id=str(data.get("run_id") or ""),
            action_id=str(data.get("action_id") or ""),
            input_type=str(data.get("input_type") or INPUT_TYPE_TEXT),
            question=str(data.get("question") or ""),
            question_hash=str(data.get("question_hash") or "").lower(),
            resume_phase=str(data.get("resume_phase") or "exploration"),
            requested_at=str(data.get("requested_at") or ""),
        )


@dataclass(frozen=True)
class RuntimeInputAnswer:
    answer_id: str
    checkpoint: str
    run_id: str
    action_id: str
    question_hash: str
    answer: str
    answered_at: str
    input_type: str = INPUT_TYPE_TEXT
    version: int = INPUT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.version != INPUT_PROTOCOL_VERSION:
            raise ValueError(f"Unsupported input answer version: {self.version}")
        _required_identity(self.checkpoint, self.run_id, self.action_id)
        if not self.answer_id.strip():
            raise ValueError("Input answer id is required.")
        if self.input_type != INPUT_TYPE_TEXT:
            raise ValueError(f"Unsupported Runtime input type: {self.input_type}")
        if not _SHA256_PATTERN.fullmatch(self.question_hash):
            raise ValueError("Input answer question_hash must be a SHA-256 hex digest.")
        if self.checkpoint != runtime_input_checkpoint(self.question_hash):
            raise ValueError("Input answer checkpoint does not match its question hash.")
        _validate_text(self.answer, "answer", MAX_INPUT_ANSWER_CHARS)
        _parse_timestamp(self.answered_at, "answered_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "answer_id": self.answer_id,
            "checkpoint": self.checkpoint,
            "run_id": self.run_id,
            "action_id": self.action_id,
            "input_type": self.input_type,
            "question_hash": self.question_hash,
            "answer": self.answer,
            "answered_at": self.answered_at,
        }

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "answer_id": self.answer_id,
            "checkpoint": self.checkpoint,
            "run_id": self.run_id,
            "action_id": self.action_id,
            "input_type": self.input_type,
            "question_hash": self.question_hash,
            "answer_chars": len(self.answer),
            "answered_at": self.answered_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeInputAnswer":
        if not isinstance(data, dict):
            raise ValueError("Input answer record must be an object.")
        return cls(
            version=_strict_int(data.get("version", INPUT_PROTOCOL_VERSION), "version"),
            answer_id=str(data.get("answer_id") or ""),
            checkpoint=str(data.get("checkpoint") or ""),
            run_id=str(data.get("run_id") or ""),
            action_id=str(data.get("action_id") or ""),
            input_type=str(data.get("input_type") or INPUT_TYPE_TEXT),
            question_hash=str(data.get("question_hash") or "").lower(),
            answer=str(data.get("answer") or ""),
            answered_at=str(data.get("answered_at") or ""),
        )


def create_runtime_input_request(
    run_id: str,
    action_id: str,
    question: str,
    *,
    requested_at: str,
    resume_phase: str = "exploration",
    input_type: str = INPUT_TYPE_TEXT,
) -> RuntimeInputRequest:
    normalized_question = question.strip()
    question_hash = runtime_input_question_hash(
        run_id,
        action_id,
        normalized_question,
        input_type,
    )
    return RuntimeInputRequest(
        checkpoint=runtime_input_checkpoint(question_hash),
        run_id=run_id,
        action_id=action_id,
        input_type=input_type,
        question=normalized_question,
        question_hash=question_hash,
        resume_phase=resume_phase.strip() or "exploration",
        requested_at=requested_at,
    )


def create_runtime_input_answer(
    request: RuntimeInputRequest,
    answer: str,
    *,
    answered_at: str,
) -> RuntimeInputAnswer:
    return RuntimeInputAnswer(
        answer_id=uuid4().hex,
        checkpoint=request.checkpoint,
        run_id=request.run_id,
        action_id=request.action_id,
        input_type=request.input_type,
        question_hash=request.question_hash,
        answer=answer.strip(),
        answered_at=answered_at,
    )


def runtime_input_question_hash(
    run_id: str,
    action_id: str,
    question: str,
    input_type: str = INPUT_TYPE_TEXT,
) -> str:
    canonical = json.dumps(
        {
            "version": INPUT_PROTOCOL_VERSION,
            "run_id": run_id.strip(),
            "action_id": action_id.strip(),
            "input_type": input_type.strip(),
            "question": question.strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def runtime_input_checkpoint(question_hash: str) -> str:
    if not _SHA256_PATTERN.fullmatch(question_hash):
        raise ValueError("Input checkpoint requires a SHA-256 question hash.")
    return f"input-{question_hash[:24]}"


def utc_input_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Input timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc).isoformat()


def _required_identity(checkpoint: str, run_id: str, action_id: str) -> None:
    if not checkpoint.strip() or not run_id.strip() or not action_id.strip():
        raise ValueError("Input checkpoint, run id, and action id are required.")


def _validate_text(value: str, field_name: str, limit: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Input {field_name} must be non-empty text.")
    if "\x00" in value:
        raise ValueError(f"Input {field_name} must not contain NUL characters.")
    if len(value) > limit:
        raise ValueError(f"Input {field_name} exceeds the {limit}-character limit.")


def _parse_timestamp(value: str, field_name: str) -> datetime:
    if not value.strip():
        raise ValueError(f"Input {field_name} is required.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Input {field_name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Input {field_name} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _strict_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Input {field_name} must be an integer.")
    return value
