from __future__ import annotations

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
        self.assertEqual(result.selected_paths, ["main.py"])
        self.assertIn("parser behavior", result.summary)
        self.assertEqual(len(client.calls), 3)

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


if __name__ == "__main__":
    unittest.main()
