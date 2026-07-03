"""Allowlisted validation command runner."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .models import ValidationResult

ALLOWED_PREFIXES = (
    "python -m unittest",
    "python -m pytest",
    "pytest",
    "npm test",
    "npm run test",
    "npm run lint",
    "ruff check",
)


def run_validation(repo_path: str | Path, commands: list[str]) -> list[ValidationResult]:
    results: list[ValidationResult] = []
    for command in commands:
        cleaned = command.strip()
        if not _is_allowed(cleaned):
            results.append(
                ValidationResult(
                    command=cleaned,
                    allowed=False,
                    exit_code=None,
                    stdout="",
                    stderr="Command rejected because it is not in the validation allowlist.",
                )
            )
            continue

        completed = subprocess.run(
            cleaned,
            cwd=Path(repo_path),
            shell=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=120,
        )
        results.append(
            ValidationResult(
                command=cleaned,
                allowed=True,
                exit_code=completed.returncode,
                stdout=completed.stdout.strip(),
                stderr=completed.stderr.strip(),
            )
        )
    return results


def _is_allowed(command: str) -> bool:
    lowered = " ".join(command.lower().split())
    return any(lowered == prefix or lowered.startswith(prefix + " ") for prefix in ALLOWED_PREFIXES)
