from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.cli import _print_report
from repopilot_agent.models import WorkflowReport


class CLIOutputTests(unittest.TestCase):
    def test_report_prints_agent_plan_acceptance_and_completion_state(self) -> None:
        report = WorkflowReport(
            task="inspect repository",
            repo_path="C:/repo",
            files_scanned=1,
            agent_state={
                "objective": "inspect repository",
                "phase": "inspection",
                "status": "running",
                "iteration": 1,
                "plan": [
                    {
                        "step_id": "investigate_repository",
                        "title": "Investigate repository evidence",
                        "status": "in_progress",
                    }
                ],
                "acceptance_criteria": [
                    {
                        "criterion_id": "analysis_complete",
                        "description": "Repository evidence addresses the task.",
                        "required": True,
                        "status": "pending",
                    }
                ],
            },
            agent_completion_ready=False,
            agent_completion_blockers=[
                "plan:investigate_repository",
                "acceptance:analysis_complete",
                "proposal:src/main.py:uninspected",
            ],
            agent_proposed_edits=[
                {
                    "path": "src/main.py",
                    "revision": 2,
                    "status": "proposed",
                    "inspected": False,
                }
            ],
            agent_proposed_diff="--- a/src/main.py\n+++ b/src/main.py\n",
            agent_pending_approval={
                "checkpoint": "checkpoint-1",
                "action_id": "apply-1",
                "action_kind": "apply_patch",
                "payload_hash": "a" * 64,
                "file_scope": ["src/main.py"],
                "command_allowlist": [],
                "action": {
                    "kind": "apply_patch",
                    "action_id": "apply-1",
                    "arguments": {"path": "src/main.py"},
                },
                "diff": "--- a/src/main.py\n+++ b/src/main.py\n",
                "diff_truncated": False,
            },
        )

        output = io.StringIO()
        with redirect_stdout(output):
            _print_report(report)

        rendered = output.getvalue()
        self.assertIn("Plan state:", rendered)
        self.assertIn("investigate_repository [in_progress]", rendered)
        self.assertIn("Acceptance state:", rendered)
        self.assertIn("analysis_complete [pending, required]", rendered)
        self.assertIn("Completion ready: no", rendered)
        self.assertIn("acceptance:analysis_complete", rendered)
        self.assertIn("Virtual proposed edits:", rendered)
        self.assertIn("src/main.py [revision 2, proposed, inspection required]", rendered)
        self.assertIn("Agent virtual proposed diff", rendered)
        self.assertIn("+++ b/src/main.py", rendered)
        self.assertIn("Pending runtime approval", rendered)
        self.assertIn("apply_patch apply-1 at checkpoint checkpoint-1", rendered)
        self.assertIn("Payload SHA-256: " + ("a" * 64), rendered)
        self.assertIn("File scope: src/main.py", rendered)
        self.assertIn('"kind": "apply_patch"', rendered)


if __name__ == "__main__":
    unittest.main()
