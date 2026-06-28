"""Core data models for the local RepoPilot workflow."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RepoFile:
    path: Path
    relative_path: str
    size_bytes: int
    language: str
    content: str


@dataclass(frozen=True)
class SearchHit:
    path: str
    score: int
    reasons: list[str]
    preview: str


@dataclass(frozen=True)
class PlanStep:
    order: int
    title: str
    detail: str
    status: str = "pending"


@dataclass(frozen=True)
class ValidationResult:
    command: str
    allowed: bool
    exit_code: int | None
    stdout: str
    stderr: str


@dataclass
class WorkflowReport:
    task: str
    repo_path: str
    files_scanned: int
    relevant_files: list[SearchHit] = field(default_factory=list)
    plan: list[PlanStep] = field(default_factory=list)
    validation: list[ValidationResult] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
