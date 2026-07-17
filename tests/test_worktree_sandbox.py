from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.worktree_sandbox import (
    DirtyWorktreeError,
    WorktreeSandboxError,
    create_worktree_sandbox,
    list_worktree_sandboxes,
    remove_worktree_sandbox,
)


def initialize_repository(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.local"], cwd=path, check=True)
    (path / "README.md").write_text("# Fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=path, check=True, capture_output=True, text=True)


class WorktreeSandboxTests(unittest.TestCase):
    def test_cli_create_list_and_remove_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            managed = root / "managed"
            initialize_repository(repo)

            created_process = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "repopilot.py"),
                    "sandbox",
                    "create",
                    "--repo",
                    str(repo),
                    "--name",
                    "cli-case",
                    "--worktree-root",
                    str(managed),
                    "--json",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            created = json.loads(created_process.stdout)
            sandbox_path = Path(created["path"])
            try:
                listed_process = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "repopilot.py"),
                        "sandbox",
                        "list",
                        "--repo",
                        str(repo),
                        "--worktree-root",
                        str(managed),
                        "--json",
                    ],
                    cwd=ROOT,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                listed = json.loads(listed_process.stdout)
                self.assertEqual(listed["sandboxes"][0]["path"], str(sandbox_path))

                removed_process = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "repopilot.py"),
                        "sandbox",
                        "remove",
                        "--repo",
                        str(repo),
                        "--path",
                        str(sandbox_path),
                        "--worktree-root",
                        str(managed),
                        "--json",
                    ],
                    cwd=ROOT,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                removed = json.loads(removed_process.stdout)
                self.assertTrue(removed["removed"])
                self.assertFalse(sandbox_path.exists())
            finally:
                if sandbox_path.exists():
                    remove_worktree_sandbox(repo, sandbox_path, force=True, worktree_root=managed)

    def test_create_list_and_remove_clean_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            managed = root / "managed"
            initialize_repository(repo)

            sandbox = create_worktree_sandbox(
                repo,
                name="case-one",
                worktree_root=managed,
            )
            try:
                self.assertEqual(Path(sandbox.path), (managed / "case-one").resolve())
                self.assertEqual(sandbox.base_ref, "HEAD")
                self.assertTrue(sandbox.detached)
                self.assertTrue(sandbox.clean)
                self.assertTrue(sandbox.managed)
                self.assertFalse(sandbox.primary)
                self.assertEqual((Path(sandbox.path) / "README.md").read_text(encoding="utf-8"), "# Fixture\n")

                listed = list_worktree_sandboxes(repo, worktree_root=managed)

                self.assertEqual([item.path for item in listed], [sandbox.path])
                self.assertEqual(listed[0].head, sandbox.head)
            finally:
                if Path(sandbox.path).exists():
                    remove_worktree_sandbox(repo, sandbox.path, force=True, worktree_root=managed)

            self.assertFalse(Path(sandbox.path).exists())

    def test_create_rejects_dirty_source_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            initialize_repository(repo)
            (repo / "README.md").write_text("dirty\n", encoding="utf-8")

            with self.assertRaises(DirtyWorktreeError) as context:
                create_worktree_sandbox(repo, worktree_root=root / "managed")

            self.assertIn("Source repository has uncommitted changes", str(context.exception))

    def test_remove_requires_force_for_dirty_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            managed = root / "managed"
            initialize_repository(repo)
            sandbox = create_worktree_sandbox(repo, name="dirty-case", worktree_root=managed)
            (Path(sandbox.path) / "README.md").write_text("sandbox change\n", encoding="utf-8")

            with self.assertRaises(DirtyWorktreeError) as context:
                remove_worktree_sandbox(repo, sandbox.path, worktree_root=managed)

            self.assertIn("Sandbox has uncommitted changes", str(context.exception))
            self.assertTrue(Path(sandbox.path).exists())

            result = remove_worktree_sandbox(
                repo,
                sandbox.path,
                force=True,
                worktree_root=managed,
            )

            self.assertTrue(result.removed)
            self.assertTrue(result.forced)
            self.assertFalse(Path(sandbox.path).exists())

    def test_remove_refuses_registered_worktree_outside_managed_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            managed = root / "managed"
            outside = root / "outside-worktree"
            initialize_repository(repo)
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(outside), "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
            try:
                with self.assertRaises(WorktreeSandboxError) as context:
                    remove_worktree_sandbox(repo, outside, force=True, worktree_root=managed)

                self.assertIn("outside RepoPilot's managed root", str(context.exception))
                self.assertEqual(list_worktree_sandboxes(repo, worktree_root=managed), [])
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(outside)],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                )

    def test_create_rejects_unsafe_sandbox_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            initialize_repository(repo)

            for name in ("../escape", "nested/path", "", "name with spaces"):
                with self.subTest(name=name):
                    with self.assertRaises(WorktreeSandboxError):
                        create_worktree_sandbox(repo, name=name, worktree_root=root / "managed")


if __name__ == "__main__":
    unittest.main()
