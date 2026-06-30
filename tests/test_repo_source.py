from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.repo_source import (  # noqa: E402
    GitHubRepositoryInput,
    parse_github_repository_input,
    resolve_repository_reference,
    sync_repository_reference,
)


class RepositorySourceTests(unittest.TestCase):
    def test_parse_github_input_supports_url_ssh_and_slug(self) -> None:
        self.assertEqual(
            parse_github_repository_input("https://github.com/CHOS1N11111/RepoPilot-Agent.git").html_url,
            "https://github.com/CHOS1N11111/RepoPilot-Agent",
        )
        self.assertEqual(
            parse_github_repository_input("git@github.com:CHOS1N11111/RepoPilot-Agent.git").clone_url,
            "git@github.com:CHOS1N11111/RepoPilot-Agent.git",
        )
        self.assertEqual(
            parse_github_repository_input("CHOS1N11111/RepoPilot-Agent").clone_url,
            "https://github.com/CHOS1N11111/RepoPilot-Agent.git",
        )

    def test_resolve_github_reference_clones_to_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            remote = _create_remote_repo(workspace / "remote")
            cache = workspace / "cache"

            patched = GitHubRepositoryInput(
                owner="example",
                repo="project",
                clone_url=remote.as_uri(),
                html_url="https://github.com/example/project",
            )
            with patch("repopilot_agent.repo_source.parse_github_repository_input", return_value=patched):
                source = resolve_repository_reference(
                    repo_source="github",
                    github_url="https://github.com/example/project",
                    cache_root=cache,
                )

            self.assertEqual(source.source, "github")
            self.assertTrue(Path(source.local_path, "README.md").is_file())
            self.assertEqual(source.github_url, "https://github.com/example/project")

    def test_sync_github_reference_clones_requested_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            remote = _create_remote_repo(workspace / "remote")
            subprocess.run(["git", "checkout", "-b", "feature"], cwd=remote, check=True, capture_output=True, text=True)
            (remote / "README.md").write_text("feature\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=remote, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "Feature"], cwd=remote, check=True, capture_output=True, text=True)

            patched = GitHubRepositoryInput(
                owner="example",
                repo="project",
                clone_url=remote.as_uri(),
                html_url="https://github.com/example/project",
            )
            with patch("repopilot_agent.repo_source.parse_github_repository_input", return_value=patched):
                source = sync_repository_reference(
                    repo_source="github",
                    github_url="https://github.com/example/project",
                    branch="feature",
                    cache_root=workspace / "cache",
                )

            self.assertEqual(source.branch, "feature")
            self.assertTrue(source.synced)
            self.assertIn("feature", Path(source.local_path, "README.md").read_text(encoding="utf-8"))

    def test_sync_github_reference_updates_cached_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            remote = _create_remote_repo(workspace / "remote")
            cache = workspace / "cache"
            patched = GitHubRepositoryInput(
                owner="example",
                repo="project",
                clone_url=remote.as_uri(),
                html_url="https://github.com/example/project",
            )
            with patch("repopilot_agent.repo_source.parse_github_repository_input", return_value=patched):
                first = sync_repository_reference(
                    repo_source="github",
                    github_url="https://github.com/example/project",
                    cache_root=cache,
                )

            (remote / "README.md").write_text("hello again\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=remote, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "Update"], cwd=remote, check=True, capture_output=True, text=True)

            with patch("repopilot_agent.repo_source.parse_github_repository_input", return_value=patched):
                second = sync_repository_reference(
                    repo_source="github",
                    github_url="https://github.com/example/project",
                    cache_root=cache,
                )

            self.assertEqual(first.local_path, second.local_path)
            self.assertTrue(second.cached)
            self.assertTrue(second.synced)
            self.assertIn("hello again", Path(second.local_path, "README.md").read_text(encoding="utf-8"))


def _create_remote_repo(path: Path) -> Path:
    subprocess.run(["git", "init", path], check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.local"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=path, check=True, capture_output=True, text=True)
    return path


if __name__ == "__main__":
    unittest.main()
