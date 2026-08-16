from __future__ import annotations

import hashlib
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

from repopilot_agent.execution import ExecutionBudget
from repopilot_agent.llm.base import LLMMessage
from repopilot_agent.memory import MemoryStore, default_memory_path
from repopilot_agent.runtime import AgentRuntime, RuntimeAction, RuntimePolicy, SQLiteRuntimeStore
from repopilot_agent.task_runs import clear_task_runs, create_task_run, update_task_run
from repopilot_agent.web_server import RepoPilotRequestHandler
from repopilot_agent.worktree_sandbox import create_worktree_sandbox, remove_worktree_sandbox


def initialize_repository(path: Path, *, validation_passes: bool = True) -> None:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.local"], cwd=path, check=True)
    (path / "notes.txt").write_text("before\n", encoding="utf-8")
    (path / "test_sample.py").write_text(
        "import unittest\n\n\n"
        "class SmokeTest(unittest.TestCase):\n"
        "    def test_smoke(self):\n"
        f"        self.assertTrue({validation_passes})\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "notes.txt", "test_sample.py"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=path, check=True, capture_output=True, text=True)


class RuntimeApprovalWebApiTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_task_runs()

    def tearDown(self) -> None:
        clear_task_runs()

    def test_grant_endpoint_executes_exact_write_and_returns_hashes_and_diff(self) -> None:
        with self._pending_task() as fixture:
            request = fixture["approval"]
            data = self._post(
                fixture["server"],
                "/api/task-runs/runtime-approval/grant",
                {
                    "run_id": fixture["task_run"].run_id,
                    "source_repo": str(fixture["source"]),
                    "checkpoint": request["checkpoint"],
                    "payload_hash": request["payload_hash"],
                    "file_scope": request["file_scope"],
                    "command_allowlist": request["command_allowlist"],
                },
            )

            task_run = data["task_run"]
            result = data["write_result"]
            self.assertEqual(task_run["status"], "review_pending")
            self.assertFalse(task_run["can_approve_runtime"])
            self.assertEqual(
                (fixture["source"] / "notes.txt").read_text(encoding="utf-8"),
                "before\n",
            )
            self.assertEqual(
                (fixture["sandbox"] / "notes.txt").read_text(encoding="utf-8"),
                "after\n",
            )
            evidence = result["write_observation"]["data"]["write_evidence"][0]
            self.assertEqual(evidence["before_sha256"], hashlib.sha256(b"before\n").hexdigest())
            self.assertEqual(evidence["after_sha256"], hashlib.sha256(b"after\n").hexdigest())
            self.assertTrue(result["rollback_available"])
            self.assertIn("+after", result["resulting_diff"])
            self.assertEqual(
                task_run["result"]["agent_write_history"],
                [
                    {
                        "action_id": "web-write",
                        "status": "completed",
                        "changed_files": ["notes.txt"],
                        "approved_paths": ["notes.txt"],
                    }
                ],
            )
            event_types = [
                event["event_type"] for event in task_run["result"]["agent_events"]
            ]
            self.assertIn("approval_consumed", event_types)
            self.assertIn("rollback_snapshot_recorded", event_types)
            snapshot_event = next(
                event
                for event in task_run["result"]["agent_events"]
                if event["event_type"] == "rollback_snapshot_recorded"
            )
            public_snapshot = snapshot_event["payload"]["snapshots"][0]
            self.assertEqual(public_snapshot, {"path": "notes.txt", "existed": True})
            public_result = json.dumps(task_run["result"])
            self.assertNotIn('"original_content"', public_result)
            self.assertNotIn('"applied_content"', public_result)

    def test_reject_endpoint_leaves_managed_worktree_unchanged(self) -> None:
        with self._pending_task() as fixture:
            request = fixture["approval"]
            data = self._post(
                fixture["server"],
                "/api/task-runs/runtime-approval/reject",
                {
                    "run_id": fixture["task_run"].run_id,
                    "source_repo": str(fixture["source"]),
                    "checkpoint": request["checkpoint"],
                    "reason": "Not this change.",
                },
            )

            self.assertEqual(data["task_run"]["status"], "cancelled")
            self.assertFalse(data["task_run"]["can_approve_runtime"])
            self.assertEqual(
                (fixture["sandbox"] / "notes.txt").read_text(encoding="utf-8"),
                "before\n",
            )
            event_types = [
                event["event_type"]
                for event in data["task_run"]["result"]["agent_events"]
            ]
            self.assertIn("approval_rejected", event_types)
            self.assertNotIn("action_started", event_types)

    def test_grant_rejects_insufficient_write_and_diff_budget(self) -> None:
        with self._pending_task() as fixture:
            fixture["task_run"].execution_budget = ExecutionBudget(max_tool_calls=1)
            request = fixture["approval"]
            with self.assertRaises(HTTPError) as caught:
                self._post(
                    fixture["server"],
                    "/api/task-runs/runtime-approval/grant",
                    {
                        "run_id": fixture["task_run"].run_id,
                        "source_repo": str(fixture["source"]),
                        "checkpoint": request["checkpoint"],
                        "payload_hash": request["payload_hash"],
                        "file_scope": request["file_scope"],
                        "command_allowlist": request["command_allowlist"],
                    },
                )

            error = json.loads(caught.exception.read().decode("utf-8"))
            self.assertEqual(caught.exception.code, 409)
            self.assertIn("cannot cover", error["error"])
            self.assertEqual(error["task_run"]["status"], "awaiting_approval")
            self.assertEqual(
                (fixture["sandbox"] / "notes.txt").read_text(encoding="utf-8"),
                "before\n",
            )

    def test_write_then_exact_validation_approval_records_passing_evidence(self) -> None:
        command = "python -m unittest -q test_sample"
        with self._pending_task([command]) as fixture:
            write_request = fixture["approval"]
            write_data = self._post(
                fixture["server"],
                "/api/task-runs/runtime-approval/grant",
                {
                    "run_id": fixture["task_run"].run_id,
                    "source_repo": str(fixture["source"]),
                    "checkpoint": write_request["checkpoint"],
                    "payload_hash": write_request["payload_hash"],
                    "file_scope": write_request["file_scope"],
                    "command_allowlist": write_request["command_allowlist"],
                },
            )

            waiting = write_data["task_run"]
            validation_request = waiting["result"]["agent_pending_approval"]
            self.assertEqual(waiting["status"], "awaiting_approval")
            self.assertTrue(waiting["can_approve_runtime"])
            self.assertEqual(validation_request["action_kind"], "validate")
            self.assertEqual(
                validation_request["action"]["arguments"]["command"],
                command,
            )
            self.assertEqual(validation_request["command_allowlist"], [command])

            validation_data = self._post(
                fixture["server"],
                "/api/task-runs/runtime-approval/grant",
                {
                    "run_id": fixture["task_run"].run_id,
                    "source_repo": str(fixture["source"]),
                    "checkpoint": validation_request["checkpoint"],
                    "payload_hash": validation_request["payload_hash"],
                    "file_scope": validation_request["file_scope"],
                    "command_allowlist": validation_request["command_allowlist"],
                    "use_llm": False,
                    "api_key": "must-not-be-persisted",
                },
            )

            task_run = validation_data["task_run"]
            result = validation_data["validation_result"]
            self.assertEqual(task_run["status"], "review_pending")
            self.assertFalse(task_run["can_approve_runtime"])
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["validation"]["command"], command)
            self.assertTrue(result["validation"]["passed"])
            self.assertEqual(result["validation"]["exit_code"], 0)
            self.assertEqual(task_run["result"]["validation"][0]["command"], command)
            cycle = task_run["result"]["agent_validation_cycle"]
            self.assertEqual(cycle["next_index"], 1)
            self.assertEqual(len(cycle["results"]), 1)
            acceptance = {
                item["criterion_id"]: item
                for item in task_run["result"]["agent_state"]["acceptance_criteria"]
            }
            self.assertEqual(acceptance["validation_1"]["status"], "passed")
            self.assertTrue(acceptance["validation_1"]["evidence_ref"])
            self.assertNotIn("must-not-be-persisted", json.dumps(task_run))

    def test_rejecting_validation_does_not_execute_the_command(self) -> None:
        command = "python -m unittest -q test_sample"
        with self._pending_task([command]) as fixture:
            write_request = fixture["approval"]
            write_data = self._post(
                fixture["server"],
                "/api/task-runs/runtime-approval/grant",
                {
                    "run_id": fixture["task_run"].run_id,
                    "source_repo": str(fixture["source"]),
                    "checkpoint": write_request["checkpoint"],
                    "payload_hash": write_request["payload_hash"],
                    "file_scope": write_request["file_scope"],
                    "command_allowlist": write_request["command_allowlist"],
                },
            )
            validation_request = write_data["task_run"]["result"][
                "agent_pending_approval"
            ]
            action_count_before = sum(
                event["event_type"] == "action_started"
                for event in write_data["task_run"]["result"]["agent_events"]
            )

            rejected = self._post(
                fixture["server"],
                "/api/task-runs/runtime-approval/reject",
                {
                    "run_id": fixture["task_run"].run_id,
                    "source_repo": str(fixture["source"]),
                    "checkpoint": validation_request["checkpoint"],
                    "reason": "Do not run tests yet.",
                },
            )

            task_run = rejected["task_run"]
            action_count_after = sum(
                event["event_type"] == "action_started"
                for event in task_run["result"]["agent_events"]
            )
            self.assertEqual(task_run["status"], "cancelled")
            self.assertEqual(rejected["rejection"]["command"], command)
            self.assertEqual(action_count_after, action_count_before)
            self.assertEqual(
                (fixture["sandbox"] / "notes.txt").read_text(encoding="utf-8"),
                "after\n",
            )

    def test_failed_validation_is_returned_to_the_same_web_agent_run(self) -> None:
        command = "python -m unittest -q test_sample"
        with self._pending_task([command], validation_passes=False) as fixture:
            write_request = fixture["approval"]
            write_data = self._post(
                fixture["server"],
                "/api/task-runs/runtime-approval/grant",
                {
                    "run_id": fixture["task_run"].run_id,
                    "source_repo": str(fixture["source"]),
                    "checkpoint": write_request["checkpoint"],
                    "payload_hash": write_request["payload_hash"],
                    "file_scope": write_request["file_scope"],
                    "command_allowlist": write_request["command_allowlist"],
                },
            )
            validation_request = write_data["task_run"]["result"][
                "agent_pending_approval"
            ]
            client = FakeLLMClient([read_file_decision("notes.txt")])

            with patch(
                "repopilot_agent.web_server._runtime_continuation_llm_client",
                return_value=client,
            ):
                data = self._post(
                    fixture["server"],
                    "/api/task-runs/runtime-approval/grant",
                    {
                        "run_id": fixture["task_run"].run_id,
                        "source_repo": str(fixture["source"]),
                        "checkpoint": validation_request["checkpoint"],
                        "payload_hash": validation_request["payload_hash"],
                        "file_scope": validation_request["file_scope"],
                        "command_allowlist": validation_request["command_allowlist"],
                        "use_llm": True,
                        "agent_max_steps": 1,
                        "api_key": "request-only-secret",
                    },
                )

            task_run = data["task_run"]
            self.assertEqual(data["validation_result"]["status"], "failed")
            self.assertEqual(task_run["status"], "review_pending")
            self.assertEqual(task_run["result"]["agent_steps"][-1]["action"], "read_file")
            self.assertEqual(task_run["result"]["agent_run_id"], task_run["run_id"])
            self.assertIn(command, client.calls[0][1].content)
            self.assertIn("Passed: no", client.calls[0][1].content)
            self.assertIn("FAILED", client.calls[0][1].content)
            self.assertNotIn("request-only-secret", json.dumps(task_run))

    def _pending_task(
        self,
        validation_commands: list[str] | None = None,
        *,
        validation_passes: bool = True,
    ):
        return _PendingTaskFixture(
            validation_commands,
            validation_passes=validation_passes,
        )

    @staticmethod
    def _post(server, path: str, payload: dict) -> dict:
        request = Request(
            f"http://127.0.0.1:{server.server_port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))


class _PendingTaskFixture:
    def __init__(
        self,
        validation_commands: list[str] | None = None,
        *,
        validation_passes: bool = True,
    ) -> None:
        self.validation_commands = list(validation_commands or [])
        self.validation_passes = validation_passes

    def __enter__(self) -> dict:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "source"
        self.managed = root / "managed"
        self.previous_worktree_root = os.environ.get("REPOPILOT_WORKTREE_ROOT")
        os.environ["REPOPILOT_WORKTREE_ROOT"] = str(self.managed)
        initialize_repository(
            self.source,
            validation_passes=self.validation_passes,
        )
        self.sandbox_record = create_worktree_sandbox(
            self.source,
            name="web-runtime-write",
            worktree_root=self.managed,
        )
        self.sandbox = Path(self.sandbox_record.path)
        self.task_run = create_task_run(
            self.source,
            "update notes",
            self.validation_commands,
        )
        memory = MemoryStore(default_memory_path(self.source))
        store = SQLiteRuntimeStore(memory)
        runtime = AgentRuntime(
            self.sandbox,
            self.task_run.task,
            run_id=self.task_run.run_id,
            policy=RuntimePolicy.managed_worktree(
                allowed_edit_paths=["notes.txt"],
                worktree_root=str(self.managed),
            ),
            store=store,
        )
        action = RuntimeAction(
            kind="apply_patch",
            arguments={
                "path": "notes.txt",
                "expected_sha256": hashlib.sha256(b"before\n").hexdigest(),
                "hunks": [{"old_text": "before", "new_text": "after"}],
            },
            action_id="web-write",
            idempotency_key="web-write-v1",
        )
        waiting = runtime.execute(action)
        runtime.stop("approval_required", waiting.summary)
        self.approval = waiting.data["approval_request"]
        result = {
            "agent_run_id": self.task_run.run_id,
            "agent_pending_approval": self.approval,
            "agent_events": [event.to_dict() for event in runtime.events],
            "agent_state": {},
            "agent_stop_reason": "approval_required",
        }
        update_task_run(
            self.task_run,
            "awaiting_approval",
            "Waiting for exact Runtime approval.",
            sandbox_path=str(self.sandbox),
            sandbox_head=self.sandbox_record.head,
            result=result,
        )
        memory.save_task_run(self.task_run.to_record())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return {
            "source": self.source,
            "managed": self.managed,
            "sandbox": self.sandbox,
            "approval": self.approval,
            "task_run": self.task_run,
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
        if self.previous_worktree_root is None:
            os.environ.pop("REPOPILOT_WORKTREE_ROOT", None)
        else:
            os.environ["REPOPILOT_WORKTREE_ROOT"] = self.previous_worktree_root
        self.temp.cleanup()


class FakeLLMClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.model = "fake-web-agent"
        self.calls: list[list[LLMMessage]] = []

    def complete(self, messages: list[LLMMessage]) -> str:
        self.calls.append(messages)
        return self.responses.pop(0)


def read_file_decision(path: str) -> str:
    return json.dumps(
        {
            "version": 2,
            "rationale": "Inspect the implementation after failed validation.",
            "action": {"kind": "read_file", "arguments": {"path": path}},
            "expected_evidence": "Current implementation content.",
            "state_update": {
                "focus": "Diagnose failed validation.",
                "add_findings": [],
                "add_open_questions": [],
                "resolve_open_questions": [],
                "plan_updates": [],
                "acceptance_updates": [],
            },
            "finish_reason": "",
            "user_question": "",
        }
    )


if __name__ == "__main__":
    unittest.main()
