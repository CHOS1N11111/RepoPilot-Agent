"""Helpers for extracting structured JSON from model output."""

from __future__ import annotations

import json
from typing import Any

from .base import LLMError


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = _strip_code_fence(text.strip())
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise LLMError("LLM response did not contain a JSON object.")
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM response contained invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise LLMError("LLM response JSON must be an object.")
    return value


def _strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return text
