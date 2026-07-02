from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.memory import MemoryStore, default_memory_path
from repopilot_agent.models import PlanMetadata, SearchHit, WorkflowReport
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

    def test_search_files_matches_symbol_variants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("def parse(value):\n    return value\n", encoding="utf-8")
            (root / "notes.md").write_text("parser notes without implementation\n", encoding="utf-8")

            files = scan_repository(root)
            hits = search_files("fix parser failure", files)

            self.assertTrue(hits)
            self.assertEqual(hits[0].path, "main.py")
            self.assertTrue(any("symbol matches 'parse'" in reason for reason in hits[0].reasons))

    def test_search_files_uses_path_intent_for_web_ui(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "server.py").write_text("def render():\n    return 'ok'\n", encoding="utf-8")
            (root / "web").mkdir()
            (root / "web" / "static").mkdir()
            (root / "web" / "static" / "app.js").write_text("function renderPanel() {}\n", encoding="utf-8")

            files = scan_repository(root)
            hits = search_files("improve web ui panel", files)

            self.assertTrue(hits)
            self.assertEqual(hits[0].path, "web/static/app.js")
            self.assertTrue(any("path intent matches web_ui" in reason for reason in hits[0].reasons))

    def test_search_files_pairs_source_and_test_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "src" / "auth.py").write_text("def login_user():\n    return True\n", encoding="utf-8")
            (root / "tests" / "test_auth.py").write_text("def test_login_user():\n    assert True\n", encoding="utf-8")

            files = scan_repository(root)
            hits = search_files("fix login behavior", files, limit=4)
            paths = [hit.path for hit in hits]

            self.assertIn("src/auth.py", paths)
            self.assertIn("tests/test_auth.py", paths)
            paired_hit = next(hit for hit in hits if hit.path == "tests/test_auth.py")
            self.assertTrue(any("paired with src/auth.py" in reason for reason in paired_hit.reasons))

    def test_search_preview_returns_multiple_matching_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = "\n".join(
                [
                    "def login_user():",
                    "    return True",
                    "",
                    "def unrelated():",
                    "    return None",
                    "",
                    "def logout_user():",
                    "    return True",
                ]
            )
            (root / "auth.py").write_text(content, encoding="utf-8")

            files = scan_repository(root)
            hits = search_files("login logout user", files)

            self.assertIn("def login_user", hits[0].preview)
            self.assertIn("def logout_user", hits[0].preview)
            self.assertIn("...", hits[0].preview)

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

    def test_run_workflow_reuses_related_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("def parse(value):\n    return value\n", encoding="utf-8")
            store = MemoryStore(default_memory_path(root))
            history_report = WorkflowReport(
                task="fix parser validation failure",
                repo_path=tmp,
                files_scanned=1,
                plan_metadata=PlanMetadata(source="rules"),
                summary="RepoPilot analyzed a parser failure and recommended parser validation.",
            )
            store.create_run(tmp, "fix parser validation failure", "run", history_report)

            report = run_workflow(root, "fix parser failure")

            self.assertTrue(report.memory_context)
            self.assertEqual(report.memory_context[0].task, "fix parser validation failure")
            self.assertTrue(any(step.title == "Review pinned and related memory" for step in report.plan))

    def test_run_workflow_prioritizes_pinned_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("def parse(value):\n    return value\n", encoding="utf-8")
            store = MemoryStore(default_memory_path(root))
            history_report = WorkflowReport(
                task="document release checklist",
                repo_path=tmp,
                files_scanned=1,
                plan_metadata=PlanMetadata(source="rules"),
                summary="RepoPilot analyzed release documentation.",
            )
            pinned_id = store.create_run(tmp, "document release checklist", "run", history_report)
            store.set_run_pinned(pinned_id, True)

            report = run_workflow(root, "fix parser failure")

            self.assertTrue(report.memory_context)
            self.assertEqual(report.memory_context[0].run_id, pinned_id)
            self.assertTrue(report.memory_context[0].pinned)

    def test_run_workflow_can_disable_related_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("def parse(value):\n    return value\n", encoding="utf-8")
            store = MemoryStore(default_memory_path(root))
            history_report = WorkflowReport(
                task="fix parser validation failure",
                repo_path=tmp,
                files_scanned=1,
                plan_metadata=PlanMetadata(source="rules"),
                summary="RepoPilot analyzed a parser failure and recommended parser validation.",
            )
            store.create_run(tmp, "fix parser validation failure", "run", history_report)

            report = run_workflow(root, "fix parser failure", use_memory=False)

            self.assertEqual(report.memory_context, [])
            self.assertFalse(any(step.title == "Review pinned and related memory" for step in report.plan))

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
