from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.agent_loop import run_agent_loop, select_agent_hits
from repopilot_agent.llm.base import LLMMessage
from repopilot_agent.models import RepoFile, SearchHit


class FakeLLMClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.model = "fake-agent"
        self.calls: list[list[LLMMessage]] = []

    def complete(self, messages: list[LLMMessage]) -> str:
        self.calls.append(messages)
        return self.responses.pop(0)


class AgentLoopTests(unittest.TestCase):
    def test_agent_loop_searches_reads_and_finishes(self) -> None:
        files = [
            RepoFile(
                path=Path("main.py"),
                relative_path="main.py",
                size_bytes=32,
                language="python",
                content="def parse(value):\n    return value\n",
            ),
            RepoFile(
                path=Path("README.md"),
                relative_path="README.md",
                size_bytes=20,
                language="markdown",
                content="Project documentation\n",
            ),
        ]
        initial_hits = [
            SearchHit(path="README.md", score=5, reasons=["important project file"], preview="Project documentation")
        ]
        client = FakeLLMClient(
            [
                '{"thought":"Find parser code.","action":"search_files","query":"parse","path":"",'
                '"selected_paths":[],"summary":""}',
                '{"thought":"Read the parser implementation.","action":"read_file","query":"","path":"main.py",'
                '"selected_paths":[],"summary":""}',
                '{"thought":"Enough context is available.","action":"finish","query":"","path":"",'
                '"selected_paths":["main.py"],"summary":"main.py contains the parser behavior."}',
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = run_agent_loop(
                "fix parse behavior",
                tmp,
                files,
                initial_hits,
                client,
                max_steps=3,
            )

        self.assertEqual([step.action for step in result.steps], ["search_files", "read_file", "finish"])
        self.assertEqual(result.steps[0].selected_paths, [])
        self.assertEqual(result.steps[1].selected_paths, ["main.py"])
        self.assertEqual(result.selected_paths, ["main.py"])
        self.assertIn("parser behavior", result.summary)
        self.assertEqual(len(client.calls), 3)
        self.assertTrue(result.runtime_run_id)
        self.assertEqual(result.events[0].event_type, "run_started")
        self.assertEqual(result.events[-1].event_type, "run_stopped")
        self.assertIsNotNone(result.working_state)
        self.assertEqual(result.working_state.status, "completed")
        self.assertEqual(result.working_state.iteration, 3)
        self.assertEqual(result.working_state.selected_paths, ["main.py"])
        state_events = [
            event for event in result.events if event.event_type == "working_state_updated"
        ]
        self.assertEqual(len(state_events), 4)
        self.assertNotIn("def parse", json.dumps([event.payload for event in state_events]))
        self.assertIn("Agent working state:", client.calls[0][1].content)
        self.assertIn("Iteration: 0", client.calls[0][1].content)
        self.assertIn("Iteration: 1", client.calls[1][1].content)

    def test_select_agent_hits_prioritizes_selected_paths(self) -> None:
        files = [
            RepoFile(Path("main.py"), "main.py", 10, "python", "def parse(value):\n    return value\n"),
            RepoFile(Path("README.md"), "README.md", 10, "markdown", "docs\n"),
        ]
        hits = [SearchHit(path="README.md", score=10, reasons=["important project file"], preview="docs")]

        selected = select_agent_hits(hits, files, ["main.py"], limit=2)

        self.assertEqual(selected[0].path, "main.py")
        self.assertEqual(selected[0].reasons, ["selected by iterative agent"])
        self.assertEqual(selected[1].path, "README.md")

    def test_step_limit_state_keeps_fallback_selected_paths(self) -> None:
        files = [
            RepoFile(Path("README.md"), "README.md", 10, "markdown", "docs\n"),
        ]
        hits = [
            SearchHit(
                path="README.md",
                score=10,
                reasons=["important project file"],
                preview="docs",
            )
        ]
        client = FakeLLMClient(
            [
                '{"thought":"Search once.","action":"search_files","query":"missing",'
                '"path":"","selected_paths":[],"summary":""}',
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = run_agent_loop(
                "inspect docs",
                tmp,
                files,
                hits,
                client,
                max_steps=1,
            )

        self.assertEqual(result.selected_paths, ["README.md"])
        self.assertEqual(result.working_state.selected_paths, ["README.md"])
        self.assertEqual(result.working_state.status, "stopped")
        self.assertEqual(result.working_state.stop_reason, "step_limit")


if __name__ == "__main__":
    unittest.main()
