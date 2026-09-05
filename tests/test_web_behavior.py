from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend behavior tests.")
class WebBehaviorTests(unittest.TestCase):
    def test_javascript_regressions(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), "--test", "tests/web/test_app.cjs"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
