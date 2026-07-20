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
from repopilot_agent.task_runs import (
    TaskRunError,
    checkpoint_task_run,
    clear_task_runs,
    create_task_run,
    create_task_run_branch,
    prepare_task_run_resume,
    request_task_run_cancel,
    request_task_run_pause,
    task_run_from_record,
    update_task_run,
)
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

            update_task_run(task_run, "awaiting_approval", "Proposal ready.", proposal_id="proposal-1")
            waiting = task_run.to_public_dict()
            self.assertTrue(waiting["can_approve"])
            self.assertFalse(waiting["can_pause"])

            update_task_run(task_run, "repair_pending", "Validation failed.")
            self.assertTrue(task_run.to_public_dict()["can_repair"])

    def test_pause_and_resume_at_approval_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_run = create_task_run(tmp, "fix login", [])
            update_task_run(task_run, "awaiting_approval", "Proposal ready.", proposal_id="proposal-1")

            request_task_run_pause(task_run)
            self.assertEqual(task_run.status, "paused")
            self.assertEqual(task_run.resume_status, "awaiting_approval")

            prepare_task_run_resume(task_run)
            self.assertEqual(task_run.status, "awaiting_approval")
            self.assertEqual(task_run.proposal_id, "proposal-1")

    def test_cancel_request_stops_at_safe_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_run = create_task_run(tmp, "fix login", [])
            update_task_run(task_run, "exploring", "Exploring.")

            request_task_run_cancel(task_run)
            self.assertEqual(task_run.status, "cancelling")
            self.assertTrue(checkpoint_task_run(task_run, "exploring"))
            self.assertEqual(task_run.status, "cancelled")
            self.assertIn("preserved", task_run.message)

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
            self.assertEqual(store.list_task_runs(limit=1)[0]["run_id"], task_run.run_id)

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
