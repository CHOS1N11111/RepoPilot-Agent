from __future__ import annotations

import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.memory import MemoryStore
from repopilot_agent.runtime import (
    AgentRuntime,
    InMemoryRuntimeStore,
    RuntimeAction,
    SQLiteRuntimeStore,
    create_agent_working_state,
    normalize_parallel_read_arguments,
    runtime_action_tool_call_cost,
    runtime_started_tool_call_count,
)
from repopilot_agent.runtime import tools as runtime_tools


def batch_action(*members: dict, action_id: str = "batch-1") -> RuntimeAction:
    return RuntimeAction(
        kind="parallel_read",
        arguments={"actions": list(members)},
        action_id=action_id,
        idempotency_key=action_id,
    )


def read_member(path: str) -> dict:
    return {"kind": "read_file", "arguments": {"path": path}}


class ParallelReadBatchTests(unittest.TestCase):
    def test_contract_normalizes_members_and_rejects_unsafe_or_duplicate_work(self) -> None:
        normalized = normalize_parallel_read_arguments(
            {
                "actions": [
                    read_member("src\\main.py"),
                    {
                        "kind": "inspect_diff",
                        "arguments": {},
                    },
                ]
            }
        )

        self.assertEqual(normalized["actions"][0]["arguments"]["path"], "src/main.py")
        self.assertEqual(normalized["actions"][1]["arguments"], {"staged": False})
        invalid = [
            {"actions": [read_member("one.py")]},
            {"actions": [read_member("../one.py"), read_member("two.py")]},
            {"actions": [read_member("one.py"), read_member("one.py")]},
            {
                "actions": [
                    read_member("one.py"),
                    {"kind": "validate", "arguments": {"command": "python bad.py"}},
                ]
            },
            {
                "actions": [
                    read_member("one.py"),
                    {"kind": "parallel_read", "arguments": {"actions": []}},
                ]
            },
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                normalize_parallel_read_arguments(arguments)
        with self.assertRaisesRegex(ValueError, "remaining tool calls"):
            normalize_parallel_read_arguments(
                {"actions": [read_member("one.py"), read_member("two.py")]},
                max_actions=1,
            )
        with self.assertRaisesRegex(ValueError, "must be an integer"):
            normalize_parallel_read_arguments(
                {"actions": [read_member("one.py"), read_member("two.py")]},
                max_actions=True,
            )

    def test_members_execute_concurrently_but_results_and_paths_keep_request_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "first.txt").write_text("first\n", encoding="utf-8")
            (root / "second.txt").write_text("second\n", encoding="utf-8")
            action = batch_action(read_member("first.txt"), read_member("second.txt"))
            runtime = AgentRuntime(root, "read both files")
            original = runtime_tools._execute_parallel_read_member
            barrier = threading.Barrier(2)
            completed: list[str] = []
            thread_ids: set[int] = set()
            lock = threading.Lock()

            def synchronized(member, parent_context, selected_paths):
                with lock:
                    thread_ids.add(threading.get_ident())
                barrier.wait(timeout=2)
                if member.arguments["path"] == "first.txt":
                    time.sleep(0.03)
                result = original(member, parent_context, selected_paths)
                with lock:
                    completed.append(member.arguments["path"])
                return result

            with patch(
                "repopilot_agent.runtime.tools._execute_parallel_read_member",
                side_effect=synchronized,
            ):
                observation = runtime.execute(action)

        self.assertEqual(observation.status, "completed")
        self.assertEqual(len(thread_ids), 2)
        self.assertEqual(completed, ["second.txt", "first.txt"])
        self.assertEqual(
            [item["data"]["path"] for item in observation.data["results"]],
            ["first.txt", "second.txt"],
        )
        self.assertEqual(runtime.selected_paths, ["first.txt", "second.txt"])
        started = next(event for event in runtime.events if event.event_type == "action_started")
        self.assertEqual(started.payload["tool_call_cost"], 2)

    def test_partial_failure_preserves_success_and_all_failure_fails_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "present.txt").write_text("present\n", encoding="utf-8")
            runtime = AgentRuntime(root, "read files")

            partial = runtime.execute(
                batch_action(
                    read_member("present.txt"),
                    read_member("missing.txt"),
                    action_id="batch-partial",
                )
            )
            failed = runtime.execute(
                batch_action(
                    read_member("missing-one.txt"),
                    read_member("missing-two.txt"),
                    action_id="batch-failed",
                )
            )

        self.assertEqual(partial.status, "completed")
        self.assertEqual(partial.data["completed_count"], 1)
        self.assertEqual(partial.data["failed_count"], 1)
        self.assertEqual(partial.data["results"][1]["status"], "failed")
        self.assertIn("does not exist", partial.data["results"][1]["error"])
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.data["completed_count"], 0)

    def test_member_content_is_bounded_without_losing_complete_file_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "large.txt").write_text("x" * 8_000, encoding="utf-8")
            (root / "small.txt").write_text("small\n", encoding="utf-8")
            observation = AgentRuntime(root, "read bounded files").execute(
                batch_action(read_member("large.txt"), read_member("small.txt"))
            )

        first = observation.data["results"][0]["data"]
        self.assertLessEqual(len(first["content"]), 6_000)
        self.assertTrue(first["truncated"])
        self.assertEqual(len(first["sha256"]), 64)

        bounded = runtime_tools._bound_parallel_member_data(
            "inspect_git_status",
            {
                "changes": list(range(12)),
                "remotes": list(range(12)),
                "selected_paths": [f"file-{index}.txt" for index in range(12)],
                "diff_stat": "x" * 7_000,
                "staged_diff_stat": "y" * 7_000,
            },
        )
        self.assertEqual(len(bounded["changes"]), 8)
        self.assertEqual(len(bounded["remotes"]), 8)
        self.assertEqual(len(bounded["selected_paths"]), 8)
        self.assertLessEqual(len(bounded["diff_stat"]), 6_000)
        self.assertLessEqual(len(bounded["staged_diff_stat"]), 6_000)
        self.assertTrue(bounded["changes_truncated"])
        self.assertTrue(bounded["diff_stat_truncated"])

    def test_completed_batch_replays_without_running_members_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "one.txt").write_text("one\n", encoding="utf-8")
            (root / "two.txt").write_text("two\n", encoding="utf-8")
            runtime = AgentRuntime(root, "read files")
            action = batch_action(read_member("one.txt"), read_member("two.txt"))
            original = runtime_tools._execute_parallel_read_member
            calls = 0

            def counted(member, parent_context, selected_paths):
                nonlocal calls
                calls += 1
                return original(member, parent_context, selected_paths)

            with patch(
                "repopilot_agent.runtime.tools._execute_parallel_read_member",
                side_effect=counted,
            ):
                first = runtime.execute(action)
                replayed = runtime.execute(action)

        self.assertEqual(first.status, "completed")
        self.assertTrue(replayed.replayed)
        self.assertEqual(calls, 2)
        self.assertEqual(runtime_started_tool_call_count(runtime.events), 2)

    def test_sqlite_replay_and_interrupted_retry_keep_exact_batch_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "one.txt").write_text("one\n", encoding="utf-8")
            (root / "two.txt").write_text("two\n", encoding="utf-8")
            database = root / "runtime.sqlite3"
            action = batch_action(read_member("one.txt"), read_member("two.txt"))
            first_store = SQLiteRuntimeStore(MemoryStore(database))
            first = AgentRuntime(
                root,
                "read files",
                run_id="sqlite-batch",
                store=first_store,
            )
            self.assertEqual(first.execute(action).status, "completed")

            reopened = AgentRuntime(
                root,
                "read files",
                run_id="sqlite-batch",
                store=SQLiteRuntimeStore(MemoryStore(database)),
            )
            self.assertTrue(reopened.execute(action).replayed)
            self.assertEqual(runtime_started_tool_call_count(reopened.events), 2)

            store = InMemoryRuntimeStore()
            interrupted = AgentRuntime(
                root,
                "read files",
                run_id="interrupted-batch",
                store=store,
            )
            interrupted.record_working_state(create_agent_working_state("read files"))
            interrupted.record_decision(
                action,
                {
                    "version": 2,
                    "rationale": "Read independent files.",
                    "action": {"kind": action.kind, "arguments": action.arguments},
                    "expected_evidence": "Both file bodies.",
                    "state_update": {
                        "focus": "Read files.",
                        "add_findings": [],
                        "add_open_questions": [],
                        "resolve_open_questions": [],
                        "plan_updates": [],
                        "acceptance_updates": [],
                    },
                    "finish_reason": "",
                    "user_question": "",
                },
            )
            store.reserve(interrupted.run_id, action)
            store.append_event(
                interrupted.run_id,
                "action_started",
                action=action,
                payload={
                    "action": action.to_dict(),
                    "tool_call_cost": runtime_action_tool_call_cost(action),
                },
            )
            recovered = AgentRuntime(
                root,
                "read files",
                run_id=interrupted.run_id,
                store=store,
            )
            observation = recovered.resume_recoverable_action()

        self.assertEqual(observation.status, "completed")
        self.assertEqual(recovered.recovery_plan.next_step, "next_decision")
        self.assertEqual(
            recovered.recovery_plan.working_state.selected_paths,
            ["one.txt", "two.txt"],
        )
        recovery_started = next(
            event
            for event in recovered.events
            if event.event_type == "action_recovery_started"
        )
        self.assertEqual(recovery_started.payload["tool_call_cost"], 2)
        self.assertEqual(runtime_started_tool_call_count(recovered.events), 4)


if __name__ == "__main__":
    unittest.main()
