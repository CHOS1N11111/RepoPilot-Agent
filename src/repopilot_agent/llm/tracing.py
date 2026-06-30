"""LLM call tracing helpers."""

from __future__ import annotations

from time import perf_counter
from typing import Callable, TypeVar

from .base import LLMClient, LLMMessage
from ..models import LLMCallTrace

T = TypeVar("T")


def traced_llm_json_call(
    name: str,
    llm_client: LLMClient,
    messages: list[LLMMessage],
    parser: Callable[[str], T],
    traces: list[LLMCallTrace] | None = None,
    context_summary: str = "",
) -> T:
    started = perf_counter()
    raw_output = ""
    try:
        raw_output = llm_client.complete(messages)
        parsed = parser(raw_output)
    except Exception as exc:
        _append_trace(
            traces,
            name=name,
            model=llm_client.model,
            messages=messages,
            raw_output=raw_output,
            parsed=False,
            error=str(exc),
            latency_ms=_elapsed_ms(started),
            context_summary=context_summary,
        )
        raise
    _append_trace(
        traces,
        name=name,
        model=llm_client.model,
        messages=messages,
        raw_output=raw_output,
        parsed=True,
        error=None,
        latency_ms=_elapsed_ms(started),
        context_summary=context_summary,
    )
    return parsed


def record_llm_fallback(
    traces: list[LLMCallTrace] | None,
    name: str,
    model: str | None,
    error: str,
    context_summary: str = "",
) -> None:
    if traces is None:
        return
    traces.append(
        LLMCallTrace(
            name=name,
            model=model or "unknown",
            prompt_preview="",
            raw_output="",
            parsed=False,
            fallback_used=True,
            error=error,
            context_summary=context_summary,
        )
    )


def _append_trace(
    traces: list[LLMCallTrace] | None,
    name: str,
    model: str,
    messages: list[LLMMessage],
    raw_output: str,
    parsed: bool,
    error: str | None,
    latency_ms: int,
    context_summary: str = "",
) -> None:
    if traces is None:
        return
    traces.append(
        LLMCallTrace(
            name=name,
            model=model,
            prompt_preview=_prompt_preview(messages),
            raw_output=raw_output[:12000],
            parsed=parsed,
            fallback_used=not parsed,
            error=error,
            latency_ms=latency_ms,
            context_summary=context_summary,
        )
    )


def _prompt_preview(messages: list[LLMMessage]) -> str:
    lines = []
    for message in messages:
        lines.append(f"{message.role}: {message.content}")
    return "\n\n".join(lines)[:12000]


def _elapsed_ms(started: float) -> int:
    return int((perf_counter() - started) * 1000)
