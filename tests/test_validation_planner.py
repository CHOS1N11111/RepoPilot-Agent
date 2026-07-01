from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.validation_planner import build_validation_plan


class ValidationPlannerTests(unittest.TestCase):
    def test_test_file_gets_narrow_unittest_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_validation_plan(tmp, ["tests/test_auth.py"])

        self.assertEqual(plan.commands, ["python -m unittest tests.test_auth"])
        self.assertEqual(plan.notes, [])

    def test_source_file_pairs_to_existing_test(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "tests" / "test_auth.py").write_text("import unittest\n", encoding="utf-8")

            plan = build_validation_plan(root, ["src/auth.py"])

        self.assertEqual(plan.commands, ["python -m unittest tests.test_auth"])

    def test_python_file_falls_back_to_unittest_discover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()

            plan = build_validation_plan(root, ["src/parser.py"])

        self.assertEqual(plan.commands, ["python -m unittest discover -s tests"])

    def test_javascript_uses_npm_test_only_with_package_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"scripts":{"test":"node test.js"}}\n', encoding="utf-8")

            plan = build_validation_plan(root, ["web/static/app.js"])

        self.assertEqual(plan.commands, ["npm test"])
        self.assertEqual(plan.notes, [])

    def test_documentation_only_returns_manual_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = build_validation_plan(tmp, ["README.md"])

        self.assertEqual(plan.commands, [])
        self.assertTrue(any("Documentation-only" in note for note in plan.notes))


if __name__ == "__main__":
    unittest.main()
