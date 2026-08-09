"""Credential-free execution configuration snapshots for persistent task runs."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .execution import ExecutionBudget


EXECUTION_PROFILE_VERSION = 1
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class TaskRunExecutionProfile:
    version: int
    captured_at: str
    use_llm: bool
    model: str
    endpoint_configured: bool
    endpoint_fingerprint: str | None
    json_mode: bool | None
    allow_llm_fallback: bool
    use_memory: bool
    iterative_agent: bool
    llm_timeout_seconds: int | None
    max_repair_attempts: int
    auto_repair_enabled: bool
    execution_budget: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def create_execution_profile(
    *,
    use_llm: bool,
    model: str | None,
    endpoint_url: str | None,
    json_mode: bool | None,
    allow_llm_fallback: bool,
    use_memory: bool,
    iterative_agent: bool,
    llm_timeout_seconds: int | None,
    max_repair_attempts: int,
    auto_repair_enabled: bool,
    execution_budget: ExecutionBudget,
) -> TaskRunExecutionProfile:
    return TaskRunExecutionProfile(
        version=EXECUTION_PROFILE_VERSION,
        captured_at=_now(),
        use_llm=bool(use_llm),
        model=str(model or "").strip(),
        endpoint_configured=bool(str(endpoint_url or "").strip()),
        endpoint_fingerprint=fingerprint_endpoint(endpoint_url),
        json_mode=json_mode if isinstance(json_mode, bool) else None,
        allow_llm_fallback=bool(allow_llm_fallback),
        use_memory=bool(use_memory),
        iterative_agent=bool(iterative_agent),
        llm_timeout_seconds=_positive_optional_int(llm_timeout_seconds),
        max_repair_attempts=max(_nonnegative_int(max_repair_attempts), 0),
        auto_repair_enabled=bool(auto_repair_enabled),
        execution_budget=execution_budget.to_dict(),
    )


def execution_profile_from_record(value: object) -> TaskRunExecutionProfile | None:
    if not isinstance(value, dict) or not value:
        return None
    fingerprint = str(value.get("endpoint_fingerprint") or "").strip().lower()
    if not SHA256_PATTERN.fullmatch(fingerprint):
        fingerprint = ""
    json_mode = value.get("json_mode")
    if not isinstance(json_mode, bool):
        json_mode = None
    budget = ExecutionBudget.from_dict(value.get("execution_budget"))
    return TaskRunExecutionProfile(
        version=max(_positive_int(value.get("version"), EXECUTION_PROFILE_VERSION), 1),
        captured_at=str(value.get("captured_at") or ""),
        use_llm=_boolean(value.get("use_llm"), False),
        model=str(value.get("model") or "").strip()[:200],
        endpoint_configured=_boolean(value.get("endpoint_configured"), bool(fingerprint)),
        endpoint_fingerprint=fingerprint or None,
        json_mode=json_mode,
        allow_llm_fallback=_boolean(value.get("allow_llm_fallback"), True),
        use_memory=_boolean(value.get("use_memory"), True),
        iterative_agent=_boolean(value.get("iterative_agent"), False),
        llm_timeout_seconds=_positive_optional_int(value.get("llm_timeout_seconds")),
        max_repair_attempts=_nonnegative_int(value.get("max_repair_attempts")),
        auto_repair_enabled=_boolean(value.get("auto_repair_enabled"), False),
        execution_budget=budget.to_dict(),
    )


def fingerprint_endpoint(value: str | None) -> str | None:
    normalized = str(value or "").strip().rstrip("/")
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _boolean(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_optional_int(value: object) -> int | None:
    try:
        parsed = int(value) if value is not None else 0
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _nonnegative_int(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
