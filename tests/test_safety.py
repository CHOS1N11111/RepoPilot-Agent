from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.models import FileEditProposal
from repopilot_agent.patch_apply import apply_file_edits, capture_file_snapshots, revert_file_snapshots
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

    def test_rollback_restores_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "notes.txt"
            target.write_text("old notes\n", encoding="utf-8")
            edits = [FileEditProposal(path="notes.txt", new_content="new notes\n", rationale="Update notes.")]

            snapshots = capture_file_snapshots(root, edits)
            apply_file_edits(root, edits, task="update notes", allowed_paths=["notes.txt"])
            result = revert_file_snapshots(root, snapshots)

            self.assertTrue(result.reverted)
            self.assertEqual(result.restored_files, ["notes.txt"])
            self.assertEqual(target.read_text(encoding="utf-8"), "old notes\n")

    def test_rollback_deletes_file_created_by_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "notes.txt"
            edits = [FileEditProposal(path="notes.txt", new_content="new notes\n", rationale="Add notes.")]

            snapshots = capture_file_snapshots(root, edits)
            apply_file_edits(root, edits, task="add notes", allowed_paths=["notes.txt"])
            result = revert_file_snapshots(root, snapshots)

            self.assertTrue(result.reverted)
            self.assertEqual(result.deleted_files, ["notes.txt"])
            self.assertFalse(target.exists())

    def test_rollback_blocks_when_file_changed_after_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "notes.txt"
            target.write_text("old notes\n", encoding="utf-8")
            edits = [FileEditProposal(path="notes.txt", new_content="new notes\n", rationale="Update notes.")]

            snapshots = capture_file_snapshots(root, edits)
            apply_file_edits(root, edits, task="update notes", allowed_paths=["notes.txt"])
            target.write_text("manual notes\n", encoding="utf-8")

            with self.assertRaises(RuntimeError):
                revert_file_snapshots(root, snapshots)
            self.assertEqual(target.read_text(encoding="utf-8"), "manual notes\n")

    def test_rollback_preflight_prevents_partial_revert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text("old first\n", encoding="utf-8")
            second.write_text("old second\n", encoding="utf-8")
            edits = [
                FileEditProposal(path="first.txt", new_content="new first\n", rationale="Update first."),
                FileEditProposal(path="second.txt", new_content="new second\n", rationale="Update second."),
            ]

            snapshots = capture_file_snapshots(root, edits)
            apply_file_edits(
                root,
                edits,
                task="update first second",
                allowed_paths=["first.txt", "second.txt"],
            )
            second.write_text("manual second\n", encoding="utf-8")

            with self.assertRaises(RuntimeError):
                revert_file_snapshots(root, snapshots)
            self.assertEqual(first.read_text(encoding="utf-8"), "new first\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "manual second\n")


if __name__ == "__main__":
    unittest.main()
