from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.models import ValidationResult
from repopilot_agent.validation_feedback import build_validation_feedback


class ValidationFeedbackTests(unittest.TestCase):
    def test_build_validation_feedback_extracts_files_and_steps(self) -> None:
        result = ValidationResult(
            command="python -m unittest tests.test_auth",
            allowed=True,
            exit_code=1,
            stdout="",
            stderr=(
                "Traceback (most recent call last):\n"
                '  File "tests/test_auth.py", line 8, in test_login\n'
                "AssertionError: False is not true\n"
                "FAILED (failures=1)"
            ),
        )

        feedback = build_validation_feedback([result], task="fix login behavior")

        self.assertIsNotNone(feedback)
        self.assertIn("1 validation command", feedback.summary)
        self.assertIn("tests/test_auth.py", feedback.suspected_files)
        self.assertTrue(any("failed validation" in step.lower() for step in feedback.repair_steps))
        self.assertIn("Original task: fix login behavior", feedback.repair_task)
        self.assertIn("AssertionError", feedback.failures[0].output_excerpt)

    def test_build_validation_feedback_returns_none_for_success(self) -> None:
        result = ValidationResult(
            command="python -m unittest tests.test_auth",
            allowed=True,
            exit_code=0,
            stdout="ok",
            stderr="",
        )

        self.assertIsNone(build_validation_feedback([result]))


if __name__ == "__main__":
    unittest.main()
