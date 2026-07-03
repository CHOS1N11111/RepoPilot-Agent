from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.validator import run_validation


class ValidatorTests(unittest.TestCase):
    def test_validation_output_decoding_replaces_invalid_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_output.py").write_text(
                "import sys\n"
                "import unittest\n\n"
                "class OutputTests(unittest.TestCase):\n"
                "    def test_binary_output(self):\n"
                "        sys.stdout.buffer.write(b'\\xa7\\n')\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )

            results = run_validation(root, ["python -m unittest discover -s tests"])

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].allowed)
        self.assertEqual(results[0].exit_code, 0)
        self.assertIn("\ufffd", results[0].stdout)


if __name__ == "__main__":
    unittest.main()
