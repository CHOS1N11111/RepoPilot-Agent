from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.context_builder import ContextBudget, build_context_packet
from repopilot_agent.models import SearchHit


class ContextBuilderTests(unittest.TestCase):
    def test_context_packet_respects_budget_and_omits_extra_files(self) -> None:
        hits = [
            SearchHit(path=f"src/file_{index}.py", score=10 - index, reasons=["content matches"], preview="def run():")
            for index in range(4)
        ]

        packet = build_context_packet(
            hits,
            budget=ContextBudget(max_chars=260, max_files=4, max_preview_chars=80, max_content_chars=0),
        )

        self.assertLessEqual(len(packet.text), 260)
        self.assertTrue(packet.files)
        self.assertIn("Budget: 260 chars", packet.summary)
        self.assertTrue(packet.omitted_paths)

    def test_long_file_content_is_truncated_and_not_editable(self) -> None:
        hit = SearchHit(
            path="src/parser.py",
            score=12,
            reasons=["content matches 'parse'"],
            preview="def parse(value):",
        )
        content = "\n".join([f"line {index}" for index in range(100)]) + "\ndef parse(value):\n    return value\n"

        packet = build_context_packet(
            [hit],
            {"src/parser.py": content},
            budget=ContextBudget(max_chars=900, max_files=1, max_preview_chars=120, max_content_chars=400),
        )

        self.assertEqual(packet.files[0].path, "src/parser.py")
        self.assertTrue(packet.files[0].truncated)
        self.assertFalse(packet.files[0].direct_edit_allowed)
        self.assertEqual(packet.editable_paths, [])
        self.assertIn("Direct edit allowed: no", packet.text)
        self.assertIn("truncated", packet.summary)

    def test_full_file_content_is_editable(self) -> None:
        hit = SearchHit(
            path="src/parser.py",
            score=12,
            reasons=["content matches 'parse'"],
            preview="def parse(value):",
        )
        content = "def parse(value):\n    return value\n"

        packet = build_context_packet(
            [hit],
            {"src/parser.py": content},
            budget=ContextBudget(max_chars=2_000, max_files=1, max_preview_chars=120, max_content_chars=1_000),
        )

        self.assertEqual(packet.editable_paths, ["src/parser.py"])
        self.assertTrue(packet.files[0].direct_edit_allowed)
        self.assertIn("Direct edit allowed: yes", packet.text)


if __name__ == "__main__":
    unittest.main()
