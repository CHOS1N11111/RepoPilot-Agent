from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.llm.base import LLMMessage
from repopilot_agent.models import SearchHit
from repopilot_agent.patch_proposer import propose_patch_with_optional_llm
from repopilot_agent.planner import create_plan_with_optional_llm
from repopilot_agent.workflow import run_workflow


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

        plan, metadata = create_plan_with_optional_llm("fix parser failure", hits, client, traces=traces)

        self.assertEqual(metadata.source, "llm")
        self.assertEqual(metadata.model, "fake-planner")
        self.assertEqual(plan[0].title, "Inspect parser")
        self.assertIn("fix parser failure", client.messages[1].content)
        self.assertEqual(traces[0].name, "planner")
        self.assertTrue(traces[0].parsed)
        self.assertIn("Context budget summary", client.messages[1].content)
        self.assertIn("src/parser.py", traces[0].context_summary)

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
        )

        self.assertEqual(metadata.source, "llm")
        self.assertTrue(proposal.apply_ready)
        self.assertEqual(proposal.file_edits[0].path, "src/parser.py")
        self.assertIn("--- a/src/parser.py", proposal.proposed_diff)
        self.assertIn("+    return value or \"\"", proposal.proposed_diff)
        self.assertEqual(traces[0].name, "patch_proposal")
        self.assertTrue(traces[0].parsed)
        self.assertIn("Files eligible for direct file_edits", client.messages[1].content)
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


if __name__ == "__main__":
    unittest.main()
