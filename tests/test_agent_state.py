from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.memory import MemoryStore
from repopilot_agent.runtime import (
    MAX_RECENT_OBSERVATIONS,
    AgentRuntime,
    RuntimeAction,
    RuntimeObservation,
    SQLiteRuntimeStore,
    advance_agent_working_state,
    agent_working_state_from_record,
    create_agent_working_state,
    latest_agent_working_state,
    stop_agent_working_state,
)


class AgentWorkingStateTests(unittest.TestCase):
    def test_state_progress_is_bounded_and_excludes_unsafe_paths(self) -> None:
        state = create_agent_working_state("x" * 3_000)
        for iteration in range(MAX_RECENT_OBSERVATIONS + 3):
            state = advance_agent_working_state(
                state,
                RuntimeAction(
                    kind="read_file",
                    arguments={"path": "src/main.py"},
                    action_id=f"read-{iteration}",
                ),
                RuntimeObservation(
                    action_id=f"read-{iteration}",
                    action_kind="read_file",
                    status="completed",
                    summary="s" * 700,
                ),
                selected_paths=["src\\main.py", "../secret.txt", "src/main.py"],
            )

        self.assertEqual(len(state.objective), 2_000)
        self.assertEqual(state.iteration, MAX_RECENT_OBSERVATIONS + 3)
        self.assertEqual(len(state.recent_observations), MAX_RECENT_OBSERVATIONS)
        self.assertEqual(len(state.recent_observations[-1].summary), 500)
        self.assertEqual(state.selected_paths, ["src/main.py"])
        self.assertEqual(state.phase, "inspection")
        self.assertEqual(state.status, "running")

    def test_terminal_and_legacy_records_are_normalized(self) -> None:
        state = create_agent_working_state("inspect repository")
        finished = stop_agent_working_state(state, "finished")
        restored = agent_working_state_from_record(
            {
                **finished.to_dict(),
                "version": -4,
                "iteration": -2,
                "selected_paths": ["src/main.py", "../../outside", 3],
                "recent_observations": ["invalid"],
                "api_key": "must-be-ignored",
                "base_url": "https://must-not-be-loaded.example",
            }
        )

        self.assertIsNotNone(restored)
        self.assertEqual(restored.version, 1)
        self.assertEqual(restored.iteration, 0)
        self.assertEqual(restored.selected_paths, ["src/main.py"])
        self.assertEqual(restored.status, "completed")
        self.assertNotIn("api_key", restored.to_dict())
        self.assertNotIn("base_url", restored.to_dict())

    def test_invalid_latest_snapshot_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(tmp, "inspect repository", run_id="invalid-state")
            expected = create_agent_working_state("inspect repository")
            runtime.record_working_state(expected)
            runtime.store.append_event(
                runtime.run_id,
                "working_state_updated",
                payload={"working_state": {"api_key": "invalid"}},
            )

            restored = latest_agent_working_state(runtime.events)

            self.assertEqual(restored, expected)

    def test_sqlite_runtime_restores_latest_working_state_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_path = Path(tmp) / "memory.sqlite3"
            store = SQLiteRuntimeStore(MemoryStore(memory_path))
            runtime = AgentRuntime(
                tmp,
                "inspect repository",
                run_id="state-run",
                store=store,
            )
            state = create_agent_working_state("inspect repository")
            runtime.record_working_state(state)
            advanced = advance_agent_working_state(
                state,
                RuntimeAction(kind="search_files", action_id="search-1"),
                RuntimeObservation(
                    action_id="search-1",
                    action_kind="search_files",
                    status="completed",
                    summary="Found two relevant files.",
                ),
                selected_paths=["src/main.py"],
            )
            runtime.record_working_state(advanced)

            reopened = AgentRuntime(
                tmp,
                "inspect repository",
                run_id="state-run",
                store=SQLiteRuntimeStore(MemoryStore(memory_path)),
            )

            self.assertEqual(reopened.working_state, advanced)
            state_events = [
                event
                for event in reopened.events
                if event.event_type == "working_state_updated"
            ]
            self.assertEqual(len(state_events), 2)


if __name__ == "__main__":
    unittest.main()
