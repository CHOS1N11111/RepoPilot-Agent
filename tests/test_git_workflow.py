from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.git_summary import build_git_workflow_summary, build_pull_request_readiness
from repopilot_agent.git_tools import _parse_status, inspect_repository


class GitWorkflowTests(unittest.TestCase):
    def test_parse_status_with_tracking_and_changes(self) -> None:
        output = "\n".join(
            [
                "## feature/git-awareness...origin/feature/git-awareness [ahead 2, behind 1]",
                " M README.md",
                "A  src/repopilot_agent/git_tools.py",
                "?? tests/test_git_workflow.py",
            ]
        )

        branch, upstream, ahead, behind, changes = _parse_status(output)

        self.assertEqual(branch, "feature/git-awareness")
        self.assertEqual(upstream, "origin/feature/git-awareness")
        self.assertEqual(ahead, 2)
        self.assertEqual(behind, 1)
        self.assertEqual(len(changes), 3)
        self.assertEqual(changes[0].description, "working tree modified")
        self.assertEqual(changes[2].description, "untracked")

    def test_git_summary_from_temporary_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            (root / "README.md").write_text("# Demo\n", encoding="utf-8")

            state = inspect_repository(root)
            summary = build_git_workflow_summary(
                root,
                validation_notes=["python -m unittest discover -s tests"],
            )

            self.assertFalse(state.clean)
            self.assertEqual(state.changes[0].path, "README.md")
            self.assertEqual(summary.suggested_commit_message, "Update project documentation")
            self.assertIn("## What changed", summary.pull_request.body)
            self.assertIn("python -m unittest discover -s tests", summary.pull_request.body)
            self.assertFalse(summary.pr_readiness.ready)
            self.assertIn("No GitHub remote", summary.pr_readiness.blockers[0])

    def test_pr_readiness_blocks_dirty_tree_without_github_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            (root / "README.md").write_text("# Demo\n", encoding="utf-8")

            readiness = build_pull_request_readiness(root, pull_request_title="Update docs")

            self.assertFalse(readiness.ready)
            self.assertTrue(readiness.needs_commit)
            self.assertTrue(any("No GitHub remote" in item for item in readiness.blockers))
            self.assertTrue(any("uncommitted changes" in item for item in readiness.blockers))
            self.assertIn("git status --short", readiness.suggested_commands)

    def test_pr_readiness_passes_for_clean_pushed_feature_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.local"], cwd=root, check=True)
            (root / "README.md").write_text("# Demo\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "Initial"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(["git", "switch", "-c", "feature/pr-ready"], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://github.com/example/project.git"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=root, check=True)
            subprocess.run(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/feature/pr-ready", "HEAD"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "branch", "--set-upstream-to", "origin/feature/pr-ready"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )

            readiness = build_pull_request_readiness(root, pull_request_title="Add PR readiness")

            self.assertTrue(readiness.ready)
            self.assertEqual(readiness.repository.owner, "example")
            self.assertEqual(readiness.repository.repo, "project")
            self.assertEqual(readiness.base_branch, "main")
            self.assertEqual(readiness.head_branch, "feature/pr-ready")
            self.assertFalse(readiness.needs_commit)
            self.assertFalse(readiness.needs_push)
            self.assertIn("gh pr create --repo example/project", readiness.create_pr_command)


if __name__ == "__main__":
    unittest.main()
