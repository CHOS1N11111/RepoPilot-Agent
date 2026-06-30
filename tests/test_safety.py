from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.models import FileEditProposal
from repopilot_agent.patch_apply import apply_file_edits
from repopilot_agent.safety import SafetyCheckError, check_file_edits


class SafetyCheckTests(unittest.TestCase):
    def test_blocks_protected_local_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = check_file_edits(
                tmp,
                [FileEditProposal(path="log.md", new_content="hidden\n", rationale="Update log.")],
                task="update log",
                allowed_paths=["log.md"],
            )

        self.assertFalse(result.ok)
        self.assertTrue(any(finding.code == "blocked_file" for finding in result.findings))

    def test_blocks_empty_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("important\n", encoding="utf-8")

            result = check_file_edits(
                root,
                [FileEditProposal(path="notes.txt", new_content="", rationale="Clear notes.")],
                task="update notes",
                allowed_paths=["notes.txt"],
            )

        self.assertFalse(result.ok)
        self.assertTrue(any(finding.code == "empty_overwrite" for finding in result.findings))

    def test_blocks_large_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = "\n".join(f"line {index}: keep this content" for index in range(40))
            (root / "module.py").write_text(original, encoding="utf-8")

            result = check_file_edits(
                root,
                [FileEditProposal(path="module.py", new_content="pass\n", rationale="Simplify module.")],
                task="update module",
                allowed_paths=["module.py"],
            )

        self.assertFalse(result.ok)
        self.assertTrue(any(finding.code == "large_deletion" for finding in result.findings))

    def test_warns_on_weak_task_relevance_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("old notes\n", encoding="utf-8")

            result = check_file_edits(
                root,
                [FileEditProposal(path="notes.txt", new_content="new notes\n", rationale="Update notes.")],
                task="fix parser behavior",
                allowed_paths=["notes.txt"],
            )

        self.assertTrue(result.ok)
        self.assertTrue(any(finding.code == "weak_task_relevance" for finding in result.findings))

    def test_apply_blocks_paths_not_in_approved_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "other.txt"
            target.write_text("old\n", encoding="utf-8")

            with self.assertRaises(SafetyCheckError) as raised:
                apply_file_edits(
                    root,
                    [FileEditProposal(path="other.txt", new_content="new\n", rationale="Update other.")],
                    task="update approved file",
                    allowed_paths=["approved.txt"],
                )

            self.assertFalse(raised.exception.result.ok)
            self.assertEqual(target.read_text(encoding="utf-8"), "old\n")

    def test_apply_allows_normal_small_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "notes.txt"
            target.write_text("old notes\n", encoding="utf-8")

            result = apply_file_edits(
                root,
                [FileEditProposal(path="notes.txt", new_content="new notes\n", rationale="Update notes.")],
                task="update notes",
                allowed_paths=["notes.txt"],
            )

            self.assertTrue(result.applied)
            self.assertTrue(result.safety_check.ok)
            self.assertEqual(target.read_text(encoding="utf-8"), "new notes\n")


if __name__ == "__main__":
    unittest.main()
