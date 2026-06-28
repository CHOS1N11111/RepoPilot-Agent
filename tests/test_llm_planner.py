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
from repopilot_agent.planner import create_plan_with_optional_llm
from repopilot_agent.workflow import run_workflow


class FakeLLMClient:
    def __init__(self, response: str, model: str = "fake-planner") -> None:
        self.response = response
        self.model = model
        self.messages: list[LLMMessage] = []

    def complete(self, messages: list[LLMMessage]) -> str:
        self.messages = messages
        return self.response


class LLMPlannerTests(unittest.TestCase):
    def test_create_plan_with_llm_response(self) -> None:
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

        plan, metadata = create_plan_with_optional_llm("fix parser failure", hits, client)

        self.assertEqual(metadata.source, "llm")
        self.assertEqual(metadata.model, "fake-planner")
        self.assertEqual(plan[0].title, "Inspect parser")
        self.assertIn("fix parser failure", client.messages[1].content)

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


if __name__ == "__main__":
    unittest.main()
