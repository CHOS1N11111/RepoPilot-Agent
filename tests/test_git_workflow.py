from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.git_summary import build_git_workflow_summary
from repopilot_agent.git_tools import _parse_status, inspect_repository


class GitWorkflowTests(unittest.TestCase):
    def test_parse_status_with_tracking_and_changes(self) -> None:
        output = "\n".join(
            [
                "## feature/git-awareness...origin/feature/git-awareness [ahead 2, behind 1]",
                " M README.md",
                "A  src/repopilot_agent/git_tools.py",
                "?? tests/test_git_workflow.py",
            ]
        )

        branch, upstream, ahead, behind, changes = _parse_status(output)

        self.assertEqual(branch, "feature/git-awareness")
        self.assertEqual(upstream, "origin/feature/git-awareness")
        self.assertEqual(ahead, 2)
        self.assertEqual(behind, 1)
        self.assertEqual(len(changes), 3)
        self.assertEqual(changes[0].description, "working tree modified")
        self.assertEqual(changes[2].description, "untracked")

    def test_git_summary_from_temporary_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            (root / "README.md").write_text("# Demo\n", encoding="utf-8")

            state = inspect_repository(root)
            summary = build_git_workflow_summary(
                root,
                validation_notes=["python -m unittest discover -s tests"],
            )

            self.assertFalse(state.clean)
            self.assertEqual(state.changes[0].path, "README.md")
            self.assertEqual(summary.suggested_commit_message, "Update project documentation")
            self.assertIn("## What changed", summary.pull_request.body)
            self.assertIn("python -m unittest discover -s tests", summary.pull_request.body)


if __name__ == "__main__":
    unittest.main()
