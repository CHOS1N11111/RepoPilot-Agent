"""Allowlisted validation command runner."""

from __future__ import annotations

import shlex
import shutil
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
        arguments = _allowed_arguments(cleaned)
        if arguments is None:
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

        try:
            completed = subprocess.run(
                arguments,
                cwd=Path(repo_path),
                shell=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _timeout_text(exc.stdout)
            stderr = _timeout_text(exc.stderr)
            timeout_message = "Validation command timed out after 120 seconds."
            results.append(
                ValidationResult(
                    command=cleaned,
                    allowed=True,
                    exit_code=None,
                    stdout=stdout.strip(),
                    stderr="\n".join(
                        part for part in [stderr.strip(), timeout_message] if part
                    ),
                )
            )
            continue
        except OSError as exc:
            results.append(
                ValidationResult(
                    command=cleaned,
                    allowed=True,
                    exit_code=None,
                    stdout="",
                    stderr=f"Validation command could not start: {exc}",
                )
            )
            continue
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


def _allowed_arguments(command: str) -> list[str] | None:
    if not command:
        return None
    try:
        lexer = shlex.shlex(
            command,
            posix=True,
            punctuation_chars=";&|<>",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        arguments = list(lexer)
    except ValueError:
        return None
    if not arguments or any(
        token and all(character in ";&|<>" for character in token)
        for token in arguments
    ):
        return None
    lowered = [argument.casefold() for argument in arguments]
    allowed = any(
        lowered[: len(prefix.split())] == prefix.casefold().split()
        for prefix in ALLOWED_PREFIXES
    )
    if not allowed:
        return None
    executable = shutil.which(arguments[0])
    if executable:
        arguments[0] = executable
    return arguments


def _timeout_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""
