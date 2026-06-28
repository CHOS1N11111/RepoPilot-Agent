"""Base interfaces for LLM providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class LLMError(RuntimeError):
    """Raised when an LLM provider cannot produce a usable response."""


@dataclass(frozen=True)
class LLMMessage:
    role: str
    content: str


class LLMClient(Protocol):
    model: str

    def complete(self, messages: list[LLMMessage]) -> str:
        """Return the model response text for a chat-style prompt."""
