from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.repository_instructions import (
    INSTRUCTION_TRUST_BOUNDARY,
    MAX_APPLIED_INSTRUCTION_FILES,
    MAX_INSTRUCTION_FILE_BYTES,
    discover_repository_instructions,
    resolve_repository_instructions,
)


class RepositoryInstructionTests(unittest.TestCase):
    def test_resolves_root_and_nested_instructions_by_scope_and_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "pkg").mkdir(parents=True)
            (root / "web").mkdir()
            (root / "AGENTS.md").write_text("Use Python 3.10.", encoding="utf-8")
            (root / "src" / "AGENTS.md").write_text("Use unittest.", encoding="utf-8")
            (root / "web" / "AGENTS.md").write_text("Use browser tests.", encoding="utf-8")

            instruction_set = discover_repository_instructions(root)
            context = resolve_repository_instructions(
                instruction_set,
                ["src/pkg/parser.py"],
            )

            self.assertEqual(
                [item.path for item in instruction_set.files],
                ["AGENTS.md", "src/AGENTS.md", "web/AGENTS.md"],
            )
            self.assertEqual(
                [item.path for item in context.files],
                ["AGENTS.md", "src/AGENTS.md"],
            )
            self.assertEqual([item.scope for item in context.files], [".", "src"])
            self.assertLess(context.text.index("Use Python 3.10."), context.text.index("Use unittest."))
            self.assertNotIn("Use browser tests.", context.text)
            self.assertIn(INSTRUCTION_TRUST_BOUNDARY, context.text)
            self.assertEqual(
                [item["precedence"] for item in context.to_dict()["files"]],
                [1, 2],
            )

    def test_multiple_targets_merge_unique_scopes_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for directory in ("src", "tests"):
                (root / directory).mkdir()
                (root / directory / "AGENTS.md").write_text(directory, encoding="utf-8")
            (root / "AGENTS.md").write_text("root", encoding="utf-8")

            context = resolve_repository_instructions(
                discover_repository_instructions(root),
                ["tests/test_api.py", "src/api.py", "src/api.py"],
            )

            self.assertEqual(context.target_paths, ("tests/test_api.py", "src/api.py"))
            self.assertEqual(
                [item.path for item in context.files],
                ["AGENTS.md", "src/AGENTS.md", "tests/AGENTS.md"],
            )

    def test_no_targets_applies_only_repository_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "nested").mkdir()
            (root / "AGENTS.md").write_text("root", encoding="utf-8")
            (root / "nested" / "AGENTS.md").write_text("nested", encoding="utf-8")

            context = resolve_repository_instructions(discover_repository_instructions(root))

            self.assertEqual([item.path for item in context.files], ["AGENTS.md"])

    def test_parent_and_ignored_instruction_files_are_not_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            root = workspace / "repo"
            root.mkdir()
            (workspace / "AGENTS.md").write_text("private parent rule", encoding="utf-8")
            (root / "AGENTS.md").write_text("repo rule", encoding="utf-8")
            for directory in (".git", ".repopilot", "node_modules", "build"):
                (root / directory).mkdir()
                (root / directory / "AGENTS.md").write_text("ignored", encoding="utf-8")

            instruction_set = discover_repository_instructions(root)

            self.assertEqual([item.path for item in instruction_set.files], ["AGENTS.md"])
            self.assertNotIn("private parent rule", instruction_set.files[0].content)

    def test_unsafe_target_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instruction_set = discover_repository_instructions(tmp)
            for path in ("../outside.py", "/absolute.py", "C:/absolute.py", "src/../outside.py"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    resolve_repository_instructions(instruction_set, [path])

    def test_content_is_redacted_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = "sk-abcdefgh12345678"
            content = f"OPENAI_API_KEY={secret}\n" + ("follow style\n" * 2_000)
            (root / "AGENTS.md").write_text(content, encoding="utf-8")

            context = resolve_repository_instructions(
                discover_repository_instructions(root),
                max_chars=1_200,
            )

            self.assertNotIn(secret, context.text)
            self.assertIn("[REDACTED]", context.text)
            self.assertLessEqual(len(context.text), 1_200)
            self.assertTrue(context.truncated)
            self.assertEqual(len(context.files[0].content_sha256), 64)
            public_context = context.to_dict()
            self.assertNotIn("content", public_context["files"][0])
            self.assertNotIn(secret, str(public_context))
            self.assertLessEqual(len(public_context["text"]), 1_200)

    def test_oversized_and_invalid_utf8_files_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_bytes(b"x" * (MAX_INSTRUCTION_FILE_BYTES + 1))
            (root / "src").mkdir()
            (root / "src" / "AGENTS.md").write_bytes(b"\xff\xfe\xfa")

            instruction_set = discover_repository_instructions(root)

            self.assertEqual(instruction_set.files, ())
            self.assertEqual(
                {(item.path, item.reason) for item in instruction_set.issues},
                {("AGENTS.md", "file_too_large"), ("src/AGENTS.md", "invalid_utf8")},
            )

    def test_applied_file_limit_preserves_root_and_deepest_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("root", encoding="utf-8")
            current = root
            target_parts: list[str] = []
            for index in range(MAX_APPLIED_INSTRUCTION_FILES + 3):
                name = f"d{index:02d}"
                target_parts.append(name)
                current = current / name
                current.mkdir()
                (current / "AGENTS.md").write_text(name, encoding="utf-8")
            target = "/".join([*target_parts, "file.py"])

            context = resolve_repository_instructions(
                discover_repository_instructions(root),
                [target],
            )

            self.assertEqual(len(context.files), MAX_APPLIED_INSTRUCTION_FILES)
            self.assertEqual(context.files[0].path, "AGENTS.md")
            self.assertTrue(context.files[-1].path.endswith("d18/AGENTS.md"))
            self.assertTrue(context.omitted_applicable_paths)
            self.assertTrue(context.truncated)

    @unittest.skipUnless(hasattr(os, "symlink"), "symbolic links are unavailable")
    def test_symlink_outside_repository_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            root = workspace / "repo"
            outside = workspace / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "AGENTS.md").write_text("outside", encoding="utf-8")
            link = root / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symbolic links are unavailable: {exc}")

            instruction_set = discover_repository_instructions(root)

            self.assertEqual(instruction_set.files, ())


if __name__ == "__main__":
    unittest.main()
