from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.recovery import inspect_task_run_recovery
from repopilot_agent.task_runs import (
    clear_task_runs,
    create_task_run,
    mark_task_run_interrupted,
    request_task_run_pause,
    update_task_run,
)


def initialize_repository(path: Path) -> str:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.local"], cwd=path, check=True)
    (path / "README.md").write_text("# Fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=path, check=True, capture_output=True, text=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class RecoveryReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_task_runs()

    def tearDown(self) -> None:
        clear_task_runs()

    def test_clean_sandbox_checkpoint_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            head = initialize_repository(root)
            task_run = create_task_run(root, "inspect auth", [])
            update_task_run(
                task_run,
                "exploring",
                "Exploring.",
                sandbox_path=str(root),
                sandbox_head=head,
            )
            mark_task_run_interrupted(task_run)

            readiness = inspect_task_run_recovery(task_run)
            data = readiness.to_dict()

            self.assertTrue(readiness.ready)
            self.assertEqual(data["blockers"], [])
            self.assertEqual(readiness.checkpoint, "sandbox_analysis")
            self.assertEqual(_check(data, "sandbox_clean")["status"], "passed")
            self.assertEqual(_check(data, "sandbox_head")["status"], "passed")

    def test_dirty_sandbox_blocks_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            head = initialize_repository(root)
            task_run = create_task_run(root, "inspect auth", [])
            update_task_run(
                task_run,
                "exploring",
                "Exploring.",
                sandbox_path=str(root),
                sandbox_head=head,
            )
            mark_task_run_interrupted(task_run)
            (root / "README.md").write_text("# Dirty\n", encoding="utf-8")

            data = inspect_task_run_recovery(task_run).to_dict()

            self.assertFalse(data["ready"])
            self.assertEqual(_check(data, "sandbox_clean")["status"], "failed")
            self.assertIn("uncommitted changes", data["summary"])

    def test_changed_sandbox_head_blocks_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            head = initialize_repository(root)
            task_run = create_task_run(root, "inspect auth", [])
            update_task_run(
                task_run,
                "exploring",
                "Exploring.",
                sandbox_path=str(root),
                sandbox_head=head,
            )
            mark_task_run_interrupted(task_run)
            (root / "README.md").write_text("# New commit\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "Move head"], cwd=root, check=True, capture_output=True, text=True)

            data = inspect_task_run_recovery(task_run).to_dict()

            self.assertFalse(data["ready"])
            self.assertEqual(_check(data, "sandbox_clean")["status"], "passed")
            self.assertEqual(_check(data, "sandbox_head")["status"], "failed")
            self.assertIn("HEAD changed", _check(data, "sandbox_head")["detail"])

    def test_approval_recovery_requires_matching_persisted_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_run = create_task_run(tmp, "review proposal", [])
            update_task_run(
                task_run,
                "awaiting_approval",
                "Proposal ready.",
                proposal_id="proposal-1",
            )
            request_task_run_pause(task_run)

            missing = inspect_task_run_recovery(task_run)
            invalid = inspect_task_run_recovery(task_run, ["invalid-record"])
            matching = inspect_task_run_recovery(
                task_run,
                {"proposal_id": "proposal-1", "repo_path": tmp},
            )

            self.assertFalse(missing.ready)
            self.assertIn("was not found", missing.summary)
            self.assertFalse(invalid.ready)
            self.assertIn("is invalid", invalid.summary)
            self.assertTrue(matching.ready)
            self.assertEqual(
                _check(matching.to_dict(), "proposal_session")["status"],
                "passed",
            )

    def test_checkpoint_reference_mismatch_blocks_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_run = create_task_run(tmp, "review proposal", [])
            update_task_run(
                task_run,
                "awaiting_approval",
                "Proposal ready.",
                proposal_id="proposal-1",
            )
            request_task_run_pause(task_run)
            task_run.proposal_id = "proposal-2"

            data = inspect_task_run_recovery(
                task_run,
                {"proposal_id": "proposal-2", "repo_path": tmp},
            ).to_dict()

            self.assertFalse(data["ready"])
            self.assertEqual(_check(data, "execution_checkpoint")["status"], "failed")

    def test_legacy_task_without_checkpoint_history_gets_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initialize_repository(root)
            task_run = create_task_run(root, "inspect auth", [])
            task_run.checkpoints = []
            update_task_run(task_run, "failed", "Legacy task failed.")

            data = inspect_task_run_recovery(task_run).to_dict()

            self.assertTrue(data["ready"])
            self.assertEqual(_check(data, "execution_checkpoint")["status"], "warning")
            self.assertEqual(len(data["warnings"]), 1)


def _check(readiness: dict[str, object], name: str) -> dict[str, object]:
    checks = readiness.get("checks")
    if not isinstance(checks, list):
        raise AssertionError("Recovery checks are missing.")
    return next(item for item in checks if isinstance(item, dict) and item.get("name") == name)


if __name__ == "__main__":
    unittest.main()
