from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.agent_loop import run_agent_loop, select_agent_hits
from repopilot_agent.execution import AcceptanceCriterion, ExecutionBudget
from repopilot_agent.llm.base import LLMMessage
from repopilot_agent.models import MemoryContextItem, RepoFile, SearchHit


def decision(
    kind: str,
    arguments: dict,
    rationale: str,
    expected_evidence: str,
    *,
    focus: str = "",
    findings: list[str] | None = None,
    open_questions: list[str] | None = None,
    resolved_questions: list[str] | None = None,
    finish_reason: str = "",
) -> str:
    return json.dumps(
        {
            "version": 2,
            "rationale": rationale,
            "action": {"kind": kind, "arguments": arguments},
            "expected_evidence": expected_evidence,
            "state_update": {
                "focus": focus,
                "add_findings": findings or [],
                "add_open_questions": open_questions or [],
                "resolve_open_questions": resolved_questions or [],
            },
            "finish_reason": finish_reason,
            "user_question": "",
        }
    )


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
                decision(
                    "search_files",
                    {"query": "parse"},
                    "Find parser code.",
                    "Paths and previews that mention parse.",
                    focus="Locate the parser implementation.",
                    open_questions=["Which file implements parse?"],
                ),
                decision(
                    "read_file",
                    {"path": "main.py"},
                    "Read the parser implementation.",
                    "The implementation and surrounding behavior in main.py.",
                    focus="Understand parser behavior.",
                    findings=["main.py matched the parse search."],
                    resolved_questions=["Which file implements parse?"],
                    open_questions=["How does parse handle values?"],
                ),
                decision(
                    "finish",
                    {"selected_paths": ["main.py"]},
                    "Enough context is available.",
                    "A completed finish observation with main.py selected.",
                    focus="Summarize the parser target.",
                    findings=["main.py defines parse and returns the provided value."],
                    resolved_questions=["How does parse handle values?"],
                    finish_reason="main.py contains the parser behavior.",
                ),
            ]
        )
        traces = []
        memory = [
            MemoryContextItem(
                run_id="pinned",
                task="prior parser task",
                summary="Use a parser regression test.",
                mode="run",
                created_at="2026-01-01T00:00:00+00:00",
                applied=True,
                score=100,
                reasons=["pinned memory"],
                pinned=True,
            )
        ]

        with tempfile.TemporaryDirectory() as tmp:
            result = run_agent_loop(
                "fix parse behavior",
                tmp,
                files,
                initial_hits,
                client,
                traces=traces,
                max_steps=3,
                memory_context=memory,
                current_diff="+OPENAI_API_KEY=must-not-reach-the-model",
                acceptance_criteria=[
                    AcceptanceCriterion(
                        "analysis_complete",
                        "analysis",
                        "Understand parser behavior.",
                    )
                ],
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
        self.assertEqual(result.working_state.focus, "Summarize the parser target.")
        self.assertEqual(
            result.working_state.findings,
            [
                "main.py matched the parse search.",
                "main.py defines parse and returns the provided value.",
            ],
        )
        self.assertEqual(result.working_state.open_questions, [])
        self.assertIn("finish observation", result.working_state.expected_evidence)
        self.assertIn("Paths and previews", result.steps[0].expected_evidence)
        self.assertEqual(
            result.steps[1].state_update["add_findings"],
            ["main.py matched the parse search."],
        )
        self.assertEqual(result.steps[2].finish_reason, result.summary)
        state_events = [
            event for event in result.events if event.event_type == "working_state_updated"
        ]
        self.assertEqual(len(state_events), 4)
        self.assertNotIn("def parse", json.dumps([event.payload for event in state_events]))
        self.assertIn("Managed context packet:", client.calls[0][1].content)
        self.assertIn("## Agent Working State", client.calls[0][1].content)
        self.assertIn("## Pinned Memory", client.calls[0][1].content)
        self.assertIn("prior parser task", client.calls[0][1].content)
        self.assertIn("Understand parser behavior", client.calls[0][1].content)
        self.assertIn("Agent steps: 3 remaining", client.calls[0][1].content)
        self.assertIn("Agent steps: 2 remaining", client.calls[1][1].content)
        self.assertIn("[REDACTED]", client.calls[0][1].content)
        self.assertNotIn("must-not-reach-the-model", client.calls[0][1].content)
        self.assertIn("Iteration: 0", client.calls[0][1].content)
        self.assertIn("Iteration: 1", client.calls[1][1].content)
        self.assertIn('"version":2', client.calls[0][0].content)
        self.assertIn("Expected evidence:", client.calls[1][1].content)
        self.assertEqual(len(traces), 3)
        self.assertIn("Agent context:", traces[0].context_summary)
        self.assertIn("working_state", traces[0].context_summary)

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
                decision(
                    "search_files",
                    {"query": "missing"},
                    "Search once.",
                    "Any repository file matching missing.",
                    focus="Search documentation.",
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = run_agent_loop(
                "inspect docs",
                tmp,
                files,
                hits,
                client,
                max_steps=3,
                execution_budget=ExecutionBudget(
                    max_agent_steps=1,
                    max_tool_calls=1,
                ),
            )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result.selected_paths, ["README.md"])
        self.assertEqual(result.working_state.selected_paths, ["README.md"])
        self.assertEqual(result.working_state.status, "stopped")
        self.assertEqual(result.working_state.stop_reason, "step_limit")


if __name__ == "__main__":
    unittest.main()
