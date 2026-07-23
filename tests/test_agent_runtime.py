from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.memory import MemoryStore
from repopilot_agent.runtime import (
    AgentRuntime,
    InMemoryRuntimeStore,
    RuntimeAction,
    RuntimePolicy,
    SQLiteRuntimeStore,
)


class AgentRuntimeTests(unittest.TestCase):
    def test_read_only_loop_records_ordered_action_observation_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("def parse(value):\n    return value\n", encoding="utf-8")
            actions = iter(
                [
                    RuntimeAction(
                        kind="search_files",
                        arguments={"query": "parse"},
                        action_id="search-1",
                    ),
                    RuntimeAction(
                        kind="read_file",
                        arguments={"path": "main.py"},
                        action_id="read-1",
                    ),
                    RuntimeAction(
                        kind="finish",
                        arguments={"summary": "Parser located.", "selected_paths": ["main.py"]},
                        action_id="finish-1",
                    ),
                ]
            )
            runtime = AgentRuntime(root, "find parser", store=InMemoryRuntimeStore())

            result = runtime.run(lambda _observations: next(actions), max_steps=3)

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.stop_reason, "finished")
            self.assertEqual(result.selected_paths, ["main.py"])
            self.assertIn("parse", result.observations[1].data["content"])
            self.assertEqual([event.sequence for event in result.events], list(range(1, 9)))
            self.assertEqual(
                [event.event_type for event in result.events],
                [
                    "run_started",
                    "action_started",
                    "action_completed",
                    "action_started",
                    "action_completed",
                    "action_started",
                    "action_completed",
                    "run_stopped",
                ],
            )

    def test_edit_requires_approval_and_completed_action_is_not_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "notes.txt"
            target.write_text("before\n", encoding="utf-8")
            store = InMemoryRuntimeStore()
            action = RuntimeAction(
                kind="edit_file",
                arguments={"path": "notes.txt", "new_content": "after\n", "rationale": "Update notes."},
                action_id="edit-1",
                idempotency_key="edit-notes-v1",
            )
            waiting_runtime = AgentRuntime(
                root,
                "update notes",
                run_id="run-edit",
                policy=RuntimePolicy.sandboxed(allowed_edit_paths=["notes.txt"]),
                store=store,
            )

            waiting = waiting_runtime.execute(action)

            self.assertEqual(waiting.status, "approval_required")
            self.assertEqual(target.read_text(encoding="utf-8"), "before\n")

            approved_runtime = AgentRuntime(
                root,
                "update notes",
                run_id="run-edit",
                policy=RuntimePolicy.sandboxed(
                    approved_action_ids={"edit-1"},
                    allowed_edit_paths=["notes.txt"],
                ),
                store=store,
            )
            applied = approved_runtime.execute(action)
            self.assertEqual(applied.status, "completed")
            self.assertTrue(applied.data["applied"])
            self.assertEqual(target.read_text(encoding="utf-8"), "after\n")

            target.write_text("external change\n", encoding="utf-8")
            replayed = approved_runtime.execute(action)
            self.assertTrue(replayed.replayed)
            self.assertEqual(target.read_text(encoding="utf-8"), "external change\n")
            self.assertIn("action_replayed", [event.event_type for event in store.list_events("run-edit")])

    def test_interrupted_reservation_requires_recovery_instead_of_reexecution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "notes.txt"
            target.write_text("before\n", encoding="utf-8")
            store = InMemoryRuntimeStore()
            action = RuntimeAction(
                kind="edit_file",
                arguments={"path": "notes.txt", "new_content": "after\n"},
                action_id="edit-1",
                idempotency_key="interrupted-edit",
            )
            self.assertEqual(store.reserve("run-recovery", action).status, "new")
            runtime = AgentRuntime(
                root,
                "update notes",
                run_id="run-recovery",
                policy=RuntimePolicy.sandboxed(
                    approved_action_ids={"edit-1"},
                    allowed_edit_paths=["notes.txt"],
                ),
                store=store,
            )

            observation = runtime.execute(action)

            self.assertEqual(observation.status, "recovery_required")
            self.assertIn("Automatic replay is blocked", observation.error or "")
            self.assertEqual(target.read_text(encoding="utf-8"), "before\n")

    def test_command_must_be_listed_and_action_approved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "test_sample.py").write_text(
                "import unittest\n\nclass SampleTest(unittest.TestCase):\n"
                "    def test_ok(self):\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            action = RuntimeAction(
                kind="validate",
                arguments={"command": "python -m unittest discover"},
                action_id="validate-1",
            )
            denied_runtime = AgentRuntime(
                tmp,
                "validate",
                policy=RuntimePolicy.sandboxed(allowed_commands=[]),
            )
            self.assertEqual(denied_runtime.execute(action).status, "policy_denied")

            waiting_runtime = AgentRuntime(
                tmp,
                "validate",
                policy=RuntimePolicy.sandboxed(allowed_commands=["python -m unittest discover"]),
            )
            self.assertEqual(waiting_runtime.execute(action).status, "approval_required")

            approved_runtime = AgentRuntime(
                tmp,
                "validate",
                policy=RuntimePolicy.sandboxed(
                    approved_action_ids={"validate-1"},
                    allowed_commands=["python -m unittest discover"],
                ),
            )
            completed = approved_runtime.execute(action)
            self.assertEqual(completed.status, "completed")
            self.assertTrue(completed.data["allowed"])
            self.assertEqual(completed.data["exit_code"], 0)

            command_action = RuntimeAction(
                kind="run_command",
                arguments={"command": "python -m unittest discover"},
                action_id="command-1",
            )
            command_runtime = AgentRuntime(
                tmp,
                "run tests",
                policy=RuntimePolicy.sandboxed(
                    approved_action_ids={"command-1"},
                    allowed_commands=["python -m unittest discover"],
                ),
            )
            command_result = command_runtime.execute(command_action)
            self.assertEqual(command_result.status, "completed")
            self.assertEqual(command_result.data["exit_code"], 0)

    def test_git_status_diff_and_user_input_tools_return_structured_observations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Runtime Tester"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "runtime@example.local"], cwd=root, check=True)
            target = root / "notes.txt"
            target.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "notes.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "Initial"], cwd=root, check=True, capture_output=True, text=True)
            target.write_text("after\n", encoding="utf-8")
            runtime = AgentRuntime(root, "inspect changes")

            status = runtime.execute(RuntimeAction(kind="inspect_git_status", action_id="git-1"))
            diff = runtime.execute(RuntimeAction(kind="inspect_diff", action_id="diff-1"))
            question = runtime.execute(
                RuntimeAction(
                    kind="ask_user",
                    arguments={"question": "Should this change be applied?"},
                    action_id="ask-1",
                )
            )

            self.assertEqual(status.status, "completed")
            self.assertEqual(status.data["changes"][0]["path"], "notes.txt")
            self.assertEqual(diff.status, "completed")
            self.assertIn("+after", diff.data["diff"])
            self.assertEqual(question.status, "input_required")
            self.assertEqual(question.data["question"], "Should this change be applied?")

    def test_edit_outside_allowed_path_is_denied_before_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            action = RuntimeAction(
                kind="edit_file",
                arguments={"path": "other.txt", "new_content": "content\n"},
                action_id="edit-other",
            )
            runtime = AgentRuntime(
                tmp,
                "edit notes",
                policy=RuntimePolicy.sandboxed(
                    approved_action_ids={"edit-other"},
                    allowed_edit_paths=["notes.txt"],
                ),
            )

            observation = runtime.execute(action)

            self.assertEqual(observation.status, "policy_denied")
            self.assertFalse(Path(tmp, "other.txt").exists())

    def test_sqlite_store_persists_events_and_terminal_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# Fixture\n", encoding="utf-8")
            memory = MemoryStore(root / "memory.sqlite3")
            action = RuntimeAction(
                kind="read_file",
                arguments={"path": "README.md"},
                action_id="read-1",
                idempotency_key="read-readme",
            )
            runtime = AgentRuntime(
                root,
                "read docs",
                run_id="persistent-run",
                store=SQLiteRuntimeStore(memory),
            )
            first = runtime.execute(action)
            self.assertEqual(first.status, "completed")

            reopened_store = SQLiteRuntimeStore(MemoryStore(root / "memory.sqlite3"))
            reopened = AgentRuntime(
                root,
                "read docs",
                run_id="persistent-run",
                store=reopened_store,
            )
            replayed = reopened.execute(action)

            self.assertTrue(replayed.replayed)
            events = reopened_store.list_events("persistent-run")
            self.assertEqual([event.sequence for event in events], list(range(1, len(events) + 1)))
            self.assertEqual(events[-1].event_type, "action_replayed")

    def test_same_idempotency_key_rejects_changed_action_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "one.txt").write_text("one\n", encoding="utf-8")
            (root / "two.txt").write_text("two\n", encoding="utf-8")
            store = InMemoryRuntimeStore()
            first = RuntimeAction(
                kind="read_file",
                arguments={"path": "one.txt"},
                action_id="read-1",
                idempotency_key="same-key",
            )
            changed = RuntimeAction(
                kind="read_file",
                arguments={"path": "two.txt"},
                action_id="read-2",
                idempotency_key="same-key",
            )
            runtime = AgentRuntime(root, "read", run_id="conflict-run", store=store)

            self.assertEqual(runtime.execute(first).status, "completed")
            conflict = runtime.execute(changed)

            self.assertEqual(conflict.status, "failed")
            self.assertIn("Idempotency key conflicts", conflict.summary)


if __name__ == "__main__":
    unittest.main()
