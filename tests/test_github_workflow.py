from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.github_tools import inspect_github_repository, parse_github_remote, resolve_github_repository


class FakeGitHubClient:
    def get_json(self, path: str, query: dict[str, Any] | None = None) -> Any:
        if path.endswith("/issues"):
            return [
                {
                    "number": 7,
                    "title": "Improve planner output",
                    "state": "open",
                    "user": {"login": "alice"},
                    "labels": [{"name": "enhancement"}],
                    "updated_at": "2026-06-28T10:00:00Z",
                    "html_url": "https://github.com/CHOS1N11111/RepoPilot-Agent/issues/7",
                },
                {
                    "number": 8,
                    "title": "PR item in issues endpoint",
                    "state": "open",
                    "user": {"login": "bob"},
                    "labels": [],
                    "updated_at": "2026-06-28T11:00:00Z",
                    "html_url": "https://github.com/CHOS1N11111/RepoPilot-Agent/pull/8",
                    "pull_request": {},
                },
            ]
        if path.endswith("/pulls"):
            return [
                {
                    "number": 3,
                    "title": "Add GitHub status awareness",
                    "state": "open",
                    "user": {"login": "alice"},
                    "head": {"ref": "feature/github-status", "sha": "abc123"},
                    "base": {"ref": "master"},
                    "updated_at": "2026-06-28T12:00:00Z",
                    "html_url": "https://github.com/CHOS1N11111/RepoPilot-Agent/pull/3",
                }
            ]
        if path.endswith("/pulls/3/reviews"):
            return [
                {
                    "user": {"login": "reviewer"},
                    "state": "APPROVED",
                    "submitted_at": "2026-06-28T12:30:00Z",
                    "body": "Looks good.",
                    "html_url": "https://github.com/CHOS1N11111/RepoPilot-Agent/pull/3#pullrequestreview-1",
                }
            ]
        if path.endswith("/commits/abc123/check-runs"):
            return {
                "check_runs": [
                    {
                        "name": "tests",
                        "status": "completed",
                        "conclusion": "success",
                        "html_url": "https://github.com/CHOS1N11111/RepoPilot-Agent/actions/runs/1",
                    }
                ]
            }
        if path.endswith("/commits/abc123/status"):
            return {
                "statuses": [
                    {
                        "context": "legacy-ci",
                        "state": "success",
                        "target_url": "https://ci.example.test/build/1",
                    }
                ]
            }
        raise AssertionError(f"Unexpected GitHub API path: {path}")


class GitHubWorkflowTests(unittest.TestCase):
    def test_parse_github_remote_supports_https_and_ssh(self) -> None:
        self.assertEqual(
            parse_github_remote("https://github.com/CHOS1N11111/RepoPilot-Agent.git"),
            ("CHOS1N11111", "RepoPilot-Agent"),
        )
        self.assertEqual(
            parse_github_remote("git@github.com:CHOS1N11111/RepoPilot-Agent.git"),
            ("CHOS1N11111", "RepoPilot-Agent"),
        )

    def test_inspect_github_repository_reads_issues_pr_reviews_and_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://github.com/CHOS1N11111/RepoPilot-Agent.git"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )

            repository = resolve_github_repository(root)
            snapshot = inspect_github_repository(root, client=FakeGitHubClient())

            self.assertIsNotNone(repository)
            self.assertEqual(repository.owner, "CHOS1N11111")
            self.assertEqual(len(snapshot.issues), 1)
            self.assertEqual(snapshot.issues[0].number, 7)
            self.assertEqual(len(snapshot.pull_requests), 1)
            self.assertEqual(snapshot.pull_requests[0].reviews[0].state, "APPROVED")
            self.assertEqual(snapshot.pull_requests[0].checks[0].name, "tests")
            self.assertEqual(snapshot.pull_requests[0].checks[1].name, "legacy-ci")


if __name__ == "__main__":
    unittest.main()
