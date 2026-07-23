"""Reusable action-observation loop with policy and recovery checkpoints."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from ..models import RepoFile
from .models import (
    STOPPING_OBSERVATION_STATUSES,
    RuntimeAction,
    RuntimeObservation,
    RuntimePolicy,
    RuntimeRunResult,
)
from .store import InMemoryRuntimeStore, RuntimeEventStore
from .tools import RuntimeToolContext, execute_runtime_tool


class AgentRuntime:
    def __init__(
        self,
        repo_path: str | Path,
        task: str,
        *,
        run_id: str | None = None,
        policy: RuntimePolicy | None = None,
        store: RuntimeEventStore | None = None,
        files: list[RepoFile] | None = None,
    ) -> None:
        self.run_id = run_id or uuid4().hex
        self.task = task
        self.policy = policy or RuntimePolicy.read_only()
        self.store = store or InMemoryRuntimeStore()
        self.context = RuntimeToolContext(repo_path, task, self.policy, files=files)
        self._started = False
        self._stopped = False

    @property
    def events(self):
        return self.store.list_events(self.run_id)

    @property
    def selected_paths(self) -> list[str]:
        return list(self.context.selected_paths)

    def start(self) -> None:
        if self._started:
            return
        self.store.append_event(
            self.run_id,
            "run_started",
            payload={"task": self.task, "repo_path": str(self.context.repo_path)},
        )
        self._started = True

    def execute(self, action: RuntimeAction) -> RuntimeObservation:
        self.start()
        if self._stopped:
            return RuntimeObservation(
                action_id=action.action_id,
                action_kind=action.kind,
                status="failed",
                summary="Runtime has already stopped.",
                error="No actions can execute after run_stopped.",
            )

        decision, reason = self.policy.evaluate(action)
        if decision == "deny":
            observation = RuntimeObservation(
                action_id=action.action_id,
                action_kind=action.kind,
                status="policy_denied",
                summary=reason,
                error=reason,
            )
            self.store.append_event(
                self.run_id,
                "action_denied",
                action=action,
                payload={"action": action.to_dict(), "observation": observation.to_dict()},
            )
            return observation
        if decision == "approval":
            observation = RuntimeObservation(
                action_id=action.action_id,
                action_kind=action.kind,
                status="approval_required",
                summary=reason,
                data={"action": action.to_dict()},
            )
            self.store.append_event(
                self.run_id,
                "approval_required",
                action=action,
                payload={"action": action.to_dict(), "observation": observation.to_dict()},
            )
            return observation
        if action.kind == "ask_user":
            question = str(action.arguments.get("question") or "").strip()
            if not question:
                question = "The agent needs additional user input before continuing."
            observation = RuntimeObservation(
                action_id=action.action_id,
                action_kind=action.kind,
                status="input_required",
                summary=question,
                data={"question": question},
            )
            self.store.append_event(
                self.run_id,
                "input_required",
                action=action,
                payload={"action": action.to_dict(), "observation": observation.to_dict()},
            )
            return observation

        reservation = self.store.reserve(self.run_id, action)
        if reservation.status == "completed" and reservation.observation:
            observation = reservation.observation.as_replayed()
            self.store.append_event(
                self.run_id,
                "action_replayed",
                action=action,
                payload={"observation": observation.to_dict()},
            )
            return observation
        if reservation.status == "in_progress":
            observation = RuntimeObservation(
                action_id=action.action_id,
                action_kind=action.kind,
                status="recovery_required",
                summary="The action was interrupted before its result was recorded.",
                error="Automatic replay is blocked to avoid duplicating a possible side effect.",
            )
            self.store.append_event(
                self.run_id,
                "action_recovery_required",
                action=action,
                payload={"observation": observation.to_dict()},
            )
            return observation
        if reservation.status == "conflict":
            observation = RuntimeObservation(
                action_id=action.action_id,
                action_kind=action.kind,
                status="failed",
                summary="Idempotency key conflicts with a different action.",
                error="Choose a new idempotency key for the changed action payload.",
            )
            self.store.append_event(
                self.run_id,
                "action_conflict",
                action=action,
                payload={"observation": observation.to_dict()},
            )
            return observation

        self.store.append_event(
            self.run_id,
            "action_started",
            action=action,
            payload={"action": action.to_dict()},
        )
        try:
            tool_result = execute_runtime_tool(action, self.context)
            observation = RuntimeObservation(
                action_id=action.action_id,
                action_kind=action.kind,
                status="completed",
                summary=tool_result.summary,
                data=tool_result.data,
            )
        except Exception as exc:
            observation = RuntimeObservation(
                action_id=action.action_id,
                action_kind=action.kind,
                status="failed",
                summary=f"Action {action.kind} failed.",
                error=str(exc),
            )
        observation = self.store.complete(self.run_id, action, observation)
        self.store.append_event(
            self.run_id,
            "action_completed" if observation.status == "completed" else "action_failed",
            action=action,
            payload={"observation": observation.to_dict()},
        )
        return observation

    def stop(self, reason: str, summary: str = "") -> None:
        if self._stopped:
            return
        self.start()
        self.store.append_event(
            self.run_id,
            "run_stopped",
            payload={
                "reason": reason,
                "summary": summary,
                "selected_paths": self.selected_paths,
            },
        )
        self._stopped = True

    def run(
        self,
        choose_action: Callable[[list[RuntimeObservation]], RuntimeAction],
        *,
        max_steps: int,
    ) -> RuntimeRunResult:
        if max_steps <= 0:
            raise ValueError("Runtime max_steps must be greater than 0.")
        observations: list[RuntimeObservation] = []
        stop_reason = "step_limit"
        summary = ""
        for _ in range(max_steps):
            action = choose_action(list(observations))
            observation = self.execute(action)
            observations.append(observation)
            if observation.status in STOPPING_OBSERVATION_STATUSES:
                stop_reason = observation.status
                summary = observation.summary
                break
            if action.kind == "finish":
                stop_reason = "finished"
                summary = str(observation.data.get("summary") or observation.summary)
                break
        self.stop(stop_reason, summary)
        status = "completed" if stop_reason == "finished" else "waiting" if stop_reason in {
            "approval_required",
            "input_required",
            "recovery_required",
        } else "stopped"
        return RuntimeRunResult(
            run_id=self.run_id,
            status=status,
            stop_reason=stop_reason,
            observations=observations,
            events=self.events,
            selected_paths=self.selected_paths,
            summary=summary,
        )
