from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.memory import MemoryStore, default_memory_path
from repopilot_agent.execution import (
    ExecutionBudget,
    ExecutionUsage,
    build_acceptance_criteria,
    pending_completion_evidence,
)
from repopilot_agent.task_runs import (
    RESUME_CHECKPOINT_APPROVAL,
    RESUME_CHECKPOINT_BLOCKED,
    RESUME_CHECKPOINT_INSPECTION,
    RESUME_CHECKPOINT_SANDBOX,
    TaskRunError,
    build_task_run_resume_plan,
    checkpoint_task_run,
    clear_task_runs,
    create_task_run,
    create_task_run_branch,
    mark_task_run_interrupted,
    prepare_task_run_resume,
    record_task_run_checkpoint,
    request_task_run_cancel,
    request_task_run_pause,
    task_run_from_record,
    update_task_run,
)
from repopilot_agent.models import ValidationResult
from repopilot_agent.repair_loop import RepairAttemptRecord, record_validation_outcome
from repopilot_agent.worktree_sandbox import create_worktree_sandbox, remove_worktree_sandbox


def initialize_repository(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.local"], cwd=path, check=True)
    (path / "README.md").write_text("# Fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=path, check=True, capture_output=True, text=True)


class TaskRunStateTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_task_runs()

    def tearDown(self) -> None:
        clear_task_runs()

    def test_create_and_update_exposes_control_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_run = create_task_run(tmp, "fix login", ["python -m unittest"])

            queued = task_run.to_public_dict()
            self.assertEqual(queued["status"], "queued")
            self.assertTrue(queued["can_pause"])
            self.assertTrue(queued["can_cancel"])
            self.assertNotIn("api_key", queued)
            self.assertEqual(queued["latest_checkpoint"]["phase"], "task_queued")
            self.assertEqual(queued["latest_checkpoint"]["next_action"], "create_sandbox")

            update_task_run(task_run, "awaiting_approval", "Proposal ready.", proposal_id="proposal-1")
            waiting = task_run.to_public_dict()
            self.assertTrue(waiting["can_approve"])
            self.assertFalse(waiting["can_pause"])

            update_task_run(task_run, "repair_pending", "Validation failed.")
            self.assertTrue(task_run.to_public_dict()["can_repair"])

    def test_checkpoint_captures_runtime_state_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_run = create_task_run(
                tmp,
                "fix login",
                [],
                execution_budget=ExecutionBudget(max_agent_steps=5, max_tool_calls=8),
            )
            task_run.sandbox_path = str(Path(tmp) / "sandbox")
            task_run.sandbox_head = "abc123"
            task_run.proposal_id = "proposal-1"
            task_run.execution_usage = ExecutionUsage(agent_steps=2, tool_calls=3)
            task_run.repair_history = [RepairAttemptRecord(attempt=2, status="proposal_ready")]

            checkpoint = record_task_run_checkpoint(
                task_run,
                "repair_ready",
                "Repair proposal is ready.",
                "review_repair_proposal",
            )
            restored = task_run_from_record(task_run.to_record())
            public = restored.to_public_dict()

            self.assertEqual(checkpoint.sequence, 2)
            self.assertEqual(checkpoint.execution_usage["tool_calls"], 3)
            self.assertEqual(checkpoint.execution_remaining["agent_steps"], 3)
            self.assertEqual(checkpoint.repair_attempt, 2)
            self.assertEqual(checkpoint.sandbox_head, "abc123")
            self.assertEqual(public["latest_checkpoint"]["proposal_id"], "proposal-1")
            self.assertEqual(len(public["checkpoints"]), 2)

    def test_checkpoint_history_is_bounded_and_legacy_records_remain_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_run = create_task_run(tmp, "fix login", [])
            for index in range(104):
                record_task_run_checkpoint(
                    task_run,
                    "exploration_step",
                    f"Completed step {index + 1}.",
                    "continue_exploration",
                )

            restored = task_run_from_record(task_run.to_record())
            self.assertEqual(len(restored.checkpoints), 100)
            self.assertEqual(restored.checkpoints[0].sequence, 6)
            self.assertEqual(restored.checkpoints[-1].sequence, 105)

            legacy_record = task_run.to_record()
            legacy_record.pop("checkpoints")
            legacy = task_run_from_record(legacy_record)
            self.assertEqual(legacy.checkpoints, [])
            self.assertIsNone(legacy.to_public_dict()["latest_checkpoint"])

    def test_pause_and_resume_at_approval_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_run = create_task_run(tmp, "fix login", [])
            update_task_run(task_run, "awaiting_approval", "Proposal ready.", proposal_id="proposal-1")

            request_task_run_pause(task_run)
            self.assertEqual(task_run.status, "paused")
            self.assertEqual(task_run.resume_status, "awaiting_approval")
            self.assertEqual(task_run.resume_checkpoint, RESUME_CHECKPOINT_APPROVAL)
            self.assertEqual(task_run.checkpoints[-1].phase, "paused")

            with self.assertRaises(TaskRunError):
                prepare_task_run_resume(task_run, RESUME_CHECKPOINT_APPROVAL, confirmed=False)
            prepare_task_run_resume(task_run, RESUME_CHECKPOINT_APPROVAL, confirmed=True)
            self.assertEqual(task_run.status, "awaiting_approval")
            self.assertEqual(task_run.proposal_id, "proposal-1")
            self.assertIsNone(task_run.resume_checkpoint)
            self.assertEqual(task_run.last_resume_checkpoint, RESUME_CHECKPOINT_APPROVAL)
            self.assertEqual(task_run.resume_count, 1)
            self.assertIsNotNone(task_run.last_resumed_at)
            self.assertEqual(task_run.checkpoints[-1].phase, "manual_resume")

            restored = task_run_from_record(task_run.to_record())
            self.assertEqual(restored.last_resume_checkpoint, RESUME_CHECKPOINT_APPROVAL)
            self.assertEqual(restored.resume_count, 1)

    def test_cancel_request_stops_at_safe_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_run = create_task_run(tmp, "fix login", [])
            update_task_run(task_run, "exploring", "Exploring.")

            request_task_run_cancel(task_run)
            self.assertEqual(task_run.status, "cancelling")
            self.assertTrue(checkpoint_task_run(task_run, "exploring"))
            self.assertEqual(task_run.status, "cancelled")
            self.assertIn("preserved", task_run.message)
            self.assertEqual(task_run.checkpoints[-1].phase, "cancelled")

    def test_memory_store_round_trip_and_interrupted_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = MemoryStore(root / "memory.sqlite3")
            task_run = create_task_run(root, "fix login", ["python -m unittest"])
            update_task_run(task_run, "exploring", "Exploring repository.", sandbox_path=str(root / "sandbox"))
            store.save_task_run(task_run.to_record())

            record = store.get_task_run(task_run.run_id)
            self.assertIsNotNone(record)
            clear_task_runs()
            restored = task_run_from_record(record or {}, mark_interrupted=True)

            self.assertEqual(restored.status, "interrupted")
            self.assertEqual(restored.task, "fix login")
            self.assertEqual(restored.interrupted_from, "exploring")
            self.assertEqual(restored.resume_status, "exploring")
            self.assertEqual(restored.interruption_reason, "server_restart")
            self.assertEqual(restored.resume_checkpoint, RESUME_CHECKPOINT_SANDBOX)
            self.assertIsNotNone(restored.interrupted_at)
            self.assertIn("No work was resumed automatically", restored.message)
            self.assertEqual(restored.checkpoints[-1].phase, "interrupted")
            self.assertEqual(store.list_task_runs(limit=1)[0]["run_id"], task_run.run_id)

    def test_interrupt_marking_is_idempotent_for_non_active_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_run = create_task_run(tmp, "fix login", [])
            update_task_run(task_run, "validating", "Running validation.")

            mark_task_run_interrupted(task_run)
            event_count = len(task_run.events)
            detected_at = task_run.interrupted_at
            mark_task_run_interrupted(task_run)

            self.assertEqual(task_run.status, "interrupted")
            self.assertEqual(task_run.interrupted_from, "validating")
            self.assertEqual(task_run.resume_status, "validating")
            self.assertEqual(task_run.interrupted_at, detected_at)
            self.assertEqual(len(task_run.events), event_count)

    def test_interrupted_write_phase_requires_sandbox_inspection_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_run = create_task_run(tmp, "fix login", [])
            update_task_run(task_run, "applying", "Applying proposal.", sandbox_path=tmp)

            mark_task_run_interrupted(task_run)
            plan = build_task_run_resume_plan(task_run)

            self.assertEqual(plan.checkpoint, RESUME_CHECKPOINT_INSPECTION)
            self.assertTrue(plan.allowed)
            self.assertTrue(plan.reuse_sandbox)
            self.assertTrue(plan.requires_clean_sandbox)

    def test_interrupted_replanning_uses_inspection_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_run = create_task_run(tmp, "fix login", [])
            update_task_run(task_run, "replanning", "Preparing repair.", sandbox_path=tmp)

            mark_task_run_interrupted(task_run)

            self.assertEqual(task_run.resume_checkpoint, RESUME_CHECKPOINT_INSPECTION)

    def test_interrupted_cancellation_has_no_resume_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_run = create_task_run(tmp, "fix login", [])
            update_task_run(task_run, "cancelling", "Cancellation in progress.", sandbox_path=tmp)

            mark_task_run_interrupted(task_run)
            public = task_run.to_public_dict()

            self.assertEqual(public["resume_checkpoint"], RESUME_CHECKPOINT_BLOCKED)
            self.assertFalse(public["can_resume"])
            self.assertIn("Cancellation was in progress", public["resume_blocked_reason"])

    def test_resume_rejects_stale_checkpoint_without_changing_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_run = create_task_run(tmp, "fix login", [])
            update_task_run(task_run, "exploring", "Exploring.", sandbox_path=tmp)
            mark_task_run_interrupted(task_run)

            with self.assertRaises(TaskRunError) as raised:
                prepare_task_run_resume(task_run, RESUME_CHECKPOINT_APPROVAL, confirmed=True)

            self.assertIn("checkpoint changed", str(raised.exception))
            self.assertEqual(task_run.status, "interrupted")
            self.assertEqual(task_run.resume_count, 0)

    def test_execution_contract_round_trips_through_persistent_task_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            criteria = build_acceptance_criteria("update docs", ["README.md"], [])
            task_run = create_task_run(
                tmp,
                "update docs",
                [],
                execution_budget=ExecutionBudget(
                    max_agent_steps=4,
                    max_tool_calls=7,
                    max_validation_commands=2,
                    max_elapsed_seconds=90,
                ),
            )
            task_run.acceptance_criteria = criteria
            task_run.execution_usage = ExecutionUsage(agent_steps=2, tool_calls=3, elapsed_ms=1200)
            task_run.completion_evidence = pending_completion_evidence(criteria)

            restored = task_run_from_record(task_run.to_record())
            public = restored.to_public_dict()

            self.assertEqual(restored.execution_budget.max_tool_calls, 7)
            self.assertEqual(restored.execution_usage.agent_steps, 2)
            self.assertEqual(restored.acceptance_criteria[0].criterion_id, "task_change")
            self.assertEqual(restored.completion_evidence.status, "pending")
            self.assertEqual(public["execution_budget"]["remaining"]["tool_calls"], 4)

    def test_repair_loop_state_round_trips_with_automation_setting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_run = create_task_run(tmp, "fix login", [], auto_repair_enabled=True)
            task_run.repair_history, _ = record_validation_outcome(
                [],
                attempt=0,
                validation=[ValidationResult("python -m unittest", True, 1, "", "failed")],
                summary="Validation failed.",
            )
            task_run.repair_stop_reason = "repeated_validation_failure"
            task_run.repair_stop_message = "No progress."

            restored = task_run_from_record(task_run.to_record())
            public = restored.to_public_dict()

            self.assertTrue(restored.auto_repair_enabled)
            self.assertEqual(restored.repair_history, task_run.repair_history)
            self.assertEqual(public["repair_stop_reason"], "repeated_validation_failure")
            self.assertEqual(public["repair_stop_message"], "No progress.")

    def test_repository_memory_uses_local_git_exclude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initialize_repository(root)

            MemoryStore(default_memory_path(root))
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            exclude_path = subprocess.run(
                ["git", "rev-parse", "--git-path", "info/exclude"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            exclude_file = Path(exclude_path)
            if not exclude_file.is_absolute():
                exclude_file = root / exclude_file

            self.assertEqual(status, "")
            self.assertIn(".repopilot/", exclude_file.read_text(encoding="utf-8"))

    def test_branch_creation_requires_managed_completed_sandbox_and_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            managed = root / "managed"
            source.mkdir()
            initialize_repository(source)
            with patch.dict(os.environ, {"REPOPILOT_WORKTREE_ROOT": str(managed)}):
                sandbox = create_worktree_sandbox(source, name="delivery-case")
                task_run = create_task_run(source, "update readme", [])
                update_task_run(
                    task_run,
                    "completed",
                    "Completed.",
                    sandbox_path=sandbox.path,
                    sandbox_head=sandbox.head,
                )
                (Path(sandbox.path) / "README.md").write_text("# Updated\n", encoding="utf-8")
                try:
                    with self.assertRaises(TaskRunError):
                        create_task_run_branch(task_run, "feature/task-run", confirmed=False)

                    branch = create_task_run_branch(task_run, "feature/task-run", confirmed=True)
                    current = subprocess.run(
                        ["git", "branch", "--show-current"],
                        cwd=sandbox.path,
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.strip()
                    count = subprocess.run(
                        ["git", "rev-list", "--count", "HEAD"],
                        cwd=sandbox.path,
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.strip()

                    self.assertEqual(branch, "feature/task-run")
                    self.assertEqual(current, branch)
                    self.assertEqual(count, "1")
                    self.assertEqual(task_run.delivery_branch, branch)
                    self.assertIn("uncommitted and unpushed", task_run.message)
                finally:
                    remove_worktree_sandbox(source, sandbox.path, force=True)

    def test_branch_creation_rejects_primary_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initialize_repository(root)
            task_run = create_task_run(root, "update readme", [])
            update_task_run(task_run, "completed", "Completed.", sandbox_path=str(root))

            with self.assertRaises(TaskRunError) as raised:
                create_task_run_branch(task_run, "feature/unsafe", confirmed=True)

            self.assertIn("registered managed worktrees", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
