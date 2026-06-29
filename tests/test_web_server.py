from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.git_tools import get_git_diff
from repopilot_agent.web_server import RepoPilotRequestHandler, STATIC_DIR


class WebServerTests(unittest.TestCase):
    def test_static_assets_exist(self) -> None:
        self.assertTrue((STATIC_DIR / "index.html").is_file())
        self.assertTrue((STATIC_DIR / "app.css").is_file())
        self.assertTrue((STATIC_DIR / "app.js").is_file())

    def test_get_git_diff_returns_working_tree_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.local"], cwd=root, check=True)
            file_path = root / "README.md"
            file_path.write_text("first\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "Initial"], cwd=root, check=True, capture_output=True, text=True)

            file_path.write_text("first\nsecond\n", encoding="utf-8")
            diff = get_git_diff(root)

            self.assertIn("+second", diff)

    def test_propose_api_returns_patch_proposal_without_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "search.py").write_text(
                "def search_login(query):\n    return query.lower()\n",
                encoding="utf-8",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                payload = json.dumps(
                    {
                        "repo": str(root),
                        "task": "fix login search behavior",
                        "validation": ["python -m unittest discover -s tests"],
                    }
                ).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/propose",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )

                with urlopen(request, timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))

                self.assertEqual(data["task"], "fix login search behavior")
                self.assertEqual(data["validation"], [])
                self.assertTrue(data["patch_proposal"]["ready_for_patch"])
                self.assertEqual(data["patch_proposal"]["files"][0]["path"], "search.py")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_apply_api_writes_approved_file_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("old\n", encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                payload = json.dumps(
                    {
                        "repo": str(root),
                        "file_edits": [
                            {
                                "path": "notes.txt",
                                "new_content": "new\n",
                                "rationale": "Update approved content.",
                            }
                        ],
                    }
                ).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/apply",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )

                with urlopen(request, timeout=5) as response:
                    data = json.loads(response.read().decode("utf-8"))

                self.assertTrue(data["applied"])
                self.assertEqual(data["changed_files"], ["notes.txt"])
                self.assertEqual((root / "notes.txt").read_text(encoding="utf-8"), "new\n")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_apply_api_rejects_blocked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = ThreadingHTTPServer(("127.0.0.1", 0), RepoPilotRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                payload = json.dumps(
                    {
                        "repo": str(root),
                        "file_edits": [
                            {
                                "path": "log.md",
                                "new_content": "hidden\n",
                                "rationale": "Should be blocked.",
                            }
                        ],
                    }
                ).encode("utf-8")
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/apply",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )

                with self.assertRaises(Exception):
                    urlopen(request, timeout=5)
                self.assertFalse((root / "log.md").exists())
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
