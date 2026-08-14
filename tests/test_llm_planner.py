from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.llm.base import LLMMessage
from repopilot_agent.models import MemoryContextItem, SearchHit
from repopilot_agent.patch_proposer import propose_patch_with_optional_llm
from repopilot_agent.planner import create_plan_with_optional_llm
from repopilot_agent.workflow import run_workflow


def iterative_decision(
    kind: str,
    arguments: dict,
    rationale: str,
    expected_evidence: str,
    *,
    focus: str,
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
    def __init__(self, response: str | list[str], model: str = "fake-planner") -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.model = model
        self.messages: list[LLMMessage] = []
        self.calls: list[list[LLMMessage]] = []

    def complete(self, messages: list[LLMMessage]) -> str:
        self.messages = messages
        self.calls.append(messages)
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


class LLMPlannerTests(unittest.TestCase):
    def test_create_plan_with_llm_response(self) -> None:
        traces = []
        client = FakeLLMClient(
            '{"steps":[{"title":"Inspect parser","detail":"Review parser.py and identify failing branch."},'
            '{"title":"Add regression test","detail":"Capture the broken input before changing code."}]}'
        )
        hits = [
            SearchHit(
                path="src/parser.py",
                score=12,
                reasons=["path matches 'parser'"],
                preview="def parse(value):",
            )
        ]

        plan, metadata = create_plan_with_optional_llm(
            "fix parser failure",
            hits,
            client,
            traces=traces,
            repository_map_context="src/parser.py [Python]\n  function parse(value) (line 1)",
        )

        self.assertEqual(metadata.source, "llm")
        self.assertEqual(metadata.model, "fake-planner")
        self.assertEqual(plan[0].title, "Inspect parser")
        self.assertIn("fix parser failure", client.messages[1].content)
        self.assertEqual(traces[0].name, "planner")
        self.assertTrue(traces[0].parsed)
        self.assertIn("Context budget summary", client.messages[1].content)
        self.assertIn("Task-relevant repository map", client.messages[1].content)
        self.assertIn("function parse(value)", client.messages[1].content)
        self.assertIn("src/parser.py", traces[0].context_summary)

    def test_create_plan_with_llm_includes_related_memory(self) -> None:
        client = FakeLLMClient(
            '{"steps":[{"title":"Reuse parser lesson","detail":"Check prior validation before editing."}]}'
        )
        memory = [
            MemoryContextItem(
                run_id="run-1",
                task="fix parser validation failure",
                summary="Previous parser fix used a focused parser test.",
                mode="run",
                created_at="2026-01-01T00:00:00+00:00",
                applied=True,
                score=8,
                reasons=["task overlap: parser, failure"],
                validation=["python -m unittest tests.test_parser: exit 0"],
            )
        ]

        plan, metadata = create_plan_with_optional_llm(
            "fix parser failure",
            [],
            client,
            memory_context=memory,
        )

        self.assertEqual(metadata.source, "llm")
        self.assertEqual(plan[0].title, "Reuse parser lesson")
        self.assertIn("Related memory:", client.messages[1].content)
        self.assertIn("fix parser validation failure", client.messages[1].content)
        self.assertIn("python -m unittest tests.test_parser: exit 0", client.messages[1].content)

    def test_create_plan_with_llm_separates_pinned_memory(self) -> None:
        client = FakeLLMClient(
            '{"steps":[{"title":"Use pinned lesson","detail":"Check pinned memory before changing code."}]}'
        )
        memory = [
            MemoryContextItem(
                run_id="run-1",
                task="document release checklist",
                summary="Pinned release workflow lesson.",
                mode="run",
                created_at="2026-01-01T00:00:00+00:00",
                applied=False,
                score=100,
                reasons=["pinned memory"],
                pinned=True,
            ),
            MemoryContextItem(
                run_id="run-2",
                task="fix parser validation failure",
                summary="Related parser lesson.",
                mode="run",
                created_at="2026-01-02T00:00:00+00:00",
                applied=True,
                score=8,
                reasons=["task overlap: parser"],
            ),
        ]

        create_plan_with_optional_llm("fix parser failure", [], client, memory_context=memory)

        prompt = client.messages[1].content
        self.assertIn("Pinned memory:", prompt)
        self.assertIn("document release checklist", prompt)
        self.assertIn("Related memory:", prompt)
        self.assertIn("fix parser validation failure", prompt)

    def test_invalid_llm_json_falls_back_to_rules(self) -> None:
        client = FakeLLMClient("not json")

        plan, metadata = create_plan_with_optional_llm("fix parser failure", [], client)

        self.assertEqual(metadata.source, "rules")
        self.assertTrue(metadata.fallback_used)
        self.assertIsNotNone(metadata.error)
        self.assertGreaterEqual(len(plan), 5)

    def test_workflow_falls_back_when_api_key_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("def run():\n    return True\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                report = run_workflow(root, "fix run behavior", use_llm=True, llm_model="fake-model")

        self.assertEqual(report.plan_metadata.source, "rules")
        self.assertTrue(report.plan_metadata.fallback_used)
        self.assertEqual(report.plan_metadata.model, "fake-model")
        self.assertIn("OPENAI_API_KEY", report.plan_metadata.error or "")
        self.assertEqual(report.patch_proposal_metadata.source, "rules")
        self.assertTrue(report.patch_proposal_metadata.fallback_used)
        self.assertIn("OPENAI_API_KEY", report.patch_proposal_metadata.error or "")

    def test_create_patch_proposal_with_llm_response(self) -> None:
        client = FakeLLMClient(
            '{"objective":"Fix parser failure safely","files":[{"path":"src/parser.py","change_type":"bugfix",'
            '"rationale":"Parser is the matched implementation point.","suggested_actions":["Guard empty input"],'
            '"confidence":"high"}],"risks":[{"level":"medium","message":"Parser behavior may affect callers.",'
            '"mitigation":"Run parser regression tests."}],"validation_suggestions":["python -m unittest discover -s tests"],'
            '"ready_for_patch":true}'
        )
        hits = [
            SearchHit(
                path="src/parser.py",
                score=12,
                reasons=["path matches 'parser'"],
                preview="def parse(value):",
            )
        ]
        plan, _ = create_plan_with_optional_llm("fix parser failure", hits, None)

        proposal, metadata = propose_patch_with_optional_llm("fix parser failure", hits, plan, client)

        self.assertEqual(metadata.source, "llm")
        self.assertEqual(proposal.objective, "Fix parser failure safely")
        self.assertEqual(proposal.files[0].path, "src/parser.py")
        self.assertEqual(proposal.files[0].confidence, "high")
        self.assertTrue(proposal.ready_for_patch)

    def test_patch_proposal_with_llm_file_edits_includes_diff(self) -> None:
        traces = []
        client = FakeLLMClient(
            '{"objective":"Fix parser failure safely","files":[{"path":"src/parser.py","change_type":"bugfix",'
            '"rationale":"Parser is the matched implementation point.","suggested_actions":["Guard empty input"],'
            '"confidence":"high"}],"risks":[],"validation_suggestions":["python -m unittest discover -s tests"],'
            '"ready_for_patch":true,"file_edits":[{"path":"src/parser.py",'
            '"new_content":"def parse(value):\\n    return value or \\"\\"\\n",'
            '"rationale":"Guard empty input."}]}'
        )
        hits = [
            SearchHit(
                path="src/parser.py",
                score=12,
                reasons=["path matches 'parser'"],
                preview="def parse(value):",
            )
        ]
        plan, _ = create_plan_with_optional_llm("fix parser failure", hits, None)

        proposal, metadata = propose_patch_with_optional_llm(
            "fix parser failure",
            hits,
            plan,
            client,
            file_contents={"src/parser.py": "def parse(value):\n    return value\n"},
            traces=traces,
            repository_map_context="src/parser.py [Python]\n  function parse(value) (line 1)",
        )

        self.assertEqual(metadata.source, "llm")
        self.assertTrue(proposal.apply_ready)
        self.assertEqual(proposal.file_edits[0].path, "src/parser.py")
        self.assertIn("--- a/src/parser.py", proposal.proposed_diff)
        self.assertIn("+    return value or \"\"", proposal.proposed_diff)
        self.assertEqual(traces[0].name, "patch_proposal")
        self.assertTrue(traces[0].parsed)
        self.assertIn("Files eligible for direct file_edits", client.messages[1].content)
        self.assertIn("Task-relevant repository map", client.messages[1].content)
        self.assertIn("function parse(value)", client.messages[1].content)
        self.assertIn("edit allowed", traces[0].context_summary)

    def test_patch_proposal_blocks_file_edits_when_context_is_truncated(self) -> None:
        traces = []
        client = FakeLLMClient(
            '{"objective":"Fix parser failure safely","files":[{"path":"src/parser.py","change_type":"bugfix",'
            '"rationale":"Parser is the matched implementation point.","suggested_actions":["Guard empty input"],'
            '"confidence":"high"}],"risks":[],"validation_suggestions":["python -m unittest discover -s tests"],'
            '"ready_for_patch":true,"file_edits":[{"path":"src/parser.py",'
            '"new_content":"def parse(value):\\n    return value or \\"\\"\\n",'
            '"rationale":"Guard empty input."}]}'
        )
        hits = [
            SearchHit(
                path="src/parser.py",
                score=12,
                reasons=["path matches 'parser'"],
                preview="def parse(value):",
            )
        ]
        plan, _ = create_plan_with_optional_llm("fix parser failure", hits, None)
        large_content = "\n".join(f"# filler {index}" for index in range(5_000))

        proposal, metadata = propose_patch_with_optional_llm(
            "fix parser failure",
            hits,
            plan,
            client,
            file_contents={"src/parser.py": large_content},
            traces=traces,
        )

        self.assertEqual(metadata.source, "llm")
        self.assertFalse(proposal.apply_ready)
        self.assertEqual(proposal.file_edits, [])
        self.assertEqual(proposal.proposed_diff, "")
        self.assertTrue(any("full file context" in risk.message for risk in proposal.risks))
        self.assertIn("none", client.messages[1].content)
        self.assertIn("truncated", traces[0].context_summary)

    def test_invalid_patch_proposal_json_falls_back_to_rules(self) -> None:
        client = FakeLLMClient("not json")

        proposal, metadata = propose_patch_with_optional_llm("fix parser failure", [], [], client)

        self.assertEqual(metadata.source, "rules")
        self.assertTrue(metadata.fallback_used)
        self.assertIsNotNone(metadata.error)
        self.assertFalse(proposal.ready_for_patch)

    def test_invalid_patch_proposal_fields_fall_back_to_rules(self) -> None:
        client = FakeLLMClient(
            '{"objective":"Fix parser","files":[{"path":"src/parser.py","change_type":"dangerous",'
            '"rationale":"Bad enum.","suggested_actions":["Do it"],"confidence":"high"}],'
            '"risks":[],"validation_suggestions":[],"ready_for_patch":true}'
        )
        hits = [
            SearchHit(
                path="src/parser.py",
                score=12,
                reasons=["path matches 'parser'"],
                preview="def parse(value):",
            )
        ]

        proposal, metadata = propose_patch_with_optional_llm("fix parser failure", hits, [], client)

        self.assertEqual(metadata.source, "rules")
        self.assertTrue(metadata.fallback_used)
        self.assertIn("Invalid change_type", metadata.error or "")
        self.assertTrue(proposal.ready_for_patch)

    def test_workflow_uses_llm_for_plan_and_patch_proposal(self) -> None:
        client = FakeLLMClient(
            [
                '{"steps":[{"title":"Inspect parser","detail":"Review parser behavior."}]}',
                '{"objective":"Fix parser failure safely","files":[{"path":"main.py","change_type":"bugfix",'
                '"rationale":"main.py contains the matched behavior.","suggested_actions":["Guard invalid input"],'
                '"confidence":"medium"}],"risks":[],"validation_suggestions":["python -m unittest discover -s tests"],'
                '"ready_for_patch":true,"file_edits":[{"path":"main.py","new_content":"def parse(value):\\n    return value or \\"\\"\\n",'
                '"rationale":"Guard invalid input."}]}',
                '{"summary":"The diff is focused.","risk_level":"low","concerns":[],'
                '"suggested_tests":["python -m unittest discover -s tests"],"approved_for_apply":true}',
            ],
            model="fake-combined",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("def parse(value):\n    return value\n", encoding="utf-8")
            report = run_workflow(root, "fix parse failure", use_llm=True, llm_client=client)

        self.assertEqual(report.plan_metadata.source, "llm")
        self.assertEqual(report.patch_proposal_metadata.source, "llm")
        self.assertEqual(report.patch_proposal.files[0].path, "main.py")
        self.assertIsNotNone(report.patch_review)
        self.assertTrue(report.patch_review.approved_for_apply)
        self.assertEqual([trace.name for trace in report.llm_traces], ["planner", "patch_proposal", "patch_review"])
        self.assertEqual(len(client.calls), 3)
        self.assertGreater(report.repository_map["symbols_indexed"], 0)
        self.assertIn("Task-relevant repository map", client.calls[0][1].content)
        self.assertIn("Task-relevant repository map", client.calls[1][1].content)

    def test_workflow_iterative_agent_runs_before_plan_and_proposal(self) -> None:
        client = FakeLLMClient(
            [
                iterative_decision(
                    "search_files",
                    {"query": "parse"},
                    "Find parser files.",
                    "Paths and previews matching parse.",
                    focus="Locate parser files.",
                    open_questions=["Which file contains parse?"],
                ),
                iterative_decision(
                    "read_file",
                    {"path": "main.py"},
                    "Read the implementation.",
                    "Parser source and surrounding behavior.",
                    focus="Understand parser behavior.",
                    findings=["main.py matched the parser search."],
                    resolved_questions=["Which file contains parse?"],
                ),
                iterative_decision(
                    "finish",
                    {"selected_paths": ["main.py"]},
                    "Enough context is available.",
                    "A completed finish observation.",
                    focus="Prepare the implementation plan.",
                    findings=["main.py contains the parser implementation."],
                    finish_reason="main.py is the implementation target.",
                ),
                '{"steps":[{"title":"Inspect parser","detail":"Review main.py parser behavior."}]}',
                '{"objective":"Fix parser failure safely","files":[{"path":"main.py","change_type":"bugfix",'
                '"rationale":"main.py contains the selected parser behavior.","suggested_actions":["Guard invalid input"],'
                '"confidence":"high"}],"risks":[],"validation_suggestions":["python -m unittest discover -s tests"],'
                '"ready_for_patch":true,"file_edits":[]}',
            ],
            model="fake-iterative",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("def parse(value):\n    return value\n", encoding="utf-8")
            (root / "README.md").write_text("General project docs\n", encoding="utf-8")
            report = run_workflow(
                root,
                "fix parse failure",
                use_llm=True,
                llm_client=client,
                iterative_agent=True,
                agent_max_steps=3,
            )

        self.assertEqual([step.action for step in report.agent_steps], ["search_files", "read_file", "finish"])
        self.assertTrue(report.agent_run_id)
        self.assertEqual(report.agent_events[0].event_type, "run_started")
        self.assertEqual(report.agent_events[-1].event_type, "run_stopped")
        self.assertEqual(report.agent_state["status"], "completed")
        self.assertEqual(report.agent_state["iteration"], 3)
        self.assertEqual(report.agent_state["selected_paths"], ["main.py"])
        self.assertEqual(report.agent_state["focus"], "Prepare the implementation plan.")
        self.assertEqual(
            report.agent_state["findings"],
            [
                "main.py matched the parser search.",
                "main.py contains the parser implementation.",
            ],
        )
        self.assertEqual(report.agent_steps[0].expected_evidence, "Paths and previews matching parse.")
        self.assertEqual(report.relevant_files[0].path, "main.py")
        self.assertEqual(report.plan_metadata.source, "llm")
        self.assertEqual(report.patch_proposal_metadata.source, "llm")
        self.assertEqual(
            [trace.name for trace in report.llm_traces],
            ["agent_step_1", "agent_step_2", "agent_step_3", "planner", "patch_proposal"],
        )
        self.assertEqual(len(client.calls), 5)
        self.assertIn("Agent working state:", client.calls[0][1].content)
        self.assertEqual(report.execution_budget["limits"]["max_agent_steps"], 3)
        self.assertEqual(report.execution_budget["usage"]["agent_steps"], 3)
        self.assertFalse(report.execution_budget["exhausted"])
        self.assertEqual(report.acceptance_criteria[0]["criterion_id"], "analysis_complete")
        self.assertEqual(report.completion_evidence["status"], "passed")


if __name__ == "__main__":
    unittest.main()
