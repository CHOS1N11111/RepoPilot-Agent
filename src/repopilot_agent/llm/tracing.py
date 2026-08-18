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
            usage=_trace_usage(llm_client),
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
        usage=_trace_usage(llm_client),
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
    usage: dict[str, int] | None = None,
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
            input_tokens=usage.get("input_tokens") if usage else None,
            output_tokens=usage.get("output_tokens") if usage else None,
            total_tokens=usage.get("total_tokens") if usage else None,
        )
    )


def _prompt_preview(messages: list[LLMMessage]) -> str:
    lines = []
    for message in messages:
        lines.append(f"{message.role}: {message.content}")
    return "\n\n".join(lines)[:12000]


def _elapsed_ms(started: float) -> int:
    return int((perf_counter() - started) * 1000)


def _trace_usage(llm_client: LLMClient) -> dict[str, int] | None:
    value = getattr(llm_client, "last_usage", None)
    if not isinstance(value, dict):
        return None
    result: dict[str, int] = {}
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        item = value.get(name)
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            result[name] = item
    return result or None
