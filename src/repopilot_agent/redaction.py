"""Dependency-light redaction helpers for persisted and model-facing text."""

from __future__ import annotations

import re


PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.DOTALL,
)
SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|authorization|password|"
    r"client[_-]?secret|refresh[_-]?token|github[_-]?token|openai[_-]?api[_-]?key)"
    r"\b\s*[\"']?\s*(?::|=(?!=))"
)
TOKEN_VALUE_PATTERN = re.compile(
    r"(?i)\b(?:sk-[a-z0-9_-]{8,}|gh[pousr]_[a-z0-9_]{8,}|github_pat_[a-z0-9_]{8,})\b"
)
BEARER_TOKEN_PATTERN = re.compile(r"(?i)\bBearer\s+[a-z0-9._~+/-]{8,}")


def redact_context_secrets(text: str) -> str:
    """Redact common credential assignments and complete private-key blocks."""

    without_keys = PRIVATE_KEY_PATTERN.sub("[REDACTED PRIVATE KEY]", text)
    without_tokens = TOKEN_VALUE_PATTERN.sub("[REDACTED TOKEN]", without_keys)
    without_tokens = BEARER_TOKEN_PATTERN.sub("Bearer [REDACTED TOKEN]", without_tokens)
    redacted_lines: list[str] = []
    for line in without_tokens.splitlines(keepends=True):
        match = SENSITIVE_ASSIGNMENT_PATTERN.search(line)
        if match is None:
            redacted_lines.append(line)
            continue
        newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        redacted_lines.append(f"{line[:match.end()]} [REDACTED]{newline}")
    return "".join(redacted_lines)
