"""Strict contracts and accounting for bounded parallel read batches."""

from __future__ import annotations

import json
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Iterable

from .models import RuntimeAction, RuntimeEvent


PARALLEL_READ_ACTION = "parallel_read"
PARALLEL_READ_MEMBER_ACTIONS = frozenset(
    {
        "search_files",
        "read_file",
        "inspect_repository_map",
        "inspect_git_status",
        "inspect_diff",
    }
)
MIN_PARALLEL_READ_ACTIONS = 2
MAX_PARALLEL_READ_ACTIONS = 4
MAX_PARALLEL_READ_TEXT_CHARS = 500


def normalize_parallel_read_arguments(
    arguments: object,
    *,
    max_actions: int = MAX_PARALLEL_READ_ACTIONS,
) -> dict[str, Any]:
    """Validate and normalize one exact ordered parallel-read request."""

    if not isinstance(arguments, dict):
        raise ValueError("parallel_read arguments must be an object.")
    _require_exact_keys(arguments, {"actions"}, "parallel_read arguments")
    raw_actions = arguments.get("actions")
    if not isinstance(raw_actions, list):
        raise ValueError("parallel_read actions must be a list.")
    if not isinstance(max_actions, int) or isinstance(max_actions, bool):
        raise ValueError("parallel_read max_actions must be an integer.")
    effective_limit = min(max(max_actions, 0), MAX_PARALLEL_READ_ACTIONS)
    if effective_limit < MIN_PARALLEL_READ_ACTIONS:
        raise ValueError(
            "parallel_read requires at least two remaining tool calls."
        )
    if not MIN_PARALLEL_READ_ACTIONS <= len(raw_actions) <= effective_limit:
        raise ValueError(
            "parallel_read actions must contain from "
            f"{MIN_PARALLEL_READ_ACTIONS} to {effective_limit} members."
        )

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_action in enumerate(raw_actions, start=1):
        if not isinstance(raw_action, dict):
            raise ValueError(f"parallel_read member {index} must be an object.")
        _require_exact_keys(
            raw_action,
            {"kind", "arguments"},
            f"parallel_read member {index}",
        )
        kind = raw_action.get("kind")
        if kind not in PARALLEL_READ_MEMBER_ACTIONS:
            raise ValueError(
                f"parallel_read member {index} has unsupported action: {kind}"
            )
        member_arguments = _normalize_member_arguments(
            str(kind),
            raw_action.get("arguments"),
            index,
        )
        member = {"kind": str(kind), "arguments": member_arguments}
        signature = json.dumps(
            member,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if signature in seen:
            raise ValueError(
                f"parallel_read member {index} duplicates an earlier member."
            )
        seen.add(signature)
        normalized.append(member)
    return {"actions": normalized}


def parallel_read_member_actions(action: RuntimeAction) -> tuple[RuntimeAction, ...]:
    """Create deterministic, non-persisted member actions for one parent batch."""

    if action.kind != PARALLEL_READ_ACTION:
        raise ValueError("Only parallel_read actions contain read-batch members.")
    normalized = normalize_parallel_read_arguments(action.arguments)
    return tuple(
        RuntimeAction(
            kind=str(member["kind"]),
            arguments=dict(member["arguments"]),
            rationale=action.rationale,
            action_id=f"{action.action_id}.member-{index}",
            idempotency_key=(
                f"{action.effective_idempotency_key}.member-{index}"
            ),
        )
        for index, member in enumerate(normalized["actions"], start=1)
    )


def runtime_action_tool_call_cost(action: RuntimeAction) -> int:
    """Return the budget cost of starting one Runtime action."""

    if action.kind != PARALLEL_READ_ACTION:
        return 1
    try:
        return len(normalize_parallel_read_arguments(action.arguments)["actions"])
    except ValueError:
        return 1


def runtime_started_tool_call_count(events: Iterable[RuntimeEvent]) -> int:
    """Count persisted started tools, including every member of a batch."""

    total = 0
    for event in events:
        if event.event_type not in {"action_started", "action_recovery_started"}:
            continue
        stored_cost = event.payload.get("tool_call_cost")
        if (
            isinstance(stored_cost, int)
            and not isinstance(stored_cost, bool)
            and 1 <= stored_cost <= MAX_PARALLEL_READ_ACTIONS
        ):
            total += stored_cost
            continue
        raw_action = event.payload.get("action")
        try:
            action = (
                RuntimeAction.from_dict(raw_action)
                if isinstance(raw_action, dict)
                else None
            )
        except (TypeError, ValueError):
            action = None
        total += runtime_action_tool_call_cost(action) if action is not None else 1
    return total


def _normalize_member_arguments(
    kind: str,
    arguments: object,
    index: int,
) -> dict[str, Any]:
    label = f"parallel_read member {index} {kind} arguments"
    if not isinstance(arguments, dict):
        raise ValueError(f"{label} must be an object.")
    if kind == "search_files":
        _require_exact_keys(arguments, {"query"}, label)
        return {"query": _bounded_text(arguments.get("query"), "query", label)}
    if kind == "read_file":
        _require_exact_keys(arguments, {"path"}, label)
        return {"path": _normalize_path(arguments.get("path"), label)}
    if kind == "inspect_repository_map":
        _reject_unknown_keys(arguments, {"query", "limit"}, label)
        normalized: dict[str, Any] = {}
        if "query" in arguments:
            normalized["query"] = _bounded_text(
                arguments.get("query"),
                "query",
                label,
                allow_empty=True,
            )
        if "limit" in arguments:
            limit = arguments.get("limit")
            if (
                not isinstance(limit, int)
                or isinstance(limit, bool)
                or not 1 <= limit <= 20
            ):
                raise ValueError(f"{label} limit must be an integer from 1 to 20.")
            normalized["limit"] = limit
        return normalized
    if kind == "inspect_git_status":
        _require_exact_keys(arguments, set(), label)
        return {}
    if kind == "inspect_diff":
        _reject_unknown_keys(arguments, {"staged"}, label)
        staged = arguments.get("staged", False)
        if not isinstance(staged, bool):
            raise ValueError(f"{label} staged must be a boolean.")
        return {"staged": staged}
    raise ValueError(f"Unsupported parallel_read member action: {kind}")


def _normalize_path(value: object, label: str) -> str:
    path = _bounded_text(value, "path", label).replace("\\", "/")
    pure = PurePosixPath(path)
    if (
        pure.is_absolute()
        or PureWindowsPath(path).is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValueError(f"{label} path must be a safe repository-relative path.")
    return pure.as_posix()


def _bounded_text(
    value: object,
    name: str,
    label: str,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} {name} must be a string.")
    parsed = value.strip()
    if not parsed and not allow_empty:
        raise ValueError(f"{label} requires non-empty {name}.")
    if "\x00" in parsed:
        raise ValueError(f"{label} {name} must not contain NUL characters.")
    if len(parsed) > MAX_PARALLEL_READ_TEXT_CHARS:
        raise ValueError(
            f"{label} {name} exceeds the "
            f"{MAX_PARALLEL_READ_TEXT_CHARS}-character limit."
        )
    return parsed


def _require_exact_keys(value: dict, expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise ValueError(f"{label} is missing required field(s): {', '.join(missing)}.")
    if unknown:
        raise ValueError(f"{label} contains unknown field(s): {', '.join(unknown)}.")


def _reject_unknown_keys(value: dict, allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{label} contains unknown field(s): {', '.join(unknown)}.")
