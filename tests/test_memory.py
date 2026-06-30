from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.memory import MemoryStore
from repopilot_agent.models import LLMCallTrace, PlanMetadata, WorkflowReport


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


if __name__ == "__main__":
    unittest.main()
