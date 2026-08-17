from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.agent_loop import run_agent_loop
from repopilot_agent.llm.base import LLMError, LLMMessage
from repopilot_agent.models import AgentStateUpdate, RepoFile
from repopilot_agent.runtime import (
    MAX_INPUT_ANSWER_CHARS,
    AgentRuntime,
    InMemoryRuntimeStore,
    RuntimeAction,
    advance_agent_working_state,
    analyze_runtime_recovery,
    create_agent_working_state,
    create_runtime_input_answer,
)
from repopilot_agent.web_server import _public_runtime_events
from repopilot_agent.workflow import run_workflow


QUESTION = "Which compatibility behavior must remain unchanged?"


def decision(kind: str, arguments: dict, *, question: str = "") -> str:
    return json.dumps(
        {
            "version": 2,
            "rationale": "Collect the missing requirement before changing code.",
            "action": {"kind": kind, "arguments": arguments},
            "expected_evidence": "A bounded repository observation.",
            "state_update": {
                "focus": "Resolve the requested repository task.",
                "add_findings": [],
                "add_open_questions": [question] if kind == "ask_user" else [],
                "resolve_open_questions": [],
                "plan_updates": [],
                "acceptance_updates": [],
            },
            "finish_reason": "",
            "user_question": question,
        }
    )


class FakeLLMClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list[LLMMessage]] = []
        self.model = "fake-input"

    def complete(self, messages: list[LLMMessage]) -> str:
        self.calls.append(messages)
        return self.responses.pop(0)


def create_waiting_runtime(root: Path, store: InMemoryRuntimeStore) -> AgentRuntime:
    runtime = AgentRuntime(
        root,
        "update parser compatibility",
        run_id="input-run",
        store=store,
    )
    state = create_agent_working_state("update parser compatibility")
    runtime.record_working_state(state)
    action = RuntimeAction(
        kind="ask_user",
        arguments={"question": QUESTION},
        action_id="explore-1",
        idempotency_key="explore-step-1",
    )
    runtime.record_decision(action, json.loads(decision("ask_user", {}, question=QUESTION)))
    observation = runtime.execute(action)
    state = advance_agent_working_state(
        state,
        action,
        observation,
        selected_paths=[],
        state_update=AgentStateUpdate(add_open_questions=[QUESTION]),
    )
    runtime.record_working_state(state)
    runtime.stop("input_required", QUESTION)
    return runtime


class RuntimeInputTests(unittest.TestCase):
    def test_exact_answer_is_persisted_without_becoming_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = InMemoryRuntimeStore()
            runtime = create_waiting_runtime(Path(tmp), store)
            request = runtime.pending_input

            answer = runtime.submit_input(
                request["checkpoint"],
                action_id=request["action_id"],
                question_hash=request["question_hash"],
                answer="Keep accepting empty values.",
            )

            state = runtime.working_state
            self.assertEqual(answer.answer, "Keep accepting empty values.")
            self.assertEqual(runtime.pending_input, {})
            self.assertEqual(state.iteration, 1)
            self.assertEqual(state.open_questions, [])
            self.assertEqual(state.status, "running")
            self.assertEqual(len(state.user_inputs), 1)
            self.assertFalse(state.user_inputs[0].evidence)
            self.assertEqual(state.acceptance_criteria[0].status, "pending")
            self.assertEqual(
                [event.event_type for event in runtime.events].count("input_received"),
                1,
            )
            self.assertEqual(runtime.events[-1].event_type, "working_state_updated")

    def test_duplicate_identical_answer_is_idempotent_and_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = create_waiting_runtime(Path(tmp), InMemoryRuntimeStore())
            request = runtime.pending_input
            first = runtime.submit_input(
                request["checkpoint"],
                action_id=request["action_id"],
                question_hash=request["question_hash"],
                answer="Keep empty values.",
            )
            event_count = len(runtime.events)

            duplicate = runtime.submit_input(
                request["checkpoint"],
                action_id=request["action_id"],
                question_hash=request["question_hash"],
                answer="Keep empty values.",
            )

            self.assertEqual(duplicate.answer_id, first.answer_id)
            self.assertEqual(len(runtime.events), event_count)
            with self.assertRaisesRegex(ValueError, "different durable answer"):
                runtime.submit_input(
                    request["checkpoint"],
                    action_id=request["action_id"],
                    question_hash=request["question_hash"],
                    answer="Change empty values.",
                )
            with self.assertRaisesRegex(ValueError, "exact pending question"):
                runtime.submit_input(
                    request["checkpoint"],
                    action_id="other-action",
                    question_hash=request["question_hash"],
                    answer="Keep empty values.",
                )

    def test_answer_event_without_following_snapshot_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = InMemoryRuntimeStore()
            runtime = create_waiting_runtime(Path(tmp), store)
            request_record = runtime.pending_input
            request_event = next(
                event for event in runtime.events if event.event_type == "input_required"
            )
            from repopilot_agent.runtime import RuntimeInputRequest

            request = RuntimeInputRequest.from_dict(request_record)
            answer = create_runtime_input_answer(
                request,
                "Preserve empty values.",
                answered_at="2026-08-17T00:00:00+00:00",
            )
            action = RuntimeAction.from_dict(request_event.payload["action"])
            store.append_event(
                runtime.run_id,
                "input_received",
                action=action,
                payload={
                    "action": action.to_dict(),
                    "input_request": request.to_dict(),
                    "input_answer": answer.to_dict(),
                },
            )

            plan = analyze_runtime_recovery(runtime.events, objective=runtime.task)
            reopened = AgentRuntime(
                Path(tmp),
                runtime.task,
                run_id=runtime.run_id,
                store=store,
            )
            reopened.prepare_recovery()

            self.assertEqual(plan.next_step, "next_decision")
            self.assertEqual(plan.working_state.user_inputs[0].answer, "Preserve empty values.")
            self.assertEqual(reopened.working_state.user_inputs[0].answer_id, answer.answer_id)
            self.assertEqual(reopened.events[-1].event_type, "working_state_updated")

    def test_public_runtime_event_redacts_answer_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = create_waiting_runtime(Path(tmp), InMemoryRuntimeStore())
            request = runtime.pending_input
            runtime.submit_input(
                request["checkpoint"],
                action_id=request["action_id"],
                question_hash=request["question_hash"],
                answer="private answer text",
            )

            public = _public_runtime_events(runtime.events)
            received = next(
                event for event in public if event["event_type"] == "input_received"
            )
            serialized = json.dumps(received)
            self.assertNotIn("private answer text", serialized)
            self.assertNotIn('"answer":', serialized)
            self.assertEqual(received["payload"]["input_answer"]["answer_chars"], 19)

    def test_answer_length_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = create_waiting_runtime(Path(tmp), InMemoryRuntimeStore())
            request = runtime.pending_input
            with self.assertRaisesRegex(ValueError, "4000-character"):
                runtime.submit_input(
                    request["checkpoint"],
                    action_id=request["action_id"],
                    question_hash=request["question_hash"],
                    answer="x" * (MAX_INPUT_ANSWER_CHARS + 1),
                )

    def test_answer_is_available_to_next_decision_on_same_run(self) -> None:
        first_client = FakeLLMClient([decision("ask_user", {}, question=QUESTION)])
        second_client = FakeLLMClient([decision("search_files", {"query": "parser"})])
        store = InMemoryRuntimeStore()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = run_agent_loop(
                "update parser compatibility",
                root,
                [],
                [],
                first_client,
                max_steps=2,
                runtime_run_id="continued-input-run",
                runtime_store=store,
                allow_user_questions=True,
            )
            runtime = AgentRuntime(
                root,
                "update parser compatibility",
                run_id=first.runtime_run_id,
                store=store,
            )
            request = runtime.pending_input
            runtime.submit_input(
                request["checkpoint"],
                action_id=request["action_id"],
                question_hash=request["question_hash"],
                answer="Keep accepting empty values.",
            )
            continued = run_agent_loop(
                "update parser compatibility",
                root,
                [],
                [],
                second_client,
                max_steps=1,
                runtime_run_id=first.runtime_run_id,
                runtime_store=store,
                allow_user_questions=True,
                resume_existing_state=True,
            )

            prompt = second_client.calls[0][1].content
            self.assertEqual(continued.runtime_run_id, first.runtime_run_id)
            self.assertIn("Keep accepting empty values.", prompt)
            self.assertIn("not repository evidence", prompt)
            self.assertEqual(continued.working_state.iteration, 2)

    def test_question_is_not_allowed_on_last_available_step(self) -> None:
        client = FakeLLMClient([decision("ask_user", {}, question=QUESTION)])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(LLMError):
                run_agent_loop(
                    "update parser compatibility",
                    tmp,
                    [],
                    [],
                    client,
                    max_steps=1,
                    allow_user_questions=True,
                )

    def test_workflow_stops_llm_pipeline_at_durable_input_boundary(self) -> None:
        client = FakeLLMClient([decision("ask_user", {}, question=QUESTION)])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "parser.py").write_text("def parse(value):\n    return value\n", encoding="utf-8")
            report = run_workflow(
                root,
                "update parser compatibility",
                use_llm=True,
                llm_client=client,
                iterative_agent=True,
                agent_max_steps=2,
                allow_agent_questions=True,
                use_memory=False,
            )

            self.assertEqual(len(client.calls), 1)
            self.assertEqual(report.agent_stop_reason, "input_required")
            self.assertEqual(report.agent_pending_input["question"], QUESTION)
            self.assertEqual(report.plan_metadata.source, "agent_runtime")
            self.assertEqual(report.patch_proposal_metadata.source, "agent_runtime")


if __name__ == "__main__":
    unittest.main()
