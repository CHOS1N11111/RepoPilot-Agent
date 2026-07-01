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
            self.assertEqual(detail["llm_traces"][0]["name"], "planner")
            self.assertIn("Budget: 9000", detail["llm_traces"][0]["context_summary"])
            self.assertEqual(detail["timeline"][0]["step"], "scan")

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


if __name__ == "__main__":
    unittest.main()
