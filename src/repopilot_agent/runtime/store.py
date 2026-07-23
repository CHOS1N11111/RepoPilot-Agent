"""Event stores and action reservations for the RepoPilot runtime."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from ..memory import MemoryStore
from .models import RuntimeAction, RuntimeEvent, RuntimeObservation


@dataclass(frozen=True)
class ActionReservation:
    status: str
    observation: RuntimeObservation | None = None


class RuntimeEventStore(Protocol):
    def reserve(self, run_id: str, action: RuntimeAction) -> ActionReservation:
        ...

    def complete(self, run_id: str, action: RuntimeAction, observation: RuntimeObservation) -> RuntimeObservation:
        ...

    def append_event(
        self,
        run_id: str,
        event_type: str,
        *,
        action: RuntimeAction | None = None,
        payload: dict | None = None,
    ) -> RuntimeEvent:
        ...

    def list_events(self, run_id: str) -> list[RuntimeEvent]:
        ...


class InMemoryRuntimeStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._actions: dict[tuple[str, str], dict] = {}
        self._events: dict[str, list[RuntimeEvent]] = {}

    def reserve(self, run_id: str, action: RuntimeAction) -> ActionReservation:
        key = (run_id, action.effective_idempotency_key)
        signature = _action_signature(action)
        with self._lock:
            current = self._actions.get(key)
            if current is None:
                self._actions[key] = {"state": "in_progress", "signature": signature, "observation": None}
                return ActionReservation("new")
            if current["signature"] != signature:
                return ActionReservation("conflict")
            if current["state"] == "completed" and current["observation"]:
                return ActionReservation("completed", current["observation"])
            return ActionReservation("in_progress")

    def complete(self, run_id: str, action: RuntimeAction, observation: RuntimeObservation) -> RuntimeObservation:
        key = (run_id, action.effective_idempotency_key)
        signature = _action_signature(action)
        with self._lock:
            current = self._actions.get(key)
            if current and current["signature"] != signature:
                raise RuntimeError("Idempotency key is already reserved for a different action.")
            if current and current["state"] == "completed" and current["observation"]:
                return current["observation"]
            self._actions[key] = {
                "state": "completed",
                "signature": signature,
                "observation": observation,
            }
        return observation

    def append_event(
        self,
        run_id: str,
        event_type: str,
        *,
        action: RuntimeAction | None = None,
        payload: dict | None = None,
    ) -> RuntimeEvent:
        with self._lock:
            events = self._events.setdefault(run_id, [])
            event = RuntimeEvent(
                event_id=uuid4().hex,
                run_id=run_id,
                sequence=len(events) + 1,
                event_type=event_type,
                created_at=_now(),
                action_id=action.action_id if action else None,
                idempotency_key=action.effective_idempotency_key if action else None,
                payload=dict(payload or {}),
            )
            events.append(event)
            return event

    def list_events(self, run_id: str) -> list[RuntimeEvent]:
        with self._lock:
            return list(self._events.get(run_id, []))


class SQLiteRuntimeStore:
    def __init__(self, memory_store: MemoryStore) -> None:
        self.memory_store = memory_store

    def reserve(self, run_id: str, action: RuntimeAction) -> ActionReservation:
        record = self.memory_store.reserve_agent_runtime_action(
            run_id,
            action.effective_idempotency_key,
            action.to_dict(),
        )
        observation = record.get("observation")
        return ActionReservation(
            status=str(record.get("status") or "conflict"),
            observation=RuntimeObservation.from_dict(observation) if isinstance(observation, dict) else None,
        )

    def complete(self, run_id: str, action: RuntimeAction, observation: RuntimeObservation) -> RuntimeObservation:
        record = self.memory_store.complete_agent_runtime_action(
            run_id,
            action.effective_idempotency_key,
            action.to_dict(),
            observation.to_dict(),
        )
        return RuntimeObservation.from_dict(record)

    def append_event(
        self,
        run_id: str,
        event_type: str,
        *,
        action: RuntimeAction | None = None,
        payload: dict | None = None,
    ) -> RuntimeEvent:
        record = self.memory_store.append_agent_runtime_event(
            run_id,
            event_type,
            action_id=action.action_id if action else None,
            idempotency_key=action.effective_idempotency_key if action else None,
            payload=payload or {},
        )
        return RuntimeEvent.from_dict(record)

    def list_events(self, run_id: str) -> list[RuntimeEvent]:
        return [RuntimeEvent.from_dict(item) for item in self.memory_store.list_agent_runtime_events(run_id)]


def _action_signature(action: RuntimeAction) -> str:
    return json.dumps(
        {"kind": action.kind, "arguments": action.arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
