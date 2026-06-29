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
            remote = workspace / "remote"
            cache = workspace / "cache"
            subprocess.run(["git", "init", remote], check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=remote, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.local"], cwd=remote, check=True)
            (remote / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=remote, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "Initial"], cwd=remote, check=True, capture_output=True, text=True)

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


if __name__ == "__main__":
    unittest.main()
