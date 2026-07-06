from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.memory import MemoryStore
from repopilot_agent.models import LLMCallTrace, PlanMetadata, ValidationResult, WorkflowReport


class MemoryStoreTests(unittest.TestCase):
    def test_save_and_read_proposal_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "memory.sqlite3"
            store = MemoryStore(db_path)
            session = {
                "proposal_id": "proposal-1",
                "repo_path": tmp,
                "task": "update notes",
                "file_edits": [
                    {"path": "notes.txt", "new_content": "new\n", "rationale": "Update notes."}
                ],
                "validation_commands": ["python -m unittest discover -s tests"],
                "created_at": "2026-07-06T00:00:00+00:00",
                "allowed_paths": ["notes.txt"],
                "approved_paths": ["notes.txt"],
                "applied_paths": ["notes.txt"],
                "timeline": [{"step": "apply", "status": "done", "detail": "Applied 1 file."}],
                "applied": True,
                "reverted": False,
                "rollback_snapshot": [
                    {
                        "path": "notes.txt",
                        "existed": True,
                        "original_content": "old\n",
                        "applied_content": "new\n",
                    }
                ],
                "validation": [],
                "validation_feedback": None,
            }

            store.save_proposal_session(session)
            loaded = store.get_proposal_session("proposal-1")

            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["approved_paths"], ["notes.txt"])
            self.assertEqual(loaded["rollback_snapshot"][0]["original_content"], "old\n")

            session["reverted"] = True
            store.save_proposal_session(session)
            updated = store.get_proposal_session("proposal-1")

            self.assertTrue(updated["reverted"])

    def test_create_and_read_run_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "memory.sqlite3"
            store = MemoryStore(db_path)
            report = WorkflowReport(
                task="fix parser behavior",
                repo_path=tmp,
                files_scanned=2,
                plan_metadata=PlanMetadata(source="llm", model="fake"),
                summary="RepoPilot analyzed the task.",
                llm_traces=[
                    LLMCallTrace(
                        name="planner",
                        model="fake",
                        prompt_preview="task",
                        raw_output='{"steps":[]}',
                        parsed=True,
                        latency_ms=12,
                        context_summary="Budget: 9000 chars. Included parser.py.",
                    )
                ],
            )

            run_id = store.create_run(
                repo_path=tmp,
                task="fix parser behavior",
                mode="run",
                report=report,
                timeline=[{"step": "scan", "status": "done", "detail": "Scanned 2 files."}],
            )

            runs = store.list_runs()
            detail = store.get_run(run_id)

            self.assertEqual(runs[0]["id"], run_id)
            self.assertEqual(runs[0]["task"], "fix parser behavior")
            self.assertFalse(runs[0]["pinned"])
            self.assertEqual(detail["llm_traces"][0]["name"], "planner")
            self.assertIn("Budget: 9000", detail["llm_traces"][0]["context_summary"])
            self.assertEqual(detail["timeline"][0]["step"], "scan")
            self.assertFalse(detail["pinned"])

    def test_find_related_runs_returns_bounded_memory_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "memory.sqlite3"
            store = MemoryStore(db_path)
            related_report = WorkflowReport(
                task="fix parser validation failure",
                repo_path=tmp,
                files_scanned=3,
                plan_metadata=PlanMetadata(source="rules"),
                summary="RepoPilot analyzed a parser bug and recommended a focused parser test.",
                validation=[
                    ValidationResult(
                        command="python -m unittest tests.test_parser",
                        allowed=True,
                        exit_code=0,
                        stdout="ok",
                        stderr="",
                    )
                ],
                llm_traces=[
                    LLMCallTrace(
                        name="planner",
                        model="fake",
                        prompt_preview="SECRET_PROMPT",
                        raw_output="SECRET_OUTPUT",
                        parsed=True,
                    )
                ],
            )
            unrelated_report = WorkflowReport(
                task="update readme screenshots",
                repo_path=tmp,
                files_scanned=1,
                plan_metadata=PlanMetadata(source="rules"),
                summary="RepoPilot analyzed documentation copy.",
            )

            related_id = store.create_run(tmp, "fix parser validation failure", "run", related_report)
            store.create_run(tmp, "update readme screenshots", "run", unrelated_report)

            results = store.find_related_runs("fix parser failure", limit=2)

            self.assertEqual(results[0].run_id, related_id)
            self.assertIn("parser", " ".join(results[0].reasons))
            self.assertEqual(results[0].validation[0], "python -m unittest tests.test_parser: exit 0")
            self.assertNotIn("SECRET_PROMPT", str(results[0]))
            self.assertNotIn("SECRET_OUTPUT", str(results[0]))

    def test_pinned_runs_are_returned_before_related_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "memory.sqlite3"
            store = MemoryStore(db_path)
            report = WorkflowReport(
                task="update docs",
                repo_path=tmp,
                files_scanned=1,
                plan_metadata=PlanMetadata(source="rules"),
                summary="RepoPilot analyzed documentation updates.",
            )
            pinned_id = store.create_run(tmp, "document release checklist", "run", report)
            related_id = store.create_run(tmp, "fix parser validation failure", "run", report)

            self.assertTrue(store.set_run_pinned(pinned_id, True))
            self.assertFalse(store.set_run_pinned("missing", True))

            pinned = store.list_pinned_runs()
            results = store.find_related_runs("fix parser failure", limit=2)

            self.assertEqual(pinned[0].run_id, pinned_id)
            self.assertTrue(pinned[0].pinned)
            self.assertEqual(results[0].run_id, pinned_id)
            self.assertTrue(results[0].pinned)
            self.assertEqual(results[1].run_id, related_id)
            self.assertFalse(results[1].pinned)

    def test_delete_and_clear_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "memory.sqlite3"
            store = MemoryStore(db_path)
            report = WorkflowReport(
                task="fix parser behavior",
                repo_path=tmp,
                files_scanned=1,
                plan_metadata=PlanMetadata(source="rules"),
                summary="RepoPilot analyzed parser behavior.",
                validation=[
                    ValidationResult(
                        command="python -m unittest tests.test_parser",
                        allowed=True,
                        exit_code=0,
                        stdout="ok",
                        stderr="",
                    )
                ],
            )

            first_id = store.create_run(tmp, "fix parser behavior", "run", report)
            second_id = store.create_run(tmp, "fix parser validation", "run", report)

            self.assertTrue(store.delete_run(first_id))
            self.assertIsNone(store.get_run(first_id))
            self.assertIsNotNone(store.get_run(second_id))
            self.assertFalse(store.delete_run("missing"))
            self.assertEqual(store.clear_runs(), 1)
            self.assertEqual(store.list_runs(), [])


if __name__ == "__main__":
    unittest.main()
