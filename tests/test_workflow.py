from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.models import SearchHit
from repopilot_agent.patch_proposer import propose_patch
from repopilot_agent.scanner import scan_repository
from repopilot_agent.search import search_files
from repopilot_agent.workflow import run_workflow


class RepoPilotWorkflowTests(unittest.TestCase):
    def test_scan_repository_ignores_git_and_reads_text_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("private", encoding="utf-8")
            (root / "log.md").write_text("local notes", encoding="utf-8")
            (root / "README.md").write_text("# Demo\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("def login():\n    return True\n", encoding="utf-8")

            files = scan_repository(root)
            paths = {repo_file.relative_path for repo_file in files}

            self.assertEqual(paths, {"README.md", "src/app.py"})

    def test_search_files_ranks_path_and_content_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auth.py").write_text("def login_user():\n    return 'ok'\n", encoding="utf-8")
            (root / "README.md").write_text("General documentation\n", encoding="utf-8")

            files = scan_repository(root)
            hits = search_files("fix login behavior", files)

            self.assertTrue(hits)
            self.assertEqual(hits[0].path, "auth.py")

    def test_run_workflow_returns_plan_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("def calculate_total(items):\n    return sum(items)\n", encoding="utf-8")

            report = run_workflow(root, "fix calculate total for empty items")

            self.assertEqual(report.files_scanned, 1)
            self.assertGreaterEqual(len(report.plan), 5)
            self.assertIsNotNone(report.patch_proposal)
            self.assertTrue(report.patch_proposal.ready_for_patch)
            self.assertIn("RepoPilot analyzed the task", report.summary)
            self.assertIn("Prepared file-level change proposals", report.summary)

    def test_patch_proposal_describes_file_changes_and_risks(self) -> None:
        hits = [
            SearchHit(
                path="src/auth.py",
                score=12,
                reasons=["path matches 'auth'", "content matches 'token'"],
                preview="def validate_token(token):",
            ),
            SearchHit(
                path="tests/test_auth.py",
                score=7,
                reasons=["content matches 'auth'"],
                preview="def test_validate_token():",
            ),
        ]

        proposal = propose_patch("fix auth token validation", hits)

        self.assertTrue(proposal.ready_for_patch)
        self.assertEqual(proposal.files[0].path, "src/auth.py")
        self.assertEqual(proposal.files[0].change_type, "bugfix")
        self.assertEqual(proposal.files[1].change_type, "test")
        self.assertTrue(any(risk.level == "high" for risk in proposal.risks))
        self.assertIn("python -m unittest discover -s tests", proposal.validation_suggestions)


if __name__ == "__main__":
    unittest.main()
