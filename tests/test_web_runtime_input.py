from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.llm.base import LLMMessage
from repopilot_agent.memory import MemoryStore, default_memory_path
from repopilot_agent.models import AgentStateUpdate
from repopilot_agent.runtime import (
    AgentRuntime,
    RuntimeAction,
    RuntimePolicy,
    SQLiteRuntimeStore,
    advance_agent_working_state,
    create_agent_working_state,
)
from repopilot_agent.task_runs import clear_task_runs, create_task_run, update_task_run
from repopilot_agent.web_server import RepoPilotRequestHandler, _public_runtime_events
from repopilot_agent.worktree_sandbox import create_worktree_sandbox, remove_worktree_sandbox


QUESTION = "Which empty-value behavior must remain compatible?"


def agent_decision(kind: str, arguments: dict, *, question: str = "") -> str:
    return json.dumps(
        {
            "version": 2,
            "rationale": f"Use {kind} to continue the exact Agent trajectory.",
            "action": {"kind": kind, "arguments": arguments},
            "expected_evidence": f"Typed evidence from {kind}.",
            "state_update": {
                "focus": "Resolve parser compatibility.",
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
        self.model = "fake-web-input"

    def complete(self, messages: list[LLMMessage]) -> str:
        self.calls.append(messages)
        return self.responses.pop(0)


class RuntimeInputWebApiTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_task_runs()

    def tearDown(self) -> None:
        clear_task_runs()

    def test_answer_continues_same_run_and_redacts_runtime_event(self) -> None:
        with _PendingInputTask() as fixture:
            client = FakeLLMClient(
                [agent_decision("search_files", {"query": "parser"})]
            )
            request = fixture["input_request"]
            with patch(
                "repopilot_agent.web_server._runtime_continuation_llm_client",
                return_value=client,
            ):
                data = self._post(
                    fixture["server"],
                    {
                        "run_id": fixture["task_run"].run_id,
                        "source_repo": str(fixture["source"]),
                        "checkpoint": request["checkpoint"],
                        "action_id": request["action_id"],
                        "question_hash": request["question_hash"],
                        "answer": "Keep returning the empty value unchanged.",
                        "use_llm": True,
                        "agent_max_steps": 1,
                    },
                )

            task_run = data["task_run"]
            self.assertEqual(task_run["run_id"], fixture["task_run"].run_id)
            self.assertEqual(task_run["status"], "review_pending")
            self.assertFalse(task_run["can_answer_input"])
            self.assertNotIn("answer", data["input_answer"])
            self.assertEqual(
                data["input_answer"]["answer_chars"],
                len("Keep returning the empty value unchanged."),
            )
            self.assertIn("Keep returning the empty value unchanged.", client.calls[0][1].content)
            user_input = task_run["result"]["agent_state"]["user_inputs"][0]
            self.assertEqual(user_input["answer"], "Keep returning the empty value unchanged.")
            self.assertFalse(user_input["evidence"])
            received = next(
                event
                for event in task_run["result"]["agent_events"]
                if event["event_type"] == "input_received"
            )
            self.assertNotIn("answer", received["payload"]["input_answer"])
            self.assertNotIn(
                "Keep returning the empty value unchanged.",
                json.dumps(received),
            )

            duplicate = self._post(
                fixture["server"],
                {
                    "run_id": fixture["task_run"].run_id,
                    "source_repo": str(fixture["source"]),
                    "checkpoint": request["checkpoint"],
                    "action_id": request["action_id"],
                    "question_hash": request["question_hash"],
                    "answer": "Keep returning the empty value unchanged.",
                },
            )
            self.assertEqual(
                duplicate["input_answer"]["answer_id"],
                data["input_answer"]["answer_id"],
            )
            self.assertEqual(duplicate["task_run"]["status"], "review_pending")

    def test_wrong_question_hash_does_not_consume_pending_input(self) -> None:
        with _PendingInputTask() as fixture:
            request = fixture["input_request"]
            with patch(
                "repopilot_agent.web_server._runtime_continuation_llm_client",
                return_value=FakeLLMClient([]),
            ):
                with self.assertRaises(HTTPError) as caught:
                    self._post(
                        fixture["server"],
                        {
                            "run_id": fixture["task_run"].run_id,
                            "source_repo": str(fixture["source"]),
                            "checkpoint": request["checkpoint"],
                            "action_id": request["action_id"],
                            "question_hash": "0" * 64,
                            "answer": "Keep compatibility.",
                            "use_llm": True,
                            "agent_max_steps": 1,
                        },
                    )
            body = json.loads(caught.exception.read().decode("utf-8"))
            self.assertEqual(caught.exception.code, 409)
            self.assertEqual(body["task_run"]["status"], "awaiting_input")
            events = SQLiteRuntimeStore(
                MemoryStore(default_memory_path(fixture["source"]))
            ).list_events(fixture["task_run"].run_id)
            self.assertNotIn("input_received", [event.event_type for event in events])

    def test_continuation_can_persist_a_second_exact_question(self) -> None:
        with _PendingInputTask() as fixture:
            second_question = "Should whitespace-only values also remain unchanged?"
            client = FakeLLMClient(
                [agent_decision("ask_user", {}, question=second_question)]
            )
            request = fixture["input_request"]
            with patch(
                "repopilot_agent.web_server._runtime_continuation_llm_client",
                return_value=client,
            ):
                data = self._post(
                    fixture["server"],
                    {
                        "run_id": fixture["task_run"].run_id,
                        "source_repo": str(fixture["source"]),
                        "checkpoint": request["checkpoint"],
                        "action_id": request["action_id"],
                        "question_hash": request["question_hash"],
                        "answer": "Keep empty values unchanged.",
                        "use_llm": True,
                        "agent_max_steps": 2,
                    },
                )

            next_request = data["task_run"]["result"]["agent_pending_input"]
            self.assertEqual(data["task_run"]["status"], "awaiting_input")
            self.assertTrue(data["task_run"]["can_answer_input"])
            self.assertEqual(next_request["question"], second_question)
            self.assertNotEqual(next_request["checkpoint"], request["checkpoint"])
            self.assertEqual(next_request["action_id"], "explore-2")

    @staticmethod
    def _post(server, payload: dict) -> dict:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/task-runs/runtime-input/answer",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))


class _PendingInputTask:
    def __enter__(self) -> dict:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "source"
        self.managed = root / "managed"
        self.previous_root = os.environ.get("REPOPILOT_WORKTREE_ROOT")
        os.environ["REPOPILOT_WORKTREE_ROOT"] = str(self.managed)
        self.source.mkdir()
        subprocess.run(
            ["git", "init"],
            cwd=self.source,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=self.source, check=True)
        subprocess.run(
            ["git", "config", "user.email", "tester@example.local"],
            cwd=self.source,
            check=True,
        )
        (self.source / "parser.py").write_text(
            "def parse(value):\n    return value\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "add", "parser.py"],
            cwd=self.source,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial"],
            cwd=self.source,
            check=True,
            capture_output=True,
            text=True,
        )
        self.sandbox_record = create_worktree_sandbox(
            self.source,
            name="web-runtime-input",
            worktree_root=self.managed,
        )
        self.sandbox = Path(self.sandbox_record.path)
        self.task_run = create_task_run(
            self.source,
            "update parser compatibility",
            [],
        )
        memory = MemoryStore(default_memory_path(self.source))
        store = SQLiteRuntimeStore(memory)
        runtime = AgentRuntime(
            self.sandbox,
            self.task_run.task,
            run_id=self.task_run.run_id,
            policy=RuntimePolicy.managed_worktree(
                allowed_edit_paths=["parser.py"],
                worktree_root=str(self.managed),
            ),
            store=store,
        )
        state = create_agent_working_state(self.task_run.task)
        runtime.record_working_state(state)
        action = RuntimeAction(
            kind="ask_user",
            arguments={"question": QUESTION},
            action_id="explore-1",
            idempotency_key="explore-step-1",
        )
        runtime.record_decision(
            action,
            json.loads(agent_decision("ask_user", {}, question=QUESTION)),
        )
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
        self.input_request = runtime.pending_input
        result = {
            "agent_run_id": self.task_run.run_id,
            "agent_pending_input": self.input_request,
            "agent_pending_question": QUESTION,
            "agent_events": _public_runtime_events(runtime.events),
            "agent_state": state.to_dict(),
            "agent_stop_reason": "input_required",
            "agent_steps": [],
            "llm_traces": [],
        }
        update_task_run(
            self.task_run,
            "awaiting_input",
            "Waiting for exact Runtime input.",
            sandbox_path=str(self.sandbox),
            sandbox_head=self.sandbox_record.head,
            result=result,
        )
        memory.save_task_run(self.task_run.to_record())
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            RepoPilotRequestHandler,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return {
            "source": self.source,
            "sandbox": self.sandbox,
            "task_run": self.task_run,
            "input_request": self.input_request,
            "server": self.server,
        }

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        if self.sandbox.exists():
            remove_worktree_sandbox(
                self.source,
                self.sandbox,
                force=True,
                worktree_root=self.managed,
            )
        if self.previous_root is None:
            os.environ.pop("REPOPILOT_WORKTREE_ROOT", None)
        else:
            os.environ["REPOPILOT_WORKTREE_ROOT"] = self.previous_root
        self.temp.cleanup()


if __name__ == "__main__":
    unittest.main()
