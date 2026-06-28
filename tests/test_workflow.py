from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

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
            self.assertIn("RepoPilot analyzed the task", report.summary)


if __name__ == "__main__":
    unittest.main()
