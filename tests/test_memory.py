from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.memory import MemoryStore
from repopilot_agent.models import LLMCallTrace, PlanMetadata, ValidationResult, WorkflowReport


class MemoryStoreTests(unittest.TestCase):
    def test_existing_trace_table_migrates_token_usage_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.sqlite3"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE llm_traces (
                        id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        model TEXT NOT NULL,
                        prompt_preview TEXT NOT NULL,
                        raw_output TEXT NOT NULL,
                        parsed INTEGER NOT NULL,
                        fallback_used INTEGER NOT NULL,
                        error TEXT,
                        latency_ms INTEGER,
                        context_summary TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO llm_traces (
                        id, run_id, name, model, prompt_preview, raw_output,
                        parsed, fallback_used, context_summary
                    ) VALUES ('trace-1', 'run-1', 'planner', 'legacy', '', '', 1, 0, '')
                    """
                )
                conn.commit()

            MemoryStore(db_path)

            with closing(sqlite3.connect(db_path)) as conn:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(llm_traces)").fetchall()
                }
                row = conn.execute(
                    "SELECT input_tokens, output_tokens, total_tokens FROM llm_traces"
                ).fetchone()
            self.assertTrue({"input_tokens", "output_tokens", "total_tokens"}.issubset(columns))
            self.assertEqual(row, (None, None, None))

    def test_existing_runs_table_migrates_repository_instructions_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.sqlite3"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE runs (
                        id TEXT PRIMARY KEY,
                        repo_path TEXT NOT NULL,
                        task TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        proposal_id TEXT,
                        plan_source TEXT,
                        proposal_source TEXT,
                        review_source TEXT,
                        applied INTEGER NOT NULL DEFAULT 0,
                        pinned INTEGER NOT NULL DEFAULT 0,
                        timeline_json TEXT NOT NULL,
                        agent_runtime_run_id TEXT
                    )
                    """
                )
                conn.commit()

            MemoryStore(db_path)

            with closing(sqlite3.connect(db_path)) as conn:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()
                }
            self.assertIn("repository_instructions_json", columns)

    def test_list_task_runs_by_status_returns_all_matching_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "memory.sqlite3")
            for index, status in enumerate(["exploring", "completed", "validating"]):
                store.save_task_run(
                    {
                        "run_id": f"task-{index}",
                        "source_repo": tmp,
                        "status": status,
                    }
                )

            active = store.list_task_runs_by_status({"exploring", "validating"})

            self.assertEqual({item["run_id"] for item in active}, {"task-0", "task-2"})
            self.assertEqual(store.list_task_runs_by_status(set()), [])

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
                "parent_proposal_id": "proposal-root",
                "repair_attempt": 1,
                "max_repair_attempts": 2,
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
            self.assertEqual(loaded["parent_proposal_id"], "proposal-root")
            self.assertEqual(loaded["repair_attempt"], 1)
            self.assertEqual(loaded["max_repair_attempts"], 2)
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
                agent_run_id="runtime-1",
                agent_pending_approval={
                    "checkpoint": "approval-1",
                    "action_id": "apply-1",
                    "action_kind": "apply_patch",
                    "payload_hash": "a" * 64,
                },
                repository_instructions={
                    "text": "Bounded root guidance.",
                    "files": [
                        {
                            "path": "AGENTS.md",
                            "scope": ".",
                            "precedence": 1,
                            "content_sha256": "b" * 64,
                        }
                    ],
                },
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
                        input_tokens=90,
                        output_tokens=10,
                        total_tokens=100,
                    )
                ],
            )
            store.append_agent_runtime_event(
                "runtime-1",
                "run_started",
                payload={"task": "fix parser behavior"},
            )
            store.append_agent_runtime_event(
                "runtime-1",
                "working_state_updated",
                payload={
                    "working_state": {
                        "plan": [
                            {
                                "step_id": "inspect",
                                "status": "completed",
                                "evidence_action_ids": ["read-1"],
                            }
                        ],
                        "acceptance_criteria": [],
                    }
                },
            )
            store.append_agent_runtime_event(
                "runtime-1",
                "approval_required",
                action_id="apply-1",
                payload={"approval_request": report.agent_pending_approval},
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
            self.assertEqual(runs[0]["agent_runtime_run_id"], "runtime-1")
            self.assertFalse(runs[0]["pinned"])
            self.assertEqual(
                runs[0]["repository_instructions"]["files"][0]["path"],
                "AGENTS.md",
            )
            self.assertEqual(detail["llm_traces"][0]["name"], "planner")
            self.assertIn("Budget: 9000", detail["llm_traces"][0]["context_summary"])
            self.assertEqual(detail["llm_traces"][0]["input_tokens"], 90)
            self.assertEqual(detail["llm_traces"][0]["output_tokens"], 10)
            self.assertEqual(detail["llm_traces"][0]["total_tokens"], 100)
            self.assertEqual(detail["agent_events"][0]["event_type"], "run_started")
            self.assertEqual(detail["agent_pending_approval"]["checkpoint"], "approval-1")
            self.assertEqual(detail["agent_trajectory"]["run_id"], "runtime-1")
            self.assertEqual(detail["agent_trajectory"]["event_count"], 3)
            self.assertEqual(
                detail["agent_trajectory"]["metrics"]["evidence_eligible_items"],
                1,
            )
            self.assertEqual(
                detail["agent_trajectory"]["metrics"]["llm"]["token_source"],
                "provider",
            )
            self.assertEqual(detail["timeline"][0]["step"], "scan")
            self.assertFalse(detail["pinned"])
            self.assertEqual(
                detail["repository_instructions"]["text"],
                "Bounded root guidance.",
            )

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

    def test_history_cleanup_preserves_runtime_events_for_resumable_task_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "memory.sqlite3")
            store.save_task_run(
                {
                    "run_id": "task-runtime",
                    "source_repo": tmp,
                    "status": "paused",
                }
            )
            store.append_agent_runtime_event(
                "task-runtime",
                "run_started",
                payload={"task": "fix parser"},
            )
            store.append_agent_runtime_event(
                "orphan-runtime",
                "run_started",
                payload={"task": "temporary analysis"},
            )

            store.clear_runs()

            self.assertEqual(len(store.list_agent_runtime_events("task-runtime")), 1)
            self.assertEqual(store.list_agent_runtime_events("orphan-runtime"), [])


if __name__ == "__main__":
    unittest.main()
