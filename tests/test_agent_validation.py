from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.agent_validation import (
    AgentValidationError,
    continue_agent_after_validation,
    execute_pending_agent_validation,
    reject_pending_agent_validation,
    request_agent_validation,
)
from repopilot_agent.execution import ExecutionBudget
from repopilot_agent.llm.base import LLMMessage
from repopilot_agent.runtime import (
    AgentRuntime,
    InMemoryRuntimeStore,
    RuntimeAction,
    RuntimeObservation,
    RuntimePolicy,
    advance_agent_working_state,
    create_agent_working_state,
    prepare_post_write_acceptance,
)
from repopilot_agent.worktree_sandbox import (
    create_worktree_sandbox,
    remove_worktree_sandbox,
)


class FakeLLMClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.model = "fake-agent"
        self.calls: list[list[LLMMessage]] = []

    def complete(self, messages: list[LLMMessage]) -> str:
        self.calls.append(messages)
        return self.responses.pop(0)


def read_file_decision(path: str) -> str:
    return json.dumps(
        {
            "version": 2,
            "rationale": "Inspect the implementation after observing validation.",
            "action": {"kind": "read_file", "arguments": {"path": path}},
            "expected_evidence": "Current implementation content.",
            "state_update": {
                "focus": "Diagnose the validation result.",
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


def initialize_repository(path: Path, *, passing: bool) -> None:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.local"], cwd=path, check=True)
    (path / "main.py").write_text("value = 1\n", encoding="utf-8")
    (path / "test_sample.py").write_text(
        "import unittest\n\n"
        "class SampleTests(unittest.TestCase):\n"
        "    def test_value(self):\n"
        f"        self.assertTrue({passing})\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=path, check=True, capture_output=True, text=True)


class AgentValidationTests(unittest.TestCase):
    def test_exact_validation_waits_for_approval_and_updates_acceptance(self) -> None:
        with _ValidationFixture(passing=True) as fixture:
            request = request_agent_validation(**fixture.request_args)

            self.assertEqual(request.observation.status, "approval_required")
            self.assertEqual(request.pending_approval["action_kind"], "validate")
            self.assertEqual(request.pending_approval["command_allowlist"], [fixture.command])
            self.assertNotIn(
                "action_started",
                [event.event_type for event in fixture.store.list_events(fixture.run_id)],
            )

            approval = request.pending_approval
            result = execute_pending_agent_validation(
                **fixture.execution_args,
                checkpoint=approval["checkpoint"],
                payload_hash=approval["payload_hash"],
                file_scope=approval["file_scope"],
                command_allowlist=approval["command_allowlist"],
            )

            self.assertEqual(result.status, "passed")
            self.assertTrue(result.observation.data["passed"])
            criterion = next(
                item
                for item in result.working_state["acceptance_criteria"]
                if item["criterion_id"] == "validation_1"
            )
            self.assertEqual(criterion["status"], "passed")
            self.assertEqual(criterion["evidence_ref"], fixture.command)
            self.assertEqual(
                criterion["evidence_action_ids"],
                [result.observation.action_id],
            )

    def test_failed_validation_is_evidence_but_not_acceptance(self) -> None:
        with _ValidationFixture(passing=False) as fixture:
            request = request_agent_validation(**fixture.request_args)
            approval = request.pending_approval
            result = execute_pending_agent_validation(
                **fixture.execution_args,
                checkpoint=approval["checkpoint"],
                payload_hash=approval["payload_hash"],
                file_scope=approval["file_scope"],
                command_allowlist=approval["command_allowlist"],
            )

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.observation.status, "verification_failed")
            criterion = next(
                item
                for item in result.working_state["acceptance_criteria"]
                if item["criterion_id"] == "validation_1"
            )
            self.assertEqual(criterion["status"], "failed")
            self.assertEqual(criterion["evidence_action_ids"], [result.observation.action_id])

    def test_rejection_and_cycle_mismatch_never_execute_command(self) -> None:
        with _ValidationFixture(passing=True) as fixture:
            request = request_agent_validation(**fixture.request_args)
            approval = request.pending_approval
            with self.assertRaises(AgentValidationError):
                execute_pending_agent_validation(
                    **{**fixture.execution_args, "expected_command": "python -m unittest other"},
                    checkpoint=approval["checkpoint"],
                    payload_hash=approval["payload_hash"],
                    file_scope=approval["file_scope"],
                    command_allowlist=approval["command_allowlist"],
                )

            rejected = reject_pending_agent_validation(
                fixture.source,
                fixture.sandbox,
                fixture.task,
                fixture.run_id,
                fixture.store,
                checkpoint=approval["checkpoint"],
                expected_command=fixture.command,
                worktree_root=fixture.managed,
            )

            self.assertEqual(rejected["status"], "rejected")
            self.assertNotIn(
                "action_started",
                [event.event_type for event in fixture.store.list_events(fixture.run_id)],
            )

    def test_validation_evidence_continues_the_same_controller(self) -> None:
        with _ValidationFixture(passing=False) as fixture:
            request = request_agent_validation(**fixture.request_args)
            approval = request.pending_approval
            validation = execute_pending_agent_validation(
                **fixture.execution_args,
                checkpoint=approval["checkpoint"],
                payload_hash=approval["payload_hash"],
                file_scope=approval["file_scope"],
                command_allowlist=approval["command_allowlist"],
            )
            client = FakeLLMClient([read_file_decision("main.py")])

            continued = continue_agent_after_validation(
                fixture.source,
                fixture.sandbox,
                fixture.task,
                fixture.run_id,
                fixture.store,
                client,
                [validation],
                max_steps=1,
                execution_budget=ExecutionBudget(
                    max_agent_steps=1,
                    max_tool_calls=2,
                    max_validation_commands=1,
                    max_elapsed_seconds=60,
                ),
                worktree_root=fixture.managed,
                repair_context="Repair attempt 1 of 2 after failed validation.",
            )

            self.assertEqual(continued.steps[0].action, "read_file")
            self.assertEqual(continued.working_state.objective, fixture.task)
            self.assertGreater(
                continued.working_state.iteration,
                validation.working_state["iteration"],
            )
            prompt = client.calls[0][1].content
            self.assertIn(fixture.command, prompt)
            self.assertIn("Passed: no", prompt)
            self.assertIn("FAILED", prompt)
            self.assertIn("Repair attempt 1 of 2", prompt)
            decision_ids = [
                event.action_id
                for event in fixture.store.list_events(fixture.run_id)
                if event.event_type == "decision_recorded"
            ]
            self.assertEqual(len(decision_ids), len(set(decision_ids)))


class _ValidationFixture:
    def __init__(self, *, passing: bool) -> None:
        self.passing = passing

    def __enter__(self) -> "_ValidationFixture":
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "source"
        self.managed = root / "managed"
        initialize_repository(self.source, passing=self.passing)
        self.sandbox_record = create_worktree_sandbox(
            self.source,
            name="validation-loop",
            worktree_root=self.managed,
        )
        self.sandbox = Path(self.sandbox_record.path)
        self.store = InMemoryRuntimeStore()
        self.run_id = "validation-run"
        self.task = "validate the managed change"
        self.command = "python -m unittest test_sample"
        runtime = AgentRuntime(
            self.sandbox,
            self.task,
            run_id=self.run_id,
            policy=RuntimePolicy.managed_worktree(
                allowed_edit_paths=["main.py"],
                worktree_root=str(self.managed),
            ),
            store=self.store,
        )
        state = create_agent_working_state(self.task)
        write_action = RuntimeAction(
            kind="apply_patch",
            arguments={"path": "main.py"},
            action_id="write-cycle",
        )
        state = advance_agent_working_state(
            state,
            write_action,
            RuntimeObservation(
                action_id=write_action.action_id,
                action_kind="apply_patch",
                status="applied",
                summary="Applied the approved managed change.",
                data={"applied": True, "changed_files": ["main.py"]},
            ),
            selected_paths=["main.py"],
        )
        state = prepare_post_write_acceptance(
            state,
            write_action_id=write_action.action_id,
            changed_paths=["main.py"],
            validation_commands=[self.command],
        )
        runtime.record_working_state(state)
        self.request_args = {
            "source_repo": self.source,
            "sandbox_path": self.sandbox,
            "task": self.task,
            "run_id": self.run_id,
            "store": self.store,
            "cycle_id": write_action.action_id,
            "command_index": 0,
            "command_count": 1,
            "command": self.command,
            "worktree_root": self.managed,
        }
        self.execution_args = {
            key: value
            for key, value in self.request_args.items()
            if key != "command"
        }
        self.execution_args["expected_command"] = self.command
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.sandbox.exists():
            remove_worktree_sandbox(
                self.source,
                self.sandbox,
                force=True,
                worktree_root=self.managed,
            )
        self.temp.cleanup()


if __name__ == "__main__":
    unittest.main()
